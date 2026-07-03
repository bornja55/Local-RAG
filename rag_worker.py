"""
rag_worker.py — โปรเซสแยกต่างหากที่โหลด torch/faiss/embedding/reranker/LLM ทั้งหมด
รันเป็น local HTTP server อยู่เบื้องหลัง แยกขาดจากโปรเซสของ Streamlit โดยสิ้นเชิง

เหตุผลที่ต้องแยก: พิสูจน์แล้วว่า Streamlit เปล่าๆ (ไม่มี RAG) ไม่ crash เลย แต่พอโหลด
torch+faiss+transformers models หนักๆ ไว้ในโปรเซสเดียวกับ Streamlit แล้วจะ crash แบบ
Windows access violation ที่ WINHTTP.dll (offset เดิมซ้ำทุกครั้ง) หลัง idle ไปสักพัก —
สาเหตุน่าจะเป็น native library conflict (OpenMP duplicate runtime ระหว่าง faiss-cpu กับ
PyTorch) ที่ทำให้ heap เพี้ยนแบบ delayed แล้วไปโผล่ตอน Streamlit เรียก WinHTTP ภายหลัง
การย้าย native libraries ทั้งหมดออกจากโปรเซสของ Streamlit จึงตัดปัญหานี้ได้ตรงจุด

Endpoints:
    GET  /health  -> {"status": "loading"|"ready"|"error", "detail": "..."}
    POST /chat    -> body {"session_id": "...", "prompt": "..."}
                     -> {"response": "...", "sources": [...], "tokens": N}
                     -> หรือ {"error": "..."} ถ้าพัง
    POST /draft/questions -> body {"topic": "...", "instructions": "..."}
                     -> {"questions": [...], "sources": [...]}
                     -> หรือ {"error": "..."} ถ้าพัง
                     สร้างคำถามเพิ่มเติมเฉพาะจุดที่ context ไม่ครอบคลุม ก่อนร่างจริง (ดู ADR-002)
    POST /draft   -> body {"session_id": "...", "topic": "...", "instructions": "...",
                            "answers": {"คำถาม": "คำตอบ", ...}}
                     -> {"draft_markdown": "...", "scrutiny": "...", "sources": [...]}
                     -> หรือ {"error": "..."} ถ้าพัง
                     โหมดร่างเอกสารนโยบายใหม่ + auto-scrutinize ในคำขอเดียว ใช้ context จาก
                     index เดียวกับ /chat แต่ปลดกฎ grounding เข้มงวดออก (ดู ADR-001, CONTEXT.md)
                     answers เป็น optional — คำถามที่ไม่มีคำตอบ (หรือคำตอบว่าง) จะถูก mark เป็น
                     [ต้องระบุ: ...] ในร่างแทนการเดาคำตอบเอง (ดู ADR-002)
                     session_id เป็น optional — ถ้าให้มา ร่าง+scrutiny ที่สร้างเสร็จจะถูกฉีดเข้า
                     chat memory ของ session นั้น (ติดป้ายกำกับชัดเจนว่ายังไม่อนุมัติ) ทำให้ /chat
                     ในเซสชันเดียวกันหยิบมาคุยต่อได้ (ดู ADR-004)
    POST /review/target -> body {"source": "upload"|"corpus", "file_name": "...",
                            "content_base64": "..." (เฉพาะ source=="upload")}
                     -> {"file_name": "...", "review_topics": [...], "suggested_cross_reference_docs": [...]}
                     -> หรือ {"error": "...", "message": "..."} ถ้าพัง/parse heading ไม่ได้/ฟอร์แมตไม่รองรับ
                     รับเอกสารเป้าหมาย (Target document access — parse ตรง ไม่ผ่าน FAISS) คืน Review
                     Topic ผสมจาก heading จริง + checklist-derived พร้อมเอกสารที่เกี่ยวข้องที่ auto-suggest
                     ไว้ให้ยืนยัน/แก้ (ดู ADR-006)
    POST /review/topic -> body {"review_topics": [...], "confirmed_cross_reference_docs": [...],
                            "answers": {...}, "topic_id": "...", "requesting_followup_for_answer": bool}
                     -> {"topic_id": "...", "prefill": "...", "prefill_sources": [...]}
                        หรือ {"topic_id": "...", "follow_up_question": "..."|null}
                     ถามทีละหัวข้อรีวิว พร้อม Prefill (ก่อนถาม) / follow-up (หลังตอบ, เฉพาะ
                     checklist-derived) — stateless เต็มรูปแบบ client resend ทุกอย่างทุกครั้ง (ดู ADR-006)
    POST /draft/questions/interactive -> body {"topic": "...", "instructions": "...",
                            "review_topics": [...] (ว่าง = ครั้งแรก), "answers": {...}, "topic_id": "...",
                            "requesting_followup_for_answer": bool}
                     -> ครั้งแรก: {"review_topics": [...], "sources": [...]}
                        ครั้งถัดไป: เหมือน /review/topic
                     คำถามเพิ่มเติมก่อนร่างแบบทีละข้อ+prefill+follow-up (ขยาย ADR-002 เป็น 10-25 ข้อ
                     จัดหมวดหมู่) — reuse กลไกเดียวกับ /review/topic แต่ไม่ scope ต่อเอกสารเป้าหมาย
                     ไม่แก้ /draft/questions เดิมแม้แต่บรรทัดเดียว (ดู ADR-007)
    POST /review/finalize -> body {"file_name": "...", "review_topics": [...], "answers": {...},
                            "confirmed_cross_reference_docs": [...]}
                     -> {"change_report_markdown": "...", "updated_document_markdown": "...", "sources": [...]}
                     -> หรือ {"error": "...", "message": "..."} ถ้าพัง
                     ขั้นตอนสุดท้ายของโหมดรีวิวเอกสาร รวมคำตอบทุกหัวข้อเป็นรายงานสรุปการเปลี่ยนแปลง +
                     เอกสารฉบับปรับปรุงคู่กัน (ดู ADR-006 ข้อ 6 — เอกสารเพิ่มเติมนอกเหนือตัวอย่าง endpoint
                     ในข้อ 9 แต่จำเป็นต่อผลลัพธ์ตามที่ตัดสินใจไว้)

โมเดลสำรอง (fallback):
    ถ้าตั้ง GEMINI_MODEL_CHAT_FALLBACK / GEMINI_MODEL_DRAFT_FALLBACK ไว้ใน .env (รองรับหลายโมเดล
    คั่นด้วย comma เรียงเป็นลำดับ) ระบบจะไล่สลับไปใช้โมเดลสำรองทีละตัวโดยอัตโนมัติ เฉพาะกรณี retry
    โมเดลหลักครบ 3 ครั้งแล้วยังชนโควตาอยู่ (ดู ADR-003) ไม่ได้ retry ซ้ำต่อโมเดลสำรองแต่ละตัว —
    ถ้าโมเดลสำรองทุกตัวในรายการ error หมดก็คืน error ตัวสุดท้ายตามปกติ

Session cleanup:
    session ที่ไม่ได้ใช้งานเกิน SESSION_IDLE_TIMEOUT_SECONDS (ดีฟอลต์ 8 ชม.) จะถูกลบออกจาก memory
    อัตโนมัติทุก 10 นาที ป้องกัน worker กินแรมโตไม่มีเพดานถ้าปล่อยรันต่อเนื่องนานๆ (ดู ADR-005)

รันแบบ standalone:
    venv\\Scripts\\python.exe rag_worker.py
(ปกติแล้ว app.py จะ auto-start ให้เองถ้ายังไม่ได้รันอยู่ ไม่ต้องรันมือ)
"""
import os
import io
import re
import sys
import json
import time
import base64
import threading
import traceback
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
BGE_M3_PATH      = os.path.join(BASE_DIR, "models", "bge-m3")
RERANKER_PATH    = os.path.join(BASE_DIR, "models", "bge-reranker-v2-m3")
STORAGE_DIR      = os.path.join(BASE_DIR, "storage")
DATA_DIRS        = [
    os.path.join(BASE_DIR, "Policies"),
    os.path.join(BASE_DIR, "Procedures"),
    os.path.join(BASE_DIR, "Manuals"),
    os.path.join(BASE_DIR, "Forms"),
]
LOG_FILE         = os.path.join(BASE_DIR, "rag_worker.log")
PORT             = 8765


