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

โมเดลสำรอง (fallback):
    ถ้าตั้ง GEMINI_MODEL_CHAT_FALLBACK / GEMINI_MODEL_DRAFT_FALLBACK ไว้ใน .env ระบบจะสลับไปใช้
    โมเดลสำรองโดยอัตโนมัติ เฉพาะกรณี retry โมเดลหลักครบ 3 ครั้งแล้วยังชนโควตาอยู่ (ดู ADR-003)
    ไม่ได้ retry โมเดลสำรองซ้ำ — ถ้าโมเดลสำรอง error ด้วยก็คืน error ตามปกติ

Session cleanup:
    session ที่ไม่ได้ใช้งานเกิน SESSION_IDLE_TIMEOUT_SECONDS (ดีฟอลต์ 8 ชม.) จะถูกลบออกจาก memory
    อัตโนมัติทุก 10 นาที ป้องกัน worker กินแรมโตไม่มีเพดานถ้าปล่อยรันต่อเนื่องนานๆ (ดู ADR-005)

รันแบบ standalone:
    venv\\Scripts\\python.exe rag_worker.py
(ปกติแล้ว app.py จะ auto-start ให้เองถ้ายังไม่ได้รันอยู่ ไม่ต้องรันมือ)
"""
import os
import re
import sys
import json
import time
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

# โมเดลสำรองเมื่อโมเดลหลักชนโควตาต่อเนื่อง (ดู ADR-003) — ค่าเริ่มต้นว่างเปล่า = ไม่มี fallback,
# พฤติกรรมเหมือนเดิมทุกประการ ตั้งค่าผ่าน .env เท่านั้น (เช่น GEMINI_MODEL_CHAT_FALLBACK=gemma-4-26b-a4b-it)
GEMINI_MODEL_CHAT_FALLBACK  = os.environ.get("GEMINI_MODEL_CHAT_FALLBACK", "").strip()
GEMINI_MODEL_DRAFT_FALLBACK = os.environ.get("GEMINI_MODEL_DRAFT_FALLBACK", "").strip()

# หมดอายุ session ที่ไม่ได้ใช้งานนาน (ดู ADR-005) — ป้องกัน _sessions dict โตไม่มีเพดานถ้า worker
# รันต่อเนื่องนานๆ โดยไม่ restart ค่าเริ่มต้น 8 ชั่วโมง ปรับได้ผ่าน .env
SESSION_IDLE_TIMEOUT_SECONDS = int(os.environ.get("SESSION_IDLE_TIMEOUT_SECONDS", str(8 * 60 * 60)))

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


def _complete_with_fallback(
    primary_model: str, fallback_model: str, prompt: str, log_prefix: str
) -> tuple[str | None, Exception | None]:
    """เรียก llm.complete(prompt) พร้อม retry เดิม (3 ครั้ง + backoff) บนโมเดลหลัก ใช้ร่วมกันโดย
    _handle_clarify_questions และ _handle_draft (ทุกจุดที่เรียก llm.complete() แบบ one-shot)
    ถ้า retry ครบ 3 ครั้งแล้วยังเป็น quota error อยู่ และมีการตั้งค่าโมเดลสำรองไว้ (ไม่ใช่ค่าว่าง)
    จะลองโมเดลสำรองอีก 1 ครั้ง (ไม่ retry ซ้ำ) — ดู ADR-003
    คืนค่า (text, error) โดย text เป็น None ถ้าทั้งสองโมเดลล้มเหลว"""
    from llama_index.llms.google_genai import GoogleGenAI

    last_error = None
    for attempt in range(3):
        try:
            t0 = time.time()
            resp = GoogleGenAI(model=primary_model).complete(prompt)
            log(f"{log_prefix} สำเร็จใน {time.time() - t0:.2f}s (โมเดล: {primary_model})")
            return resp.text, None
        except Exception as e:
            last_error = e
            log(f"{log_prefix} error (โมเดล {primary_model}): {type(e).__name__} - {e}")
            if _is_quota_error(e) and attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            break

    if fallback_model and _is_quota_error(last_error):
        try:
            log(f"{log_prefix} โมเดลหลัก {primary_model} ชนโควตาครบ retry แล้ว "
                f"กำลังลองโมเดลสำรอง {fallback_model}...")
            t0 = time.time()
            resp = GoogleGenAI(model=fallback_model).complete(prompt)
            log(f"{log_prefix} สำเร็จใน {time.time() - t0:.2f}s (โมเดลสำรอง: {fallback_model})")
            return resp.text, None
        except Exception as e:
            last_error = e
            log(f"{log_prefix} error (โมเดลสำรอง {fallback_model}): {type(e).__name__} - {e}")

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
    from llama_index.llms.google_genai import GoogleGenAI
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
        llm = GoogleGenAI(model=model)
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

    # ── fallback ไปโมเดลสำรอง ถ้าตั้งค่าไว้ และ retry โมเดลหลักครบแล้วยังชนโควตาอยู่ (ดู ADR-003) ──
    if response_obj is None and GEMINI_MODEL_CHAT_FALLBACK and _is_quota_error(last_error):
        try:
            log(f"[CHAT session={session_id[:8]}] โมเดลหลัก {GEMINI_MODEL_CHAT} ชนโควตาครบ retry แล้ว "
                f"กำลังลองโมเดลสำรอง {GEMINI_MODEL_CHAT_FALLBACK}...")
            fallback_engine = _build_chat_engine(GEMINI_MODEL_CHAT_FALLBACK)
            t0 = time.time()
            response_obj = fallback_engine.chat(prompt)
            log(f"[CHAT session={session_id[:8]}] สำเร็จใน {time.time() - t0:.2f}s (โมเดลสำรอง)")
        except Exception as e:
            last_error = e
            log(f"[CHAT session={session_id[:8]}] error (โมเดลสำรอง {GEMINI_MODEL_CHAT_FALLBACK}): "
                f"{type(e).__name__} - {e}")

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


def _retrieve_context(query_str: str, top_n: int = 15) -> tuple[str, list[dict]]:
    """ดึง context ที่เกี่ยวข้องจาก index+reranker เดียวกับ Q&A ใช้ทั้งขั้นตอนร่างและ scrutinize
    (ดู ADR-001 ข้อ 6 — ไม่สร้าง index แยกสำหรับโหมดร่างเอกสาร)
    คืนค่า (context_text รวมเป็น string เดียว, รายการ sources แบบเดียวกับที่ /chat คืนให้)"""
    from llama_index.core.schema import QueryBundle

    retriever = _index.as_retriever(similarity_top_k=60)
    nodes = retriever.retrieve(query_str)
    nodes = _reranker._postprocess_nodes(nodes, QueryBundle(query_str=query_str))[:top_n]

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


def main():
    log("=" * 50)
    log("RAG worker กำลังเริ่มทำงาน...")

    # ห่อการ bind + serve ด้วย try/except เสมอ เพราะโปรเซสนี้รันแบบ detached
    # (stdout/stderr ไปที่ DEVNULL) — ถ้าไม่ log ไว้ก่อนตาย exception จะหายไปเงียบๆ
    # โดยไม่มีร่องรอยอะไรเลยใน rag_worker.log ทำให้ debug ไม่ได้เลย
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        log(f"BIND ล้มเหลว: ไม่สามารถเปิด port {PORT} ได้ ({type(e).__name__}: {e}) "
            f"— อาจมีโปรแกรมอื่นใช้ port นี้อยู่แล้ว หรือมี rag_worker.py instance เก่าค้างอยู่ "
            f"ลองรัน stop_worker.bat แล้วเช็ค netstat -ano | findstr :{PORT}")
        with _state_lock:
            _status["status"] = "error"
            _status["detail"] = f"Bind port {PORT} ล้มเหลว: {e}"
        return

    loader_thread = threading.Thread(target=_load_everything, daemon=True)
    loader_thread.start()

    cleanup_thread = threading.Thread(target=_cleanup_idle_sessions, daemon=True)
    cleanup_thread.start()

    log(f"HTTP server ฟังอยู่ที่ 127.0.0.1:{PORT} (โหลดโมเดลต่อใน background, "
        f"cleanup session ทุกไม่เกิน {SESSION_IDLE_TIMEOUT_SECONDS}s idle)")
    try:
        server.serve_forever()
    except Exception as e:
        log(f"serve_forever() ล้มเหลว: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        with _state_lock:
            _status["status"] = "error"
            _status["detail"] = f"Server ล้ม: {e}"


if __name__ == "__main__":
    main()
