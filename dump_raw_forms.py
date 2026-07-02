import os
import pandas as pd
from docx import Document

INPUT_DIR = r"D:\Review Policy\As is Policy\4. แบบฟอร์ม"
RAW_OUTPUT_DIR = r"D:\Review Policy\Local  RAG\Forms_Raw"

if not os.path.exists(RAW_OUTPUT_DIR):
    os.makedirs(RAW_OUTPUT_DIR)

def extract_docx(file_path):
    try:
        doc = Document(file_path)
        return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
    except Exception as e:
        return f"Error: {e}"

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
        return f"Error: {e}"

for root, dirs, files in os.walk(INPUT_DIR):
    for file in files:
        if file.startswith("~$"): continue
        file_path = os.path.join(root, file)
        file_name = os.path.splitext(file)[0]
        ext = os.path.splitext(file)[1].lower()
        
        raw_text = ""
        if ext == '.docx':
            raw_text = extract_docx(file_path)
        elif ext == '.xlsx':
            raw_text = extract_xlsx(file_path)
            
        if raw_text:
            out_path = os.path.join(RAW_OUTPUT_DIR, f"{file_name}.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(raw_text)
            print(f"Dumped: {file_name}.txt")
