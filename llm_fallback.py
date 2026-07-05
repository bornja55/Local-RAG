"""
llm_fallback.py — retry + fallback logic สำหรับเรียก Gemini (แยกออกมาจาก rag_worker.py
ตาม Architecture report High #2) — โมดูลนี้ **ห้าม** import llama_index/torch/faiss ที่
top-level เด็ดขาด เพื่อให้เขียน pure unit test ได้โดยไม่ต้องมี API key หรือโหลดโมเดลเลย
(GoogleGenAI ถูก import แบบ lazy ข้างใน build_llm() เท่านั้น)

พฤติกรรมทั้งหมดคงเดิม 100% จาก rag_worker.py เดิม (ดู ADR-003):
- retry โมเดลหลัก 3 ครั้ง + backoff เฉพาะ quota error เท่านั้น (timeout ไม่ retry เพราะ
  backoff ไม่ช่วยอะไร มีแต่เสียเวลาก่อนได้ลองโมเดลสำรองจริงๆ)
- fallback ไปโมเดลสำรองทีละตัวเมื่อ error เป็นแบบ "โมเดลหลักใช้ไม่ได้ตอนนี้"
  (quota / timeout / 503 / 504 / UNAVAILABLE — ดู is_fallback_worthy_error)

การใช้งานจาก worker (ผ่าน wrapper ใน worker_handlers.py):
    complete_with_fallback(model, fallbacks, prompt, "[TAG]",
                           timeout_ms=..., log=state.log)
การใช้งานจาก unit test (ไม่ต้องมี API key):
    complete_with_fallback(..., llm_factory=fake_factory, sleep=lambda s: None,
                           log=lambda m: None)
"""
import re
import time

# ค่าดีฟอลต์เดียวกับ GEMINI_REQUEST_TIMEOUT_MS ใน worker_config.py — ใส่ไว้กันโมดูลนี้
# ต้องพึ่ง worker_config (จะได้ import ได้เดี่ยวๆ ใน unit test) ผู้เรียกจริงส่งค่าจาก config มาเสมอ
_DEFAULT_TIMEOUT_MS = 5 * 60 * 1000


def is_quota_error(e: Exception) -> bool:
    """เช็คว่า exception เป็น quota/rate-limit error หรือไม่ — ใช้ word-boundary regex แทน
    substring match ตรงๆ กัน false positive (เช่น "429" ไปแมตช์เลข ID ที่ขึ้นต้นด้วย 429,
    หรือ "quota" ไปแมตช์คำว่า "quotation") สำคัญขึ้นกว่าเดิมเพราะผลของฟังก์ชันนี้ตอนนี้ใช้ตัดสินใจ
    ว่าจะสลับไปโมเดลสำรองเลยหรือไม่ (ดู ADR-003) ไม่ใช่แค่ retry โมเดลเดิมเหมือนก่อนหน้า"""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code == 429:
        return True

    s = str(e)
    if re.search(r"\b429\b", s):
        return True
    if "RESOURCE_EXHAUSTED" in s:
        return True
    if re.search(r"\bquota\b", s, re.IGNORECASE):
        return True
    return False


def is_fallback_worthy_error(e: Exception) -> bool:
    """เช็คว่า exception นี้ควรสลับไปโมเดลสำรองหรือไม่ — ครอบคลุมกว้างกว่า is_quota_error()
    เพราะเดิมฟังก์ชันนี้ (ก่อน 2026-07-03) ใช้แค่ is_quota_error() เป็นเงื่อนไขเดียวในการตัดสินใจ
    fallback ทำให้ timeout error (จาก GEMINI_REQUEST_TIMEOUT_MS ที่เพิ่มไว้ก่อนหน้า) ไม่เคยสลับไป
    โมเดลสำรองเลยแม้จะตั้งค่าไว้ครบ 5 ตัว — โมเดลหลักช้า/ค้างแล้วผู้ใช้ได้แค่ error ตรงๆ ทั้งที่มี
    โมเดลสำรองพร้อมใช้ (พบจากการรีวิวโค้ดแบบ outsider — ดู ADR-003 หมายเหตุเพิ่มเติม 2026-07-03 ส่วน
    "fallback ไม่ครอบคลุม timeout") ตอนนี้ครอบคลุมทั้ง quota error และ timeout/connection/server error
    เพราะทั้งสองแบบคือ "โมเดลหลักใช้ไม่ได้ตอนนี้" เหมือนกัน ควรลองโมเดลสำรองแทนการโยน error ตรงๆ
    หมายเหตุ: ใช้แค่ตัดสินใจ "จะ fallback ไหม" เท่านั้น ไม่ใช้กับ retry-with-backoff ของโมเดลหลัก
    (retry ยังผูกกับ is_quota_error() เดิม เพราะ backoff มีประโยชน์กับ quota ที่ตอบเร็วเท่านั้น —
    ถ้าเอา timeout ไป retry ด้วย backoff จะยิ่งเสียเวลาซ้ำก่อนได้ลองโมเดลสำรองจริงๆ)"""
    if is_quota_error(e):
        return True

    type_name = type(e).__name__
    if re.search(r"timeout|deadline", type_name, re.IGNORECASE):
        return True

    s = str(e)
    if re.search(r"timed?\s*out|deadline exceeded|timeout", s, re.IGNORECASE):
        return True
    if re.search(r"\b(503|504)\b", s):
        return True
    if re.search(r"\bUNAVAILABLE\b", s):
        return True
    return False


