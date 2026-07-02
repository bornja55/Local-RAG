"""
test_rag_pipeline.py — สคริปต์ทดสอบเดียวที่ครอบคลุมทั้ง pipeline (แทนที่ test_*.py เดิมทั้งหมด
ที่สร้างขึ้นระหว่างการ debug ปัญหา Windows access violation / WINHTTP.dll crash)

ทดสอบ end-to-end ผ่าน HTTP เหมือนที่ app.py ใช้จริง:
    1. worker (rag_worker.py) ต้อง start ได้ และ /health ต้องกลับมาเป็น "ready" ภายในเวลาที่กำหนด
    2. ส่งคำถามจริงไป /chat แล้วต้องได้คำตอบที่มีเนื้อหา (ไม่ error)
    3. ถามคำถามที่สองในเซสชันเดียวกัน เพื่อยืนยันว่า chat memory (ประวัติสนทนา) ทำงานข้ามคำถามได้
    4. เรียก /draft/questions แล้วต้องได้ list คำถาม (ดู ADR-002)
    5. เรียก /draft พร้อม session_id เดียวกับข้อ 2-3 แล้วต้องได้ draft_markdown + scrutiny (ดู ADR-001/ADR-002)
    6. ถามในแชทปกติ (session เดียวกับข้อ 5) เกี่ยวกับร่างที่เพิ่งสร้าง ต้องได้คำตอบไม่ error
       (ยืนยันว่า chat เห็นร่างในเซสชันเดียวกันได้ — ดู ADR-004)

วิธีรัน:
    venv\\Scripts\\python.exe test_rag_pipeline.py

ไม่ต้องรัน rag_worker.py หรือ Streamlit เองก่อน — สคริปต์นี้ auto-start worker ให้เหมือน app.py
(ถ้า worker รันอยู่แล้วจากการเปิด Streamlit ค้างไว้ ก็จะใช้ตัวที่รันอยู่เลย ไม่ต้องรอโหลดใหม่)
"""
import os
import sys
import json
import time
import uuid
import subprocess
import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER_SCRIPT = os.path.join(BASE_DIR, "rag_worker.py")
WORKER_URL = "http://127.0.0.1:8765"

PASS = "✅ PASS"
FAIL = "❌ FAIL"


