"""
app.py — Streamlit UI แบบบางๆ (thin client)

*** ไม่มี torch/faiss/llama_index/transformers import อยู่ในไฟล์นี้เลย ***

โครงสร้างใหม่: RAG/LLM ทั้งหมด (embedding, reranker, FAISS, Gemini) ถูกย้ายไปรันใน
rag_worker.py ซึ่งเป็นโปรเซสแยกต่างหาก คุยกันผ่าน local HTTP (127.0.0.1:8765) เท่านั้น
ไฟล์นี้จึงเบามาก โหลดเร็ว ไม่มี native library หนักๆ อยู่ในโปรเซสเดียวกับ Streamlit

เหตุผล: พิสูจน์แล้วด้วย test_minimal_streamlit.py ว่า Streamlit เปล่าๆ ไม่ crash เลย
แต่พอโหลด torch+faiss+transformers ไว้ในโปรเซสเดียวกับ Streamlit จะ crash แบบ Windows
access violation ที่ WINHTTP.dll (offset เดิมซ้ำทุกครั้ง, ไม่เกี่ยวกับ antivirus/telemetry/
asyncio policy — ไล่ตัดทีละสมมติฐานจนเหลือแต่ "native libs อยู่ผิดโปรเซส") การแยกโปรเซส
จึงเป็นทางแก้ที่ตรงจุดที่สุดเท่าที่ตรวจสอบได้

ไฟล์เดิมแบบ monolithic (ทุกอย่างในไฟล์เดียว) สำรองไว้ที่ app_monolithic_backup.py
"""
import os
import sys
import json
import time
import base64
import datetime
import subprocess
import urllib.request
import urllib.error
import uuid

import streamlit as st

from generate_docx import markdown_to_docx

WORKER_HOST = "127.0.0.1"
WORKER_PORT = 8765
WORKER_URL = f"http://{WORKER_HOST}:{WORKER_PORT}"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER_SCRIPT = os.path.join(BASE_DIR, "rag_worker.py")


def _load_dotenv(path: str) -> None:
    """โหลด KEY=VALUE จาก .env แบบง่ายๆ (เหมือน rag_worker.py) ใช้แค่เพื่ออ่าน COMPANY_NAME
    มาโชว์บนหน้าเว็บ — ตัว API key จริงถูกใช้งานฝั่ง rag_worker.py เท่านั้น"""
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
COMPANY_NAME = os.environ.get("COMPANY_NAME", "องค์กรของคุณ")


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


def _wait_for_worker_ready(max_wait_seconds: int = 400) -> None:
    start = time.time()
    placeholder = st.empty()
    while True:
        status = _check_worker_health()
        elapsed = int(time.time() - start)

        if status is None:
            placeholder.info(f"⏳ กำลังเริ่ม RAG worker process... ({elapsed}s)")
        elif status.get("status") == "ready":
            placeholder.empty()
            return
        elif status.get("status") == "error":
            placeholder.error(f"❌ Worker โหลดล้มเหลว: {status.get('detail')}\n\nดูรายละเอียดที่ rag_worker.log")
            st.stop()
        else:
            detail = status.get("detail", "กำลังโหลด...")
            placeholder.info(f"⏳ {detail} ({elapsed}s / ครั้งแรกอาจนานถึง ~4 นาที)")

        if elapsed > max_wait_seconds:
            placeholder.error("โหลดนานเกินไป กรุณาเช็ค rag_worker.log หรือรัน rag_worker.py แยกดูตรงๆ")
            st.stop()

        time.sleep(1.5)


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


# ── UI: เดินหัวข้อทีละอัน (ใช้ร่วมกันทั้งโหมดรีวิวเอกสาร ADR-006 และโหมดร่างเอกสาร interactive
# ADR-007 — ADR-007 ข้อ 3 ตั้งใจ reuse กลไก "ถามทีละข้อ + ย้อนกลับได้ + prefill" นี้จาก ADR-006 โดย
# ไม่ยืมนิยามเต็มของ "หัวข้อรีวิว" — state ทั้งหมดเก็บใน st.session_state ใต้ namespace {prefix}_*
# ไม่มี server-side session ใหม่ฝั่ง worker (ดู ADR-006 ผลที่ตามมา) ─────────────────────────────

