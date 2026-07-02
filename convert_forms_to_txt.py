import os
import pandas as pd
import docx2txt

INPUT_DIR = r"D:\Review Policy\As is Policy\4. แบบฟอร์ม"
OUTPUT_DIR = r"D:\Review Policy\Local  RAG\Forms"

if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

def process_files():
    for root, dirs, files in os.walk(INPUT_DIR):
        for file in files:
            if file.startswith("~$"): continue
            
            file_path = os.path.join(root, file)
            file_name = os.path.splitext(file)[0]
            ext = os.path.splitext(file)[1].lower()
            
            out_path = os.path.join(OUTPUT_DIR, f"{file_name}.txt")
            if os.path.exists(out_path):
                continue
                
            raw_text = ""
            try:
                if ext in ['.docx']:
                    print(f"Reading DOCX: {file}")
                    raw_text = docx2txt.process(file_path)
                elif ext in ['.xlsx']:
                    print(f"Reading XLSX: {file}")
                    xls = pd.ExcelFile(file_path)
                    for sheet_name in xls.sheet_names:
                        df = pd.read_excel(xls, sheet_name=sheet_name)
                        raw_text += f"\n--- Sheet: {sheet_name} ---\n"
                        raw_text += df.to_string(index=False) + "\n"
                
                if raw_text and raw_text.strip():
                    with open(out_path, "w", encoding="utf-8") as f:
                        f.write(raw_text)
                    print(f"Saved -> {out_path}")
            except Exception as e:
                print(f"Error processing {file}: {e}")

if __name__ == "__main__":
    print("Starting Form Conversion...")
    process_files()
    print("Conversion Completed!")
