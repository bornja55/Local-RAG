"""
worker_client.py — client/process-management layer สำหรับคุยกับ rag_worker.py
(แยกออกมาจาก app.py ตาม Architecture report Medium #2)

*** ไม่มี streamlit import ในไฟล์นี้เลย (ตั้งใจ) *** — ทำให้ทดสอบ process-management /
HTTP-client logic แยกจาก UI ได้โดยไม่ต้องลาก streamlit มาทั้งชุด และ script อื่น
(เช่น test_rag_pipeline.py) จะ reuse ฟังก์ชันชุดนี้ได้ตรงๆ ในอนาคต

ส่วนที่เป็น UI (เช่น _wait_for_worker_ready ที่วาด placeholder/st.stop) ยังอยู่ที่ app.py
เพราะเป็น presentation logic — ไฟล์นี้ถือเฉพาะ infra ล้วนๆ:
    - health check + auto-start worker process (DETACHED บน Windows)
    - HTTP POST helper กลาง + helper เฉพาะ endpoint ทุกตัว (timeout ตามลักษณะงานของแต่ละ endpoint)
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import worker_config as config

WORKER_HOST = "127.0.0.1"
WORKER_PORT = 8765
WORKER_URL = f"http://{WORKER_HOST}:{WORKER_PORT}"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER_SCRIPT = os.path.join(BASE_DIR, "rag_worker.py")

# เผื่อ overhead เครือข่าย/JSON serialize เหนือเวลาที่ตัว LLM call ใช้จริง (ไม่เกี่ยวกับ
# GEMINI_REQUEST_TIMEOUT_MS โดยตรง — กันกรณี timeout พอดิบพอดีจนตัด response ที่เพิ่งเสร็จจริงๆ)
_NETWORK_BUFFER_SECONDS = 30


def _worst_case_timeout_seconds(num_llm_calls: int, fallback_models: list[str]) -> int:
    """คำนวณ client-side timeout ที่ควรใช้ต่อ endpoint หนึ่งๆ ให้ครอบคลุม worst-case ของ
    auto-fallback chain เต็มรูปแบบ (ดู ADR-003 หมายเหตุ 2026-07-05 — พบจาก /scrutinize +
    debug-mantra repro ว่าก่อนหน้านี้ทุก _call_worker_* คำนวณ timeout จากแค่
    "จำนวน LLM call ทางตรรกะ x GEMINI_REQUEST_TIMEOUT_MS" โดยไม่ได้คูณด้วยจำนวนโมเดลที่
    _complete_with_fallback()/_handle_chat() จะไล่ลอง (primary + fallback ตัวละ 1 attempt เต็ม
    GEMINI_REQUEST_TIMEOUT_MS ถ้าเจอ timeout error ต่อเนื่อง — ดู llm_fallback.complete_with_fallback)
    ทำให้ client timeout น้อยกว่า worst-case จริงมาก โดยเฉพาะ /chat ที่ค้างที่ 120s มาตั้งแต่ก่อนมี
    fallback chain เลย ไม่เคยถูกปรับตามเลย (worst-case จริงตอนนั้น ~26 นาที ด้วย 4 fallback model
    ตามค่าใน .env.example)

    คำนวณจาก config ตรงๆ (ไม่ hardcode เลขคงที่) เพื่อไม่ให้ค่านี้ค้างผิดอีกถ้ามีคนเพิ่ม/ลด
    จำนวน fallback model หรือปรับ GEMINI_REQUEST_TIMEOUT_MS ในอนาคต — self-correcting by construction

    num_llm_calls: จำนวน LLM call ทางตรรกะต่อ 1 request ของ endpoint นั้น (เช่น /draft = 2
        คือ ร่าง+scrutinize ต่อเนื่องกัน, /chat = 1, prefill/follow-up ทีละครั้ง = 1)
    fallback_models: GEMINI_MODEL_CHAT_FALLBACK หรือ GEMINI_MODEL_DRAFT_FALLBACK แล้วแต่ endpoint
        นั้นใช้โมเดลกลุ่มไหน (list ว่าง = ไม่มี fallback ก็ยังคำนวณถูก เหลือแค่ 1 attempt/call)"""
    attempts_per_call = 1 + len(fallback_models)
    worst_case = num_llm_calls * attempts_per_call * (config.GEMINI_REQUEST_TIMEOUT_MS / 1000)
    return int(worst_case) + _NETWORK_BUFFER_SECONDS


# ── Worker process management ───────────────────────────────────────────────

def _check_worker_health() -> dict | None:
    """คืนค่า status dict ถ้า worker ตอบกลับ, None ถ้ายังต่อไม่ติด (ยังไม่เริ่ม/ยังไม่ bind port)"""
    try:
        with urllib.request.urlopen(f"{WORKER_URL}/health", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _start_worker_process() -> None:
    python_exe = sys.executable
    creationflags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP: worker รันอิสระจาก Streamlit เต็มที่
        # ไม่ผูกกับ console/process ของ Streamlit เลย จะได้ไม่โดน kill ตาม และไม่แชร์
        # native library state ใดๆ ข้ามโปรเซสกัน
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [python_exe, WORKER_SCRIPT],
        cwd=BASE_DIR,
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def ensure_worker_started() -> bool:
    """เช็ค + สั่ง start worker ทุก rerun (ตั้งใจไม่ cache) เพื่อให้ auto-restart ได้ถ้า worker ตาย
    กลางคัน — _start_worker_process() จะถูกเรียกก็ต่อเมื่อ health check คืนค่า None (ไม่มีอะไร
    ฟังอยู่ที่ port เลย) เท่านั้น ถ้า worker กำลังโหลดอยู่ (status == "loading") จะไม่ spawn ซ้ำ"""
    if _check_worker_health() is None:
        _start_worker_process()
    return True


# ── HTTP client ──────────────────────────────────────────────────────────────

def _call_worker_endpoint(path: str, payload: dict, timeout: int) -> dict:
    """helper กลางสำหรับเรียก worker endpoint แบบ POST + JSON body — คืน response JSON เสมอ
    (ทั้งกรณีสำเร็จและ error, error ถูกห่อเป็น {"error": "..."} เพื่อให้ผู้เรียกเช็คแบบเดียวกันหมด)
    ใช้ร่วมกันโดย helper เฉพาะ endpoint ด้านล่างทุกตัว กันโค้ด try/except ซ้ำ"""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{WORKER_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}


def _call_worker_chat(session_id: str, prompt: str) -> dict:
    """เรียก /chat — timeout คำนวณจาก worst-case ของ GEMINI_MODEL_CHAT_FALLBACK เต็มรายการ (ดู
    _worst_case_timeout_seconds) เพราะ _handle_chat ผูก GEMINI_REQUEST_TIMEOUT_MS เหมือน /draft
    ทุกประการ และมี fallback loop ของตัวเองที่ไล่ลอง GEMINI_MODEL_CHAT_FALLBACK เต็มรายการเช่นกัน
    (ก่อนหน้านี้ค่านี้คงที่ 120s มาตั้งแต่ก่อนมี fallback chain — ไม่เคยถูกปรับตาม พบจาก /scrutinize
    2026-07-05 ว่า worst-case จริงอาจสูงถึง ~26 นาทีด้วยค่า .env.example ปัจจุบัน)"""
    timeout = _worst_case_timeout_seconds(1, config.GEMINI_MODEL_CHAT_FALLBACK)
    return _call_worker_endpoint("/chat", {"session_id": session_id, "prompt": prompt}, timeout=timeout)


def _call_worker_draft(
    topic: str, instructions: str, answers: dict | None = None, session_id: str | None = None
) -> dict:
    """เรียก /draft — ใช้โมเดลที่ reasoning ดีกว่า (GEMINI_MODEL_DRAFT) และเรียก LLM 2 ครั้งทางตรรกะ
    (ร่าง + scrutinize) ต่อคำขอ แต่ละครั้งอาจไล่ลอง GEMINI_MODEL_DRAFT_FALLBACK เต็มรายการถ้าเจอ
    timeout/quota ต่อเนื่อง (ดู _worst_case_timeout_seconds) จึง timeout ยาวกว่า _call_worker_chat มาก
    answers: คำถาม->คำตอบ จากขั้นตอนคำถามเพิ่มเติม (ดู ADR-002/ADR-007) ส่งต่อให้ worker เพื่อ ground ร่าง
    session_id: ถ้าส่งไป worker จะฉีดร่าง+scrutiny เข้า chat memory ของ session นี้ ทำให้โหมด
    ถาม-ตอบปกติในเซสชันเดียวกันอ้างอิงร่างนี้ต่อได้ (ดู ADR-004)"""
    timeout = _worst_case_timeout_seconds(2, config.GEMINI_MODEL_DRAFT_FALLBACK)
    return _call_worker_endpoint(
        "/draft",
        {"topic": topic, "instructions": instructions, "answers": answers or {}, "session_id": session_id},
        timeout=timeout,
    )


def _call_worker_topic_step(endpoint: str, payload: dict) -> dict:
    """เรียก endpoint ทีละหัวข้อ (ใช้ร่วมกันโดย /draft/questions/interactive และ /review/topic
    เพราะ request/response shape เหมือนกันทุกประการ — ADR-007 ข้อ 3 ตั้งใจ reuse กลไกนี้จาก ADR-006)
    ทำ LLM call ทางตรรกะครั้งเดียว (prefill หรือ follow-up) ต่อคำขอ ใช้ GEMINI_MODEL_DRAFT_FALLBACK
    เหมือนโหมดร่าง/รีวิวอื่นๆ (ดู _worst_case_timeout_seconds)"""
    timeout = _worst_case_timeout_seconds(1, config.GEMINI_MODEL_DRAFT_FALLBACK)
    return _call_worker_endpoint(endpoint, payload, timeout=timeout)


def _call_worker_review_target(source: str, file_name: str, content_base64: str | None = None) -> dict:
    """เรียก /review/target — ส่งเอกสารเป้าหมาย (อัปโหลดหรือเลือกจาก corpus) ไปให้ worker parse
    heading + สร้าง Review Topics (รวม LLM call ทางตรรกะ 1 ครั้งสำหรับ checklist-derived topics —
    ดู ADR-006) + auto-suggest เอกสารที่เกี่ยวข้อง (ดู _worst_case_timeout_seconds)"""
    timeout = _worst_case_timeout_seconds(1, config.GEMINI_MODEL_DRAFT_FALLBACK)
    return _call_worker_endpoint(
        "/review/target",
        {"source": source, "file_name": file_name, "content_base64": content_base64},
        timeout=timeout,
    )


def _call_worker_review_finalize(
    file_name: str, review_topics: list[dict], answers: dict, confirmed_docs: list[str]
) -> dict:
    """เรียก /review/finalize — ขั้นตอนสุดท้ายของโหมดรีวิวเอกสาร รวมคำตอบทุกหัวข้อเป็นรายงานสรุปการ
    เปลี่ยนแปลง + เอกสารฉบับปรับปรุงคู่กัน (ดู ADR-006 ข้อ 6) — LLM call ทางตรรกะครั้งเดียวแต่ context
    อาจใหญ่ (รวมคำตอบทุกหัวข้อ) (ดู _worst_case_timeout_seconds)"""
    timeout = _worst_case_timeout_seconds(1, config.GEMINI_MODEL_DRAFT_FALLBACK)
    return _call_worker_endpoint(
        "/review/finalize",
        {
            "file_name": file_name,
            "review_topics": review_topics,
            "answers": answers,
            "confirmed_cross_reference_docs": confirmed_docs,
        },
        timeout=timeout,
    )
