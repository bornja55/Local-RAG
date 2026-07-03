"""
test_fallback_model.py — ทดสอบแบบแยกเดี่ยวว่าโมเดลที่ตั้งใจใช้เป็น fallback (เช่น Gemma 4)
เรียกผ่าน GoogleGenAI ได้จริงหรือไม่ ก่อนจะตั้งเป็น GEMINI_MODEL_CHAT_FALLBACK /
GEMINI_MODEL_DRAFT_FALLBACK ใน .env จริง (ดู ADR-003 — ไม่เคยทดสอบมาก่อนว่า class นี้
รองรับ model string ของ Gemma หรือไม่ ถ้าใช้ไม่ได้ ตอนโควตาโมเดลหลักหมดจริง fallback จะ error
ซ้อน error แทนที่จะช่วยอะไร)

ไม่ผ่าน rag_worker.py ไม่ผ่าน app.py — เรียก LLM ตรงๆ เพื่อแยกปัญหาให้ชัดว่าเป็นเรื่องโมเดล
ไม่ใช่เรื่อง logic อื่นในระบบ

วิธีรัน:
    venv\\Scripts\\python.exe test_fallback_model.py gemma-4-26b-a4b-it
    venv\\Scripts\\python.exe test_fallback_model.py gemma-4-31b-it
    venv\\Scripts\\python.exe test_fallback_model.py                  (ไม่ใส่ argument = ทดสอบ
                                                                         gemini-3.5-flash เป็น baseline)
"""
import os
import sys
import traceback

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

if "GOOGLE_API_KEY" not in os.environ:
    print("ไม่พบ GOOGLE_API_KEY — เช็คว่ามีไฟล์ .env ที่ root โปรเจกต์ และมี GOOGLE_API_KEY=... อยู่ในนั้น")
    sys.exit(1)

model_name = sys.argv[1] if len(sys.argv) > 1 else "gemini-3.5-flash"
print(f"กำลังทดสอบโมเดล: {model_name}")
print("-" * 50)

try:
    from llama_index.llms.google_genai import GoogleGenAI

    llm = GoogleGenAI(model=model_name)
    resp = llm.complete("ตอบคำว่า 'พร้อมใช้งาน' คำเดียวพอ ห้ามตอบอย่างอื่น")
    print("คำตอบที่ได้:")
    print(resp.text)
    print("-" * 50)
    print(f"✅ โมเดล '{model_name}' เรียกผ่าน GoogleGenAI ได้จริง — ตั้งเป็น fallback ใน .env ได้")
except Exception as e:
    print(f"❌ โมเดล '{model_name}' เรียกไม่ได้: {type(e).__name__} - {e}")
    print()
    print(traceback.format_exc())
    print("-" * 50)
    print(f"อย่าเพิ่งตั้ง '{model_name}' เป็นค่า GEMINI_MODEL_CHAT_FALLBACK / GEMINI_MODEL_DRAFT_FALLBACK")
    print("จนกว่าจะแก้ปัญหานี้ได้ก่อน — ลองเช็คชื่อโมเดลให้ตรงกับที่ Google AI Studio ระบุไว้")
    sys.exit(1)
