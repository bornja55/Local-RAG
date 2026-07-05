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


def run_with_fallback(
    primary_model: str,
    fallback_models: list[str],
    factory,
    call,
    log_prefix: str,
    *,
    log=print,
    sleep=None,
) -> tuple[object | None, Exception | None]:
    """ตรรกะ retry+fallback กลาง (ดู ADR-003, แยกออกมาเพิ่มเติม 2026-07-05) — แยก "จะเรียกโมเดลไหน
    ตามลำดับไหน เมื่อไหร่ต้อง retry/เมื่อไหร่ต้อง fallback" (ฟังก์ชันนี้ ไม่รู้จัก prompt หรือรูปแบบ
    ผลลัพธ์เลย) ออกจาก "เรียกโมเดลนั้นยังไงและเอาผลลัพธ์อะไรออกมา" (factory/call ที่ผู้เรียกกำหนดเอง)

    เหตุผล: ก่อนหน้านี้ `_handle_chat` ใน worker_handlers.py เขียน retry+fallback loop เองแยกต่างหาก
    ทั้งหมด (เพราะใช้ `chat_engine.chat()` แทน `llm.complete()`) ทำให้มีตรรกะเดียวกันซ้ำอยู่ 2 ที่ —
    ที่หนึ่งมี unit test คุ้ม 25 เทส (complete_with_fallback ด้านล่าง) อีกที่ไม่มีเทสคุ้มเลย
    (ดู HANDOFF.md "0b" ข้อ 4) ตอนนี้ complete_with_fallback() เป็นแค่ wrapper บางๆ ของฟังก์ชันนี้
    และ `_handle_chat` เรียกฟังก์ชันนี้ตรงๆ ด้วย — ทำให้ทั้งสอง path ใช้ตรรกะเดียวกันที่มีเทสคุ้มแล้ว
    โดยอัตโนมัติ ไม่มีโอกาสที่สองที่จะ diverge กันอีกเหมือนที่เคยเกิดบั๊ก ADR-003 เดิม (retry primary
    ตอน timeout — แก้แล้วที่นี่ที่เดียว ใช้ร่วมกันทั้งคู่)

    factory(model): callable สร้าง object ใหม่ (llm หรือ chat_engine) — เรียกใหม่ทุกครั้งที่ attempt
    รวมถึงตอน retry โมเดลหลักซ้ำด้วย (ไม่ใช่แค่ตอนสลับโมเดล) ปลอดภัยเสมอ (ยืนยันจาก diagnostic tests
    เดิมของ `_build_chat_engine` ใน worker_handlers.py)
    call(obj): callable เรียกจริงบน object ที่ได้จาก factory คืนผลลัพธ์ หรือ raise ถ้าล้มเหลว

    พฤติกรรม retry (3 ครั้ง + backoff 10s/20s เฉพาะ quota error)/fallback (ไล่ทีละโมเดลตามลำดับใน
    fallback_models จนกว่าจะสำเร็จหรือหมดรายการ ไม่ retry ซ้ำต่อโมเดลสำรอง) เหมือน complete_with_fallback
    เดิมทุกประการ คืนค่า (result, error) โดย result เป็น None ถ้าทุกโมเดลล้มเหลว

    หมายเหตุ (2026-07-05, พบระหว่างเขียน test_handle_chat_fallback.py): sleep ดีฟอลต์เป็น None แล้ว
    resolve เป็น time.sleep จริงข้างในฟังก์ชัน (ไม่ใช่ sleep=time.sleep ตรงๆ ที่ signature) เพราะถ้า
    bind ค่า time.sleep ไว้ตรงๆ ตอน def (ตอน import โมดูลนี้) การ monkeypatch time.sleep ในเทสภายหลัง
    (เช่น `time.sleep = lambda s: ...`) จะไม่มีผลเลย — ค่า default ที่ bind ไว้แล้วเป็นคนละ object กับ
    time.sleep ตัวใหม่ที่ถูกแทนที่ ทำให้เทสที่ผ่าน _handle_chat (เรียกฟังก์ชันนี้ตรงๆ โดยไม่ส่ง sleep
    มาเอง) ต้องรอ backoff จริง 10s+20s ทุกครั้งที่รันเทส (ยืนยันจาก timestamp ใน log จริงตอนรันเทส
    ก่อนแก้จุดนี้) resolve แบบ lazy ข้างในฟังก์ชันแทน ทำให้ monkeypatch ทำงานถูกต้อง พฤติกรรมจริง
    (ไม่ inject sleep เอง) ไม่เปลี่ยนเลย เพราะ time.sleep ที่ resolve ได้ก็คือตัวเดียวกันอยู่ดี"""
    if sleep is None:
        sleep = time.sleep
    last_error = None
    for attempt in range(3):
        try:
            t0 = time.time()
            result = call(factory(primary_model))
            log(f"{log_prefix} สำเร็จใน {time.time() - t0:.2f}s (โมเดล: {primary_model})")
            return result, None
        except Exception as e:
            last_error = e
            log(f"{log_prefix} error (โมเดล {primary_model}): {type(e).__name__} - {e}")
            if is_quota_error(e) and attempt < 2:
                sleep(10 * (attempt + 1))
                continue
            break

    if fallback_models and is_fallback_worthy_error(last_error):
        # หมายเหตุ (2026-07-05, พบระหว่าง /scrutinize + debug-mantra repro): เดิม log บรรทัดถัดไปนี้
        # hardcode ชื่อ primary_model และคำว่า "ชนโควตา" ตลอดทุกรอบของ loop — ผิดตั้งแต่ fallback ตัว
        # ที่ 2 เป็นต้นไป (โมเดลที่เพิ่ง fail จริงคือ fallback ตัวก่อนหน้า ไม่ใช่ primary) และผิดเมื่อ
        # error ไม่ใช่ quota เลย (เข้า branch นี้ผ่าน is_fallback_worthy_error ซึ่งครอบคลุม timeout/503/504
        # ด้วย ไม่ใช่แค่ quota) track โมเดล+ประเภท error ที่เพิ่ง fail จริงแทน กันสับสนตอนอ่าน log จริง
        failed_model = primary_model
        for fallback_model in fallback_models:
            try:
                log(f"{log_prefix} โมเดล {failed_model} ใช้ไม่ได้ ({type(last_error).__name__}) "
                    f"กำลังลองโมเดลสำรอง {fallback_model}...")
                t0 = time.time()
                result = call(factory(fallback_model))
                log(f"{log_prefix} สำเร็จใน {time.time() - t0:.2f}s (โมเดลสำรอง: {fallback_model})")
                return result, None
            except Exception as e:
                last_error = e
                failed_model = fallback_model
                log(f"{log_prefix} error (โมเดลสำรอง {fallback_model}): {type(e).__name__} - {e}")
                continue

    return None, last_error


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
    คืนค่า (text, error) โดย text เป็น None ถ้าทุกโมเดลล้มเหลว — ตอนนี้เป็นแค่ wrapper บางๆ ของ
    run_with_fallback() ด้านบน (ดู docstring ของฟังก์ชันนั้นสำหรับเหตุผลที่แยกออกมา 2026-07-05)
    พฤติกรรม/signature เดิมทุกประการ ไม่กระทบ caller เดิมเลย

    พารามิเตอร์ keyword-only ทั้งหมดมีไว้เพื่อ dependency injection ใน unit test เท่านั้น
    (llm_factory: callable(model) -> llm ที่มี .complete(prompt) -> obj ที่มี .text) —
    โค้ดจริงใน worker ส่งแค่ timeout_ms กับ log มา ที่เหลือใช้ค่าดีฟอลต์"""
    factory = llm_factory or (lambda m: build_llm(m, timeout_ms))
    return run_with_fallback(
        primary_model, fallback_models, factory, lambda llm: llm.complete(prompt).text,
        log_prefix, log=log, sleep=sleep,
    )
