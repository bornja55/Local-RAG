"""
worker_handlers.py — business logic ของทุก endpoint (แยกออกมาจาก rag_worker.py ตาม
Architecture report High #1) — chat / draft / review / interactive-questions ทั้งหมด
HTTP layer (class Handler) อยู่ที่ rag_worker.py และเรียกฟังก์ชัน _handle_* ในไฟล์นี้เท่านั้น

ข้อควรระวัง: state ที่ rebind ได้ (_index/_reranker/_sys_prompt) ต้องอ้างผ่าน state.X ที่
call time เสมอ (ดูหมายเหตุใน worker_state.py) ส่วน lock/dict อ้างตรงได้เพราะไม่เคย rebind
"""
import llm_fallback
import worker_config as config
import worker_state as state
from worker_parsing import (
    _extract_target_document,
    _parse_bullet_questions,
    _parse_categorized_bullets,
    _split_review_finalize_output,
)
from worker_prompts import (
    _build_clarify_questions_prompt,
    _build_draft_categorized_questions_prompt,
    _build_draft_sys_prompt,
    _build_followup_sys_prompt,
    _build_prefill_sys_prompt,
    _build_review_checklist_prompt,
    _build_review_finalize_prompt,
    _build_scrutinize_sys_prompt,
)
from worker_retrieval import _retrieve_context, _retrieve_context_scoped

log = state.log
# หมายเหตุ (2026-07-05): _is_quota_error/_is_fallback_worthy_error เคย alias ไว้ที่นี่สำหรับ
# _handle_chat ใช้เอง — ตอนนี้ _handle_chat เรียก llm_fallback.run_with_fallback() ตรงๆ แทน (ตัดสิน
# ใจ retry/fallback อยู่ในนั้นแล้ว) จึงไม่มีจุดไหนในไฟล์นี้ต้องใช้ 2 ฟังก์ชันนี้ตรงๆ อีก ลบ alias ออก


def _build_llm(model: str):
    """wrapper ของ llm_fallback.build_llm() ที่เติม GEMINI_REQUEST_TIMEOUT_MS จาก config ให้ —
    call site ทุกจุดในไฟล์นี้เรียกด้วย signature เดิม (_build_llm(model)) เหมือนก่อนแยกไฟล์"""
    return llm_fallback.build_llm(model, config.GEMINI_REQUEST_TIMEOUT_MS)


def _complete_with_fallback(
    primary_model: str, fallback_models: list[str], prompt: str, log_prefix: str
) -> tuple[str | None, Exception | None]:
    """wrapper ของ llm_fallback.complete_with_fallback() ที่เติม timeout/log ของ worker ให้ —
    call site ทุกจุดในไฟล์นี้เรียกด้วย signature เดิมเหมือนก่อนแยกไฟล์ (ดู ADR-003)"""
    return llm_fallback.complete_with_fallback(
        primary_model, fallback_models, prompt, log_prefix,
        timeout_ms=config.GEMINI_REQUEST_TIMEOUT_MS, log=state.log,
    )