def _load_dotenv(path: str) -> None:
    """โหลด KEY=VALUE จาก .env แบบง่ายๆ ไม่ต้องพึ่ง python-dotenv (ไม่มีใน requirements.txt)
    ไม่ทับค่าที่ set ไว้แล้วใน environment จริง (เช่นถ้า deploy ผ่าน CI/systemd ที่ set env เอง)"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(os.path.join(BASE_DIR, ".env"))

# ── Env setup (เหมือน app.py เดิม แต่ตัด Streamlit-specific env vars ออก) ──────
os.environ["OMP_NUM_THREADS"] = "1"
# faiss-cpu (Intel MKL/OpenMP) กับ PyTorch (OpenMP ของตัวเอง) ชนกันได้บน Windows
# แม้จะแยกโปรเซสจาก Streamlit แล้ว ก็ยังตั้งไว้กันเหนียวเพราะ conflict นี้เกิดขึ้นได้
# ในตัวโปรเซสนี้เองระหว่าง faiss กับ torch
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["TQDM_DISABLE"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

if "GOOGLE_API_KEY" not in os.environ:
    raise RuntimeError(
        "ไม่พบ GOOGLE_API_KEY — สร้างไฟล์ .env ที่ root โปรเจกต์ (ดู .env.example) "
        "แล้วใส่ GOOGLE_API_KEY=<คีย์จริงของคุณ>"
    )

# ชื่อองค์กร — ตั้งค่าผ่าน .env (COMPANY_NAME=...) เพื่อให้ deploy ให้ลูกค้า/องค์กรอื่น
# ได้โดยไม่ต้องแก้โค้ด ถ้าไม่ตั้งจะใช้คำกลางๆ แทน
COMPANY_NAME = os.environ.get("COMPANY_NAME", "องค์กรของคุณ")

# โมเดล Gemini แยกตามโหมด — Q&A ปกติใช้โมเดลเบา/ถูก (retrieval ล้วน ไม่ต้อง reasoning มาก)
# ส่วนโหมดร่างเอกสาร+scrutinize ใช้โมเดลที่ reasoning ดีกว่า (ดู ADR-001 สำหรับเหตุผลการเลือก)
GEMINI_MODEL_CHAT  = os.environ.get("GEMINI_MODEL_CHAT", "gemini-3.1-flash-lite")
GEMINI_MODEL_DRAFT = os.environ.get("GEMINI_MODEL_DRAFT", "gemini-3.5-flash")

def _parse_model_chain(env_value: str) -> list[str]:
    """แปลงค่า env var โมเดลสำรองเป็น list — รองรับทั้งค่าเดิมของ ADR-003 (โมเดลเดียว ไม่มี comma)
    และค่าใหม่ (หลายโมเดลคั่นด้วย comma เรียงเป็นลำดับที่จะไล่ลองทีละตัว) backward-compatible 100%:
    ค่าที่ไม่มี comma เลยจะได้ list ที่มีสมาชิกตัวเดียว พฤติกรรมเดิมทุกประการ ค่าว่างเปล่าได้ list ว่าง
    (= ไม่มี fallback เหมือนเดิม)"""
    return [m.strip() for m in env_value.split(",") if m.strip()]


# โมเดลสำรองเมื่อโมเดลหลักชนโควตาต่อเนื่อง (ดู ADR-003) — ค่าเริ่มต้นว่างเปล่า = ไม่มี fallback,
# พฤติกรรมเหมือนเดิมทุกประการ ตั้งค่าผ่าน .env เท่านั้น รองรับหลายโมเดลคั่นด้วย comma เรียงเป็นลำดับ
# ที่จะไล่ลองทีละตัวจนกว่าจะสำเร็จ (ขยายจาก "โมเดลสำรอง 1 ตัว" เดิม — ดู ADR-003 หมายเหตุเพิ่มเติม
# 2026-07-03) เช่น GEMINI_MODEL_CHAT_FALLBACK=gemma-4-26b-a4b-it,gemini-2.5-flash-lite
GEMINI_MODEL_CHAT_FALLBACK  = _parse_model_chain(os.environ.get("GEMINI_MODEL_CHAT_FALLBACK", ""))
GEMINI_MODEL_DRAFT_FALLBACK = _parse_model_chain(os.environ.get("GEMINI_MODEL_DRAFT_FALLBACK", ""))

# หมดอายุ session ที่ไม่ได้ใช้งานนาน (ดู ADR-005) — ป้องกัน _sessions dict โตไม่มีเพดานถ้า worker
# รันต่อเนื่องนานๆ โดยไม่ restart ค่าเริ่มต้น 8 ชั่วโมง ปรับได้ผ่าน .env
SESSION_IDLE_TIMEOUT_SECONDS = int(os.environ.get("SESSION_IDLE_TIMEOUT_SECONDS", str(8 * 60 * 60)))

# Timeout ต่อ 1 request ที่ยิงไป Gemini API (หน่วยมิลลิวินาที ตามที่ google-genai SDK กำหนด) —
# ก่อนหน้านี้ไม่เคยตั้งเลยสักที่ ทำให้พบระหว่างทดสอบ ADR-006 ว่า request บางครั้งค้างเป็นนาทีโดยไม่มี
# log ตามมาเลย (เจอครั้งแรกตอนทดสอบ /review/target กับเอกสารจริงที่มี heading เยอะ — ดูบันทึกใน
# HANDOFF.md) ค่าเริ่มต้น 5 นาที กว้างพอสำหรับ gemini-3.5-flash ที่เคยสังเกตว่าช้าได้ถึง ~170 วินาที
# ในบางครั้ง แต่ยังจำกัดเพดานไว้ไม่ให้ค้างไม่มีที่สิ้นสุด ทำให้ retry/fallback logic เดิม (ดู ADR-003)
# ทำงานได้จริง ปรับได้ผ่าน .env
GEMINI_REQUEST_TIMEOUT_MS = int(os.environ.get("GEMINI_REQUEST_TIMEOUT_MS", str(5 * 60 * 1000)))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# import faiss ก่อน torch เสมอ กันปัญหา OpenMP DLL โหลดผิดลำดับ
import faiss  # noqa: E402,F401

_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ── Global state ─────────────────────────────────────────────────────────
_state_lock = threading.Lock()
_status = {"status": "loading", "detail": "กำลังเริ่มต้น..."}
_index = None
_reranker = None
_sys_prompt = None
_sessions: dict[str, object] = {}  # session_id -> ChatMemoryBuffer
_session_last_used: dict[str, float] = {}  # session_id -> unix timestamp ที่ใช้งานล่าสุด (ดู ADR-005)
_sessions_lock = threading.Lock()


def _get_available_documents() -> str:
    file_list = []
    for d in DATA_DIRS:
        if os.path.exists(d):
            for f in os.listdir(d):
                if f.endswith((".md", ".pdf", ".txt")):
                    file_list.append(f)
    return "\n- ".join(file_list) if file_list else "ไม่พบเอกสาร"


def _build_sys_prompt() -> str:
    return (
        f"คุณคือผู้ช่วยตอบคำถามเรื่องนโยบายและระเบียบปฏิบัติของ {COMPANY_NAME} "
        "กฎเหล็ก: หน้าที่ของคุณคือการหาคำตอบจากบริบท (Context) ที่ให้มาเท่านั้น ห้ามคิดคำตอบขึ้นมาเองโดยเด็ดขาด "
        "หากข้อมูลในบริบทไม่มีคำตอบสำหรับคำถามนั้น ให้คุณตอบไปตรงๆ ว่า 'ขออภัยครับ ไม่พบข้อมูลเรื่องนี้ในนโยบายขององค์กร' "
        "แต่หากพบข้อมูล ให้คุณสรุปคำตอบอย่างละเอียดและเป็นมืออาชีพ พร้อมอ้างอิงรหัสเอกสารและหัวข้อเสมอ\n\n"
        "--- รายชื่อเอกสารทั้งหมดที่มีอยู่ในฐานข้อมูล RAG ณ ตอนนี้ ---\n- "
        f"{_get_available_documents()}\n"
        "--------------------------------------------------\n"
        "หมายเหตุ: หากผู้ใช้ถามว่า 'มีเอกสารอะไรบ้าง' หรือ 'มีเอกสาร X ไหม' ให้คุณตรวจสอบจาก [รายชื่อเอกสารทั้งหมดที่มีอยู่ในฐานข้อมูล RAG] ด้านบนนี้ได้เลย และอธิบายให้ผู้ใช้ฟัง"
    )


def _build_draft_sys_prompt() -> str:
    """system prompt สำหรับโหมดร่างเอกสาร — ต่างจาก _build_sys_prompt() (Q&A) ตรงที่ปลดกฎ
    'ห้ามแต่งคำตอบเอง' ออก เพราะเป้าหมายของโหมดนี้คือแต่งเนื้อหาใหม่โดยนิยาม (ดู ADR-001)"""
    return (
        f"คุณคือผู้ช่วยร่างนโยบายและระเบียบปฏิบัติให้กับ {COMPANY_NAME} "
        "หน้าที่ของคุณคือแต่งเอกสารนโยบายฉบับใหม่ทั้งฉบับตามหัวข้อและคำสั่งที่ผู้ใช้ระบุ "
        "โดยอ้างอิงโครงสร้าง รูปแบบ ศัพท์เฉพาะ และหลักการของนโยบายที่มีอยู่แล้ว (ดู Context ด้านล่าง) "
        "ให้สอดคล้องกัน แต่คุณแต่งเนื้อหาใหม่ที่ยังไม่มีอยู่ในเอกสารเดิมได้ "
        "(ต่างจากโหมดถาม-ตอบปกติที่ห้ามแต่งคำตอบเองโดยเด็ดขาด)\n\n"
        "กติกา:\n"
        "1. เขียนเป็นภาษาไทย รูปแบบ Markdown (ใช้ # ## สำหรับหัวข้อ, - สำหรับ bullet)\n"
        "2. ถ้า Context มีข้อมูลที่เกี่ยวข้อง (เช่น รูปแบบเลขที่เอกสาร โครงสร้างหมวดหมู่ ศัพท์เฉพาะองค์กร) "
        "ให้ใช้ให้สอดคล้องกัน\n"
        "3. ขึ้นต้นเอกสารด้วยบรรทัดกำกับชัดเจนว่า "
        "'⚠️ เอกสารนี้เป็นร่างที่สร้างโดย AI ต้องผ่านการตรวจสอบและอนุมัติจากผู้มีอำนาจก่อนนำไปใช้จริง'\n"
        "4. ห้ามอ้างข้อเท็จจริง ตัวเลข หรือข้อกำหนดที่ไม่มีมูลจาก Context หรือความรู้ทั่วไปที่สมเหตุสมผล "
        "หากไม่แน่ใจให้ระบุว่าเป็นข้อเสนอแนะที่ต้องพิจารณาเพิ่มเติม\n"
        "5. ถ้าได้รับ 'คำถามที่ผู้ใช้ข้ามไม่ตอบ' มาด้วย ห้ามเดาคำตอบเองเด็ดขาด ให้ใส่เครื่องหมาย "
        "[ต้องระบุ: <คำถามนั้น>] ตรงจุดที่เกี่ยวข้องในเนื้อหาแทน (ดู ADR-002)\n\n"
        "--- นโยบาย/เอกสารที่มีอยู่แล้วในองค์กร (ใช้อ้างอิงความสอดคล้อง) ---\n"
    )


def _build_clarify_questions_prompt() -> str:
    """system prompt สำหรับขั้นตอนสร้างคำถามเพิ่มเติมก่อนร่าง (ดู ADR-002) — เป้าหมายคือหา
    'จุดที่ context ไม่ครอบคลุม' ที่ AI จะต้องมั่วถ้าไม่ถามผู้ใช้ก่อน ไม่ใช่ checklist ตายตัว"""
    return (
        f"คุณคือผู้ช่วยเตรียมข้อมูลก่อนร่างนโยบายให้กับ {COMPANY_NAME} "
        "หน้าที่ของคุณคือตรวจสอบว่าการจะร่างนโยบายตามหัวข้อที่ผู้ใช้ระบุให้ครบถ้วนและถูกต้อง "
        "ต้องมีข้อมูลเฉพาะองค์กรอะไรบ้างที่ไม่มีอยู่ใน Context ด้านล่างนี้เลย แล้วตั้งคำถามถามผู้ใช้ "
        "เพื่อขอข้อมูลเหล่านั้น แทนที่จะปล่อยให้ขั้นตอนร่างต้องเดาเอาเอง\n\n"
        "กติกา:\n"
        "1. ตั้งคำถาม 3-6 ข้อ เฉพาะเจาะจงกับหัวข้อนี้จริงๆ ห้ามใช้คำถามทั่วไปที่ใช้ได้กับทุกนโยบาย "
        "(เช่น ห้ามถาม 'ขอบเขตของนโยบายคืออะไร' เฉยๆ แบบไม่เจาะจง)\n"
        "2. ถ้า Context ด้านล่างมีข้อมูลที่เกี่ยวข้องอยู่แล้ว ห้ามถามซ้ำเรื่องนั้น\n"
        "3. ตอบเป็นรายการ Markdown bullet เท่านั้น (บรรทัดละ 1 คำถาม ขึ้นต้นด้วย '- ') "
        "ห้ามมีข้อความอื่นนอกเหนือจากรายการคำถาม ห้ามมีหัวข้อ/คำนำ/สรุปปิดท้าย\n\n"
        "--- นโยบาย/เอกสารที่มีอยู่แล้วในองค์กร (context) ---\n"
    )


def _build_scrutinize_sys_prompt() -> str:
    """system prompt สำหรับขั้นตอน auto-scrutinize ที่รันต่อทันทีหลังร่างเสร็จ — ใช้มาตรฐาน
    ระดับความรุนแรงเดียวกับ /scrutinize skill (CRITICAL/WARNING/NITPICK/VERDICT, ดู CONTEXT.md)"""
    return (
        f"คุณคือผู้ตรวจทานนโยบายอิสระของ {COMPANY_NAME} หน้าที่ของคุณคือวิจารณ์ร่างนโยบายที่แนบมา "
        "โดยเทียบกับนโยบาย/ระเบียบปฏิบัติที่มีอยู่จริงของบริษัท (Context เดียวกับที่ใช้ร่าง) "
        "หาความขัดแย้ง ช่องว่าง หรือความเสี่ยง แล้วรายงานผลตามระดับความรุนแรงนี้เท่านั้น:\n\n"
        "- **CRITICAL**: ขัดแย้งกับนโยบายเดิมอย่างชัดเจน หรือมีความเสี่ยงสูงหากนำไปใช้จริงโดยไม่แก้ไข\n"
        "- **WARNING**: ควรตรวจสอบเพิ่มเติมก่อนอนุมัติใช้จริง ไม่ใช่ข้อผิดพลาดชัดเจนแต่มีความเสี่ยง\n"
        "- **NITPICK**: จุดเล็กน้อย ไม่กระทบสาระสำคัญ (เช่น รูปแบบ ความสม่ำเสมอของศัพท์)\n"
        "- **VERDICT**: สรุปภาพรวม 1 บรรทัดว่าร่างนี้พร้อมส่งต่อให้มนุษย์พิจารณาต่อหรือไม่\n\n"
        "หมายเหตุสำคัญ: ถ้าในร่างมีเครื่องหมาย [ต้องระบุ: ...] หลงเหลืออยู่ (แปลว่าผู้ใช้ข้ามคำถามนั้น "
        "ไม่ตอบตอนสร้างร่าง) ให้ยกขึ้นเป็นประเด็นระดับ WARNING เสมอทุกจุดที่พบ พร้อมระบุว่าเป็นข้อมูลที่ยังขาด "
        "ไม่ใช่ข้อขัดแย้งกับนโยบายเดิม\n\n"
        "ตอบเป็น Markdown ใช้หัวข้อระดับความรุนแรงเป็นตัวหนา ถ้าไม่พบประเด็นในระดับใด ให้เขียนว่า "
        "'ไม่พบประเด็น' ใต้หัวข้อนั้น อย่าให้คำแนะนำนอกเหนือขอบเขตของนโยบายที่มีอยู่ใน Context\n\n"
        "--- นโยบาย/เอกสารที่มีอยู่แล้วในองค์กร (ใช้เทียบความสอดคล้อง) ---\n"
    )


def _load_everything() -> None:
    """โหลดโมเดล/index ทั้งหมด (บล็อกประมาณ 4 นาทีรอบแรก) รันใน background thread"""
    global _index, _reranker, _sys_prompt
    try:
        log("เริ่มโหลด embedding model (BGE-M3)...")
        import torch
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        from llama_index.core import Settings

        embed_model = HuggingFaceEmbedding(
            model_name=BGE_M3_PATH,
            model_kwargs={"torch_dtype": torch.float16, "use_safetensors": True},
        )
        embed_model.get_text_embedding("warmup")
        log("Embedding model พร้อมแล้ว")

        Settings.embed_model = embed_model
        Settings.context_window = 1048576
        Settings.chunk_size = 400
        Settings.chunk_overlap = 40

        log("เริ่มโหลด reranker (BGE-reranker-v2-m3)...")
        from sentence_transformers import CrossEncoder
        from llama_index.core.postprocessor.types import BaseNodePostprocessor
        from pydantic import Field, PrivateAttr

        class _Reranker(BaseNodePostprocessor):
            top_n: int = Field(default=20)
            model_name: str = Field(default=RERANKER_PATH)
            _model = PrivateAttr()

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self._model = CrossEncoder(
                    self.model_name,
                    model_kwargs={"torch_dtype": torch.float16, "use_safetensors": True},
                )
                self._model.predict([["warmup", "test"]])

            @classmethod
            def class_name(cls):
                return "Reranker"

            def _postprocess_nodes(self, nodes, query_bundle=None):
                if not query_bundle or not nodes:
                    return nodes
                pairs = [[query_bundle.query_str, n.node.get_content()] for n in nodes]
                scores = self._model.predict(pairs)
                for node, score in zip(nodes, scores):
                    node.score = float(score)
                return sorted(nodes, key=lambda x: x.score or 0.0, reverse=True)[: self.top_n]

        reranker = _Reranker()
        log("Reranker พร้อมแล้ว")

        log("เริ่มโหลด FAISS index...")
        from llama_index.core import (
            VectorStoreIndex, SimpleDirectoryReader,
            StorageContext, load_index_from_storage,
        )
        from llama_index.vector_stores.faiss import FaissVectorStore

        if not os.path.exists(STORAGE_DIR):
            documents = []
            for d in DATA_DIRS:
                if os.path.exists(d):
                    documents.extend(SimpleDirectoryReader(d).load_data())
            if not documents:
                raise RuntimeError("ไม่พบเอกสารในโฟลเดอร์ Policies/Procedures/Manuals/Forms")
            index = VectorStoreIndex.from_documents(documents)
            index.storage_context.persist(persist_dir=STORAGE_DIR)
        else:
            vector_store = FaissVectorStore.from_persist_dir(STORAGE_DIR)
            storage_context = StorageContext.from_defaults(
                vector_store=vector_store, persist_dir=STORAGE_DIR
            )
            index = load_index_from_storage(storage_context)
        log("FAISS index พร้อมแล้ว")

        with _state_lock:
            _index = index
            _reranker = reranker
            _sys_prompt = _build_sys_prompt()
            _status["status"] = "ready"
            _status["detail"] = "พร้อมใช้งาน"
        log("Worker พร้อมรับคำถามแล้ว")

    except Exception as e:
        log(f"โหลดล้มเหลว: {type(e).__name__} - {e}\n{traceback.format_exc()}")
        with _state_lock:
            _status["status"] = "error"
            _status["detail"] = f"{type(e).__name__}: {e}"


def _is_quota_error(e: Exception) -> bool:
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


def _build_llm(model: str):
    """สร้าง GoogleGenAI client พร้อม request timeout ที่กำหนดไว้ชัดเจน (GEMINI_REQUEST_TIMEOUT_MS)
    — ใช้ร่วมกันทุกจุดที่เรียก GoogleGenAI() ในไฟล์นี้ (ทั้ง complete() แบบ one-shot ผ่าน
    _complete_with_fallback และ chat_engine ผ่าน _build_chat_engine) กันไม่ให้ request ค้างไม่มี
    ที่สิ้นสุดเหมือนที่เคยพบระหว่างทดสอบ (ดูหมายเหตุที่ GEMINI_REQUEST_TIMEOUT_MS ด้านบน)"""
    from llama_index.llms.google_genai import GoogleGenAI
    from google.genai import types as genai_types

    return GoogleGenAI(
        model=model, http_options=genai_types.HttpOptions(timeout=GEMINI_REQUEST_TIMEOUT_MS)
    )


def _complete_with_fallback(
    primary_model: str, fallback_models: list[str], prompt: str, log_prefix: str
) -> tuple[str | None, Exception | None]:
    """เรียก llm.complete(prompt) พร้อม retry เดิม (3 ครั้ง + backoff) บนโมเดลหลัก ใช้ร่วมกันโดย
    _handle_clarify_questions และ _handle_draft (ทุกจุดที่เรียก llm.complete() แบบ one-shot)
    ถ้า retry ครบ 3 ครั้งแล้วยังเป็น quota error อยู่ และมี fallback_models ตั้งไว้ (list ไม่ว่าง)
    จะไล่ลองทีละโมเดลตามลำดับใน fallback_models (โมเดลละ 1 ครั้ง ไม่ retry ซ้ำต่อโมเดล) จนกว่าจะสำเร็จ
    หรือหมดรายการ — ถ้าโมเดลสำรองตัวใดตัวหนึ่ง error (ไม่ว่าประเภทไหน) ไปลองตัวถัดไปในรายการต่อทันที
    ไม่หยุดกลางคัน เพราะโมเดลสำรองแต่ละตัวเป็นอิสระจากกัน error ของตัวหนึ่งไม่ได้แปลว่าตัวถัดไปจะพังด้วย
    (ดู ADR-003 — ขยายจาก "โมเดลสำรอง 1 ตัว" เป็น "รายการโมเดลสำรอง" ในหมายเหตุ 2026-07-03)
    คืนค่า (text, error) โดย text เป็น None ถ้าทุกโมเดลล้มเหลว"""
    last_error = None
    for attempt in range(3):
        try:
            t0 = time.time()
            resp = _build_llm(primary_model).complete(prompt)
            log(f"{log_prefix} สำเร็จใน {time.time() - t0:.2f}s (โมเดล: {primary_model})")
            return resp.text, None
        except Exception as e:
            last_error = e
            log(f"{log_prefix} error (โมเดล {primary_model}): {type(e).__name__} - {e}")
            if _is_quota_error(e) and attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            break

    if fallback_models and _is_quota_error(last_error):
        for fallback_model in fallback_models:
            try:
                log(f"{log_prefix} โมเดล {primary_model} ชนโควตา กำลังลองโมเดลสำรอง {fallback_model}...")
                t0 = time.time()
                resp = _build_llm(fallback_model).complete(prompt)
                log(f"{log_prefix} สำเร็จใน {time.time() - t0:.2f}s (โมเดลสำรอง: {fallback_model})")
                return resp.text, None
            except Exception as e:
                last_error = e
                log(f"{log_prefix} error (โมเดลสำรอง {fallback_model}): {type(e).__name__} - {e}")
                continue

    return None, last_error


def _cleanup_idle_sessions() -> None:
    """ลบ session ที่ไม่ได้ใช้งานเกิน SESSION_IDLE_TIMEOUT_SECONDS ออกจาก _sessions/_session_last_used
    รันเป็น background thread แยกต่างหาก ตื่นทุก 10 นาที ป้องกัน memory ของ worker โตไม่มีเพดาน
    ถ้าปล่อยรันต่อเนื่องนานๆ โดยไม่ restart (ดู ADR-005)"""
    while True:
        time.sleep(600)
        now = time.time()
        with _sessions_lock:
            expired = [
                sid for sid, last_used in _session_last_used.items()
                if now - last_used > SESSION_IDLE_TIMEOUT_SECONDS
            ]
            for sid in expired:
                _sessions.pop(sid, None)
                _session_last_used.pop(sid, None)
        if expired:
            log(f"[CLEANUP] ลบ {len(expired)} session ที่ idle เกิน {SESSION_IDLE_TIMEOUT_SECONDS}s")


def _handle_chat(session_id: str, prompt: str) -> dict:
    from llama_index.core.memory import ChatMemoryBuffer
    from llama_index.core import Settings

    with _sessions_lock:
        if session_id not in _sessions:
            _sessions[session_id] = ChatMemoryBuffer.from_defaults(token_limit=8000)
        memory = _sessions[session_id]
        _session_last_used[session_id] = time.time()

    def _build_chat_engine(model: str):
        # สร้าง llm + chat_engine ใหม่ทุกครั้งที่เรียก (ยืนยันแล้วจาก diagnostic tests ว่า
        # ปลอดภัยข้าม thread — ไม่มี state ค้างจาก request ก่อนหน้า)
        llm = _build_llm(model)
        Settings.llm = llm
        return _index.as_chat_engine(
            chat_mode="condense_plus_context",
            memory=memory,
            similarity_top_k=60,
            node_postprocessors=[_reranker],
            system_prompt=_sys_prompt,
        )

    chat_engine = _build_chat_engine(GEMINI_MODEL_CHAT)

    last_error = None
    response_obj = None
    for attempt in range(3):
        try:
            log(f"[CHAT session={session_id[:8]}] ถาม: {prompt[:50]}...")
            t0 = time.time()
            response_obj = chat_engine.chat(prompt)
            log(f"[CHAT session={session_id[:8]}] สำเร็จใน {time.time() - t0:.2f}s")
            break
        except Exception as e:
            last_error = e
            log(f"[CHAT session={session_id[:8]}] error: {type(e).__name__} - {e}")
            if _is_quota_error(e) and attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            break

    # ── fallback ไปโมเดลสำรอง ไล่ทีละตัวตามลำดับใน GEMINI_MODEL_CHAT_FALLBACK ถ้าตั้งค่าไว้ และ
    # retry โมเดลหลักครบแล้วยังชนโควตาอยู่ (ดู ADR-003 — ขยายเป็นหลายโมเดลในหมายเหตุ 2026-07-03) ──
    if response_obj is None and GEMINI_MODEL_CHAT_FALLBACK and _is_quota_error(last_error):
        for fb_model in GEMINI_MODEL_CHAT_FALLBACK:
            try:
                log(f"[CHAT session={session_id[:8]}] โมเดล {GEMINI_MODEL_CHAT} ชนโควตา "
                    f"กำลังลองโมเดลสำรอง {fb_model}...")
                fallback_engine = _build_chat_engine(fb_model)
                t0 = time.time()
                response_obj = fallback_engine.chat(prompt)
                log(f"[CHAT session={session_id[:8]}] สำเร็จใน {time.time() - t0:.2f}s "
                    f"(โมเดลสำรอง: {fb_model})")
                break
            except Exception as e:
                last_error = e
                log(f"[CHAT session={session_id[:8]}] error (โมเดลสำรอง {fb_model}): "
                    f"{type(e).__name__} - {e}")
                continue

    if response_obj is None:
        return {"error": str(last_error) if last_error else "unknown error"}

    sources = [
        {
            "file_name": n.node.metadata.get("file_name", "Unknown"),
            "content": n.node.get_content()[:200],
        }
        for n in response_obj.source_nodes
    ]
    return {
        "response": response_obj.response,
        "sources": sources,
        "tokens": (len(prompt) + len(response_obj.response)) // 4,
    }


def _nodes_to_context_and_sources(nodes) -> tuple[str, list[dict]]:
    """แปลง list ของ retrieved nodes (หลัง rerank แล้ว) เป็น (context_text, sources) รูปแบบเดียวกับ
    ที่ /chat คืนให้ — ใช้ร่วมกันโดย _retrieve_context (whole-corpus) และ _retrieve_context_scoped
    (scoped ต่อรายชื่อไฟล์ — ดู ADR-006 Cross-reference retrieval) กันโค้ดซ้ำ"""
    context_text = "\n\n".join(
        f"[{n.node.metadata.get('file_name', 'Unknown')}]\n{n.node.get_content()}"
        for n in nodes
    )
    sources = [
        {
            "file_name": n.node.metadata.get("file_name", "Unknown"),
            "content": n.node.get_content()[:200],
        }
        for n in nodes
    ]
    return context_text, sources


def _retrieve_context(query_str: str, top_n: int = 15) -> tuple[str, list[dict]]:
    """ดึง context ที่เกี่ยวข้องจาก index+reranker เดียวกับ Q&A ใช้ทั้งขั้นตอนร่างและ scrutinize
    (ดู ADR-001 ข้อ 6 — ไม่สร้าง index แยกสำหรับโหมดร่างเอกสาร) ดึงข้าม corpus ทั้งหมดแบบ semantic
    search ล้วนๆ ไม่ scope ต่อเอกสารใดเอกสารหนึ่งเลย — ถ้าต้องการ scope ต่อรายชื่อไฟล์ที่กำหนด ใช้
    _retrieve_context_scoped() แทน (ดู ADR-006 Cross-reference retrieval)
    คืนค่า (context_text รวมเป็น string เดียว, รายการ sources แบบเดียวกับที่ /chat คืนให้)"""
    from llama_index.core.schema import QueryBundle

    retriever = _index.as_retriever(similarity_top_k=60)
    nodes = retriever.retrieve(query_str)
    nodes = _reranker._postprocess_nodes(nodes, QueryBundle(query_str=query_str))[:top_n]
    return _nodes_to_context_and_sources(nodes)


def _retrieve_context_scoped(
    query_str: str, allowed_file_names: list[str], top_n: int = 15, over_fetch_k: int = 200
) -> tuple[str, list[dict]]:
    """เหมือน _retrieve_context() แต่ scope ผลลัพธ์ให้เหลือเฉพาะไฟล์ใน allowed_file_names เท่านั้น —
    ใช้กับ **Cross-reference retrieval** ของโหมดรีวิวเอกสาร (ดู ADR-006 ข้อ 3/CONTEXT.md) เพื่อไม่ให้
    เนื้อหาจากเอกสารอื่นที่บังเอิญ match คำค้นหาหลุดเข้ามาปนกับเอกสารที่เกี่ยวข้องที่ผู้ใช้ยืนยันไว้แล้ว

    หมายเหตุ implementation (สำคัญ): FaissVectorStore ที่ระบบใช้อยู่ (ดู
    venv/Lib/site-packages/llama_index/vector_stores/faiss/base.py: query()) ไม่รองรับ metadata
    filters ที่ชั้น query() เลย — ส่ง `filters=` เข้าไปจะโดน raise ValueError("Metadata filters not
    implemented for Faiss yet.") ทันที เพราะ FAISS index เองไม่มี concept ของ metadata ในตัว ระบบจึง
    ทำ filtering แบบ application-level แทน: over-fetch (similarity_top_k สูงกว่าปกติมาก ดีฟอลต์ 200)
    จากนั้นกรอง node.metadata['file_name'] ด้วย Python เอาเฉพาะที่อยู่ใน allowed list ก่อนส่งต่อให้
    reranker เดิม — ไม่ใช่การกรองที่ชั้น vector store แบบที่ ADR-006 ผลที่ตามมาพูดถึงเรื่อง metadata
    filter เผื่อไว้ แต่ได้ผลลัพธ์เดียวกัน (scope ตาม allowed_file_names) โดยไม่ต้องแก้ FaissVectorStore
    เอง ถ้า allowed_file_names ว่างเปล่า คืน context ว่างทันทีโดยไม่ retrieve เลย (กันกรณีผู้ใช้ยังไม่
    ยืนยันเอกสารที่เกี่ยวข้องเลยสักฉบับ)"""
    if not allowed_file_names:
        return "", []

    from llama_index.core.schema import QueryBundle

    allowed_set = set(allowed_file_names)
    retriever = _index.as_retriever(similarity_top_k=over_fetch_k)
    nodes = retriever.retrieve(query_str)
    nodes = [n for n in nodes if n.node.metadata.get("file_name") in allowed_set]
    if not nodes:
        return "", []
    nodes = _reranker._postprocess_nodes(nodes, QueryBundle(query_str=query_str))[:top_n]
    return _nodes_to_context_and_sources(nodes)


def _parse_bullet_questions(raw: str) -> list[str]:
    """แปลง markdown bullet list จาก LLM (ผลลัพธ์ของ _build_clarify_questions_prompt) เป็น
    list ของคำถามล้วนๆ ตัดเครื่องหมาย '- '/'* '/เลขข้อนำหน้าออก ข้ามบรรทัดว่าง"""
    questions = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        if line:
            questions.append(line)
    return questions


# ── Document Review Mode (ADR-006) — Target document access ────────────────
# Parse ไฟล์เป้าหมายตรงๆ ไม่ผ่าน FAISS/retriever เลย เพราะเอกสารเป้าหมายอาจเป็นไฟล์อัปโหลดสดที่
# ยังไม่เคย index — markdown อ่านตรง, .docx อ่านผ่าน python-docx (ดู CONTEXT.md "Target document
# access" / ADR-006 ผลที่ตามมา — คนละ parser กันชัดเจนระหว่างสองฟอร์แมต)

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_DOCX_HEADING_STYLE_RE = re.compile(r"^Heading\s+([1-9])$", re.IGNORECASE)


def _parse_markdown_headings(text: str) -> list[dict]:
    """แยกโครงสร้าง heading ('#'..'######') ของเอกสาร markdown ตรงๆ ไม่ผ่าน retriever —
    คืน list ของ {"level": int, "heading": str, "body": str} เรียงตามลำดับที่ปรากฏในเอกสาร
    body ของแต่ละ heading = เนื้อหาตั้งแต่หลัง heading นั้นจนถึง heading ถัดไป (ไม่ว่าจะ level ใด —
    ไม่ทำ nested tree ตั้งใจให้เป็น list แบนราบเดียว พอสำหรับ UI แบบถามทีละหัวข้อของ ADR-006)
    คืน list ว่างถ้าไม่พบ heading เลย (เอกสารเขียนเป็นพรืดเดียว) — ผู้เรียกต้อง reject ต่อ (ดู ADR-006
    ข้อ 2a: ปฏิเสธทันที ไม่ fallback เป็น checklist ล้วนๆ เงียบๆ)"""
    headings: list[dict] = []
    current_body: list[str] = []
    for line in text.splitlines():
        m = _MD_HEADING_RE.match(line)
        if m:
            if headings:
                headings[-1]["body"] = "\n".join(current_body).strip()
            headings.append({"level": len(m.group(1)), "heading": m.group(2).strip(), "body": ""})
            current_body = []
        else:
            current_body.append(line)
    if headings:
        headings[-1]["body"] = "\n".join(current_body).strip()
    return headings


def _parse_docx_headings(file_bytes: bytes) -> list[dict]:
    """แยกโครงสร้าง heading ของไฟล์ .docx ตรงๆ ผ่าน python-docx อ่าน paragraph.style.name
    ('Heading 1'/'Heading 2'/'Heading 3'...) ไม่ใช่ '#'/'##' แบบ markdown — คนละ parser กันชัดเจน
    ตามที่ ADR-006 ผลที่ตามมากำชับไว้ (ดู CONTEXT.md "Target document access")
    คืนรูปแบบเดียวกับ _parse_markdown_headings() คือ list ของ {"level", "heading", "body"}"""
    from docx import Document as DocxDocument

    doc = DocxDocument(io.BytesIO(file_bytes))
    headings: list[dict] = []
    current_body: list[str] = []
    for para in doc.paragraphs:
        style_name = (para.style.name if para.style else "") or ""
        m = _DOCX_HEADING_STYLE_RE.match(style_name.strip())
        if m:
            if headings:
                headings[-1]["body"] = "\n".join(current_body).strip()
            headings.append({"level": int(m.group(1)), "heading": para.text.strip(), "body": ""})
            current_body = []
        elif headings:
            if para.text.strip():
                current_body.append(para.text)
    if headings:
        headings[-1]["body"] = "\n".join(current_body).strip()
    return headings


def _get_document_path(file_name: str) -> str | None:
    """หา full path ของเอกสารที่ index ไว้แล้วใน DATA_DIRS จากชื่อไฟล์ (ไม่สนตัวพิมพ์ใหญ่/เล็ก) —
    ใช้ตอนเอกสารเป้าหมายของโหมดรีวิวถูกเลือกจากเอกสารที่ index ไว้แล้ว (ทางเลือกที่ 2 ตาม ADR-006
    ข้อ 2) แทนการอัปโหลดไฟล์ใหม่ตรง"""
    target_lower = file_name.strip().lower()
    for d in DATA_DIRS:
        if not os.path.exists(d):
            continue
        for f in os.listdir(d):
            if f.lower() == target_lower:
                return os.path.join(d, f)
    return None


def _extract_target_document(body: dict) -> tuple[dict | None, dict | None]:
    """รับ request body ของ /review/target แล้วคืน (target_info, error) — อย่างใดอย่างหนึ่งเป็น
    None เสมอ target_info = {"file_name": str, "headings": [...]}

    source == "upload": ต้องมี content_base64 (ไฟล์ดิบ, encode มาจากฝั่ง client)
    source == "corpus": หาไฟล์จาก DATA_DIRS ด้วย file_name ตรงๆ (อ่านแบบ direct parse เหมือนกัน
    ไม่ผ่าน retriever แม้จะเป็นเอกสารที่ index ไว้แล้วก็ตาม — ดู CONTEXT.md "Target document access")"""
    source = (body.get("source") or "").strip().lower()
    file_name = (body.get("file_name") or "").strip()
    if not file_name:
        return None, {"error": "missing_file_name", "message": "กรุณาระบุชื่อไฟล์เอกสารเป้าหมาย"}

    ext = os.path.splitext(file_name)[1].lower()
    if ext not in (".md", ".txt", ".docx"):
        return None, {
            "error": "unsupported_format",
            "message": (
                f"โหมดรีวิวเอกสารรองรับเฉพาะไฟล์ .md และ .docx เท่านั้น ('{ext or 'ไม่ทราบนามสกุล'}' "
                "ยังไม่รองรับ) กรุณาลองใช้โหมดร่างเอกสารแทน"
            ),
        }

    raw_bytes: bytes | None = None
    if source == "upload":
        content_b64 = body.get("content_base64")
        if not content_b64:
            return None, {"error": "missing_content", "message": "ไม่พบเนื้อหาไฟล์ที่อัปโหลด"}
        try:
            raw_bytes = base64.b64decode(content_b64)
        except Exception:
            return None, {"error": "invalid_content", "message": "ถอดรหัสเนื้อหาไฟล์ที่อัปโหลดไม่สำเร็จ"}
    elif source == "corpus":
        path = _get_document_path(file_name)
        if not path:
            return None, {
                "error": "not_found",
                "message": f"ไม่พบไฟล์ '{file_name}' ในเอกสารที่ index ไว้แล้ว",
            }
        try:
            with open(path, "rb") as f:
                raw_bytes = f.read()
        except OSError as e:
            return None, {"error": "read_failed", "message": f"อ่านไฟล์ไม่สำเร็จ: {e}"}
    else:
        return None, {
            "error": "invalid_source",
            "message": "source ต้องเป็น 'upload' หรือ 'corpus' เท่านั้น",
        }

    try:
        if ext == ".docx":
            headings = _parse_docx_headings(raw_bytes)
        else:
            headings = _parse_markdown_headings(raw_bytes.decode("utf-8", errors="replace"))
    except Exception as e:
        return None, {"error": "parse_failed", "message": f"อ่านโครงสร้างเอกสารไม่สำเร็จ: {e}"}

    if not headings:
        return None, {
            "error": "unparseable_headings",
            "message": (
                "ไม่พบโครงสร้างหัวข้อ (heading) ในเอกสารนี้เลย — โหมดรีวิวเอกสารต้องการเอกสารที่มี "
                "โครงสร้างหัวข้อชัดเจน (markdown '#'..'######' หรือ Word Heading styles) เอกสารที่เขียน "
                "เป็นพรืดเดียวไม่มีหัวข้อ หรือไฟล์สแกนภาพ ไม่รองรับในโหมดนี้ กรุณาลองใช้โหมดร่างเอกสารแทน "
                "(ดู ADR-006 ข้อ 2a — ระบบเลือกปฏิเสธชัดเจนแทนการ fallback เป็น checklist ล้วนๆ เงียบๆ "
                "เพราะจะรีวิวได้ไม่ครอบคลุมเนื้อหาจริง)"
            ),
        }

    return {"file_name": file_name, "headings": headings}, None


def _handle_clarify_questions(topic: str, instructions: str) -> dict:
    """สร้างคำถามเพิ่มเติมก่อนร่าง (ดู ADR-002) — ดึง context เดียวกับ /draft ก่อน แล้วให้ AI
    ถามเฉพาะจุดที่ context ไม่ครอบคลุม ใช้ GEMINI_MODEL_CHAT เพราะเป็นงานเบากว่าร่าง/scrutinize มาก
    (มี auto-fallback ไปโมเดลสำรองถ้าตั้งค่าไว้ — ดู ADR-003)"""
    query_str = f"{topic} {instructions}".strip()
    context_text, sources = _retrieve_context(query_str)

    sys_prompt = _build_clarify_questions_prompt() + context_text
    user_msg = (
        f"หัวข้อนโยบายที่ผู้ใช้ต้องการร่าง: {topic}\n"
        f"คำสั่ง/รายละเอียดเพิ่มเติมที่ผู้ใช้ให้มาแล้ว: {instructions.strip() or '(ไม่มี)'}"
    )

    log(f"[CLARIFY] เริ่มสร้างคำถามสำหรับหัวข้อ: {topic[:50]}...")
    text, error = _complete_with_fallback(
        GEMINI_MODEL_CHAT, GEMINI_MODEL_CHAT_FALLBACK, sys_prompt + "\n\n" + user_msg, "[CLARIFY]"
    )
    if text is None:
        return {"error": str(error) if error else "unknown error (clarify)"}

    questions = _parse_bullet_questions(text)
    log(f"[CLARIFY] ได้ {len(questions)} คำถาม")
    return {"questions": questions, "sources": sources}


def _inject_draft_into_session(session_id: str, topic: str, draft_text: str, scrutiny_text: str) -> None:
    """ฉีดร่าง+scrutiny ที่เพิ่งสร้างเข้า chat memory ของ session เดียวกัน เป็น synthetic
    user+assistant turn คู่หนึ่ง ติดป้ายกำกับชัดเจนว่าเป็นร่างที่ยังไม่อนุมัติ ทำให้ /chat ในเซสชัน
    เดียวกันหยิบมาคุยต่อได้ โดยไม่ต้องแก้กฎเหล็กของ _build_sys_prompt() (ดู ADR-004)
    ขอบเขตแค่เซสชันเดียวกันเท่านั้น — ไม่บันทึกลง index ถาวร ไม่ปนกับนโยบายจริงที่อนุมัติแล้ว"""
    from llama_index.core.memory import ChatMemoryBuffer
    from llama_index.core.llms import ChatMessage, MessageRole

    with _sessions_lock:
        if session_id not in _sessions:
            _sessions[session_id] = ChatMemoryBuffer.from_defaults(token_limit=8000)
        memory = _sessions[session_id]
        _session_last_used[session_id] = time.time()

    label = (
        f'[ร่างเอกสารที่ AI สร้างในโหมดร่างเอกสาร หัวข้อ "{topic}" '
        "— ยังไม่ผ่านการตรวจสอบ/อนุมัติ ไม่ใช่นโยบายที่ใช้งานจริง]"
    )
    synthetic_user_msg = f'(ระบบ) ผู้ใช้เพิ่งสร้างร่างนโยบายหัวข้อ "{topic}" ในโหมดร่างเอกสาร'
    synthetic_assistant_msg = (
        f"{label}\n\n{draft_text}\n\n--- ผลตรวจทาน (scrutiny) ---\n{scrutiny_text}"
    )

    memory.put(ChatMessage(role=MessageRole.USER, content=synthetic_user_msg))
    memory.put(ChatMessage(role=MessageRole.ASSISTANT, content=synthetic_assistant_msg))
    log(f"[DRAFT session={session_id[:8]}] ฉีดร่าง+scrutiny เข้า chat memory แล้ว (ดู ADR-004)")


def _handle_draft(
    topic: str, instructions: str, answers: dict | None = None, session_id: str | None = None
) -> dict:
    """โหมดร่างเอกสาร + auto-scrutinize ในคำขอเดียว (ดู ADR-001 ข้อ 2 — flow เดียว ไม่ใช่ 2 ปุ่ม)
    1) ดึง context จากนโยบายที่มีอยู่ 2) ร่างเอกสารใหม่ 3) วิจารณ์ร่างของตัวเองเทียบ context เดิม
    ใช้ GEMINI_MODEL_DRAFT (แพงกว่า/reasoning ดีกว่า GEMINI_MODEL_CHAT) ทั้งสองขั้นตอน
    (มี auto-fallback ไปโมเดลสำรองถ้าตั้งค่าไว้ — ดู ADR-003)

    answers: dict ของคำถาม -> คำตอบ ที่ได้จาก _handle_clarify_questions (ดู ADR-002) — คำถามที่
    ผู้ใช้ข้ามไม่ตอบ (ไม่มี key หรือคำตอบว่าง) จะถูกส่งต่อให้ draft prompt มาร์กเป็น [ต้องระบุ: ...]
    แทนการเดาคำตอบเอง

    session_id: ถ้าให้มา จะฉีดร่าง+scrutiny ที่สร้างเสร็จเข้า chat memory ของ session นั้น
    ทำให้ /chat ในเซสชันเดียวกันอ้างอิงร่างนี้ต่อได้ (ดู ADR-004)"""
    query_str = f"{topic} {instructions}".strip()
    context_text, sources = _retrieve_context(query_str)

    # รวมคำถาม-คำตอบเสริม (ถ้ามี) เข้ากับ user message — แยกที่ตอบแล้ว vs ที่ข้ามไม่ตอบ
    answers = answers or {}
    answered_lines = []
    unanswered = []
    for q, a in answers.items():
        a = (a or "").strip()
        if a:
            answered_lines.append(f"- ถาม: {q}\n  ตอบ: {a}")
        else:
            unanswered.append(q)

    extra_info = ""
    if answered_lines:
        extra_info += "\n\nข้อมูลเพิ่มเติมจากคำถามที่ระบบถามและผู้ใช้ตอบ:\n" + "\n".join(answered_lines)
    if unanswered:
        extra_info += (
            "\n\nคำถามที่ผู้ใช้ข้ามไม่ตอบ (ห้ามเดาคำตอบเอง ให้ใส่เครื่องหมาย [ต้องระบุ: <คำถาม>] "
            "ตรงจุดที่เกี่ยวข้องในร่างแทน):\n" + "\n".join(f"- {q}" for q in unanswered)
        )

    # ── ขั้นที่ 1: ร่างเอกสาร ──────────────────────────────────────────────
    draft_sys_prompt = _build_draft_sys_prompt() + context_text
    draft_user_msg = (
        f"หัวข้อนโยบายที่ต้องการร่าง: {topic}\n"
        f"คำสั่ง/รายละเอียดเพิ่มเติมจากผู้ใช้: {instructions.strip() or '(ไม่มี)'}"
        f"{extra_info}"
    )

    log(f"[DRAFT] เริ่มร่างหัวข้อ: {topic[:50]}...")
    draft_text, draft_error = _complete_with_fallback(
        GEMINI_MODEL_DRAFT, GEMINI_MODEL_DRAFT_FALLBACK,
        draft_sys_prompt + "\n\n" + draft_user_msg, "[DRAFT]",
    )
    if draft_text is None:
        return {"error": str(draft_error) if draft_error else "unknown error (draft)"}

    # ── ขั้นที่ 2: scrutinize ร่างที่เพิ่งสร้าง เทียบ context เดิม ──────────
    scrutinize_sys_prompt = _build_scrutinize_sys_prompt() + context_text
    scrutinize_user_msg = f"ร่างนโยบายที่ต้องตรวจ:\n\n{draft_text}"

    log("[SCRUTINIZE] เริ่มตรวจร่าง...")
    scrutiny_text, scrutiny_error = _complete_with_fallback(
        GEMINI_MODEL_DRAFT, GEMINI_MODEL_DRAFT_FALLBACK,
        scrutinize_sys_prompt + "\n\n" + scrutinize_user_msg, "[SCRUTINIZE]",
    )
    if scrutiny_text is None:
        return {"error": str(scrutiny_error) if scrutiny_error else "unknown error (scrutinize)"}

    # ── ฉีดร่าง+scrutiny เข้า chat memory ของ session (ถ้าให้ session_id มา) — ดู ADR-004 ──────
    if session_id:
        try:
            _inject_draft_into_session(session_id, topic, draft_text, scrutiny_text)
        except Exception as e:
            # การฉีดเข้า memory ล้มเหลวไม่ควรทำให้ร่างที่สร้างสำเร็จแล้วหายไป แค่ log ไว้
            log(f"[DRAFT session={session_id[:8]}] inject เข้า memory ล้มเหลว: {type(e).__name__} - {e}")

    return {
        "draft_markdown": draft_text,
        "scrutiny": scrutiny_text,
        "sources": sources,
    }


# ── Document Review Mode (ADR-006) — Review Topic generation + Prefill/Follow-up ───────────
# หัวข้อรีวิว (Review Topic) มาจาก 2 แหล่งผสมกัน (ดู ADR-006 ข้อ 4/CONTEXT.md):
#   1) heading-derived — heading จริงของเอกสารเป้าหมาย (จาก _extract_target_document) ไม่มี follow-up
#   2) checklist-derived — LLM สร้างเพิ่มจากสิ่งที่ heading เดิมไม่ครอบคลุม (ต่อยอด ADR-002) มี follow-up ได้
# กลไก Prefill/follow-up ด้านล่างนี้ใช้ร่วมกันกับ ADR-007 (/draft/questions/interactive) ด้วย เพราะ
# state model และกลไกถามทีละข้อ+ย้อนกลับได้+prefill เหมือนกันทุกประการ (ดู ADR-006 ผลที่ตามมา)

_CATEGORY_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")


def _parse_categorized_bullets(raw: str) -> list[dict]:
    """แปลง markdown ที่จัดกลุ่มด้วย '### หมวดหมู่' + bullet '- ข้อ' เป็น list ของ
    {"category": str, "heading": str} — ใช้ร่วมกันโดย checklist-derived Review Topic ของ ADR-006
    และคำถามเพิ่มเติมแบบจัดหมวดหมู่ของ ADR-007 ถ้าไม่มี '### ' เลยในผลลัพธ์ ทุก bullet จะอยู่หมวด
    'ทั่วไป' (fallback กันกรณี LLM ลืมใส่หมวดหมู่) บรรทัดที่ไม่ใช่ bullet/หัวข้อหมวด (เช่น
    '(ไม่มีหัวข้อเพิ่มเติม)') จะถูกข้ามเฉยๆ ทำให้ผลลัพธ์เป็น list ว่างอย่างถูกต้อง"""
    items: list[dict] = []
    current_category = "ทั่วไป"
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        cat_match = _CATEGORY_HEADING_RE.match(line)
        if cat_match:
            current_category = cat_match.group(1).strip()
            continue
        bullet_match = re.match(r"^[-*]\s+(.+)$", line) or re.match(r"^\d+[.)]\s+(.+)$", line)
        if bullet_match:
            items.append({"category": current_category, "heading": bullet_match.group(1).strip()})
    return items


def _build_heading_topics(headings: list[dict]) -> list[dict]:
    """แปลง heading ที่ parse ได้จาก Target document access เป็น Review Topic (แหล่งที่ 1 ตาม
    ADR-006 ข้อ 4) — allow_followup=False เสมอ เพราะเป้าหมายของหัวข้อประเภทนี้คือยืนยัน/แก้เนื้อหา
    ที่มีอยู่แล้วในเอกสารผ่าน Prefill ไม่ใช่ขุดข้อมูลใหม่เป็นชั้นๆ แบบ checklist-derived"""
    return [
        {
            "id": f"h{i}",
            "source": "heading",
            "level": h["level"],
            "heading": h["heading"],
            "body": h["body"],
            "category": None,
            "allow_followup": False,
        }
        for i, h in enumerate(headings)
    ]


def _build_review_checklist_prompt() -> str:
    """system prompt สำหรับสร้างหัวข้อรีวิวแบบ checklist-derived (แหล่งที่ 2 ของ Review Topic ตาม
    ADR-006 ข้อ 4) — ต่อยอดกลไกเดียวกับ _build_clarify_questions_prompt() ของ ADR-002 (AI ถามเฉพาะ
    จุดที่ context ไม่ครอบคลุม ไม่ใช่ checklist ตายตัว) แต่เพิ่มเงื่อนไข dedup กับ heading ของเอกสาร
    เป้าหมายด้วย ไม่ใช่แค่ dedup กับ context เดิมแบบ ADR-002 (ดู ADR-006 ข้อ 4)"""
    return (
        f"คุณคือผู้ช่วยเตรียมหัวข้อรีวิวเอกสารนโยบายให้กับ {COMPANY_NAME} "
        "หน้าที่ของคุณคือดูว่าเอกสารเป้าหมายที่จะรีวิว (ดูหัวข้อที่มีอยู่แล้วด้านล่าง) ยังขาดประเด็นสำคัญ "
        "อะไรบ้างที่เอกสารประเภทนี้ (นโยบาย/ระเบียบปฏิบัติ/คู่มือ/แบบฟอร์ม) ควรมี โดยดูจากนโยบาย/เอกสาร "
        "อื่นที่เกี่ยวข้องในองค์กร (Context ด้านล่าง) เป็นแนวทางประกอบ แล้วตั้งเป็นหัวข้อรีวิวเพิ่มเติม\n\n"
        "กติกา (สำคัญมาก):\n"
        "1. ห้ามตั้งหัวข้อที่ซ้ำหรือใกล้เคียงกับ 'หัวข้อที่เอกสารเป้าหมายมีอยู่แล้ว' ด้านล่างนี้เด็ดขาด "
        "— ต้องเป็นสิ่งที่เอกสารเป้าหมายยังไม่มีจริงๆ เท่านั้น\n"
        "2. ถ้า Context มีข้อมูลที่เกี่ยวข้องอยู่แล้วครบถ้วน ห้ามตั้งเป็นหัวข้อรีวิวซ้ำ (สืบทอดกฎเดียวกับ "
        "ขั้นตอนสร้างคำถามเพิ่มเติมก่อนร่างของ ADR-002)\n"
        "3. จัดกลุ่มหัวข้อเป็นหมวดหมู่ ขึ้นต้นแต่ละหมวดด้วย '### ชื่อหมวดหมู่' แล้วตามด้วยหัวข้อย่อย "
        "ขึ้นต้นด้วย '- ' บรรทัดละ 1 หัวข้อ ห้ามมีข้อความอื่นนอกเหนือจากนี้\n"
        "4. ถ้าเอกสารเป้าหมายครอบคลุมครบถ้วนแล้วจริงๆ ไม่ต้องเติมหัวข้อเพิ่ม ให้ตอบว่า "
        "'(ไม่มีหัวข้อเพิ่มเติม)' เฉยๆ โดยไม่ต้องมีหมวดหมู่\n\n"
    )


def _generate_checklist_topics(doc_display_name: str, headings: list[dict], context_text: str) -> list[dict]:
    """เรียก LLM (GEMINI_MODEL_DRAFT — ดู ADR-006 ข้อ 8) สร้าง checklist-derived Review Topic
    เพิ่มเติมจาก heading-derived เดิม แล้วแปลงผลลัพธ์เป็น topic dict พร้อม id 'c0','c1',...
    (มี auto-fallback ไปโมเดลสำรองถ้าตั้งค่าไว้ — ดู ADR-003) ถ้าขั้นตอนนี้ล้มเหลว ไม่บล็อกทั้ง request
    (heading-derived topics ยังใช้รีวิวได้ตามปกติ) แค่ log แล้วคืน list ว่าง"""
    existing_headings_text = "\n".join(f"- {h['heading']}" for h in headings) or "(ไม่มี)"
    sys_prompt = _build_review_checklist_prompt()
    user_msg = (
        f"เอกสารเป้าหมาย: {doc_display_name}\n\n"
        f"หัวข้อที่เอกสารเป้าหมายมีอยู่แล้ว:\n{existing_headings_text}\n\n"
        f"--- นโยบาย/เอกสารอื่นที่เกี่ยวข้องในองค์กร (Context) ---\n{context_text or '(ไม่มี context)'}"
    )
    log(f"[REVIEW-CHECKLIST] เริ่มสร้างหัวข้อรีวิวเพิ่มเติมสำหรับ: {doc_display_name[:50]}...")
    text, error = _complete_with_fallback(
        GEMINI_MODEL_DRAFT, GEMINI_MODEL_DRAFT_FALLBACK, sys_prompt + "\n\n" + user_msg,
        "[REVIEW-CHECKLIST]",
    )
    if text is None:
        log(f"[REVIEW-CHECKLIST] ล้มเหลว: {error} — ข้ามขั้นตอนนี้ ใช้แค่ heading-derived topics")
        return []

    parsed = _parse_categorized_bullets(text)
    topics = [
        {
            "id": f"c{i}",
            "source": "checklist",
            "level": None,
            "heading": item["heading"],
            "body": "",
            "category": item["category"],
            "allow_followup": True,
        }
        for i, item in enumerate(parsed)
    ]
    log(f"[REVIEW-CHECKLIST] ได้ {len(topics)} หัวข้อเพิ่มเติม")
    return topics


def _suggest_cross_reference_docs(
    query_str: str, exclude_file_name: str, limit: int = 5
) -> tuple[list[str], str]:
    """auto-suggest เอกสารที่เกี่ยวข้อง (Cross-reference documents — ดู ADR-006 ข้อ 3/CONTEXT.md)
    จาก whole-corpus retrieval ธรรมดา (ยังไม่ scope เพราะยังไม่มีรายชื่อที่ผู้ใช้ยืนยันแล้ว) คืน
    (รายชื่อไฟล์ที่ไม่ซ้ำ ไม่รวมเอกสารเป้าหมายเอง จำกัด limit ไฟล์, context_text ดิบสำหรับใช้สร้าง
    checklist-derived topic ต่อ — ไม่ต้อง retrieve ซ้ำสองรอบ)"""
    context_text, sources = _retrieve_context(query_str, top_n=15)
    suggested: list[str] = []
    for s in sources:
        fn = s.get("file_name", "")
        if fn and fn != exclude_file_name and fn not in suggested:
            suggested.append(fn)
        if len(suggested) >= limit:
            break
    return suggested, context_text


def _build_prefill_sys_prompt() -> str:
    """system prompt สำหรับสร้าง Prefill (คำตอบเสนอแนะ) ก่อนถามผู้ใช้แต่ละหัวข้อรีวิว/คำถามเพิ่มเติม
    (ดู ADR-006 ข้อ 5, ADR-007 ข้อ 3, กฎบังคับร่วมใน CONTEXT.md 'Prefill') ใช้ร่วมกันทั้งโหมดรีวิว
    เอกสารและโหมดร่างเอกสารแบบ interactive (ADR-007) เพราะกลไก prefill เหมือนกันทุกประการ — ห้าม
    restate กฎ provenance ที่ CONTEXT.md เป็นเจ้าของอยู่แล้วซ้ำที่อื่น (กันบั๊กซ้ำซ้อนที่เคยเกิดในเซสชันนี้)"""
    return (
        f"คุณคือผู้ช่วยเตรียมคำตอบเสนอแนะ (Prefill) ให้กับ {COMPANY_NAME} ก่อนถามผู้ใช้แต่ละหัวข้อ "
        "หน้าที่ของคุณคือดู Context ด้านล่าง (เอกสารที่เกี่ยวข้อง) แล้วเสนอคำตอบที่น่าจะเป็นสำหรับหัวข้อ "
        "ที่ระบุ ให้ผู้ใช้ยืนยันหรือแก้ไข แทนที่จะถามเปล่าทุกครั้ง\n\n"
        "กติกา (สำคัญมาก):\n"
        "1. ตอบเฉพาะเนื้อหาที่มีมูลจาก Context เท่านั้น ห้ามแต่งขึ้นเอง — ถ้า Context ไม่มีข้อมูลที่เกี่ยวข้อง "
        "เลย ให้ตอบว่า '(ไม่พบข้อมูลอ้างอิงที่เกี่ยวข้อง กรุณากรอกเอง)' เฉยๆ\n"
        "2. ทุกครั้งที่อ้างอิงเนื้อหาจากเอกสารใน Context ต้องระบุ 'วันที่บังคับใช้' และ 'วันที่แก้ไขล่าสุด' "
        "ของเอกสารต้นทางนั้นกำกับด้วยเสมอ (ข้อมูลนี้อยู่ใน Context อยู่แล้ว อ่านตรงได้เลย ไม่ต้องเดา) — "
        "ห้ามตัดสินเองว่าเนื้อหานั้น 'ยังใช้ได้จริงไหม' เป็นดุลยพินิจของผู้ใช้เสมอ แค่โชว์วันที่ให้ครบพอ\n"
        "3. ตอบสั้น กระชับ เป็นข้อความล้วน ไม่ต้องมีคำนำ/สรุปปิดท้าย ไม่ต้องใส่หัวข้อซ้ำคำถาม\n\n"
        "--- นโยบาย/เอกสารที่เกี่ยวข้อง (Context) ---\n"
    )


def _build_followup_sys_prompt() -> str:
    """system prompt สำหรับสร้าง follow-up question ตามคำตอบก่อนหน้า (ดู ADR-006 ข้อ 4 — เฉพาะ
    checklist-derived topic เท่านั้น, ADR-007 ข้อ 2) ใช้ร่วมกันทั้งสอง ADR"""
    return (
        f"คุณคือผู้ช่วยเตรียมข้อมูลก่อนร่าง/รีวิวนโยบายให้กับ {COMPANY_NAME} "
        "หน้าที่ของคุณคือดูคำถามและคำตอบที่ผู้ใช้เพิ่งตอบ แล้วพิจารณาว่าควรถามคำถามต่อยอด (follow-up) "
        "อีก 1 ข้อหรือไม่ เพื่อขุดรายละเอียดที่จำเป็นเพิ่ม (เช่น ตอบ 'ทบทวนปีละ 1 ครั้ง' ควรถามต่อว่า "
        "'ใครเป็นผู้รับผิดชอบทบทวน')\n\n"
        "กติกา:\n"
        "1. ถ้าคำตอบที่ได้ครบถ้วนเพียงพอแล้ว ไม่จำเป็นต้องถามต่อ ให้ตอบว่า 'ไม่มี' คำเดียวเท่านั้น\n"
        "2. ถ้าควรถามต่อ ให้ตอบเป็นคำถาม 1 ข้อเท่านั้น เป็นข้อความล้วน ไม่มีเครื่องหมาย bullet ไม่มีคำนำ\n"
        "3. ห้ามถามคำถามที่ Context ด้านล่างมีคำตอบอยู่แล้ว\n\n"
        "--- นโยบาย/เอกสารที่เกี่ยวข้อง (Context) ---\n"
    )


def _generate_topic_prefill(topic: dict, context_text: str) -> str:
    """สร้าง Prefill สำหรับหัวข้อเดียว — heading-derived topic (ADR-006) ใช้ topic['body'] (เนื้อหา
    เดิมของเอกสารเป้าหมายเอง) เป็นข้อมูลตั้งต้นหลัก เสริมด้วย cross-reference context ส่วน
    checklist-derived topic (ทั้ง ADR-006 และ ADR-007) ไม่มี body ของตัวเอง ใช้ context อย่างเดียว
    ใช้ GEMINI_MODEL_DRAFT (มี auto-fallback ถ้าตั้งค่าไว้ — ดู ADR-003)"""
    own_body = (topic.get("body") or "").strip()
    combined_context = context_text or ""
    if own_body:
        combined_context = f"[เนื้อหาเดิมของเอกสารเป้าหมายในหัวข้อนี้]\n{own_body}\n\n{combined_context}"
    if not combined_context.strip():
        return "(ไม่พบข้อมูลอ้างอิงที่เกี่ยวข้อง กรุณากรอกเอง)"

    sys_prompt = _build_prefill_sys_prompt() + combined_context
    user_msg = f"หัวข้อ: {topic.get('heading', '')}"
    text, error = _complete_with_fallback(
        GEMINI_MODEL_DRAFT, GEMINI_MODEL_DRAFT_FALLBACK, sys_prompt + "\n\n" + user_msg,
        f"[PREFILL {topic.get('id', '?')}]",
    )
    if text is None:
        log(f"[PREFILL {topic.get('id', '?')}] ล้มเหลว: {error}")
        return "(สร้างคำตอบเสนอแนะไม่สำเร็จ กรุณากรอกเอง)"
    return text.strip()


def _generate_topic_followup(topic: dict, answer: str, context_text: str) -> str | None:
    """สร้าง follow-up question ตามคำตอบที่ผู้ใช้เพิ่งตอบ — เฉพาะ topic ที่ allow_followup=True
    เท่านั้น (heading-derived topic ของ ADR-006 ไม่มี follow-up เด็ดขาด ดู ADR-006 ข้อ 4)
    คืน None ถ้าไม่จำเป็นต้องถามต่อ หรือถ้า LLM call ล้มเหลว (ไม่บล็อก flow หลักจากความล้มเหลวนี้)"""
    if not topic.get("allow_followup"):
        return None
    answer = (answer or "").strip()
    if not answer:
        return None

    sys_prompt = _build_followup_sys_prompt() + (context_text or "(ไม่มี context)")
    user_msg = f"คำถาม: {topic.get('heading', '')}\nคำตอบของผู้ใช้: {answer}"
    text, error = _complete_with_fallback(
        GEMINI_MODEL_DRAFT, GEMINI_MODEL_DRAFT_FALLBACK, sys_prompt + "\n\n" + user_msg,
        f"[FOLLOWUP {topic.get('id', '?')}]",
    )
    if text is None:
        log(f"[FOLLOWUP {topic.get('id', '?')}] ล้มเหลว: {error}")
        return None
    text = text.strip()
    if not text or text == "ไม่มี" or text.startswith("ไม่มี"):
        return None
    return text


def _handle_review_target(body: dict) -> dict:
    """POST /review/target — รับเอกสารเป้าหมาย คืน Review Topics (heading-derived + checklist-derived
    ผสมกัน) + รายการเอกสารที่เกี่ยวข้องที่ auto-suggest ไว้ ในคำตอบเดียวกัน (ดู ADR-006 ข้อ 9 — ไม่แยก
    endpoint) heading-derived มาก่อนเสมอ (สะท้อนโครงสร้างจริงของเอกสาร) checklist-derived ต่อท้าย"""
    target, error = _extract_target_document(body)
    if error:
        return error

    file_name = target["file_name"]
    headings = target["headings"]
    heading_topics = _build_heading_topics(headings)

    query_str = f"{file_name} " + " ".join(h["heading"] for h in headings[:15])
    suggested_docs, context_text = _suggest_cross_reference_docs(query_str, file_name)

    checklist_topics = _generate_checklist_topics(file_name, headings, context_text)

    log(f"[REVIEW-TARGET] {file_name}: {len(heading_topics)} heading-derived + "
        f"{len(checklist_topics)} checklist-derived topics, {len(suggested_docs)} เอกสารที่เกี่ยวข้องแนะนำ")

    return {
        "file_name": file_name,
        "review_topics": heading_topics + checklist_topics,
        "suggested_cross_reference_docs": suggested_docs,
    }


def _handle_review_topic(body: dict) -> dict:
    """POST /review/topic — ถามทีละหัวข้อรีวิว พร้อม Prefill (ดู ADR-006 ข้อ 5) stateless เต็มรูปแบบ:
    client ส่ง review_topics ทั้งก้อน + confirmed_cross_reference_docs + answers ที่ตอบไปแล้วมาทุกครั้ง
    (ดู ADR-006 ผลที่ตามมา — ไม่มี server-side session ใหม่ฝั่ง worker)

    ทำงาน 2 โหมดตาม requesting_followup_for_answer:
    - False (ดีฟอลต์): คืน prefill ของ topic_id ที่ระบุ ก่อนโชว์คำถามให้ผู้ใช้ตอบ
    - True: ผู้ใช้เพิ่งตอบ topic_id นี้ไป (ค่าอยู่ใน answers[topic_id]) เช็คว่าควรมี follow-up หรือไม่
      (เฉพาะ topic ที่ allow_followup=True เท่านั้น — ดู ADR-006 ข้อ 4)"""
    review_topics = body.get("review_topics") or []
    topic_id = body.get("topic_id")
    confirmed_docs = body.get("confirmed_cross_reference_docs") or []
    answers = body.get("answers") or {}
    requesting_followup = bool(body.get("requesting_followup_for_answer"))

    topic = next((t for t in review_topics if t.get("id") == topic_id), None)
    if topic is None:
        return {"error": "topic_not_found", "message": f"ไม่พบหัวข้อรีวิว id='{topic_id}'"}

    query_str = topic.get("heading", "")
    context_text, sources = _retrieve_context_scoped(query_str, confirmed_docs, top_n=10)

    if requesting_followup:
        answer = answers.get(topic_id, "")
        follow_up = _generate_topic_followup(topic, answer, context_text)
        return {"topic_id": topic_id, "follow_up_question": follow_up}

    prefill = _generate_topic_prefill(topic, context_text)
    return {"topic_id": topic_id, "prefill": prefill, "prefill_sources": sources}


# ── ADR-007: ขยายคำถามเพิ่มเติมก่อนร่างเอกสารแบบ interactive (ทีละข้อ + follow-up + prefill) ────
# reuse กลไก prefill/follow-up เดียวกับโหมดรีวิวเอกสารด้านบนทั้งหมด (ดู ADR-007 ข้อ 3 — ยืมแค่กลไก
# "ถามทีละข้อ + ย้อนกลับได้ + prefill" ไม่ใช่ยืมนิยามเต็มของ "หัวข้อรีวิว") หัวข้อของโหมดนี้เป็น
# checklist-derived ล้วนๆ เสมอ (ไม่มีเอกสารเป้าหมายให้ดึง heading จริงเหมือน ADR-006)

def _build_draft_categorized_questions_prompt() -> str:
    """system prompt สร้างคำถามเพิ่มเติมก่อนร่างแบบขยาย 2-5 เท่า + จัดหมวดหมู่ (ดู ADR-007 ข้อ 1)
    ต่อยอด _build_clarify_questions_prompt() เดิมของ ADR-002 แต่ไม่แก้ของเดิม (endpoint คนละตัว
    ตาม ADR-007 ข้อ 5) เพิ่มจำนวนและการจัดกลุ่มหมวดหมู่"""
    return (
        f"คุณคือผู้ช่วยเตรียมข้อมูลก่อนร่างนโยบายให้กับ {COMPANY_NAME} "
        "หน้าที่ของคุณคือตรวจสอบว่าการจะร่างนโยบายตามหัวข้อที่ผู้ใช้ระบุให้ครบถ้วนและถูกต้อง "
        "ต้องมีข้อมูลเฉพาะองค์กรอะไรบ้างที่ไม่มีอยู่ใน Context ด้านล่างนี้เลย แล้วตั้งคำถามถามผู้ใช้ "
        "เพื่อขอข้อมูลเหล่านั้น แทนที่จะปล่อยให้ขั้นตอนร่างต้องเดาเอาเอง\n\n"
        "กติกา (สำคัญมาก):\n"
        "1. ตั้งคำถามรวมประมาณ 10-25 ข้อ (มากกว่าเดิม 2-5 เท่า) เฉพาะเจาะจงกับหัวข้อนี้จริงๆ "
        "ห้ามใช้คำถามทั่วไปที่ใช้ได้กับทุกนโยบาย\n"
        "2. จัดกลุ่มคำถามเป็นหมวดหมู่ตามหัวข้อนโยบาย (เช่น ขอบเขต / บทบาทและความรับผิดชอบ / "
        "ตัวชี้วัด / การอนุมัติ — ปรับหมวดหมู่ให้เหมาะกับหัวข้อที่ผู้ใช้ระบุจริง ไม่ต้องใช้ชื่อหมวดตายตัว) "
        "ขึ้นต้นแต่ละหมวดด้วย '### ชื่อหมวดหมู่' แล้วตามด้วยคำถามย่อยขึ้นต้นด้วย '- ' บรรทัดละ 1 คำถาม\n"
        "3. ถ้า Context ด้านล่างมีข้อมูลที่เกี่ยวข้องอยู่แล้ว ห้ามถามซ้ำเรื่องนั้น\n"
        "4. ห้ามมีข้อความอื่นนอกเหนือจากรายการคำถามที่จัดหมวดหมู่แล้ว ห้ามมีคำนำ/สรุปปิดท้าย\n\n"
        "--- นโยบาย/เอกสารที่มีอยู่แล้วในองค์กร (context) ---\n"
    )


def _handle_draft_questions_interactive(body: dict) -> dict:
    """POST /draft/questions/interactive (ดู ADR-007 ข้อ 5) — ไม่แตะ /draft/questions เดิมเลย
    เรียกได้ 2 แบบตามว่ามี review_topics ส่งมาหรือไม่:

    1) ครั้งแรก (ไม่มี review_topics ในคำขอ หรือเป็น list ว่าง): สร้างคำถามแบบจัดหมวดหมู่ 10-25 ข้อ
       (ดู ADR-007 ข้อ 1) คืน review_topics ให้ client เก็บไว้ resend ต่อในคำขอถัดๆ ไป
    2) ครั้งถัดไป: ทำงานเหมือน _handle_review_topic() ทุกประการ (prefill/follow-up ทีละข้อ) แต่ scope
       การ retrieve เป็น whole-corpus เสมอ (ไม่มี target document / confirmed_cross_reference_docs
       เพราะโหมดร่างเอกสารไม่มีเอกสารเป้าหมายให้ scope ต่อ — ต่างจาก ADR-006)"""
    review_topics = body.get("review_topics") or []
    topic = (body.get("topic") or "").strip()
    instructions = body.get("instructions") or ""

    if not review_topics:
        if not topic:
            return {"error": "missing_topic", "message": "กรุณาระบุหัวข้อนโยบายที่ต้องการร่าง"}
        query_str = f"{topic} {instructions}".strip()
        context_text, sources = _retrieve_context(query_str)

        sys_prompt = _build_draft_categorized_questions_prompt() + context_text
        user_msg = (
            f"หัวข้อนโยบายที่ผู้ใช้ต้องการร่าง: {topic}\n"
            f"คำสั่ง/รายละเอียดเพิ่มเติมที่ผู้ใช้ให้มาแล้ว: {instructions.strip() or '(ไม่มี)'}"
        )
        log(f"[DRAFT-QUESTIONS-INTERACTIVE] เริ่มสร้างคำถามจัดหมวดหมู่สำหรับหัวข้อ: {topic[:50]}...")
        text, error = _complete_with_fallback(
            GEMINI_MODEL_DRAFT, GEMINI_MODEL_DRAFT_FALLBACK, sys_prompt + "\n\n" + user_msg,
            "[DRAFT-QUESTIONS-INTERACTIVE]",
        )
        if text is None:
            return {"error": str(error) if error else "unknown error (draft questions interactive)"}

        parsed = _parse_categorized_bullets(text)
        topics = [
            {
                "id": f"c{i}",
                "source": "checklist",
                "level": None,
                "heading": item["heading"],
                "body": "",
                "category": item["category"],
                "allow_followup": True,
            }
            for i, item in enumerate(parsed)
        ]
        log(f"[DRAFT-QUESTIONS-INTERACTIVE] ได้ {len(topics)} คำถามจัดหมวดหมู่")
        return {"review_topics": topics, "sources": sources}

    # ── ครั้งถัดไป: prefill/follow-up ทีละหัวข้อ เหมือน _handle_review_topic() แต่ไม่ scope
    # ต่อเอกสารเป้าหมาย (โหมดร่างเอกสารไม่มี target document — ดึงข้าม corpus ทั้งหมดเหมือน /draft เดิม)
    topic_id = body.get("topic_id")
    answers = body.get("answers") or {}
    requesting_followup = bool(body.get("requesting_followup_for_answer"))

    found_topic = next((t for t in review_topics if t.get("id") == topic_id), None)
    if found_topic is None:
        return {"error": "topic_not_found", "message": f"ไม่พบคำถาม id='{topic_id}'"}

    query_str = f"{topic} {found_topic.get('heading', '')}".strip()
    context_text, sources = _retrieve_context(query_str, top_n=10)

    if requesting_followup:
        answer = answers.get(topic_id, "")
        follow_up = _generate_topic_followup(found_topic, answer, context_text)
        return {"topic_id": topic_id, "follow_up_question": follow_up}

    prefill = _generate_topic_prefill(found_topic, context_text)
    return {"topic_id": topic_id, "prefill": prefill, "prefill_sources": sources}


# ── ADR-006 ข้อ 6: รายงานสรุปการเปลี่ยนแปลง + เอกสารฉบับปรับปรุงคู่กัน ─────────────────────────
# หมายเหตุ: endpoint นี้ไม่ได้อยู่ในตัวอย่าง endpoint ที่ ADR-006 ข้อ 9 ยกไว้ตรงๆ (ยกแค่ /review/target
# กับ /review/topic เป็น "ตัวอย่าง endpoint") แต่จำเป็นต่อการส่งมอบผลลัพธ์ตามที่ตัดสินใจไว้ในข้อ 6
# — endpoint ใหม่เพิ่มเติม ไม่แตะ /draft* เดิมเช่นเดียวกับ endpoint อื่นๆ ของโหมดนี้

_REVIEW_FINALIZE_SENTINEL = "===DOCUMENT==="


def _build_review_finalize_prompt() -> str:
    """system prompt สำหรับขั้นตอนสุดท้ายของโหมดรีวิวเอกสาร — รวมคำตอบทุกหัวข้อรีวิวที่ผู้ใช้ตอบ/แก้
    ไว้ (ทั้ง heading-derived และ checklist-derived) ให้เป็นผลลัพธ์ 2 ส่วนคู่กันตาม ADR-006 ข้อ 6
    ('รายงานสรุปการเปลี่ยนแปลงทีละหัวข้อ + เอกสารฉบับปรับปรุงคู่กัน')"""
    return (
        f"คุณคือผู้ช่วยสรุปผลรีวิวเอกสารนโยบายให้กับ {COMPANY_NAME} "
        "หน้าที่ของคุณคือรวมคำตอบที่ผู้ใช้ตอบไว้ทีละหัวข้อรีวิว (ทั้งหัวข้อที่มาจากเนื้อหาเดิมของเอกสาร "
        "และหัวข้อ checklist ที่เพิ่มเข้ามาใหม่) ให้เป็นผลลัพธ์ 2 ส่วน:\n\n"
        "ส่วนที่ 1 — รายงานสรุปการเปลี่ยนแปลง (Markdown): สรุปทีละหัวข้อว่ามีการแก้ไข/เพิ่มเติมอะไรบ้าง "
        "เทียบกับเนื้อหาเดิม ใช้ '### ' ขึ้นต้นแต่ละหัวข้อ ถ้าหัวข้อไหนผู้ใช้ไม่ได้ตอบ/ข้ามไป ให้ระบุว่า "
        "'ยังไม่มีการเปลี่ยนแปลง (ผู้ใช้ข้าม)'\n\n"
        f"ส่วนที่ 2 — เอกสารฉบับปรับปรุง (Markdown): ขึ้นบรรทัดแรกด้วยตัวคั่น '{_REVIEW_FINALIZE_SENTINEL}' "
        "เดี่ยวๆ แล้วตามด้วยเอกสารฉบับเต็มที่ปรับปรุงแล้ว ใช้โครงสร้างหัวข้อเดิมของเอกสารเป็นหลัก (คงหัวข้อ "
        "ที่ผู้ใช้ไม่ได้แก้ไว้เหมือนเดิม) แทรกเนื้อหาที่แก้/เพิ่มจากคำตอบของผู้ใช้ลงในตำแหน่งที่เหมาะสม "
        "และเพิ่มหัวข้อใหม่จากหัวข้อ checklist ที่ผู้ใช้ตอบไว้ต่อท้าย\n\n"
        "กติกา (สำคัญมาก):\n"
        "1. ขึ้นต้นเอกสารฉบับปรับปรุงด้วยบรรทัดกำกับชัดเจนว่า "
        "'⚠️ เอกสารนี้เป็นฉบับปรับปรุงที่ AI ช่วยรวบรวม ต้องผ่านการตรวจสอบและอนุมัติจากผู้มีอำนาจก่อนใช้จริง'\n"
        "2. หัวข้อ checklist ที่ผู้ใช้ข้ามไม่ตอบ (ไม่มีคำตอบ หรือคำตอบว่าง) ห้ามเดาเนื้อหาขึ้นมาเอง "
        "ให้ใส่เครื่องหมาย [ต้องระบุ: <ชื่อหัวข้อ>] แทน (ใช้ convention เดียวกับโหมดร่างเอกสาร — ดู ADR-002)\n"
        "3. ห้ามอ้างข้อเท็จจริง/ตัวเลขที่ไม่มีมูลจากคำตอบผู้ใช้หรือ context ที่ให้มา\n"
        "4. เขียนเป็นภาษาไทย รูปแบบ Markdown (# ## ### สำหรับหัวข้อ, - สำหรับ bullet)\n\n"
    )


def _split_review_finalize_output(text: str) -> tuple[str, str]:
    """แยกผลลัพธ์จาก _build_review_finalize_prompt() เป็น (change_report_markdown, updated_document_markdown)
    โดยหาบรรทัดตัวคั่น _REVIEW_FINALIZE_SENTINEL — ถ้าไม่เจอ (LLM ไม่ทำตามรูปแบบที่ขอ) คืนทั้งก้อนเป็น
    change_report แล้วปล่อย updated_document ว่างเปล่า ให้ผู้ใช้เห็นอย่างน้อยส่วนสรุปการเปลี่ยนแปลง"""
    if _REVIEW_FINALIZE_SENTINEL in text:
        before, _, after = text.partition(_REVIEW_FINALIZE_SENTINEL)
        return before.strip(), after.strip()
    return text.strip(), ""


def _handle_review_finalize(body: dict) -> dict:
    """POST /review/finalize — ขั้นตอนสุดท้ายของโหมดรีวิวเอกสาร รวมคำตอบทุกหัวข้อเป็นรายงานสรุปการ
    เปลี่ยนแปลง + เอกสารฉบับปรับปรุงคู่กัน (ดู ADR-006 ข้อ 6) เรียกครั้งเดียวหลังผู้ใช้ตอบ/ข้ามครบทุกหัวข้อ
    ใช้ GEMINI_MODEL_DRAFT เหมือนขั้นตอนอื่นๆ ของโหมดรีวิว (มี auto-fallback ถ้าตั้งค่าไว้ — ดู ADR-003)"""
    file_name = (body.get("file_name") or "").strip()
    review_topics = body.get("review_topics") or []
    answers = body.get("answers") or {}
    confirmed_docs = body.get("confirmed_cross_reference_docs") or []

    if not review_topics:
        return {"error": "missing_review_topics", "message": "ไม่มีหัวข้อรีวิวให้สรุปผล"}

    topic_lines = []
    for t in review_topics:
        tid = t.get("id")
        ans = (answers.get(tid) or "").strip()
        label = f"[{t.get('source')}] {t.get('heading')}"
        if t.get("body"):
            topic_lines.append(
                f"### {label}\nเนื้อหาเดิม: {t['body']}\nคำตอบ/แก้ไขจากผู้ใช้: {ans or '(ข้าม ไม่ตอบ)'}"
            )
        else:
            topic_lines.append(
                f"### {label} (หัวข้อใหม่จาก checklist)\nคำตอบจากผู้ใช้: {ans or '(ข้าม ไม่ตอบ)'}"
            )
    topics_text = "\n\n".join(topic_lines)

    query_str = f"{file_name} " + " ".join(t.get("heading", "") for t in review_topics[:15])
    context_text, sources = _retrieve_context_scoped(query_str, confirmed_docs, top_n=15)

    sys_prompt = _build_review_finalize_prompt() + (context_text or "(ไม่มี context)")
    user_msg = f"เอกสารเป้าหมาย: {file_name}\n\nคำตอบทุกหัวข้อรีวิว:\n\n{topics_text}"
    log(f"[REVIEW-FINALIZE] เริ่มสรุปผลรีวิวสำหรับ: {file_name[:50]}...")
    text, error = _complete_with_fallback(
        GEMINI_MODEL_DRAFT, GEMINI_MODEL_DRAFT_FALLBACK, sys_prompt + "\n\n" + user_msg,
        "[REVIEW-FINALIZE]",
    )
    if text is None:
        return {"error": str(error) if error else "unknown error (review finalize)"}

    change_report, updated_document = _split_review_finalize_output(text)
    return {
        "change_report_markdown": change_report,
        "updated_document_markdown": updated_document,
        "sources": sources,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - ปิด default stderr log ของ http.server
        pass

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            with _state_lock:
                self._send_json(200, dict(_status))
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/chat":
            self._handle_chat_request()
        elif self.path == "/draft/questions":
            self._handle_clarify_request()
        elif self.path == "/draft":
            self._handle_draft_request()
        elif self.path == "/review/target":
            self._handle_review_target_request()
        elif self.path == "/review/topic":
            self._handle_review_topic_request()
        elif self.path == "/draft/questions/interactive":
            self._handle_draft_questions_interactive_request()
        elif self.path == "/review/finalize":
            self._handle_review_finalize_request()
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_chat_request(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            session_id = body.get("session_id", "default")
            prompt = body.get("prompt", "")
            with _state_lock:
                ready = _status["status"] == "ready"
            if not ready:
                self._send_json(503, {"error": "worker ยังโหลดโมเดลไม่เสร็จ"})
                return
            result = _handle_chat(session_id, prompt)
            self._send_json(200, result)
        except Exception as e:
            log(f"do_POST /chat error: {type(e).__name__} - {e}\n{traceback.format_exc()}")
            self._send_json(500, {"error": str(e)})

    def _handle_clarify_request(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            topic = body.get("topic", "")
            instructions = body.get("instructions", "")
            with _state_lock:
                ready = _status["status"] == "ready"
            if not ready:
                self._send_json(503, {"error": "worker ยังโหลดโมเดลไม่เสร็จ"})
                return
            if not topic.strip():
                self._send_json(400, {"error": "กรุณาระบุหัวข้อนโยบายที่ต้องการร่าง"})
                return
            result = _handle_clarify_questions(topic, instructions)
            self._send_json(200, result)
        except Exception as e:
            log(f"do_POST /draft/questions error: {type(e).__name__} - {e}\n{traceback.format_exc()}")
            self._send_json(500, {"error": str(e)})

    def _handle_draft_request(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            topic = body.get("topic", "")
            instructions = body.get("instructions", "")
            answers = body.get("answers") or {}
            session_id = body.get("session_id") or None
            with _state_lock:
                ready = _status["status"] == "ready"
            if not ready:
                self._send_json(503, {"error": "worker ยังโหลดโมเดลไม่เสร็จ"})
                return
            if not topic.strip():
                self._send_json(400, {"error": "กรุณาระบุหัวข้อนโยบายที่ต้องการร่าง"})
                return
            result = _handle_draft(topic, instructions, answers, session_id)
            self._send_json(200, result)
        except Exception as e:
            log(f"do_POST /draft error: {type(e).__name__} - {e}\n{traceback.format_exc()}")
            self._send_json(500, {"error": str(e)})

    def _handle_review_target_request(self) -> None:
        """POST /review/target — ดู ADR-006 ข้อ 9 (endpoint ใหม่ทั้งหมด ไม่แตะ /draft* เดิม)"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            with _state_lock:
                ready = _status["status"] == "ready"
            if not ready:
                self._send_json(503, {"error": "worker ยังโหลดโมเดลไม่เสร็จ"})
                return
            result = _handle_review_target(body)
            code = 400 if "error" in result else 200
            self._send_json(code, result)
        except Exception as e:
   