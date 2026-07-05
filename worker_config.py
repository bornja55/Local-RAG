"""
worker_config.py — config/env loading ของ RAG worker (แยกออกมาจาก rag_worker.py ตาม
Architecture report High #1) — โหลด .env, ตั้ง env vars ที่จำเป็น, และประกาศค่าคงที่ทั้งหมด

โมดูลนี้ import ได้โดย **ไม่ต้องมี GOOGLE_API_KEY** ตั้งใจ — การเช็ค API key อยู่ที่
rag_worker.py (entrypoint) แทน เพื่อให้โมดูลอื่น (worker_prompts, worker_parsing ฯลฯ)
และ unit test import config ได้โดยไม่ล้ม พฤติกรรมตอน start worker จริงคงเดิม:
ไม่มี key = worker ไม่ start (raise ที่ rag_worker.py ก่อน bind port เหมือนเดิม)

หมายเหตุ: โมดูลนี้มี side effect ตอน import (โหลด .env + ตั้ง env vars) เหมือนตำแหน่งเดิม
ใน rag_worker.py ทุกประการ — ต้องถูก import ก่อน faiss/torch เสมอ (rag_worker.py ทำให้อยู่แล้ว)
"""
import os

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
