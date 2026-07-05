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

WORKER_HOST = "127.0.0.1"
WORKER_PORT = 8765
WORKER_URL = f"http://{WORKER_HOST}:{WORKER_PORT}"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER_SCRIPT = os.path.join(BASE_DIR, "rag_worker.py")


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
    return _call_worker_endpoint("/chat", {"session_id": session_id, "prompt": prompt}, timeout=120)


def _call_worker_draft(
    topic: str, instructions: str, answers: dict | None = None, session_id: str | None = None
) -> dict:
    """เรียก /draft — ใช้โมเดลที่ reasoning ดีกว่า (GEMINI_MODEL_DRAFT) และเรียก LLM 2 ครั้ง
    (ร่าง + scrutinize) ต่อคำขอ จึงตั้ง timeout ยาวกว่า _call_worker_chat — worker จำกัด 1 LLM call
    ไว้ไม่เกิน GEMINI_REQUEST_TIMEOUT_MS (ดีฟอลต์ 5 นาที) ต่อครั้ง ดังนั้น 2 ครั้งรวมกันในทางทฤษฎีอาจ
    ถึงเกือบ 10 นาที (กรณีเลวร้ายที่สุด) จึงตั้ง client timeout ไว้กว้างกว่านั้นพอสมควร
    answers: คำถาม->คำตอบ จากขั้นตอนคำถามเพิ่มเติม (ดู ADR-002/ADR-007) ส่งต่อให้ worker เพื่อ ground ร่าง
    session_id: ถ้าส่งไป worker จะฉีดร่าง+scrutiny เข้า chat memory ของ session นี้ ทำให้โหมด
    ถาม-ตอบปกติในเซสชันเดียวกันอ้างอิงร่างนี้ต่อได้ (ดู ADR-004)"""
    return _call_worker_endpoint(
        "/draft",
        {"topic": topic, "instructions": instructions, "answers": answers or {}, "session_id": session_id},
        timeout=660,
    )


def _call_worker_topic_step(endpoint: str, payload: dict) -> dict:
    """เรียก endpoint ทีละหัวข้อ (ใช้ร่วมกันโดย /draft/questions/interactive และ /review/topic
    เพราะ request/response shape เหมือนกันทุกประการ — ADR-007 ข้อ 3 ตั้งใจ reuse กลไกนี้จาก ADR-006)
    ทำ LLM call ครั้งเดียว (prefill หรือ follow-up) ต่อคำขอ ตั้ง timeout ให้กว้างกว่า
    GEMINI_REQUEST_TIMEOUT_MS ฝั่ง worker (ดีฟอลต์ 5 นาที) พอสมควร กันเคส client timeout ก่อน worker เอง"""
    return _call_worker_endpoint(endpoint, payload, timeout=360)


def _call_worker_review_target(source: str, file_name: str, content_base64: str | None = None) -> dict:
    """เรียก /review/target — ส่งเอกสารเป้าหมาย (อัปโหลดหรือเลือกจาก corpus) ไปให้ worker parse
    heading + สร้าง Review Topics (รวม LLM call 1 ครั้งสำหรับ checklist-derived topics — ดู ADR-006)
    + auto-suggest เอกสารที่เกี่ยวข้อง — ตั้ง timeout กว้างกว่า GEMINI_REQUEST_TIMEOUT_MS ฝั่ง worker"""
    return _call_worker_endpoint(
        "/review/target",
        {"source": source, "file_name": file_name, "content_base64": content_base64},
        timeout=360,
    )


def _call_worker_review_finalize(
    file_name: str, review_topics: list[dict], answers: dict, confirmed_docs: list[str]
) -> dict:
    """เรียก /review/finalize — ขั้นตอนสุดท้ายของโหมดรีวิวเอกสาร รวมคำตอบทุกหัวข้อเป็นรายงานสรุปการ
    เปลี่ยนแปลง + เอกสารฉบับปรับปรุงคู่กัน (ดู ADR-006 ข้อ 6) — LLM call ครั้งเดียวแต่ context อาจใหญ่
    (รวมคำตอบทุกหัวข้อ) ตั้ง timeout กว้างกว่า GEMINI_REQUEST_TIMEOUT_MS ฝั่ง worker พอสมควร"""
    return _call_worker_endpoint(
        "/review/finalize",
        {
            "file_name": file_name,
            "review_topics": review_topics,
            "answers": answers,
            "confirmed_cross_reference_docs": confirmed_docs,
        },
        timeout=420,
    )
