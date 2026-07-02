import os
import sys

# ป้องกันปัญหาภาษาไทยบน Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    
from llama_index.core import VectorStoreIndex, SimpleDirectoryReader, Settings, StorageContext
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
import torch
import faiss
from llama_index.vector_stores.faiss import FaissVectorStore

# ปิด Warning ที่ไม่จำเป็น
if hasattr(sys.stderr, 'flush'):
    _original_flush = sys.stderr.flush
    def _safe_flush():
        try:
            _original_flush()
        except OSError:
            pass
    sys.stderr.flush = _safe_flush  
os.environ["TOKENIZERS_PARALLELISM"] = "false"
# ปล่อยให้มันโชว์ Progress Bar ได้
# os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

# 1. ตั้งค่า Path แบบ Relative ตามที่ขอ
persist_dir = "./storage"
data_dirs = [
    "./Policies",
    "./Procedures",
    "./Manuals",
    "./Forms"
]

print("กำลังโหลด Embedding Model (BAAI/bge-m3)...")
# โหลดโมเดลด้วย Local Path
embed_model = HuggingFaceEmbedding(model_name="./models/bge-m3", model_kwargs={"torch_dtype": torch.float16, "use_safetensors": True})  
Settings.embed_model = embed_model
Settings.chunk_size = 400
Settings.chunk_overlap = 40
Settings.llm = None 

print("กำลังดึงรายชื่อไฟล์และอ่านเนื้อหาทั้งหมด... (ขั้นตอนนี้ขึ้นอยู่กับความเร็วฮาร์ดดิสก์)")
documents = []
for d in data_dirs:
    if os.path.exists(d):
        print(f"กำลังอ่านไฟล์จากโฟลเดอร์: {os.path.basename(d)} ...")
        docs = SimpleDirectoryReader(d).load_data()
        documents.extend(docs)

if not documents:
    print("ไม่พบเอกสารใดๆ กรุณาตรวจสอบโฟลเดอร์")
    sys.exit()

print(f"อ่านไฟล์สำเร็จ รวมทั้งหมด {len(documents)} ชิ้น")
print("กำลังประมวลผลและสร้าง Vector Store Index... (ขั้นตอนนี้จะใช้เวลาประมาณ 4-5 นาทีตามความเร็ว GPU)")

# เตรียม FAISS Index
faiss_index = faiss.IndexFlatIP(1024)
vector_store = FaissVectorStore(faiss_index=faiss_index)
storage_context = StorageContext.from_defaults(vector_store=vector_store)

# เอา insert_batch_size=1000000 ออกแล้ว ปล่อยให้มันทำงานทีละ 2048 ท่อนตามค่าเริ่มต้น (จะได้ไม่ค้าง)
index = VectorStoreIndex.from_documents(documents, storage_context=storage_context, show_progress=True)

print("กำลังแพ็กข้อมูลลงฮาร์ดดิสก์ (ขั้นตอนนี้อาจจะดูนิ่งไปประมาณ 1-3 นาที กรุณารอสักครู่)...")
index.storage_context.persist(persist_dir=persist_dir)

print("==================================================")
print("สร้าง Index สำเร็จและบันทึกลงโฟลเดอร์ storage เรียบร้อย!")
print("ตอนนี้สามารถเปิดรัน Streamlit app.py ได้ตามปกติแล้วครับ")
print("==================================================")
