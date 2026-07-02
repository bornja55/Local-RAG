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


def _call_worker_chat(session_id: str, prompt: str) -> dict:
    body = json.dumps({"session_id": session_id, "prompt": prompt}).encode("utf-8")
    req = urllib.request.Request(
        f"{WORKER_URL}/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}


def _call_worker_questions(topic: str, instructions: str) -> dict:
    """เรียก /draft/questions — ขั้นตอนสร้างคำถามเพิ่มเติมก่อนร่าง (ดู ADR-002) ใช้
    GEMINI_MODEL_CHAT ฝั่ง worker จึงเบากว่า _call_worker_draft มาก timeout สั้นกว่า"""
    body = json.dumps({"topic": topic, "instructions": instructions}).encode("utf-8")
    req = urllib.request.Request(
        f"{WORKER_URL}/draft/questions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}


def _call_worker_draft(
    topic: str, instructions: str, answers: dict | None = None, session_id: str | None = None
) -> dict:
    """เรียก /draft — ใช้โมเดลที่ reasoning ดีกว่า (GEMINI_MODEL_DRAFT) และเรียก LLM 2 ครั้ง
    (ร่าง + scrutinize) ต่อคำขอ จึงตั้ง timeout ยาวกว่า _call_worker_chat
    answers: คำถาม->คำตอบ จากขั้นตอนคำถามเพิ่มเติม (ดู ADR-002) ส่งต่อให้ worker เพื่อ ground ร่าง
    session_id: ถ้าส่งไป worker จะฉีดร่าง+scrutiny เข้า chat memory ของ session นี้ ทำให้โหมด
    ถาม-ตอบปกติในเซสชันเดียวกันอ้างอิงร่างนี้ต่อได้ (ดู ADR-004)"""
    body = json.dumps({
        "topic": topic,
        "instructions": instructions,
        "answers": answers or {},
        "session_id": session_id,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{WORKER_URL}/draft",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}


# ── UI ───────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Policy RAG Assistant", page_icon="🤖", layout="wide")
st.title("🤖 Policy RAG Assistant (Powered by Gemini)")
st.caption(f"ระบบถาม-ตอบนโยบายและระเบียบปฏิบัติของ {COMPANY_NAME}")

ensure_worker_started()
_wait_for_worker_ready()

if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())

# ── โหมดร่างเอกสาร (ทดลอง) ───────────────────────────────────────────────
# แยกขาดจากโหมด Q&A เดิมโดยสิ้นเชิงตาม ADR-001 — เปิดผ่าน toggle ในไซด์บาร์ ไม่ใช่
# slash command ในกล่องแชทเดิม เพื่อไม่ให้ผู้ใช้เผลอสลับโหมดจากการพิมพ์ปกติ ถ้าเปิดโหมดนี้
# จะ render UI ของโหมดร่างแล้ว st.stop() ตัดจบก่อนถึงโค้ด Chat UI เดิมด้านล่าง (ไม่แตะ/ไม่
# กระทบพฤติกรรม Q&A เดิมที่ทดสอบแล้วว่าทำงานถูกต้อง)
st.sidebar.markdown("---")
draft_mode = st.sidebar.toggle(
    "📝 โหมดร่างเอกสาร (ทดลอง)",
    value=False,
    help=(
        "แต่งร่างนโยบายใหม่ทั้งฉบับ พร้อมตรวจสอบความสอดคล้องกับนโยบายที่มีอยู่โดยอัตโนมัติ "
        "— แยกจากโหมดถาม-ตอบปกติโดยสิ้นเชิง ไม่กระทบการทำงานของโหมดถาม-ตอบ"
    ),
)

if draft_mode:
    st.subheader("📝 ร่างนโยบายใหม่ + ตรวจสอบอัตโนมัติ (ทดลอง)")
    st.caption(
        "ระบบจะถามข้อมูลที่จำเป็นเพิ่มก่อน (เฉพาะจุดที่นโยบายเดิมไม่ครอบคลุม) แล้วค่อยแต่งร่างนโยบายใหม่ทั้งฉบับ "
        "โดยอ้างอิงนโยบายที่มีอยู่แล้วให้สอดคล้องกัน พร้อมวิจารณ์ร่างของตัวเองทันที — ผลลัพธ์เป็นจุดเริ่มต้นสำหรับ"
        "มนุษย์ตรวจสอบต่อ ไม่ใช่เอกสารสำเร็จรูป"
    )

    clarify = st.session_state.get("draft_clarify")
    dr = st.session_state.get("draft_result")

    # ── Stage 1: หัวข้อ + คำสั่งเสริม -> สร้างคำถามเพิ่มเติม (ดู ADR-002) ──────
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
                with st.spinner("กำลังวิเคราะห์ว่าต้องขอข้อมูลอะไรเพิ่มก่อนร่าง..."):
                    q_result = _call_worker_questions(topic, instructions)
                if "error" in q_result:
                    st.error(f"เกิดข้อผิดพลาด: {q_result['error']}")
                else:
                    st.session_state.draft_clarify = {
                        "topic": topic,
                        "instructions": instructions,
                        "questions": q_result.get("questions", []),
                    }
                    st.rerun()

    # ── Stage 2: ตอบคำถามเพิ่มเติม (ข้ามได้) -> ยืนยันร่าง ──────────────────
    elif not dr:
        st.markdown(f"**หัวข้อ:** {clarify['topic']}")
        if st.button("🔄 เริ่มใหม่", key="restart_from_stage2"):
            st.session_state.pop("draft_clarify", None)
            st.rerun()

        questions = clarify.get("questions", [])
        if questions:
            st.caption(
                "ตอบเท่าที่รู้ได้เลย ข้อไหนไม่แน่ใจข้ามได้ — ระบบจะทำเครื่องหมาย [ต้องระบุ: ...] "
                "ไว้ในร่างตรงจุดที่ยังขาดข้อมูล แทนการเดาคำตอบขึ้นมาเอง"
            )
        else:
            st.caption("ข้อมูลในนโยบายเดิมครอบคลุมพอแล้ว ระบบไม่มีคำถามเพิ่มเติมสำหรับหัวข้อนี้ กดยืนยันร่างได้เลย")

        with st.form("draft_answers_form"):
            answers = {}
            for i, q in enumerate(questions):
                answers[q] = st.text_input(q, key=f"draft_answer_{i}")
            confirm_submitted = st.form_submit_button("✅ ยืนยันร่างนโยบาย")

        if confirm_submitted:
            with st.spinner("กำลังร่างเอกสารและตรวจสอบความสอดคล้อง... (อาจใช้เวลาสักครู่)"):
                draft_result = _call_worker_draft(
                    clarify["topic"], clarify["instructions"], answers,
                    session_id=st.session_state.session_id,
                )
                st.session_state.draft_result = {"topic": clarify["topic"], **draft_result}
                # ถ้าร่างสำเร็จ (ไม่ error) แปลว่า worker ฉีดร่างเข้า chat memory ของเซสชันนี้แล้ว
                # (ดู ADR-004) — ตั้ง flag ไว้โชว์ banner แจ้งเตือนในโหมดถาม-ตอบปกติด้านล่าง
                if "error" not in draft_result:
                    st.session_state.has_draft_in_session = True
                st.rerun()

    # ── Stage 3: แสดงผลลัพธ์ ────────────────────────────────────────────────
    else:
        if st.button("🔄 ร่างหัวข้อใหม่", key="restart_from_stage3"):
            st.session_state.pop("draft_clarify", None)
            st.session_state.pop("draft_result", None)
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
# แสดงเฉพาะตอนอยู่ในโหมดถาม-ตอบปกติ (ไม่ใช่โหมดร่างเอกสาร ที่ st.stop() ตัดจบไปแล้วด้านบน)
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
