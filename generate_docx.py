"""
generate_docx.py — แปลง markdown ธรรมดา (ที่ได้จากโหมดร่างเอกสาร/scrutinize ใน rag_worker.py)
เป็นไฟล์ .docx แบบโครงสร้างเปล่า (heading/paragraph/bullet) ไม่เลียนแบบ template บริษัทจริง
(ไม่มีตารางเซ็นสั่ง/ทวนสอบ, ประวัติแก้ไข, หัวกระดาษ) — ตามการตัดสินใจใน ADR-001 ข้อ 5

ผู้ใช้คัดลอกเนื้อหาจากไฟล์นี้ไปใส่ template จริงขององค์กรเองก่อนเสนออนุมัติ
"""
import io
import re

from docx import Document
from docx.shared import Pt, RGBColor

# จับทั้ง **bold** ของ markdown และมาร์ก [ต้องระบุ: ...] (ดู ADR-002) ในรอบเดียว เพื่อรักษาลำดับ
# ตำแหน่งข้อความให้ถูกต้องเวลาทั้งสองแบบอยู่ในบรรทัดเดียวกัน
_INLINE_RE = re.compile(r"\*\*(?P<bold>.+?)\*\*|(?P<marker>\[ต้องระบุ:[^\]]*\])")


def _add_markdown_line(doc: Document, line: str) -> None:
    """เพิ่มหนึ่งบรรทัด markdown ลงในเอกสาร — รองรับ heading (#/##/###), bullet (-/*),
    และ inline **bold** ภายในย่อหน้าธรรมดา/bullet เท่านั้น (ไม่รองรับ markdown ซับซ้อนกว่านี้)"""
    stripped = line.rstrip()

    if not stripped.strip():
        doc.add_paragraph("")
        return

    heading_match = re.match(r"^(#{1,3})\s+(.*)$", stripped)
    if heading_match:
        level = len(heading_match.group(1))
        text = heading_match.group(2).strip()
        doc.add_heading(text, level=level)
        return

    bullet_match = re.match(r"^[-*]\s+(.*)$", stripped)
    if bullet_match:
        para = doc.add_paragraph(style="List Bullet")
        _add_runs_with_bold(para, bullet_match.group(1))
        return

    para = doc.add_paragraph()
    _add_runs_with_bold(para, stripped)


def _add_runs_with_bold(paragraph, text: str) -> None:
    """แตก text เป็น run ปกติ/ตัวหนา ตามเครื่องหมาย **...** ของ markdown และทำตัวหนา+สีแดง
    ให้กับมาร์ก [ต้องระบุ: ...] (ดู ADR-002) เพื่อให้ผู้ตรวจร่างเห็นจุดที่ยังขาดข้อมูลชัดเจน
    เปิดไฟล์ Word แล้วไม่กลืนไปกับข้อความปกติ"""
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos:m.start()])
        if m.group("bold") is not None:
            run = paragraph.add_run(m.group("bold"))
            run.bold = True
        else:
            run = paragraph.add_run(m.group("marker"))
            run.bold = True
            run.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:])


def markdown_to_docx(markdown_text: str, title: str) -> bytes:
    """แปลง markdown_text เป็นไฟล์ .docx (คืนค่าเป็น bytes พร้อมส่งผ่าน st.download_button)
    title จะถูกใส่เป็น Heading 0 (Title style) อยู่บนสุดของเอกสาร"""
    doc = Document()

    style = doc.styles["Normal"]
    style.font.name = "TH Sarabun New"
    style.font.size = Pt(14)

    if title:
        doc.add_heading(title, level=0)

    for line in markdown_text.splitlines():
        _add_markdown_line(doc, line)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
