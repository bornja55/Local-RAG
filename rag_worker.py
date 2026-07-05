"""
rag_worker.py — โปรเซสแยกต่างหากที่โหลด torch/faiss/embedding/reranker/LLM ทั้งหมด
รันเป็น local HTTP server อยู่เบื้องหลัง แยกขาดจากโปรเซสของ Streamlit โดยสิ้นเชิง

เหตุผลที่ต้องแยก: พิสูจน์แล้วว่า Streamlit เปล่าๆ (ไม่มี RAG) ไม่ crash เลย แต่พอโหลด
torch+faiss+transformers models หนักๆ ไว้ในโปรเซสเดียวกับ Streamlit แล้วจะ crash แบบ
Windows access violation ที่ WINHTTP.dll (offset เดิมซ้ำทุกครั้ง) หลัง idle ไปสักพัก —
สาเหตุน่าจะเป็น native library conflict (OpenMP duplicate runtime ระหว่าง faiss-cpu กับ
PyTorch) ที่ทำให้ heap เพี้ยนแบบ delayed แล้วไปโผล่ตอน Streamlit เรียก WinHTTP ภายหลัง
การย้าย native libraries ทั้งหมดออกจากโปรเซสของ Streamlit จึงตัดปัญหานี้ได้ตรงจุด

โครงสร้างไฟล์ (แยกตาม Architecture report 2026-07-03 — pure move ไม่เปลี่ยนพฤติกรรม):
    rag_worker.py       ← entrypoint: HTTP layer (class Handler), _load_everything, main()
    worker_config.py    ← config/env loading + ค่าคงที่ทั้งหมด
    worker_state.py     ← global state + locks + log()
    worker_prompts.py   ← prompt builders ทั้ง 9 ฟังก์ชัน
    worker_parsing.py   ← parsers (bullet/heading/docx/categorized/finalize-split)
    worker_retrieval.py ← retrieval จาก FAISS index + reranker
    worker_handlers.py  ← business logic ของทุก endpoint
    llm_fallback.py     ← retry + fallback ไปโมเดลสำรอง (pure, unit-testable — ดู ADR-003)

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
import sys
import json
import time
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# worker_config มี side effect ตอน import (โหลด .env + ตั้ง OMP/HF env vars) — ต้องมาก่อน
# faiss/torch เสมอ เหมือนลำดับเดิมใน rag_worker.py ก่อนแยกไฟล์ทุกประการ
import worker_config as config

# เช็ค API key ที่ entrypoint (ตำแหน่งเดิมของพฤติกรรมนี้ก่อนแยกไฟล์) — ตั้งใจไม่เช็คใน
# worker_config เพื่อให้โมดูลอื่น/unit test import config ได้โดยไม่ต้องมี key
if "GOOGLE_API_KEY" not in os.environ:
    raise RuntimeError(
        "ไม่พบ GOOGLE_API_KEY — สร้างไฟล์ .env ที่ root โปรเจกต์ (ดู .env.example) "
        "แล้วใส่ GOOGLE_API_KEY=<คีย์จริงของคุณ>"
    )

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# import faiss ก่อน torch เสมอ กันปัญหา OpenMP DLL โหลดผิดลำดับ
import faiss  # noqa: E402,F401

import worker_state as state  # noqa: E402
import worker_handlers as handlers  # noqa: E402
from worker_prompts import _build_sys_prompt  # noqa: E402

log = state.log


def _load_everything() -> None:
    """โหลดโมเดล/index ทั้งหมด (บล็อกประมาณ 4 นาทีรอบแรก) รันใน background thread"""
    try:
        log("เริ่มโหลด embedding model (BGE-M3)...")
        import torch
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        from llama_index.core import Settings

        embed_model = HuggingFaceEmbedding(
            model_name=config.BGE_M3_PATH,
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
            model_name: str = Field(default=config.RERANKER_PATH)
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

        if not os.path.exists(config.STORAGE_DIR):
            documents = []
            for d in config.DATA_DIRS:
                if os.path.exists(d):
                    documents.extend(SimpleDirectoryReader(d).load_data())
            if not documents:
                raise RuntimeError("ไม่พบเอกสารในโฟลเดอร์ Policies/Procedures/Manuals/Forms")
            index = VectorStoreIndex.from_documents(documents)
            index.storage_context.persist(persist_dir=config.STORAGE_DIR)
        else:
            vector_store = FaissVectorStore.from_persist_dir(config.STORAGE_DIR)
            storage_context = StorageContext.from_defaults(
                vector_store=vector_store, persist_dir=config.STORAGE_DIR
            )
            index = load_index_from_storage(storage_context)
        log("FAISS index พร้อมแล้ว")

        with state._state_lock:
            state._index = index
            state._reranker = reranker
            state._sys_prompt = _build_sys_prompt()
            state._status["status"] = "ready"
            state._status["detail"] = "พร้อมใช้งาน"
        log("Worker พร้อมรับคำถามแล้ว")

    except Exception as e:
        log(f"โหลดล้มเหลว: {type(e).__name__} - {e}\n{traceback.format_exc()}")
        with state._state_lock:
            state._status["status"] = "error"
            state._status["detail"] = f"{type(e).__name__}: {e}"


def _cleanup_idle_sessions() -> None:
    """ลบ session ที่ไม่ได้ใช้งานเกิน SESSION_IDLE_TIMEOUT_SECONDS ออกจาก SessionStore
    รันเป็น background thread แยกต่างหาก ตื่นทุก 10 นาที ป้องกัน memory ของ worker โตไม่มีเพดาน
    ถ้าปล่อยรันต่อเนื่องนานๆ โดยไม่ restart (ดู ADR-005 — logic การ expire อยู่ใน
    SessionStore.cleanup_idle() ที่ lock ให้เองแล้ว ดู Architecture report Low #2)"""
    while True:
        time.sleep(600)
        expired = state.sessions.cleanup_idle(config.SESSION_IDLE_TIMEOUT_SECONDS)
        if expired:
            log(f"[CLEANUP] ลบ {len(expired)} session ที่ idle เกิน {config.SESSION_IDLE_TIMEOUT_SECONDS}s")


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

    def _require_ready(self) -> bool:
        """เช็คว่า worker โหลดโมเดลเสร็จหรือยัง — ถ้ายัง ส่ง 503 ให้เองแล้วคืน False
        (ผู้เรียกแค่ `if not self._require_ready(): return` ต้นทุก endpoint)
        รวม boilerplate ที่เคยซ้ำกัน 7 จุดเหลือจุดเดียว (ดู Architecture report Medium #1) —
        endpoint ใหม่ในอนาคตเรียก method นี้แทนการ copy บล็อก lock+check เอง จะได้ไม่มีโอกาสลืม"""
        with state._state_lock:
            ready = state._status["status"] == "ready"
        if not ready:
            self._send_json(503, {"error": "worker ยังโหลดโมเดลไม่เสร็จ"})
        return ready

    def do_GET(self):
        if self.path == "/health":
            with state._state_lock:
                self._send_json(200, dict(state._status))
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
            if not self._require_ready():
                return
            result = handlers._handle_chat(session_id, prompt)
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
            if not self._require_ready():
                return
            if not topic.strip():
                self._send_json(400, {"error": "กรุณาระบุหัวข้อนโยบายที่ต้องการร่าง"})
                return
            result = handlers._handle_clarify_questions(topic, instructions)
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
            if not self._require_ready():
                return
            if not topic.strip():
                self._send_json(400, {"error": "กรุณาระบุหัวข้อนโยบายที่ต้องการร่าง"})
                return
            result = handlers._handle_draft(topic, instructions, answers, session_id)
            self._send_json(200, result)
        except Exception as e:
            log(f"do_POST /draft error: {type(e).__name__} - {e}\n{traceback.format_exc()}")
            self._send_json(500, {"error": str(e)})

    def _handle_review_target_request(self) -> None:
        """POST /review/target — ดู ADR-006 ข้อ 9 (endpoint ใหม่ทั้งหมด ไม่แตะ /draft* เดิม)"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not self._require_ready():
                return
            result = handlers._handle_review_target(body)
            code = 400 if "error" in result else 200
            self._send_json(code, result)
        except Exception as e:
            log(f"do_POST /review/target error: {type(e).__name__} - {e}\n{traceback.format_exc()}")
            self._send_json(500, {"error": str(e)})

    def _handle_review_topic_request(self) -> None:
        """POST /review/topic — ดู ADR-006 ข้อ 9 (stateless, client resend review_topics ทั้งก้อนทุกครั้ง)"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not self._require_ready():
                return
            if not body.get("topic_id"):
                self._send_json(400, {"error": "missing_topic_id", "message": "กรุณาระบุ topic_id"})
                return
            result = handlers._handle_review_topic(body)
            code = 400 if "error" in result else 200
            self._send_json(code, result)
        except Exception as e:
            log(f"do_POST /review/topic error: {type(e).__name__} - {e}\n{traceback.format_exc()}")
            self._send_json(500, {"error": str(e)})

    def _handle_draft_questions_interactive_request(self) -> None:
        """POST /draft/questions/interactive — ดู ADR-007 ข้อ 5 (endpoint ใหม่ ไม่แก้ /draft/questions เดิม)"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not self._require_ready():
                return
            result = handlers._handle_draft_questions_interactive(body)
            code = 400 if "error" in result else 200
            self._send_json(code, result)
        except Exception as e:
            log(f"do_POST /draft/questions/interactive error: {type(e).__name__} - {e}\n"
                f"{traceback.format_exc()}")
            self._send_json(500, {"error": str(e)})

    def _handle_review_finalize_request(self) -> None:
        """POST /review/finalize — ดู ADR-006 ข้อ 6 (endpoint เพิ่มเติมนอกเหนือตัวอย่างในข้อ 9)"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not self._require_ready():
                return
            result = handlers._handle_review_finalize(body)
            code = 400 if "error" in result else 200
            self._send_json(code, result)
        except Exception as e:
            log(f"do_POST /review/finalize error: {type(e).__name__} - {e}\n{traceback.format_exc()}")
            self._send_json(500, {"error": str(e)})


def main():
    log("=" * 50)
    log("RAG worker กำลังเริ่มทำงาน...")

    try:
        server = ThreadingHTTPServer(("127.0.0.1", config.PORT), Handler)
    except OSError as e:
        log(f"BIND ล้มเหลว: ไม่สามารถเปิด port {config.PORT} ได้ ({type(e).__name__}: {e}) "
            f"— อาจมีโปรแกรมอื่นใช้ port นี้อยู่แล้ว หรือมี rag_worker.py instance เก่าค้างอยู่ "
            f"ลองรัน stop_worker.bat แล้วเช็ค netstat -ano | findstr :{config.PORT}")
        with state._state_lock:
            state._status["status"] = "error"
            state._status["detail"] = f"Bind port {config.PORT} ล้มเหลว: {e}"
        return

    loader_thread = threading.Thread(target=_load_everything, daemon=True)
    loader_thread.start()

    cleanup_thread = threading.Thread(target=_cleanup_idle_sessions, daemon=True)
    cleanup_thread.start()

    log(f"HTTP server ฟังอยู่ที่ 127.0.0.1:{config.PORT} (โหลดโมเดลต่อใน background, "
        f"cleanup session ทุกไม่เกิน {config.SESSION_IDLE_TIMEOUT_SECONDS}s idle)")
    try:
        server.serve_forever()
    except Exception as e:
        log(f"serve_forever() ล้มเหลว: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        with state._state_lock:
            state._status["status"] = "error"
            state._status["detail"] = f"Server ล้ม: {e}"


if __name__ == "__main__":
    main()