def check_health():
    try:
        with urllib.request.urlopen(f"{WORKER_URL}/health", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def start_worker_if_needed():
    if check_health() is not None:
        print("[SETUP] worker รันอยู่แล้ว ใช้ตัวที่มีอยู่เลย")
        return
    print("[SETUP] worker ยังไม่รัน กำลัง start...")
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [sys.executable, WORKER_SCRIPT],
        cwd=BASE_DIR,
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def wait_for_ready(max_wait=400) -> bool:
    start = time.time()
    while time.time() - start < max_wait:
        status = check_health()
        elapsed = int(time.time() - start)
        if status is None:
            print(f"[WAIT] {elapsed}s - worker ยังไม่ตอบ...")
        elif status.get("status") == "ready":
            print(f"[WAIT] {elapsed}s - worker พร้อมแล้ว!")
            return True
        elif status.get("status") == "error":
            print(f"[WAIT] worker error: {status.get('detail')}")
            return False
        else:
            print(f"[WAIT] {elapsed}s - {status.get('detail', 'loading...')}")
        time.sleep(2)
    print(f"[WAIT] timeout หลังรอ {max_wait}s")
    return False


def call_chat(session_id: str, prompt: str) -> dict:
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


def call_clarify_questions(topic: str, instructions: str = "") -> dict:
    """เรียก /draft/questions — ขั้นตอนสร้างคำถามเพิ่มเติมก่อนร่าง (ดู ADR-002)"""
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


def call_draft(
    topic: str, instructions: str = "", answers: dict | None = None, session_id: str | None = None
) -> dict:
    """เรียก /draft — ใช้ GEMINI_MODEL_DRAFT และเรียก LLM 2 ครั้งต่อคำขอ (ร่าง + scrutinize)
    จึง timeout ยาวกว่า call_chat()
    session_id: ถ้าส่งไป worker จะฉีดร่าง+scrutiny เข้า chat memory ของ session นั้น (ดู ADR-004)"""
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


def main() -> int:
    results = []

    # 1. Worker เริ่มและพร้อมใช้งาน
    start_worker_if_needed()
    ready = wait_for_ready()
    results.append(("Worker start + health check", ready))
    if not ready:
        print(f"\n{FAIL} Worker ไม่พร้อม — ดู rag_worker.log")
        _print_summary(results)
        return 1

    session_id = str(uuid.uuid4())

    # 2. คำถามแรก — ต้องได้คำตอบจริง ไม่ error
    print("\n[TEST] ถามคำถามแรก...")
    r1 = call_chat(session_id, "มีเอกสารอะไรบ้างในระบบ")
    ok1 = "error" not in r1 and len(r1.get("response", "")) > 0
    results.append(("คำถามแรกได้คำตอบ (ไม่ error)", ok1))
    if ok1:
        print(f"   ตอบ: {r1['response'][:150]}...")
    else:
        print(f"   error: {r1.get('error')}")

    # 3. คำถามที่สอง — ยืนยันว่า memory/session ทำงาน (ไม่ crash ข้าม request)
    print("\n[TEST] ถามคำถามที่สองในเซสชันเดียวกัน (เช็ค memory)...")
    r2 = call_chat(session_id, "อธิบายเพิ่มเติมอีกนิดได้ไหม")
    ok2 = "error" not in r2 and len(r2.get("response", "")) > 0
    results.append(("คำถามที่สองได้คำตอบ (เช็ค session memory)", ok2))
    if ok2:
        print(f"   ตอบ: {r2['response'][:150]}...")
    else:
        print(f"   error: {r2.get('error')}")

    # 4. คำถามเพิ่มเติมก่อนร่าง (ดู ADR-002) — ต้องได้ list คำถามกลับมา ไม่ error
    print("\n[TEST] เรียก /draft/questions ด้วยหัวข้อ 'นโยบายจัดซื้อ'...")
    r3 = call_clarify_questions("นโยบายจัดซื้อ", "ต้องระบุขั้นตอนอนุมัติและวงเงินที่ต้องขออนุมัติเพิ่มเติม")
    ok3 = "error" not in r3 and isinstance(r3.get("questions"), list)
    results.append(("/draft/questions คืน list คำถาม (ไม่ error)", ok3))
    if ok3:
        print(f"   ได้ {len(r3['questions'])} คำถาม เช่น: {r3['questions'][:2]}")
    else:
        print(f"   error: {r3.get('error')}")

    # 5. โหมดร่างเอกสาร + auto-scrutinize (ดู ADR-001/ADR-002) — ส่ง answers บางข้อ ข้ามบางข้อ
    # ไปด้วย เพื่อเช็คว่า flow รวมทั้งสองขั้นทำงานร่วมกันได้ ต้องได้ draft_markdown และ scrutiny
    # กลับมาไม่ว่างเปล่า และไม่มี error key — ส่ง session_id เดียวกับ chat ด้านบนไปด้วย เพื่อทดสอบ
    # ข้อ 6 ต่อ (ดู ADR-004 — chat ในเซสชันเดียวกันต้องอ้างอิงร่างนี้ต่อได้)
    print("\n[TEST] เรียก /draft ด้วยหัวข้อ 'นโยบายจัดซื้อ' พร้อมคำตอบบางส่วน...")
    sample_answers = {q: "" for q in r3.get("questions", [])}
    for i, q in enumerate(sample_answers):
        if i == 0:
            sample_answers[q] = "ทดสอบ: ตอบแค่ข้อแรกข้อเดียว ข้อที่เหลือข้าม"
        break
    r4 = call_draft(
        "นโยบายจัดซื้อ",
        "ต้องระบุขั้นตอนอนุมัติและวงเงินที่ต้องขออนุมัติเพิ่มเติม",
        sample_answers,
        session_id=session_id,
    )
    ok4 = (
        "error" not in r4
        and len(r4.get("draft_markdown", "")) > 0
        and len(r4.get("scrutiny", "")) > 0
    )
    results.append(("/draft คืนทั้ง draft_markdown และ scrutiny (ไม่ error)", ok4))
    if ok4:
        print(f"   draft_markdown: {r4['draft_markdown'][:150]}...")
        print(f"   scrutiny: {r4['scrutiny'][:150]}...")
    else:
        print(f"   error: {r4.get('error')}")

    # 6. Chat ในเซสชันเดียวกันต้องอ้างอิงร่างที่เพิ่งสร้างได้ (ดู ADR-004) — ไม่ตรวจเนื้อหาละเอียด
    # (ผลลัพธ์ LLM ไม่ deterministic) แค่ยืนยันว่าไม่ error และได้คำตอบที่มีเนื้อหาจริง
    print("\n[TEST] ถามในแชทปกติเกี่ยวกับร่างที่เพิ่งสร้าง (เช็คว่า chat เห็นร่างในเซสชัน)...")
    r5 = call_chat(session_id, "จากร่างนโยบายจัดซื้อที่เพิ่งสร้าง มีหัวข้ออะไรบ้าง")
    ok5 = "error" not in r5 and len(r5.get("response", "")) > 0
    results.append(("chat อ้างอิงร่างในเซสชันเดียวกันได้ (ไม่ error)", ok5))
    if ok5:
        print(f"   ตอบ: {r5['response'][:150]}...")
    else:
        print(f"   error: {r5.get('error')}")

    _print_summary(results)
    return 0 if all(ok for _, ok in results) else 1


def _print_summary(results):
    print("\n" + "=" * 50)
    print("สรุปผลทดสอบ")
    print("=" * 50)
    for name, ok in results:
        print(f"{PASS if ok else FAIL}  {name}")
    print("=" * 50)


if __name__ == "__main__":
    sys.exit(main())