def _render_topic_walker(prefix: str, topics: list[dict], fetch_prefill, fetch_followup) -> str:
    """วาด UI เดินหัวข้อทีละอัน (ย้อนกลับได้ + prefill + follow-up เฉพาะหัวข้อที่ allow_followup)

    fetch_prefill(topic) -> dict {"prefill": "...", "prefill_sources": [...]} หรือ {"error": "..."}
    fetch_followup(topic, answer) -> dict {"follow_up_question": "..."|None} หรือ {"error": "..."}

    คืนค่า "walking" ระหว่างเดินหัวข้ออยู่ (เรียก st.stop() ไปแล้วข้างในฟังก์ชันนี้ทุกครั้งที่ยัง
    เดินไม่จบ) หรือ "done" เมื่อเดินครบทุกหัวข้อแล้ว (ฟังก์ชันคืนค่าปกติ ไม่ st.stop() — ผู้เรียกไป
    ขั้นตอนสรุปผลต่อได้เลย)"""
    idx_key = f"{prefix}_idx"
    stage_key = f"{prefix}_stage"
    answers_key = f"{prefix}_answers"
    prefill_cache_key = f"{prefix}_prefill_cache"
    followup_q_key = f"{prefix}_followup_q"
    followup_meta_key = f"{prefix}_followup_meta"

    for key, default in (
        (idx_key, 0), (stage_key, "main"), (answers_key, {}),
        (prefill_cache_key, {}), (followup_meta_key, {}),
    ):
        if key not in st.session_state:
            st.session_state[key] = default

    idx = st.session_state[idx_key]
    if idx >= len(topics):
        return "done"

    topic = topics[idx]
    topic_id = topic["id"]
    answers = st.session_state[answers_key]
    total = len(topics)

    st.progress(idx / total if total else 0.0)
    cat = topic.get("category")
    st.caption(f"หัวข้อ {idx + 1}/{total}" + (f" — หมวด: {cat}" if cat else ""))

    if st.session_state[stage_key] == "followup":
        question = st.session_state.get(followup_q_key, "")
        st.markdown(f"**คำถามต่อยอด:** {question}")
        followup_answer = st.text_input(
            "คำตอบ (ไม่บังคับ — ข้ามได้)", key=f"{prefix}_followup_input_{idx}"
        )
        c1, c2, c3 = st.columns(3)
        if c1.button("⬅️ ย้อนกลับ", key=f"{prefix}_followup_back_{idx}"):
            st.session_state[stage_key] = "main"
            st.rerun()
        if c2.button("⏭️ ข้าม", key=f"{prefix}_followup_skip_{idx}"):
            answers[f"{topic_id}__followup"] = ""
            st.session_state[idx_key] = idx + 1
            st.session_state[stage_key] = "main"
            st.rerun()
        if c3.button("✅ ยืนยันคำตอบต่อยอด", key=f"{prefix}_followup_confirm_{idx}"):
            answers[f"{topic_id}__followup"] = followup_answer
            st.session_state[idx_key] = idx + 1
            st.session_state[stage_key] = "main"
            st.rerun()
        st.stop()

    st.markdown(f"### {topic['heading']}")
    if topic.get("body"):
        with st.expander("เนื้อหาเดิมในเอกสาร"):
            st.write(topic["body"])

    if topic_id not in st.session_state[prefill_cache_key]:
        with st.spinner("กำลังเตรียมคำตอบเสนอแนะ (Prefill)..."):
            pf_result = fetch_prefill(topic)
        st.session_state[prefill_cache_key][topic_id] = pf_result

    pf_result = st.session_state[prefill_cache_key][topic_id]
    if "error" in pf_result:
        st.warning(f"เตรียม prefill ไม่สำเร็จ: {pf_result['error']} (กรอกเองได้ตามปกติ)")
        default_value = answers.get(topic_id, "")
    else:
        default_value = answers.get(topic_id, pf_result.get("prefill", ""))
        pf_sources = pf_result.get("prefill_sources") or []
        if pf_sources:
            with st.expander("ดูเอกสารอ้างอิงที่ใช้เตรียมคำตอบเสนอแนะ (Sources)"):
                for src in pf_sources:
                    st.write(f"**ไฟล์:** {src.get('file_name', 'Unknown')}")
                    st.write(f"**เนื้อหาที่พบ:** {src.get('content', '')}...")
                    st.write("---")

    answer_text = st.text_area(
        "คำตอบ (แก้ไข/ยืนยันคำตอบเสนอแนะด้านบนได้เลย — ปล่อยว่างถ้าข้ามหัวข้อนี้)",
        value=default_value,
        key=f"{prefix}_answer_input_{idx}",
        height=120,
    )

    c1, c2, c3 = st.columns(3)
    if c1.button("⬅️ ย้อนกลับ", key=f"{prefix}_back_{idx}", disabled=idx == 0):
        st.session_state[idx_key] = max(0, idx - 1)
        st.rerun()
    if c2.button("⏭️ ข้ามหัวข้อนี้", key=f"{prefix}_skip_{idx}"):
        answers[topic_id] = ""
        st.session_state[idx_key] = idx + 1
        st.rerun()
    if c3.button("✅ ยืนยันคำตอบ", key=f"{prefix}_confirm_{idx}"):
        answers[topic_id] = answer_text
        if topic.get("allow_followup") and answer_text.strip():
            with st.spinner("กำลังตรวจว่าควรถามคำถามต่อยอดหรือไม่..."):
                fu_result = fetch_followup(topic, answer_text)
            follow_up_q = (fu_result or {}).get("follow_up_question")
            if follow_up_q:
                st.session_state[followup_q_key] = follow_up_q
                st.session_state[followup_meta_key][topic_id] = follow_up_q
                st.session_state[stage_key] = "followup"
                st.rerun()
        st.session_state[idx_key] = idx + 1
        st.rerun()

    st.stop()


