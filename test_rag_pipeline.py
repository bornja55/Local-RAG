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

เทสต์เพิ่มเติมสำหรับ ADR-006 (โหมดรีวิวเอกสาร) / ADR-007 (คำถามเพิ่มเติมแบบ interactive) — เทสต์ใหม่
ทั้งหมด ไม่แก้เทสต์ 6 ข้อด้านบนแม้แต่บรรทัดเดียว (ดู ADR-006 ข้อ 9 / ADR-007 ข้อ 5 — endpoint ใหม่
ต้องมีเทสต์แยก ไม่แตะเทสต์เดิมที่ครอบคลุม /draft*, /chat):
    7. เรียก /review/target ด้วยเอกสารจริงจาก corpus (มี heading ชัดเจน) ต้องได้ review_topics ไม่ว่างเปล่า
    8. เรียก /review/target ด้วยเอกสารที่ไม่มี heading เลย (พรืดเดียว) ต้องถูกปฏิเสธด้วย
       error == "unparseable_headings" (ดู ADR-006 ข้อ 2a)
    9. เรียก /review/topic ด้วยหัวข้อแรกจากข้อ 7 ต้องได้ prefill กลับมาไม่ error
    10. เรียก /draft/questions/interactive ครั้งแรก (ไม่มี review_topics) ต้องได้ review_topics ที่จัด
        หมวดหมู่แล้วกลับมา แล้วเรียกซ้ำแบบขอ prefill ของหัวข้อแรก ต้องไม่ error (ดู ADR-007)

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
import base64
import subprocess
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import worker_config as config  # noqa: E402 — reuse GEMINI_REQUEST_TIMEOUT_MS/fallback lists เดียว
from worker_client import _worst_case_timeout_seconds  # noqa: E402

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


def _post(path: str, payload: dict, timeout: int) -> dict:
    """helper กลาง POST+JSON ใช้ร่วมกันโดยเทสต์ทุกข้อ (เดิม+ใหม่) — ลด try/except ซ้ำ"""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{WORKER_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}


def call_chat(session_id: str, prompt: str) -> dict:
    timeout = _worst_case_timeout_seconds(1, config.GEMINI_MODEL_CHAT_FALLBACK)
    return _post("/chat", {"session_id": session_id, "prompt": prompt}, timeout=timeout)


def call_clarify_questions(topic: str, instructions: str = "") -> dict:
    """เรียก /draft/questions — ขั้นตอนสร้างคำถามเพิ่มเติมก่อนร่าง (ดู ADR-002) ใช้
    GEMINI_MODEL_CHAT(_FALLBACK) เหมือน _handle_clarify_questions จริง"""
    timeout = _worst_case_timeout_seconds(1, config.GEMINI_MODEL_CHAT_FALLBACK)
    return _post("/draft/questions", {"topic": topic, "instructions": instructions}, timeout=timeout)


def call_draft(
    topic: str, instructions: str = "", answers: dict | None = None, session_id: str | None = None
) -> dict:
    """เรียก /draft — ใช้ GEMINI_MODEL_DRAFT และเรียก LLM 2 ครั้งทางตรรกะต่อคำขอ (ร่าง + scrutinize)
    แต่ละครั้งอาจไล่ลอง GEMINI_MODEL_DRAFT_FALLBACK เต็มรายการ (ดู worker_client._worst_case_timeout_seconds
    — ใช้สูตรเดียวกับที่ app.py ใช้จริง กันไม่ให้เทสต์นี้ false-fail จาก timeout ที่แคบเกินไปเหมือนที่เคย
    เจอมาก่อน 2026-07-05)
    session_id: ถ้าส่งไป worker จะฉีดร่าง+scrutiny เข้า chat memory ของ session นั้น (ดู ADR-004)"""
    timeout = _worst_case_timeout_seconds(2, config.GEMINI_MODEL_DRAFT_FALLBACK)
    return _post(
        "/draft",
        {"topic": topic, "instructions": instructions, "answers": answers or {}, "session_id": session_id},
        timeout=timeout,
    )


