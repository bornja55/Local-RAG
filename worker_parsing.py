"""
worker_parsing.py — parsing/extraction ทั้งหมดของ RAG worker (แยกออกมาจาก rag_worker.py ตาม
Architecture report High #1) — parse ผลลัพธ์จาก LLM (bullet/categorized bullet), parse โครงสร้าง
heading ของเอกสารเป้าหมาย (markdown/.docx — Target document access ของ ADR-006), และแยกผลลัพธ์
2 ส่วนของ /review/finalize ทุกฟังก์ชันเป็น pure function (ยกเว้น _get_document_path/_extract_target_document
ที่อ่านดิสก์) ไม่แตะ state/LLM เลย — unit test ได้โดยไม่ต้องโหลดโมเดล
"""
import base64
import io
import os
import re

from worker_config import DATA_DIRS
from worker_prompts import REVIEW_FINALIZE_SENTINEL


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


# ── Review Topic generation (ADR-006/ADR-007) — parser ฝั่งผลลัพธ์ LLM ─────────

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


def _split_review_finalize_output(text: str) -> tuple[str, str]:
    """แยกผลลัพธ์จาก _build_review_finalize_prompt() เป็น (change_report_markdown, updated_document_markdown)
    โดยหาบรรทัดตัวคั่น REVIEW_FINALIZE_SENTINEL — ถ้าไม่เจอ (LLM ไม่ทำตามรูปแบบที่ขอ) คืนทั้งก้อนเป็น
    change_report แล้วปล่อย updated_document ว่างเปล่า ให้ผู้ใช้เห็นอย่างน้อยส่วนสรุปการเปลี่ยนแปลง"""
    if REVIEW_FINALIZE_SENTINEL in text:
        before, _, after = text.partition(REVIEW_FINALIZE_SENTINEL)
        return before.strip(), after.strip()
    return text.strip(), ""