def _clear_state_prefix(prefix: str) -> None:
    """ล้าง st.session_state ทุก key ที่ขึ้นต้นด้วย prefix ที่กำหนด — ใช้ตอนกด '🔄 เริ่มใหม่'
    ของทั้งโหมดรีวิวเอกสารและโหมดร่างเอกสาร กันมี state เก่าค้างข้ามรอบ"""
    for k in list(st.session_state.keys()):
        if k.startswith(prefix):
            del st.session_state[k]


# ── UI ───────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Policy RAG Assistant", page_icon="🤖", layout="wide")
st.title("🤖 Policy RAG Assistant (Powered by Gemini)")
st.caption(f"ระบบถาม-ตอบนโยบายและระเบียบปฏิบัติของ {COMPANY_NAME}")

ensure_worker_started()
_wait_for_worker_ready()

if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())

# ── โหมดพิเศษ (ทดลอง): โหมดรีวิวเอกสาร (ADR-006) / โหมดร่างเอกสาร (ADR-001/002/007) ─────────
# ทั้งสองโหมดแยกขาดจากโหมด Q&A เดิมโดยสิ้นเชิง เปิดผ่าน toggle ในไซด์บาร์ ไม่ใช่ slash command
# ในกล่องแชทเดิม เพื่อไม่ให้ผู้ใช้เผลอสลับโหมดจากการพิมพ์ปกติ ถ้าเปิดโหมดใดโหมดหนึ่งจะ render UI
# ของโหมดนั้นแล้ว st.stop() ตัดจบก่อนถึงโค้ด Chat UI เดิมด้านล่าง (ไม่แตะ/ไม่กระทบพฤติกรรม Q&A เดิม)
# ถ้าเปิดทั้งสอง toggle พร้อมกัน โหมดรีวิวเอกสารมาก่อนเสมอ (ตรวจ review_mode ก่อน draft_mode)
st.sidebar.markdown("---")
review_mode = st.sidebar.toggle(
    "📋 โหมดรีวิวเอกสาร (ทดลอง)",
    value=False,
    help=(
        "ตรวจทานเอกสารนโยบายที่มีอยู่แล้วทีละหัวข้อ พร้อมเสนอคำตอบจากเอกสารที่เกี่ยวข้องให้ยืนยัน/แก้ไข "
        "ก่อนเสมอ ผลลัพธ์เป็นรายงานสรุปการเปลี่ยนแปลง + เอกสารฉบับปรับปรุง — แยกจากโหมดร่างเอกสารและ "
        "โหมดถาม-ตอบปกติโดยสิ้นเชิง ไม่กระทบการทำงานของทั้งสองโหมด (ดู ADR-006)"
    ),
)
draft_mode = st.sidebar.toggle(
    "📝 โหมดร่างเอกสาร (ทดลอง)",
    value=False,
    help=(
        "แต่งร่างนโยบายใหม่ทั้งฉบับ พร้อมตรวจสอบความสอดคล้องกับนโยบายที่มีอยู่โดยอัตโนมัติ "
        "— แยกจากโหมดถาม-ตอบปกติโดยสิ้นเชิง ไม่กระทบการทำงานของโหมดถาม-ตอบ"
    ),
)