def build_llm(model: str, timeout_ms: int = _DEFAULT_TIMEOUT_MS):
    """สร้าง GoogleGenAI client พร้อม request timeout ที่กำหนดไว้ชัดเจน — ใช้ร่วมกันทุกจุดที่
    เรียก GoogleGenAI() (ทั้ง complete() แบบ one-shot ผ่าน complete_with_fallback และ
    chat_engine ผ่าน _build_chat_engine ใน worker_handlers) กันไม่ให้ request ค้างไม่มีที่สิ้นสุด
    เหมือนที่เคยพบระหว่างทดสอบ (ดูหมายเหตุที่ GEMINI_REQUEST_TIMEOUT_MS ใน worker_config.py)
    import แบบ lazy ข้างในฟังก์ชันตั้งใจ — โมดูลนี้ต้อง import ได้โดยไม่มี llama_index (unit test)"""
    from llama_index.llms.google_genai import GoogleGenAI
    from google.genai import types as genai_types

    return GoogleGenAI(
        model=model, http_options=genai_types.HttpOptions(timeout=timeout_ms)
    )


def complete_with_fallback(
    primary_model: str,
    fallback_models: list[str],
    prompt: str,
    log_prefix: str,
    *,
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    log=print,
    sleep=time.sleep,
    llm_factory=None,
) -> tuple[str | None, Exception | None]:
    """เรียก llm.complete(prompt) พร้อม retry เดิม (3 ครั้ง + backoff) บนโมเดลหลัก ใช้ร่วมกันโดย
    ทุก handler ที่เรียก llm.complete() แบบ one-shot ถ้า retry ครบ 3 ครั้งแล้วยังเป็น quota error อยู่
    และมี fallback_models ตั้งไว้ (list ไม่ว่าง) จะไล่ลองทีละโมเดลตามลำดับใน fallback_models
    (โมเดลละ 1 ครั้ง ไม่ retry ซ้ำต่อโมเดล) จนกว่าจะสำเร็จหรือหมดรายการ — ถ้าโมเดลสำรองตัวใด
    ตัวหนึ่ง error (ไม่ว่าประเภทไหน) ไปลองตัวถัดไปในรายการต่อทันที ไม่หยุดกลางคัน เพราะโมเดลสำรอง
    แต่ละตัวเป็นอิสระจากกัน error ของตัวหนึ่งไม่ได้แปลว่าตัวถัดไปจะพังด้วย
    (ดู ADR-003 — ขยายจาก "โมเดลสำรอง 1 ตัว" เป็น "รายการโมเดลสำรอง" ในหมายเหตุ 2026-07-03)
    คืนค่า (text, error) โดย text เป็น None ถ้าทุกโมเดลล้มเหลว

    พารามิเตอร์ keyword-only ทั้งหมดมีไว้เพื่อ dependency injection ใน unit test เท่านั้น
    (llm_factory: callable(model) -> llm ที่มี .complete(prompt) -> obj ที่มี .text) —
    โค้ดจริงใน worker ส่งแค่ timeout_ms กับ log มา ที่เหลือใช้ค่าดีฟอลต์"""
    factory = llm_factory or (lambda m: build_llm(m, timeout_ms))

    last_error = None
    for attempt in range(3):
        try:
            t0 = time.time()
            resp = factory(primary_model).complete(prompt)
            log(f"{log_prefix} สำเร็จใน {time.time() - t0:.2f}s (โมเดล: {primary_model})")
            return resp.text, None
        except Exception as e:
            last_error = e
            log(f"{log_prefix} error (โมเดล {primary_model}): {type(e).__name__} - {e}")
            if is_quota_error(e) and attempt < 2:
                sleep(10 * (attempt + 1))
                continue
            break

    if fallback_models and is_fallback_worthy_error(last_error):
        for fallback_model in fallback_models:
            try:
                log(f"{log_prefix} โมเดล {primary_model} ชนโควตา กำลังลองโมเดลสำรอง {fallback_model}...")
                t0 = time.time()
                resp = factory(fallback_model).complete(prompt)
                log(f"{log_prefix} สำเร็จใน {time.time() - t0:.2f}s (โมเดลสำรอง: {fallback_model})")
                return resp.text, None
            except Exception as e:
                last_error = e
                log(f"{log_prefix} error (โมเดลสำรอง {fallback_model}): {type(e).__name__} - {e}")
                continue

    return None, last_error
