import os
import pandas as pd
from docx import Document
from google import genai
from google.genai import types

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path: str) -> None:
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

# กำหนดโฟลเดอร์
INPUT_DIR = r"D:\Review Policy\As is Policy\4. แบบฟอร์ม"
OUTPUT_DIR = os.path.join(BASE_DIR, "Forms")

# สร้างโฟลเดอร์ Output ถ้ายังไม่มี
if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

# Initialize Gemini Client
API_KEY = os.environ.get("GOOGLE_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "ไม่พบ GOOGLE_API_KEY — สร้างไฟล์ .env ที่ root โปรเจกต์ (ดู .env.example) "
        "แล้วใส่ GOOGLE_API_KEY=<คีย์จริงของคุณ>"
    )
client = genai.Client(api_key=API_KEY)

prompt_template = """
คุณคือผู้เชี่ยวชาญด้านการวิเคราะห์เอกสารแบบฟอร์มองค์กร
จงอ่านข้อมูลดิบจากแบบฟอร์มต่อไปนี้ และสกัดข้อมูลออกมาเป็นรูปแบบ Markdown ตามโครงสร้างที่กำหนดเท่านั้น
ถ้าส่วนไหนไม่มีข้อมูล ให้เขียนว่า "ไม่ระบุ"

โครงสร้างที่ต้องการ:
# [รหัสแบบฟอร์ม (ถ้ามี)] [ชื่อแบบฟอร์ม]

**วัตถุประสงค์การใช้งาน:**
[สรุปสั้นๆ ว่าฟอร์มนี้ใช้ทำอะไร]

**ข้อมูลที่ต้องกรอก (Required Fields):**
- [รายการที่ 1]
- [รายการที่ 2]
...

**สายการอนุมัติ (Approval Workflow):**
- [ตำแหน่งผู้เซ็นที่ 1]
- [ตำแหน่งผู้เซ็นที่ 2]
...

**เงื่อนไขและหมายเหตุ (Conditions & Remarks):**
- [เงื่อนไขที่ 1]
- [เงื่อนไขที่ 2]
...

---
ข้อมูลดิบจากแบบฟอร์ม:
{raw_text}
"""

def extract_docx(file_path):
    try:
        doc = Document(file_path)
        return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
    except Exception as e:
        print(f"Error reading DOCX {file_path}: {e}")
        return ""

def extract_xlsx(file_path):
    try:
        xls = pd.ExcelFile(file_path)
        text = ""
        for sheet_name in xls.sheet_names:
            df = pd.read_excel(xls, sheet_name=sheet_name)
            text += f"--- Sheet: {sheet_name} ---\n"
            text += df.to_string(index=False) + "\n"
        return text
    except Exception as e:
        print(f"Error reading XLSX {file_path}: {e}")
        return ""

def process_files():
    for root, dirs, files in os.walk(INPUT_DIR):
        for file in files:
            file_path = os.path.join(root, file)
            file_name = os.path.splitext(file)[0]
            ext = os.path.splitext(file)[1].lower()
            
            # ข้ามไฟล์ที่ซ่อนอยู่หรือชั่วคราว
            if file.startswith("~$"): continue
            
            raw_text = ""
            if ext in ['.docx']:
                print(f"Reading: {file_path}")
                raw_text = extract_docx(file_path)
            elif ext in ['.xlsx']:
                print(f"Reading: {file_path}")
                raw_text = extract_xlsx(file_path)
            
            if raw_text.strip():
                print(f"Processing with Gemini: {file}")
                try:
                    response = client.models.generate_content(
                        model='gemini-3.1-flash-lite',
                        contents=prompt_template.format(raw_text=raw_text)
                    )
                    
                    # บันทึกไฟล์
                    out_path = os.path.join(OUTPUT_DIR, f"{file_name}.md")
                    with open(out_path, "w", encoding="utf-8") as f:
                        f.write(response.text)
                    print(f"Saved -> {out_path}\n")
                except Exception as e:
                    print(f"Gemini API Error for {file}: {e}")
            else:
                if ext in ['.docx', '.xlsx']:
                    print(f"Skipped {file} (No text extracted)")

if __name__ == "__main__":
    print("Starting Form Extraction Process...")
    process_files()
    print("Extraction Completed!")