if review_mode:
    st.subheader("📋 รีวิวเอกสารที่มีอยู่แล้ว (ทดลอง)")
    st.caption(
        "เลือกเอกสารเป้าหมาย 1 ฉบับที่ต้องการรีวิว ระบบจะเดินตรวจทีละหัวข้อ (ทั้งหัวข้อเดิมของเอกสาร "
        "และหัวข้อที่ควรมีเพิ่มเติมตามประเภทเอกสาร) พร้อมเสนอคำตอบจากเอกสารที่เกี่ยวข้องให้ยืนยัน/แก้ไข "
        "ก่อนเสมอ ผลลัพธ์คือรายงานสรุปการเปลี่ยนแปลง + เอกสารฉบับปรับปรุงคู่กัน — เซสชันนี้หลุด/รีเฟรชแล้ว "
        "ต้องเริ่มรีวิวใหม่ (ไม่รองรับ save/resume ข้ามเซสชัน — ดู ADR-006 ข้อ 7)"
    )

    review_target = st.session_state.get("review_target")
    review_result = st.session_state.get("review_result")

    # ── Stage 1: เลือกเอกสารเป้าหมาย (อัปโหลดตรง หรือเลือกจากเอกสารที่ index ไว้แล้ว — ADR-006 ข้อ 2) ──
    if not review_target:
        source_choice = st.radio(
            "เอกสารเป้าหมายมาจากไหน",
            ["เลือกจากเอกสารที่ index ไว้แล้ว", "อัปโหลดไฟล์ใหม่"],
            key="review_source_choice",
        )
        if source_choice == "อัปโหลดไฟล์ใหม่":
            uploaded = st.file_uploader("อัปโหลดเอกสารเป้าหมาย (.md หรือ .docx)", type=["md", "docx"])
            if st.button("🔍 วิเคราะห์เอกสารเป้าหมาย", key="review_analyze_upload"):
                if not uploaded:
                    st.warning("กรุณาอัปโหลดไฟล์ก่อน")
                else:
                    with st.spinner("กำลังวิเคราะห์โครงสร้างเอกสารและเตรียมหัวข้อรีวิว..."):
                        content_b64 = base64.b64encode(uploaded.getvalue()).decode("ascii")
                        result = _call_worker_review_target("upload", uploaded.name, content_b64)
                    if "error" in result:
                        st.error(result.get("message", result["error"]))
                    else:
                        st.session_state.review_target = result
                        st.session_state.review_confirmed_docs = list(
                            result.get("suggested_cross_reference_docs", [])
                        )
                        st.rerun()
        else:
            file_name = st.text_input(
                "ชื่อไฟล์เอกสารเป้าหมาย (ตามที่ index ไว้)", placeholder="เช่น IT_Policy.md"
            )
            if st.button("🔍 วิเคราะห์เอกสารเป้าหมาย", key="review_analyze_corpus"):
                if not file_name.strip():
                    st.warning("กรุณาระบุชื่อไฟล์ก่อน")
                else:
                    with st.spinner("กำลังวิเคราะห์โครงสร้างเอกสารและเตรียมหัวข้อรีวิว..."):
                        result = _call_worker_review_target("corpus", file_name.strip())
                    if "error" in result:
                        st.error(result.get("message", result["error"]))
                    else:
                        st.session_state.review_target = result
                        st.session_state.review_confirmed_docs = list(
                            result.get("suggested_cross_reference_docs", [])
                        )
                        st.rerun()

    # ── Stage 2: ยืนยัน/แก้ไขเอกสารที่เกี่ยวข้อง (Cross-reference documents — ADR-006 ข้อ 3) ────────
    elif "review_cross_ref_confirmed" not in st.session_state:
        st.markdown(f"**เอกสารเป้าหมาย:** {review_target['file_name']}")
        if st.button("🔄 เริ่มใหม่", key="review_restart_from_crossref"):
            _clear_state_prefix("review_")
            st.rerun()

        st.caption("ระบบแนะนำเอกสารที่เกี่ยวข้องไว้ให้แล้วจากการค้นหาอัตโนมัติ ยืนยันหรือแก้ไขรายการได้ก่อนเริ่มรีวิว")
        confirmed = st.multiselect(
            "เอกสารที่เกี่ยวข้อง (ใช้เทียบความสอดคล้องระหว่างรีวิว)",
            options=sorted(set(review_target.get("suggested_cross_reference_docs", []))),
            default=st.session_state.get("review_confirmed_docs", []),
            key="review_crossref_multiselect",
        )
        if st.button("▶️ เริ่มรีวิวทีละหัวข้อ", key="review_start_topics"):
            st.session_state.review_confirmed_docs = confirmed
            st.session_state.review_cross_ref_confirmed = True
            st.rerun()

    # ── Stage 3: เดินหัวข้อรีวิวทีละอัน (prefill ก่อนถาม + follow-up หลังตอบ + ย้อนกลับได้ — ADR-006 ข้อ 5) ──
    elif not review_result:
        topics = review_target["review_topics"]
        confirmed_docs = st.session_state.get("review_confirmed_docs", [])

        if st.button("🔄 เริ่มใหม่", key="review_restart_from_topics"):
            _clear_state_prefix("review_")
            st.rerun()

        def _review_fetch_prefill(topic):
            return _call_worker_topic_step("/review/topic", {
                "review_topics": topics,
                "confirmed_cross_reference_docs": confirmed_docs,
                "answers": st.session_state.get("review_answers", {}),
                "topic_id": topic["id"],
                "requesting_followup_for_answer": False,
            })

        def _review_fetch_followup(topic, answer):
            # ต้อง set คำตอบล่าสุดก่อนส่ง เพราะ worker อ่าน answer จาก answers[topic_id]
            st.session_state.setdefault("review_answers", {})[topic["id"]] = answer
            return _call_worker_topic_step("/review/topic", {
                "review_topics": topics,
                "confirmed_cross_reference_docs": confirmed_docs,
                "answers": st.session_state["review_answers"],
                "topic_id": topic["id"],
                "requesting_followup_for_answer": True,
            })

        walk_status = _render_topic_walker("review", topics, _review_fetch_prefill, _review_fetch_followup)
        if walk_status == "done":
            st.success("ตอบครบทุกหัวข้อแล้ว")
            if st.button("📊 สรุปผลรีวิว (รายงานการเปลี่ยนแปลง + เอกสารฉบับปรับปรุง)", key="review_finalize_btn"):
                with st.spinner("กำลังรวบรวมผลรีวิวทั้งหมด... (อาจใช้เวลาสักครู่)"):
                    result = _call_worker_review_finalize(
                        review_target["file_name"], topics,
                        st.session_state.get("review_answers", {}), confirmed_docs,
                    )
                st.session_state.review_result = result
                st.rerun()

    # ── Stage 4: แสดงผลลัพธ์ (ADR-006 ข้อ 6 — รายงานสรุปการเปลี่ยนแปลง + เอกสารฉบับปรับปรุงคู่กัน) ──
    else:
        if st.button("🔄 รีวิวเอกสารใหม่", key="review_restart_from_result"):
            _clear_state_prefix("review_")
            st.rerun()

        if "error" in review_result:
            st.error(f"เกิดข้อผิดพลาด: {review_result.get('message', review_result['error'])}")
        else:
            st.markdown("### รายงานสรุปการเปลี่ยนแปลง")
            st.markdown(review_result.get("change_report_markdown", ""))

            st.markdown("### เอกสารฉบับปรับปรุง")
            updated_doc = review_result.get("updated_document_markdown", "")
            if updated_doc:
                st.markdown(updated_doc)
                docx_bytes = markdown_to_docx(updated_doc, title=review_target["file_name"])
                base_name = review_target["file_name"].rsplit(".", 1)[0]
                st.download_button(
                    label="📥 ดาวน์โหลดเอกสารฉบับปรับปรุงเป็น Word (.docx)",
                    data=docx_bytes,
                    file_name=f"review_{base_name}_updated.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            else:
                st.warning("ไม่สามารถสร้างเอกสารฉบับปรับปรุงได้โดยอัตโนมัติ — ดูรายงานสรุปการเปลี่ยนแปลงด้านบนแทน")

            r_sources = review_result.get("sources", [])
            if r_sources:
                with st.expander("ดูเอกสารอ้างอิงที่ใช้สรุปผล (Sources)"):
                    for src in r_sources:
                        st.write(f"**ไฟล์:** {src.get('file_name', 'Unknown')}")
                        st.write(f"**เนื้อหาที่พบ:** {src.get('content', '')}...")
                        st.write("---")
    st.stop()

elif draft_mode:
    st.subheader("📝 ร่างนโยบายใหม่ + ตรวจสอบอัตโนมัติ (ทดลอง)")
    st.caption(
        "ระบบจะถามข้อมูลที่จำเป็นเพิ่มก่อน (เฉพาะจุดที่นโยบายเดิมไม่ครอบคลุม) แล้วค่อยแต่งร่างนโยบายใหม่ทั้งฉบับ "
        "โดยอ้างอิงนโยบายที่มีอยู่แล้วให้สอดคล้องกัน พร้อมวิจารณ์ร่างของตัวเองทันที — ผลลัพธ์เป็นจุดเริ่มต้นสำหรับ"
        "มนุษย์ตรวจสอบต่อ ไม่ใช่เอกสารสำเร็จรูป"
    )

    clarify = st.session_state.get("draft_clarify")
    dr = st.session_state.get("draft_result")

    # ── Stage 1: หัวข้อ + คำสั่งเสริม -> สร้างคำถามเพิ่มเติมแบบจัดหมวดหมู่ (ดู ADR-007 ข้อ 1) ──────
    if not clarify:
        with st.form("draft_topic_form"):
            topic = st.text_input("หัวข้อนโยบายที่ต้องการร่าง", placeholder="เช่น นโยบายจัดซื้อ")
            instructions = st.text_area(
                "คำสั่ง/รายละเอียดเพิ่มเติม (ไม่บังคับ)",
                placeholder="เช่น ต้องระบุขั้นตอนอนุมัติ 3 ระดับ, อ้างอิงตามนโยบาย IT ที่มีอยู่",
            )
            ask_submitted = st.form_submit_button("❓ สร้างคำถามเพิ่มเติม")

        if ask_submitted:
            if not topic.strip():
                st.warning("กรุณาระบุหัวข้อนโยบายก่อน")
            else:
                with st.spinner("กำลังวิเคราะห์ว่าต้องขอข้อมูลอะไรเพิ่มก่อนร่าง... (คำถามแบบจัดหมวดหมู่ 10-25 ข้อ)"):
                    q_result = _call_worker_topic_step(
                        "/draft/questions/interactive", {"topic": topic, "instructions": instructions}
                    )
                if "error" in q_result:
                    st.error(f"เกิดข้อผิดพลาด: {q_result['error']}")
                else:
                    st.session_state.draft_clarify = {
                        "topic": topic,
                        "instructions": instructions,
                        "questions_topics": q_result.get("review_topics", []),
                    }
                    st.rerun()

    # ── Stage 2: ตอบคำถามเพิ่มเติมทีละข้อ พร้อม prefill + follow-up (ดู ADR-007 ข้อ 2/3) -> ยืนยันร่าง ──
    elif not dr:
        st.markdown(f"**หัวข้อ:** {clarify['topic']}")
        if st.button("🔄 เริ่มใหม่", key="restart_from_stage2"):
            st.session_state.pop("draft_clarify", None)
            _clear_state_prefix("draftq_")
            st.rerun()

        questions_topics = clarify.get("questions_topics", [])

        if not questions_topics:
            st.caption("ข้อมูลในนโยบายเดิมครอบคลุมพอแล้ว ระบบไม่มีคำถามเพิ่มเติมสำหรับหัวข้อนี้ กดยืนยันร่างได้เลย")
            if st.button("✅ ยืนยันร่างนโยบาย", key="draft_confirm_no_questions"):
                with st.spinner("กำลังร่างเอกสารและตรวจสอบความสอดคล้อง... (อาจใช้เวลาสักครู่)"):
                    draft_result = _call_worker_draft(
                        clarify["topic"], clarify["instructions"], {}, session_id=st.session_state.session_id,
                    )
                st.session_state.draft_result = {"topic": clarify["topic"], **draft_result}
                if "error" not in draft_result:
                    st.session_state.has_draft_in_session = True
                st.rerun()
        else:
            st.caption(
                "ตอบเท่าที่รู้ได้เลย ข้อไหนไม่แน่ใจข้ามได้ — ระบบเสนอคำตอบจากนโยบายที่เกี่ยวข้องให้ก่อนเสมอ "
                "(ยืนยัน/แก้ไขได้) ข้อไหนไม่ตอบระบบจะทำเครื่องหมาย [ต้องระบุ: ...] ไว้ในร่างแทนการเดาคำตอบขึ้นมาเอง"
            )

            def _draftq_fetch_prefill(topic):
                return _call_worker_topic_step("/draft/questions/interactive", {
                    "topic": clarify["topic"],
                    "instructions": clarify["instructions"],
                    "review_topics": questions_topics,
                    "answers": st.session_state.get("draftq_answers", {}),
                    "topic_id": topic["id"],
                    "requesting_followup_for_answer": False,
                })

            def _draftq_fetch_followup(topic, answer):
                st.session_state.setdefault("draftq_answers", {})[topic["id"]] = answer
                return _call_worker_topic_step("/draft/questions/interactive", {
                    "topic": clarify["topic"],
                    "instructions": clarify["instructions"],
                    "review_topics": questions_topics,
                    "answers": st.session_state["draftq_answers"],
                    "topic_id": topic["id"],
                    "requesting_followup_for_answer": True,
                })

            walk_status = _render_topic_walker(
                "draftq", questions_topics, _draftq_fetch_prefill, _draftq_fetch_followup
            )
            if walk_status == "done":
                st.success("ตอบครบทุกหัวข้อแล้ว")
                if st.button("✅ ยืนยันร่างนโยบาย", key="draft_confirm_after_questions"):
                    answers_dict = {}
                    topic_answers = st.session_state.get("draftq_answers", {})
                    for t in questions_topics:
                        answers_dict[t["heading"]] = topic_answers.get(t["id"], "")
                    followup_meta = st.session_state.get("draftq_followup_meta", {})
                    for tid, q_text in followup_meta.items():
                        if q_text:
                            answers_dict[q_text] = topic_answers.get(f"{tid}__followup", "")

                    with st.spinner("กำลังร่างเอกสารและตรวจสอบความสอดคล้อง... (อาจใช้เวลาสักครู่)"):
                        draft_result = _call_worker_draft(
                            clarify["topic"], clarify["instructions"], answers_dict,
                            session_id=st.session_state.session_id,
                        )
                    st.session_state.draft_result = {"topic": clarify["topic"], **draft_result}
                    if "error" not in draft_result:
                        st.session_state.has_draft_in_session = True
                    st.rerun()

    # ── Stage 3: แสดงผลลัพธ์ ────────────────────────────────────────────────
    else:
        if st.button("🔄 ร่างหัวข้อใหม่", key="restart_from_stage3"):
            st.session_state.pop("draft_clarify", None)
            st.session_state.pop("draft_result", None)
            _clear_state_prefix("draftq_")
            st.rerun()

        if "error" in dr:
            st.error(f"เกิดข้อผิดพลาด: {dr['error']}")
        else:
            scrutiny = dr["scrutiny"]
            if "CRITICAL" in scrutiny:
                st.warning(
                    "⚠️ พบประเด็น CRITICAL ในรายงานตรวจสอบด้านล่าง — โปรดตรวจสอบก่อนนำไปใช้จริง "
                    "(ระบบไม่บล็อกการดาวน์โหลด แต่ร่างนี้ต้องผ่านการพิจารณาจากมนุษย์ก่อนใช้เสมอ)"
                )

            st.markdown("### ร่างนโยบาย")
            st.markdown(dr["draft_markdown"])

            st.markdown("### รายงานตรวจสอบ (Scrutinize)")
            st.markdown(scrutiny)

            sources = dr.get("sources", [])
            if sources:
                with st.expander("ดูเอกสารอ้างอิงที่ใช้ในการร่าง (Sources)"):
                    for src in sources:
                        st.write(f"**ไฟล์:** {src.get('file_name', 'Unknown')}")
                        st.write(f"**เนื้อหาที่พบ:** {src.get('content', '')}...")
                        st.write("---")

            docx_bytes = markdown_to_docx(dr["draft_markdown"], title=dr["topic"])
            st.download_button(
                label="📥 ดาวน์โหลดเป็น Word (.docx)",
                data=docx_bytes,
                file_name=f"draft_{dr['topic'].strip().replace(' ', '_')}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
    st.stop()

# ── Sidebar ──────────────────────────────────────────────────────────────
st.sidebar.markdown("---")
st.sidebar.subheader("💾 จัดการบทสนทนา")
if "messages" in st.session_state and len(st.session_state.messages) > 1:
    st.sidebar.download_button(
        label="📥 ดาวน์โหลดประวัติการแชท (JSON)",
        data=json.dumps(st.session_state.messages, ensure_ascii=False, indent=2),
        file_name=f"chat_history_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        mime="application/json",
    )

# ── แจ้งเตือนถ้าเซสชันนี้มีร่างเอกสารที่ยังไม่อนุมัติ (ดู ADR-004) ─────────
# แสดงเฉพาะตอนอยู่ในโหมดถาม-ตอบปกติ (ไม่ใช่โหมดร่างเอกสาร/รีวิวเอกสาร ที่ st.stop() ตัดจบไปแล้วด้านบน)
# เพื่อกันผู้ใช้สับสนว่าร่างที่แชทอ้างถึงเป็นนโยบายที่อนุมัติแล้วจริง
if st.session_state.get("has_draft_in_session"):
    st.info(
        "📝 เซสชันนี้มีร่างเอกสารที่ AI สร้างไว้ (ยังไม่ผ่านการอนุมัติ) — สามารถถามต่อในแชทด้านล่างได้ "
        "เช่น \"อธิบายข้อ 3 ในร่างที่เพิ่งสร้างให้หน่อย\" แต่คำตอบที่อ้างอิงร่างนี้ยังไม่ใช่นโยบายที่ใช้งานจริง "
        "ต้องรอการอนุมัติก่อนเสมอ"
    )

# ── Chat UI ──────────────────────────────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state.messages = [{
        "role": "assistant",
        "content": "สวัสดีครับ! ถามคำถามเรื่องนโยบายบริษัทได้เลยครับ",
        "time": datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
    }]

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        parts = []
        if "time"   in msg: parts.append(f"⏱️ {msg['time']}")
        if "tokens" in msg: parts.append(f"🪙 Tokens: ~{msg['tokens']}")
        if parts:
            st.markdown(
                f"<small style='color:gray;'>{' | '.join(parts)}</small>",
                unsafe_allow_html=True,
            )
        if msg.get("sources"):
            with st.expander("ดูเอกสารอ้างอิง (Sources)"):
                for src in msg["sources"]:
                    st.write(f"**ไฟล์:** {src.get('file_name', 'Unknown')}")
                    st.write(f"**เนื้อหาที่พบ:** {src.get('content', '')}...")
                    st.write("---")