def call_review_target(source: str, file_name: str, content_base64: str | None = None) -> dict:
    """เรียก /review/target — ดู ADR-006 (รวม LLM call ทางตรรกะ 1 ครั้งสำหรับ checklist-derived topics)"""
    timeout = _worst_case_timeout_seconds(1, config.GEMINI_MODEL_DRAFT_FALLBACK)
    return _post(
        "/review/target",
        {"source": source, "file_name": file_name, "content_base64": content_base64},
        timeout=timeout,
    )


def call_review_topic(
    review_topics: list, confirmed_docs: list, answers: dict, topic_id: str,
    requesting_followup: bool = False,
) -> dict:
    """เรียก /review/topic — ดู ADR-006"""
    timeout = _worst_case_timeout_seconds(1, config.GEMINI_MODEL_DRAFT_FALLBACK)
    return _post(
        "/review/topic",
        {
            "review_topics": review_topics,
            "confirmed_cross_reference_docs": confirmed_docs,
            "answers": answers,
            "topic_id": topic_id,
            "requesting_followup_for_answer": requesting_followup,
        },
        timeout=timeout,
    )


def call_draft_questions_interactive(
    topic: str, instructions: str = "", review_topics: list | None = None,
    answers: dict | None = None, topic_id: str | None = None, requesting_followup: bool = False,
) -> dict:
    """เรียก /draft/questions/interactive — ดู ADR-007 (ไม่แก้ /draft/questions เดิม)"""
    timeout = _worst_case_timeout_seconds(1, config.GEMINI_MODEL_DRAFT_FALLBACK)
    return _post(
        "/draft/questions/interactive",
        {
            "topic": topic,
            "instructions": instructions,
            "review_topics": review_topics or [],
            "answers": answers or {},
            "topic_id": topic_id,
            "requesting_followup_for_answer": requesting_followup,
        },
        timeout=timeout,
    )


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

    # ── เทสต์ใหม่สำหรับ ADR-006 (โหมดรีวิวเอกสาร) — ไม่แตะเทสต์ 1-6 ด้านบนเลย ─────────────────

    # 7. /review/target ด้วยเอกสารจริงจาก corpus ที่มี heading ชัดเจน (ระเบียบปฏิบัติ IT Risk —
    # ใช้เป็น worked example ตามที่ HANDOFF.md ระบุว่ายังขาดตัวอย่าง 1 อันมา validate) ต้องได้
    # review_topics ไม่ว่างเปล่า
    print("\n[TEST] เรียก /review/target ด้วยเอกสารระเบียบปฏิบัติ IT Risk จาก corpus...")
    it_risk_file = "24CS-IT-QP-001_การบริหารความเสี่ยงด้านเทคโนโลยีสารสนเทศ.md"
    r6 = call_review_target("corpus", it_risk_file)
    ok6 = "error" not in r6 and len(r6.get("review_topics", [])) > 0
    results.append(("/review/target คืน review_topics ไม่ว่างเปล่า (ไม่ error)", ok6))
    if ok6:
        print(f"   ได้ {len(r6['review_topics'])} หัวข้อรีวิว, "
              f"เอกสารที่เกี่ยวข้องแนะนำ {len(r6.get('suggested_cross_reference_docs', []))} ฉบับ")
    else:
        print(f"   error/message: {r6.get('message') or r6.get('error')}")

    # 8. /review/target ด้วยเอกสารพรืดเดียวไม่มี heading เลย — ต้องถูกปฏิเสธด้วย
    # error == "unparseable_headings" (ดู ADR-006 ข้อ 2a — ปฏิเสธชัดเจน ไม่ fallback เงียบๆ)
    print("\n[TEST] เรียก /review/target ด้วยเอกสารที่ไม่มี heading เลย (ต้องถูกปฏิเสธ)...")
    prose_only = "เอกสารนี้เขียนเป็นพรืดเดียวไม่มีหัวข้อใดๆ เลย " * 5
    prose_b64 = base64.b64encode(prose_only.encode("utf-8")).decode("ascii")
    r7 = call_review_target("upload", "เอกสารทดสอบไม่มีหัวข้อ.md", prose_b64)
    ok7 = r7.get("error") == "unparseable_headings"
    results.append(("/review/target ปฏิเสธเอกสารไม่มี heading ถูกต้อง (unparseable_headings)", ok7))
    if ok7:
        print(f"   ปฏิเสธถูกต้อง: {r7.get('message', '')[:100]}...")
    else:
        print(f"   ผลลัพธ์ไม่ตรงคาด: {r7}")

    # 9. /review/topic ด้วยหัวข้อแรกจากข้อ 7 (ถ้าข้อ 7 สำเร็จ) — ต้องได้ prefill กลับมาไม่ error
    print("\n[TEST] เรียก /review/topic ขอ prefill ของหัวข้อแรก...")
    if ok6 and r6.get("review_topics"):
        first_topic_id = r6["review_topics"][0]["id"]
        confirmed_docs = r6.get("suggested_cross_reference_docs", [])
        r8 = call_review_topic(r6["review_topics"], confirmed_docs, {}, first_topic_id)
        ok8 = "error" not in r8 and "prefill" in r8
        results.append(("/review/topic คืน prefill (ไม่ error)", ok8))
        if ok8:
            print(f"   prefill: {str(r8.get('prefill', ''))[:150]}...")
        else:
            print(f"   error: {r8.get('error')}")
    else:
        results.append(("/review/topic คืน prefill (ไม่ error)", False))
        print("   ข้าม — ทดสอบข้อ 7 ไม่สำเร็จ ไม่มี review_topics ให้ใช้")

    # ── เทสต์ใหม่สำหรับ ADR-007 (คำถามเพิ่มเติมแบบ interactive) — ไม่แก้ /draft/questions เดิม ────

    # 10. /draft/questions/interactive ครั้งแรก (ไม่มี review_topics) ต้องได้ review_topics ที่จัด
    # หมวดหมู่แล้วกลับมา แล้วเรียกซ้ำแบบขอ prefill ของหัวข้อแรก ต้องไม่ error (ดู ADR-007)
    print("\n[TEST] เรียก /draft/questions/interactive ครั้งแรก (หัวข้อ 'นโยบาย PDPA')...")
    r9 = call_draft_questions_interactive("นโยบาย PDPA", "ต้องระบุประเภทข้อมูลส่วนบุคคลที่เก็บและ DPO")
    ok9 = "error" not in r9 and len(r9.get("review_topics", [])) > 0
    results.append(("/draft/questions/interactive ครั้งแรกคืน review_topics ไม่ว่างเปล่า", ok9))
    if ok9:
        print(f"   ได้ {len(r9['review_topics'])} คำถามจัดหมวดหมู่ เช่น: "
              f"{[t['heading'] for t in r9['review_topics'][:2]]}")

        first_q = r9["review_topics"][0]
        r10 = call_draft_questions_interactive(
            "นโยบาย PDPA", "ต้องระบุประเภทข้อมูลส่วนบุคคลที่เก็บและ DPO",
            review_topics=r9["review_topics"], topic_id=first_q["id"],
        )
        ok10 = "error" not in r10 and "prefill" in r10
        results.append(("/draft/questions/interactive คืน prefill ของหัวข้อแรก (ไม่ error)", ok10))
        if ok10:
            print(f"   prefill: {str(r10.get('prefill', ''))[:150]}...")
        else:
            print(f"   error: {r10.get('error')}")
    else:
        print(f"   error: {r9.get('error')}")
        results.append(("/draft/questions/interactive คืน prefill ของหัวข้อแรก (ไม่ error)", False))

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
