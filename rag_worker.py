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
        "คุณคือผู้ช่วยตอบคำถามเรื่องนโยบายและระเบียบปฏิบัติของบริษัท ทเวนตี้ โฟร์ คอน แอนด์ ซัพพลาย จำกัด (มหาชน) (24CS) "
        "กฎเหล็ก: หน้าที่ของคุณคือการหาคำตอบจากบริบท (Context) ที่ให้มาเท่านั้น ห้ามคิดคำตอบขึ้นมาเองโดยเด็ดขาด "
        "หากข้อมูลในบริบทไม่มีคำตอบสำหรับคำถามนั้น ให้คุณตอบไปตรงๆ ว่า 'ขออภัยครับ ไม่พบข้อมูลเรื่องนี้ในนโยบายของบริษัท' "
        "แต่หากพบข้อมูล ให้คุณสรุปคำตอบอย่างละเอียดและเป็นมืออาชีพ พร้อมอ้างอิงรหัสเอกสารและหัวข้อ (เช่น 24CS-PL-005 ข้อ 3.1) เสมอ\n\n"
        "--- รายชื่อเอกสารทั้งหมดที่มีอยู่ในฐานข้อมูล RAG ณ ตอนนี้ ---\n- "
        f"{_get_available_documents()}\n"
        "--------------------------------------------------\n"
        "หมายเหตุ: หากผู้ใช้ถามว่า 'มีเอกสารอะไรบ้าง' หรือ 'มีเอกสาร X ไหม' ให้คุณตรวจสอบจาก [รายชื่อเอกสารทั้งหมดที่มีอยู่ในฐานข้อมูล RAG] ด้านบนนี้ได้เลย และอธิบายให้ผู้ใช้ฟัง"
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
    s = str(e)
    return "429" in s or "RESOURCE_EXHAUSTED" in s or "quota" in s.lower()


def _handle_chat(session_id: str, prompt: str) -> dict:
    from llama_index.llms.google_genai import GoogleGenAI
    from llama_index.core.memory import ChatMemoryBuffer
    from llama_index.core import Settings

    with _sessions_lock:
        if session_id not in _sessions:
            _sessions[session_id] = ChatMemoryBuffer.from_defaults(token_limit=8000)
        memory = _sessions[session_id]

    # สร้าง llm + chat_engine ใหม่ทุกครั้งที่เรียก (ยืนยันแล้วจาก diagnostic tests ว่า
    # ปลอดภัยข้าม thread — ไม่มี state ค้างจาก request ก่อนหน้า)
    llm = GoogleGenAI(model="gemini-3.1-flash-lite")
    Settings.llm = llm

    chat_engine = _index.as_chat_engine(
        chat_mode="condense_plus_context",
        memory=memory,
        similarity_top_k=60,
        node_postprocessors=[_reranker],
        system_prompt=_sys_prompt,
    )

    last_error = None
    for attempt in range(3):
        try:
            log(f"[CHAT session={session_id[:8]}] ถาม: {prompt[:50]}...")
            t0 = time.time()
            response_obj = chat_engine.chat(prompt)
            log(f"[CHAT session={session_id[:8]}] สำเร็จใน {time.time() - t0:.2f}s")
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
        except Exception as e:
            last_error = e
            log(f"[CHAT session={session_id[:8]}] error: {type(e).__name__} - {e}")
            if _is_quota_error(e) and attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            break

    return {"error": str(last_error) if last_error else "unknown error"}


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
        if self.path != "/chat":
            self._send_json(404, {"error": "not found"})
            return
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
            log(f"do_POST error: {type(e).__name__} - {e}\n{traceback.format_exc()}")
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

    log(f"HTTP server ฟังอยู่ที่ 127.0.0.1:{PORT} (โหลดโมเดลต่อใน background)")
    try:
        server.serve_forever()
    except Exception as e:
        log(f"serve_forever() ล้มเหลว: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        with _state_lock:
            _status["status"] = "error"
            _status["detail"] = f"Server ล้ม: {e}"


if __name__ == "__main__":
    main()