if "last_question_time" not in st.session_state:
    st.session_state.last_question_time = 0

if prompt := st.chat_input("พิมพ์คำถามของคุณที่นี่..."):
    elapsed = time.time() - st.session_state.last_question_time
    if elapsed < 60:
        st.warning(f"⏳ กรุณารออีก {int(60 - elapsed)} วินาที")
        st.stop()

    st.session_state.last_question_time = time.time()
    now_str = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    st.session_state.messages.append({"role": "user", "content": prompt, "time": now_str})

    with st.chat_message("user"):
        st.markdown(prompt)
        st.markdown(f"<small style='color:gray;'>⏱️ {now_str}</small>", unsafe_allow_html=True)

    with st.chat_message("assistant"):
        with st.spinner("กำลังค้นหาและประมวลผลคำตอบ..."):
            result = _call_worker_chat(st.session_state.session_id, prompt)

        if "error" in result:
            st.error(f"เกิดข้อผิดพลาด: {result['error']}")
        else:
            full_response = result["response"]
            st.markdown(full_response)
            resp_time = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            tokens = result.get("tokens", 0)
            st.markdown(
                f"<small style='color:gray;'>⏱️ {resp_time} | 🪙 Tokens: ~{tokens}</small>",
                unsafe_allow_html=True,
            )
            sources = result.get("sources", [])
            st.session_state.messages.append({
                "role": "assistant",
                "content": full_response,
                "time": resp_time,
                "tokens": tokens,
                "sources": sources,
            })
            if sources:
                with st.expander("ดูเอกสารอ้างอิง (Sources)"):
                    for src in sources:
                        st.write(f"**ไฟล์:** {src.get('file_name', 'Unknown')}")
                        st.write(f"**เนื้อหาที่พบ:** {src.get('content', '')}...")
                        st.write("---")