def _handle_chat(session_id: str, prompt: str) -> dict:
    """หมายเหตุ (2026-07-05, ดู ADR-003 หมายเหตุเพิ่มเติมวันเดียวกัน): เดิม endpoint นี้เขียน
    retry+fallback loop เองแยกต่างหากทั้งหมด (ไม่ผ่าน llm_fallback.complete_with_fallback เพราะใช้
    chat_engine.chat() แทน llm.complete()) ทำให้ตรรกะเดียวกันซ้ำอยู่ 2 ที่ — ที่นี่ไม่มี unit test
    คุ้มเลย (ดู HANDOFF.md "0b" ข้อ 4) ตอนนี้เรียก llm_fallback.run_with_fallback() ตรงๆ แทน (ตัวเดียว
    กับที่ complete_with_fallback ใช้ ผ่าน 25 unit test อยู่แล้ว) เหลือแค่ส่วนที่เป็นของ chat จริงๆ
    (สร้าง chat_engine ผูก memory/retriever) ให้ _handle_chat ทำเอง"""
    from llama_index.core.memory import ChatMemoryBuffer
    from llama_index.core import Settings

    memory = state.sessions.get_or_create(
        session_id, lambda: ChatMemoryBuffer.from_defaults(token_limit=8000)
    )

    def _build_chat_engine(model: str):
        # สร้าง llm + chat_engine ใหม่ทุกครั้งที่เรียก (ยืนยันแล้วจาก diagnostic tests ว่า
        # ปลอดภัยข้าม thread — ไม่มี state ค้างจาก request ก่อนหน้า) — run_with_fallback() เรียก
        # factory นี้ใหม่ทุก attempt รวมตอน retry โมเดลหลักซ้ำด้วย (ไม่ใช่แค่ตอนสลับโมเดลเหมือน
        # โค้ดเดิมก่อนแก้ 2026-07-05) ปลอดภัยตามที่ comment นี้ยืนยันไว้แล้ว
        llm = _build_llm(model)
        Settings.llm = llm
        return state._index.as_chat_engine(
            chat_mode="condense_plus_context",
            memory=memory,
            similarity_top_k=60,
            node_postprocessors=[state._reranker],
            system_prompt=state._sys_prompt,
        )

    log_prefix = f"[CHAT session={session_id[:8]}]"
    log(f"{log_prefix} ถาม: {prompt[:50]}...")
    response_obj, error = llm_fallback.run_with_fallback(
        config.GEMINI_MODEL_CHAT, config.GEMINI_MODEL_CHAT_FALLBACK,
        _build_chat_engine, lambda engine: engine.chat(prompt),
        log_prefix, log=log,
    )

    if response_obj is None:
        return {"error": str(error) if error else "unknown error"}

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
        config.GEMINI_MODEL_CHAT, config.GEMINI_MODEL_CHAT_FALLBACK,
        sys_prompt + "\n\n" + user_msg, "[CLARIFY]",
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

    memory = state.sessions.get_or_create(
        session_id, lambda: ChatMemoryBuffer.from_defaults(token_limit=8000)
    )

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
        config.GEMINI_MODEL_DRAFT, config.GEMINI_MODEL_DRAFT_FALLBACK,
        draft_sys_prompt + "\n\n" + draft_user_msg, "[DRAFT]",
    )
    if draft_text is None:
        return {"error": str(draft_error) if draft_error else "unknown error (draft)"}

    # ── ขั้นที่ 2: scrutinize ร่างที่เพิ่งสร้าง เทียบ context เดิม ──────────
    scrutinize_sys_prompt = _build_scrutinize_sys_prompt() + context_text
    scrutinize_user_msg = f"ร่างนโยบายที่ต้องตรวจ:\n\n{draft_text}"

    log("[SCRUTINIZE] เริ่มตรวจร่าง...")
    scrutiny_text, scrutiny_error = _complete_with_fallback(
        config.GEMINI_MODEL_DRAFT, config.GEMINI_MODEL_DRAFT_FALLBACK,
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
        config.GEMINI_MODEL_DRAFT, config.GEMINI_MODEL_DRAFT_FALLBACK,
        sys_prompt + "\n\n" + user_msg, "[REVIEW-CHECKLIST]",
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
        config.GEMINI_MODEL_DRAFT, config.GEMINI_MODEL_DRAFT_FALLBACK,
        sys_prompt + "\n\n" + user_msg, f"[PREFILL {topic.get('id', '?')}]",
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
        config.GEMINI_MODEL_DRAFT, config.GEMINI_MODEL_DRAFT_FALLBACK,
        sys_prompt + "\n\n" + user_msg, f"[FOLLOWUP {topic.get('id', '?')}]",
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
            config.GEMINI_MODEL_DRAFT, config.GEMINI_MODEL_DRAFT_FALLBACK,
            sys_prompt + "\n\n" + user_msg, "[DRAFT-QUESTIONS-INTERACTIVE]",
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
        config.GEMINI_MODEL_DRAFT, config.GEMINI_MODEL_DRAFT_FALLBACK,
        sys_prompt + "\n\n" + user_msg, "[REVIEW-FINALIZE]",
    )
    if text is None:
        return {"error": str(error) if error else "unknown error (review finalize)"}

    change_report, updated_document = _split_review_finalize_output(text)
    return {
        "change_report_markdown": change_report,
        "updated_document_markdown": updated_document,
        "sources": sources,
    }
