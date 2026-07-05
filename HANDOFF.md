# Handoff — Policy RAG Assistant (2026-07-03, ปรับปรุงล่าสุด 2026-07-05)

## 0d. Update ล่าสุดสุด (2026-07-05 — ปิด gap `_handle_chat` ไม่มี unit test คุ้ม) ← อ่านส่วนนี้ก่อน

item #4 ใน "0b" ด้านล่าง (และที่ "0c" เคยย้ำว่ายังไม่ resolved) **แก้เสร็จแล้ว** — ดู ADR-003
หมายเหตุเพิ่มเติม 2026-07-05 (ส่วนล่างสุด) สำหรับรายละเอียดเต็ม สรุปสั้น:

- แยกตรรกะ retry+fallback ออกมาเป็น `llm_fallback.run_with_fallback()` กลาง ไม่รู้จักรูปแบบ
  prompt/response เลย (รับแค่ `factory(model)`/`call(obj)`) — `complete_with_fallback()` เดิม
  กลายเป็น wrapper บางๆ ของฟังก์ชันนี้ (25 unit test เดิมผ่านหมด ไม่แก้แม้แต่บรรทัดเดียว) และ
  `_handle_chat()` เปลี่ยนมาเรียกฟังก์ชันนี้ตรงๆ แทนการเขียน loop เองแยกต่างหาก
- เพิ่มเทสต์ 2 ชุด: `test_llm_fallback.py::TestRunWithFallbackGenericCallShape` (4 เทส) +
  ไฟล์ใหม่ `test_handle_chat_fallback.py` (5 เทส, stub `llama_index` ผ่าน `sys.modules` ไม่ต้องมี
  API key/โหลดโมเดล) — ครอบคลุม primary สำเร็จ, quota retry+backoff แล้ว fallback, timeout
  fallback ทันทีไม่ retry ซ้ำ (regression ของบั๊ก ADR-003 เดิม), ทุกโมเดล fail, ไม่มี fallback ตั้งไว้
- ระหว่างเขียนเทสพบบั๊ก 2 จุด (ทั้งคู่เป็นบั๊กใน test code เท่านั้น ไม่ใช่ production code) และแก้แล้ว:
  (1) `test_llm_fallback.py::_FakeChatEngine.chat()` wrap ผลลัพธ์ซ้ำสองชั้นโดยไม่ตั้งใจ
  (2) `llm_fallback.run_with_fallback()`'s `sleep=time.sleep` เป็น default ที่ freeze ไว้ตอน import
  ทำให้ monkeypatch `time.sleep` ในเทสไม่มีผล (เทสรอ backoff จริง 10s+20s) — เปลี่ยนเป็น
  `sleep=None` + resolve แบบ lazy ข้างในฟังก์ชันแทน พฤติกรรมจริงไม่เปลี่ยน
- verify ผ่าน py_compile ทุกไฟล์ที่เกี่ยวข้อง (`worker_client.py`, `worker_handlers.py`,
  `llm_fallback.py`, `test_rag_pipeline.py`, `rag_worker.py`, `worker_config.py`, `worker_state.py`,
  `worker_parsing.py`, `worker_prompts.py`, `worker_retrieval.py`, `app.py`,
  `test_llm_fallback.py`, `test_handle_chat_fallback.py`, `test_session_store.py`) +
  `test_llm_fallback.py` 29/29 PASS + `test_handle_chat_fallback.py` 5/5 PASS +
  `test_session_store.py` 5/5 PASS (regression, ไฟล์นี้ไม่ถูกแตะเลย)
- ยังไม่ได้รัน `test_rag_pipeline.py` จริงบน Windows หลังการ refactor รอบนี้ (เหมือนที่ "0c" เคย
  ทิ้งไว้เป็นงานค้าง) — ควรรันซ้ำเพื่อยืนยัน end-to-end จริงอีกครั้งก่อน commit

---

## 0b. Update ล่าสุดสุด (2026-07-05 — live test บนเครื่องจริงยืนยันผ่านแล้ว)

ผู้ใช้รัน `venv\Scripts\python.exe test_rag_pipeline.py` บนเครื่องจริง (Windows) — **ผล 11/11 PASS
ทั้งหมด** รวมถึง `/review/target` กับเอกสาร IT Risk จริงจาก corpus (ได้ 23 หัวข้อรีวิว) ที่เคย
timeout ตอนทดสอบครั้งก่อน (ดู "0. Update" ข้อ 1 ด้านล่าง) — **ยืนยันแล้วว่า `GEMINI_REQUEST_TIMEOUT_MS`
fix ใช้งานได้จริง ไม่ใช่แค่ static verify**

worker ที่ test เรียกใช้คือโค้ดปัจจุบันในโฟลเดอร์ (หลัง architecture refactor ใน "0a" ด้านล่าง — ยัง
ไม่ commit ตอนรันเทสนี้) ดังนั้น run นี้ **ยืนยัน live test ของ "0a" ไปด้วยในตัว** (worker start +
ทุก endpoint ทำงานถูกต้องหลัง split module) แม้จะไม่ได้แยกรัน `test_llm_fallback.py`/
`test_session_store.py` บน Windows ตรงๆ (สองไฟล์นี้ผ่าน sandbox แล้ว และ E2E นี้ครอบคลุมการทำงาน
ร่วมกันจริงของทุก module ที่ทั้งสองไฟล์ทดสอบแยกส่วน)

### Resolved จากการรันนี้
- ✅ ADR-006/ADR-007 timeout bug (`GEMINI_REQUEST_TIMEOUT_MS`) — ยืนยันแก้ได้จริง
- ✅ Architecture refactor 6 ข้อใน "0a" — worker start + ทุก endpoint ทำงานถูกต้องหลัง split module

### ยังไม่ resolved / ต้องทำต่อ
1. **โค้ด architecture refactor (`worker_*.py` ใหม่ทั้งหมด + `rag_worker.py`/`app.py` ที่แก้) ยังไม่
   commit** — ผ่าน live test แล้ว ควร `git add`/commit ทั้งชุดตาม "Key Files" ใน "0a"
2. **แก้ไขข้อความที่ผิดในเอกสารนี้เอง**: ส่วน "0" ข้อ 6 ด้านล่างเคยอ้างว่า `generate_docx.py` fix
   "ยังไม่ได้ commit" — **ไม่จริง**, เช็คกับ `git log` แล้วพบว่า commit ไปแล้วตั้งแต่ `e5cd6f3`/`83bd90b`
   (ก่อน commit ADR-006/007 เสียอีก) ไม่ต้องทำอะไรกับไฟล์นั้นซ้ำ (ดูหมายเหตุแก้ไขในข้อ 6 ด้านล่างด้วย)
3. **5-model fallback chain ไม่เคยถูกบันทึกไว้ในเอกสารนี้เลย** จนถึงตอนนี้ — `GEMINI_MODEL_CHAT_FALLBACK`/
   `GEMINI_MODEL_DRAFT_FALLBACK` เปลี่ยนจากโมเดลเดียวเป็น comma-separated list (4 โมเดล ไล่ทีละตัว)
   ตั้งแต่ commit `fb9abeb` (`_parse_model_chain()` ใน `worker_config.py`) ตัวอย่างค่าจริงใน
   `.env.example`: `GEMINI_MODEL_CHAT_FALLBACK=gemma-4-26b-a4b-it,gemini-2.5-flash-lite,gemini-3-flash-preview,gemini-2.5-flash`
   — ควรเพิ่มคำอธิบายลง README ให้คนอ่านเข้าใจ (ตอนนี้อธิบายแค่ใน code comment)
4. ~~**`/chat` (`_handle_chat` ใน `worker_handlers.py`) มี retry+fallback logic แยกเป็นของตัวเอง ไม่ผ่าน
   `_complete_with_fallback`/`llm_fallback.complete_with_fallback` ที่มี 25 unit tests คุ้ม**~~
   **resolved แล้ว (2026-07-05) — ดู "0d" ด้านบนสุด**: refactor ให้ `_handle_chat` เรียก
   `llm_fallback.run_with_fallback()` (ตรรกะกลางเดียวกับ `complete_with_fallback`) ตรงๆ แทน พร้อม
   เพิ่ม unit test เฉพาะเส้นทางนี้ครบแล้ว (`test_llm_fallback.py::TestRunWithFallbackGenericCallShape`
   + `test_handle_chat_fallback.py` ใหม่ทั้งไฟล์)

---

## 0c. Update (2026-07-05 — `/scrutinize` + `debug-mantra` session: client timeout ไม่เคยครอบคลุม fallback chain)

พบระหว่างรัน `/scrutinize` แล้วต่อด้วย `/anthropic-skills:debug-mantra` เพื่อ reproduce จริง (ไม่ใช่แค่
อ่านโค้ดเดา) — ดู ADR-003 หมายเหตุเพิ่มเติม 2026-07-05 สำหรับรายละเอียดเต็ม สรุปสั้น:

- ทุก `_call_worker_*` ใน `worker_client.py` + `test_rag_pipeline.py` ตั้ง client timeout จากแค่
  "จำนวน LLM call ทางตรรกะ x `GEMINI_REQUEST_TIMEOUT_MS`" โดยไม่คูณด้วยจำนวนโมเดลใน fallback chain
  — reproduce ด้วย dependency injection ยืนยันว่า worst-case จริงสูงกว่าที่ comment เดิมคำนวณไว้มาก
  โดยเฉพาะ `_call_worker_chat()` ที่ timeout ค้างที่ 120s มาตั้งแต่ก่อนมี fallback chain เลย
- **แก้แล้ว**: เพิ่ม `worker_client._worst_case_timeout_seconds()` คำนวณจาก config ตรงๆ แทน magic
  number ใช้แทนทุกจุด (รวม `test_rag_pipeline.py`) — `/chat` จาก 120s → คำนวณอัตโนมัติ (~1530s ด้วย
  ค่า `.env.example` ปัจจุบัน), `/draft` จาก 660s → ~3030s, endpoint อื่นๆ จาก 360-420s → ~1530s
- **แก้เพิ่ม**: log message ใน fallback loop ทั้ง 2 จุด (`llm_fallback.py`, `worker_handlers.py`)
  เคย hardcode ชื่อโมเดลหลัก + "ชนโควตา" ผิดตั้งแต่ fallback ตัวที่ 2 เป็นต้นไป (และผิดถ้า error ไม่ใช่
  quota) — แก้ให้ track โมเดล/ประเภท error ที่เพิ่ง fail จริงแทนแล้ว
- verify ผ่าน py_compile ทุกไฟล์ + `test_llm_fallback.py` (25/25) + `test_session_store.py` (5/5) +
  repro script ยืนยันตัวเลข timeout ใหม่ตรงตามสูตร — **ยังไม่ได้รัน `test_rag_pipeline.py` จริงบน
  Windows หลังแก้รอบนี้ ควรรันซ้ำเพื่อยืนยัน (เหมือนที่ "0b" เคยทำหลัง architecture refactor)**
- เพิ่มคำอธิบายเรื่อง worst-case wait time ลง README.md/README_EN.md แล้ว (อธิบายว่าเจอบ่อยเฉพาะ
  free tier ไม่ใช่บั๊ก) — **ยังไม่ได้เพิ่มใน README_MANAGEMENT.md/README_MANAGEMENT_EN.md** (แถวตาราง
  ความเสี่ยง "คำขอไปยัง AI ค้างไม่ตอบสนอง" อาจต้องปรับคำอธิบายให้ตรงกับพฤติกรรมใหม่นี้ด้วย — ยังไม่ทำ)
- item #4 ใน "0b" ด้านล่าง (ไม่มี unit test คุ้ม `_handle_chat` fallback loop) — เป็นคนละปัญหากับที่
  แก้รอบนี้ (test coverage vs. client timeout math) **resolved แล้วในรอบถัดมาวันเดียวกัน — ดู "0d"
  ด้านบนสุด**

---

## 0a. Update (2026-07-03 — Architecture refactor session)

### Goal

Execute ทั้ง 6 issues จาก `architecture_report.html` (ผลของ /improve-codebase-architecture):
แยก god-module `rag_worker.py`, ทำ fallback logic ให้ unit-test ได้, dedupe ready-check,
แยก worker-client infra ออกจาก Streamlit UI, ห่อ session state — **ทั้งหมดเป็น pure move
ไม่เปลี่ยนพฤติกรรม**

### Current State — ทั้ง 6 ข้อ DONE (verify ใน sandbox ผ่านแล้ว, **live test บนเครื่องจริงยืนยันผ่านแล้ว
2026-07-05 — ดู "0b" ด้านบน**)

**รอบ 1 (High #1 + #2):** แยก `rag_worker.py` (1,596 บรรทัด) เป็น flat modules
(**ตั้งใจไม่ทำเป็น package `rag_worker/`** — app.py launch `rag_worker.py` เป็น WORKER_SCRIPT
ผ่าน subprocess ชื่อ entrypoint ต้องคงเดิม):

- `rag_worker.py` (~430 บรรทัด) — entrypoint: API-key check, `import faiss`, `_load_everything()`,
  `_cleanup_idle_sessions()`, `class Handler`, `main()`
- `worker_config.py` — .env loading + OMP/HF env side effects + ค่าคงที่ทั้งหมด
  (**ตั้งใจไม่เช็ค GOOGLE_API_KEY ที่นี่** เพื่อให้ test import ได้ — การ raise ย้ายไปต้น rag_worker.py
  พฤติกรรมตอน start เหมือนเดิม)
- `worker_state.py` — `log()`, `_state_lock`/`_status`/`_index`/`_reranker`/`_sys_prompt`,
  `SessionStore` + singleton `sessions`
- `worker_prompts.py` — prompt builders ทั้ง 9 + `_get_available_documents` +
  `REVIEW_FINALIZE_SENTINEL` (rename จาก `_REVIEW_FINALIZE_SENTINEL`)
- `worker_parsing.py` — parsers ทั้งหมด + `_get_document_path` + `_extract_target_document`
- `worker_retrieval.py` — `_retrieve_context` / `_retrieve_context_scoped` / `_nodes_to_context_and_sources`
- `worker_handlers.py` — `_handle_*` ทั้งหมด + wrapper `_build_llm(model)` /
  `_complete_with_fallback(4 args)` ที่เติม timeout/log จาก config/state เอง (call sites ไม่เปลี่ยน signature)
- `llm_fallback.py` — pure module: `is_quota_error`, `is_fallback_worthy_error`,
  `build_llm(model, timeout_ms)`, `complete_with_fallback(..., *, timeout_ms, log, sleep, llm_factory)`
  — keyword-only params มีไว้ inject ใน test เท่านั้น
- `test_llm_fallback.py` — 25 pure unit tests (stdlib unittest) รวม regression test
  "timeout → fallback โดยไม่ retry primary" (บั๊ก ADR-003 เดิม)

**รอบ 2 (Medium #1, #2 + Low #2):**

- Medium #1: ready-check ซ้ำ 7 จุดใน Handler → `_require_ready()` method เดียว (ส่ง 503 เอง คืน bool)
- Medium #2: สร้าง `worker_client.py` (ไม่ import streamlit): `_check_worker_health`,
  `_start_worker_process`, `ensure_worker_started`, `_call_worker_endpoint` + `_call_worker_*` ทุกตัว
  — app.py import **ด้วยชื่อ underscore เดิม** ทำให้ UI call sites ไม่ต้องแก้เลย
  `_wait_for_worker_ready` คงอยู่ app.py (เป็น UI: วาด placeholder/st.stop) ลบ import
  `sys`/`subprocess`/`urllib.*` ที่ไม่ใช้แล้วออกจาก app.py
- Low #2: globals `_sessions`/`_session_last_used`/`_sessions_lock` → `class SessionStore`
  (lock ในตัวทุก method: `get_or_create(session_id, factory)` create+touch ใต้ lock เดียว,
  `cleanup_idle(timeout)` คืน expired ids) ผู้ใช้ที่แก้: `_handle_chat`, `_inject_draft_into_session`
  (worker_handlers), `_cleanup_idle_sessions` (rag_worker) + `test_session_store.py` 5 tests
  (รวม race test 8 threads)
- Low #1 (รวม prompts) นับว่าเสร็จโดย `worker_prompts.py` ตั้งแต่รอบ 1

**Verification ที่ทำแล้ว (Linux sandbox, Python 3.10, ไม่มี project deps):**
py_compile 12 ไฟล์ ✓, unittest 30/30 ✓, import chain โดยไม่มี API key ✓, no-API-key guard
ยัง raise ตอน import rag_worker ✓, pyflakes สะอาด (เหลือแค่ `faiss` unused ซึ่ง intentional มี noqa) ✓,
ไม่มี reference ตกค้างของ session globals เดิม ✓

### Next Steps (ต่อจากนี้)

1. **ผู้ใช้ต้องรัน live test บน Windows** (sandbox ทำไม่ได้ — ไม่มี venv/โมเดล/API key):
   - stop worker เก่าก่อน (stop_worker.bat หรือ kill port 8765) ให้โค้ดใหม่โหลด
   - `venv\Scripts\python.exe test_llm_fallback.py`
   - `venv\Scripts\python.exe test_session_store.py`
   - `venv\Scripts\python.exe test_rag_pipeline.py` (end-to-end — ครอบคลุม endpoint ทั้งหมด
     **และเป็นการยืนยันข้อค้างเดิมจากข้อ 0 ด้านล่างไปในตัว**)
   - เปิด Streamlit ตามปกติ ยืนยัน worker_client auto-start + UI ทั้ง 4 โหมดทำงาน
2. ถ้าผ่าน: รัน /improve-codebase-architecture ซ้ำเพื่อยืนยันว่า findings เคลียร์แล้ว (optional)
3. ลบ `.sync_probe_2.txt` (ไฟล์ทดสอบ sync ของ sandbox — ลบจาก sandbox ไม่ได้)

### Open Questions / Blockers

- ~~ผล live test ยังไม่รู้~~ **ยืนยันแล้ว 2026-07-05 — `test_rag_pipeline.py` 11/11 PASS บนเครื่องจริง
  ดู "0b" ด้านบน**
- `test_rag_pipeline.py` มี health-check/start-worker helpers ของตัวเอง ซ้ำกับ `worker_client.py`
  — reuse ได้แต่**ยังไม่ได้ทำ** (นอก scope อย่าแตะถ้าไม่ได้ขอ)

### Constraints ที่ agent ถัดไปต้องรู้ (เพิ่มจาก invariants เดิมในเอกสารนี้)

- state ที่ rebind ได้ (`_index`/`_reranker`/`_sys_prompt`) ต้องอ้างเป็น `state.X` ที่ call time เสมอ
  ห้าม `from worker_state import _index` (จะได้ None ค้าง)
- `worker_config` ต้องถูก import ก่อน `import faiss` และ faiss ก่อน torch เสมอ /
  llama_index/torch imports ใน functions เป็น lazy โดยตั้งใจ ห้าม hoist ขึ้น top-level
- convention: docstring/comment/log ภาษาไทย, module ชื่อ `worker_*` แบบ flat, test ใช้ stdlib
  unittest (ไม่ใช่ pytest), คงชื่อ underscore เดิมข้าม module เพื่อลด diff
- **Sandbox mount gotchas** (สำคัญถ้า verify ในสภาพแวดล้อมนี้อีก): mount ที่
  `/sessions/.../mnt/Local  RAG/` sync ไฟล์*ใหม่*เร็ว แต่ไฟล์*ที่แก้ทับ* lag —
  ไฟล์ที่*หดลง*เหลือ NUL bytes ท้ายไฟล์ (แก้ด้วย `tr -d '\000'`), ไฟล์ที่*โตขึ้น*โดน
  **truncate ที่ขนาดเดิม** (ต้อง reconstruct ใน /tmp เอง) — เช็ค integrity (UTF-8 decode +
  marker grep) ก่อนรันเทสจาก mount เสมอ; ไฟล์จริงฝั่ง Windows (ผ่าน Read/Grep tools) คือ source of truth

### Key Files

`rag_worker.py`, `worker_handlers.py`, `llm_fallback.py`+`test_llm_fallback.py`,
`worker_state.py`+`test_session_store.py`, `worker_client.py`, `app.py`,
`worker_config.py`/`worker_prompts.py`/`worker_parsing.py`/`worker_retrieval.py`,
`test_rag_pipeline.py` (ไม่ได้แตะ), `architecture_report.html` (ต้นเรื่อง — ครบ 6 ข้อแล้ว)

---

## 0. Update (2026-07-03, later same day — implementation session)

ADR-006 และ ADR-007 ที่อธิบายไว้ในเอกสารนี้ **implement เสร็จแล้ว** ในเซสชันถัดจากที่เขียน handoff
ฉบับนี้ — รายละเอียดทั้งหมด:

- `rag_worker.py`: เพิ่ม endpoint ใหม่ `/review/target`, `/review/topic`, `/review/finalize`
  (ADR-006), `/draft/questions/interactive` (ADR-007) — ไม่แก้ `/draft`, `/draft/questions`, `/chat`
  แม้แต่บรรทัดเดียว เพิ่ม Target document access (`_parse_markdown_headings`,
  `_parse_docx_headings`), scoped retrieval (`_retrieve_context_scoped` — ดูหมายเหตุสำคัญด้านล่าง),
  และกลไก prefill/follow-up ที่ใช้ร่วมกันระหว่างสอง ADR
- `app.py`: เพิ่ม toggle "📋 โหมดรีวิวเอกสาร" ใหม่ + เปลี่ยน Stage 2 ของโหมดร่างเอกสารเดิมจากฟอร์ม
  เดียวเป็นถามทีละข้อ+prefill+follow-up (ADR-007 ข้อ 3)
- `test_rag_pipeline.py`: เพิ่มเทสต์ 7-10 สำหรับ endpoint ใหม่ทั้งหมด **เทสต์เดิม 1-6 ตรวจสอบแล้วว่า
  byte-identical กับก่อนแก้ ไม่ถูกแตะเลย**
- `ADR.md`: อัปเดตสถานะ ADR-006/007 เป็น "Implemented แล้ว"

**สำคัญ — งานที่ค้างอยู่สำหรับเซสชัน/ผู้ใช้ถัดไป:**

1. **รันจริงแล้ว 1 ครั้งโดยผู้ใช้ — ได้ 9/11 PASS ตอนแรก, พบบั๊กจริง 1 ตัว, แก้แล้วแต่ยังไม่ได้รันซ้ำ
   ยืนยัน**: `/review/target` กับเอกสารจริงที่มี heading เยอะ timeout (client 180s) โดยที่
   `rag_worker.log` ไม่มี log "สำเร็จ"/"error" ตามมาเลย — สาเหตุคือ `GoogleGenAI()` ไม่เคยตั้ง
   request timeout ไว้เลยตั้งแต่ ADR-001 (ดู ADR.md ADR-003 "หมายเหตุเพิ่มเติม 2026-07-03" สำหรับ
   รายละเอียดเต็ม) แก้แล้วด้วย `GEMINI_REQUEST_TIMEOUT_MS` (ดีฟอลต์ 5 นาที) ผ่าน helper กลาง
   `_build_llm()` + ปรับ client timeout ใน `app.py`/`test_rag_pipeline.py` ให้กว้างกว่าตามไปด้วย —
   ~~ยังไม่ได้รัน `test_rag_pipeline.py` ซ้ำเพื่อยืนยันว่าแก้ได้จริง~~ **ยืนยันแล้ว 2026-07-05:
   รันซ้ำบนเครื่องจริง 11/11 PASS รวม `/review/target` กับเอกสารจริงที่เคย timeout — แก้ได้จริง
   ดู "0b" ด้านบน**
2. **Design decision ที่ควรรู้ก่อนรีวิวโค้ด**: `FaissVectorStore` (venv ปัจจุบัน) ไม่รองรับ
   metadata filters ที่ query() เลย (`raise ValueError`) จึง implement **Cross-reference retrieval**
   (ADR-006) เป็น application-level filter แทน (over-fetch `similarity_top_k=200` แล้วกรอง
   `file_name` ด้วย Python ก่อนส่งต่อ reranker) ไม่ใช่ metadata filter ที่ชั้น vector store จริง —
   ผลลัพธ์เดียวกัน แต่ over-fetch มากกว่าปกติ ถ้าพบว่าช้าเกินไปในทางปฏิบัติ ค่อยพิจารณา optimize ต่อ
3. **`build_index.py` ไม่ต้องแก้เลย** — `SimpleDirectoryReader` ใส่ `file_name` metadata ให้ทุก
   node อยู่แล้วโดยไม่ต้องทำอะไรเพิ่ม (พิสูจน์จาก sources ที่ `/chat` คืนมาอยู่แล้วทุกวันนี้) เพียงพอ
   สำหรับ application-level filter ในข้อ 2 — HANDOFF.md ฉบับเดิมคาดว่าต้องแก้ไฟล์นี้ด้วยแต่สุดท้ายไม่ต้อง
4. **Checklist content ต่อประเภทเอกสาร (open question เดิมในข้อ 4)**: แก้โดยให้ LLM สร้าง
   checklist-derived topic แบบ dynamic (ต่อยอดกลไก ADR-002) แทนการ hardcode checklist ตายตัวต่อ
   ประเภทเอกสาร — ทดสอบ (ยังไม่รันจริง) ด้วยเอกสารจริง
   `Procedures/24CS-IT-QP-001_การบริหารความเสี่ยงด้านเทคโนโลยีสารสนเทศ.md` เป็น worked example
5. **Endpoint เพิ่มเติมนอกเหนือ ADR text**: `POST /review/finalize` (ให้ change report + updated
   document ตาม ADR-006 ข้อ 6) ไม่ได้อยู่ในตัวอย่าง endpoint ที่ ADR-006 ข้อ 9 ยกไว้ (ยกแค่
   `/review/target`/`/review/topic`) แต่จำเป็นต่อผลลัพธ์ที่ตัดสินใจไว้ — ควรพิจารณาเพิ่มเป็นเอกสาร
   ADR-006 ข้อ 9 อย่างเป็นทางการถ้าตรวจสอบแล้วว่าถูกต้อง
6. ~~`generate_docx.py` table-rendering fix + README update ที่ค้างจาก session ก่อนหน้า
   (ดูส่วน "Next Steps" ข้อ 1 ด้านล่าง) ยังไม่ได้ commit~~ **แก้ไข 2026-07-05: อ้างผิด — เช็ค `git log`
   แล้วพบว่า commit ไปแล้วจริง (`e5cd6f3` docx fix, `83bd90b` README) ตั้งแต่ก่อน commit ADR-006/007
   (`fb9abeb`) เสียอีก ไม่ต้อง commit ซ้ำ ดู "0b" ด้านบน**

ส่วนที่เหลือของเอกสารนี้ (ข้อ 1-6 ด้านล่าง) เป็นบริบทเดิมตอนเริ่มงาน เก็บไว้เพื่ออ้างอิงว่าทำไมถึง
ตัดสินใจแบบนี้ — งานที่อธิบายไว้ใน "Next Steps" เดิมข้อ 3(a)-(f) ทำเสร็จหมดแล้วยกเว้นการรันเทสต์จริง

## 1. Goal

Implement **ADR-006 (Document Review Mode)** and **ADR-007 (expanded draft clarifying questions)** in the Thai-language Policy RAG Assistant Streamlit app at `D:\Review Policy\Local  RAG` — both are fully specified, approved, and scrutinized-clean (4 rounds), but **zero code has been written for them yet**. Everything else in this doc is settled background; the actual work for the next session is implementing these two ADRs.

## 2. Current State

**Shipped, tested, committed, pushed** to `origin/main` (`https://github.com/bornja55/Local-RAG.git`): ADR-001 through ADR-005 (draft mode + auto-scrutinize, clarifying questions, model fallback, chat-sees-draft, session eviction). Not this session's work — see `ADR.md` for details if needed, otherwise treat as stable/working and don't touch without reason.

**This session's work — ADR-006 & ADR-007 (design only, not implemented):**

Both went through `/scrutinize` **4 times** (each round found real issues, all fixed) plus one `/grill-with-docs` session to pin down scope. Full decisions are in `ADR.md` (search for `## ADR-006` / `## ADR-007`) — **read those two ADRs in full before writing any code**, this section is a summary, not a replacement.

- **ADR-006 — Document Review Mode**: brand-new mode, separate from Draft mode, triggered because Draft mode's "clarify questions" flow has no concept of a review target and was silently copying old-company content into drafts. Reviews one target document topic-by-topic. Key mechanics: **Target document access** (direct file parse — works for un-indexed uploads too, markdown read directly / `.docx` via python-docx `Heading 1/2/3` styles, *not* FAISS) vs **Cross-reference retrieval** (FAISS + new metadata filter, scoped to auto-suggested + user-confirmed related docs only — today's retrieval is corpus-wide with zero scoping, this doesn't exist yet). **Review Topic** = target doc's real headings + type-specific checklist for gaps, with dedup between the two sources. Rejects (clear message → suggests Draft mode instead) any target doc with unparseable heading structure (prose-only, scanned PDF) — no silent checklist-only fallback. One-topic-at-a-time UI, back-navigable, RAG-based **Prefill** before each question. Output = change report + updated document, side by side.
- **ADR-007 — expand draft clarifying questions 2-5x**: from 3-6 questions to ~10-25, grouped by category, with follow-ups (checklist-derived topics only — draft mode has no target doc so there's no heading-derived side). Same UI/state model as ADR-006 (one-at-a-time, back-nav, prefill), upgraded question model (`GEMINI_MODEL_CHAT` → `GEMINI_MODEL_DRAFT`).
- **Shared Prefill provenance rule — single source of truth is `CONTEXT.md`'s "Prefill" glossary entry, do not restate it in ADR-006 or ADR-007 (a duplication bug already happened once this session — see below).** Rule: every prefill sourced from a cross-reference doc must show that doc's effective date + last-revision date (already in every doc body — same mechanism Q&A already uses correctly, no new metadata parser). Explicitly does NOT have the AI judge "is this still valid" — that's the human's call, since real review cadence is ≥1x/year and the dates are enough for a human to judge staleness.
- **State model, both ADRs**: stateless server, client-side only. New endpoints resend the full topic list + all answers-so-far every request (same pattern `/draft` already uses for its `answers: dict`), state lives in `st.session_state` — no new `_review_sessions` dict on the worker, no new eviction logic needed.
- **Endpoint isolation, both ADRs**: new endpoints only (`/review/*` for ADR-006, `/draft/questions/interactive` for ADR-007) — **never modify `/draft*` or `/draft/questions`**, so `test_rag_pipeline.py` stays green untouched. Add new tests for the new endpoints instead of editing existing ones.
- Cost/budget and user drop-off concerns were deliberately **ruled out as non-issues** and removed from Consequences — this is a single-org internal tool, each user brings their own Gemini API key, ADR-003's existing auto-fallback already absorbs quota spikes. Don't reintroduce these as blockers.
- Company rename (`COMPANY_NAME`="ออริจิ้น โกลบอล เอ็มไพร์" vs indexed docs saying "ทเวนตี้ โฟร์ คอน แอนด์ ซัพพลาย") is a **real legal-entity rename, confirmed by user, not a data bug** — no corpus fix needed. It's *why* ADR-006/007 exist (draft mode was silently reusing pre-rename content unconfirmed) but the fix is the provenance rule above, not touching the corpus.
- `CONTEXT.md` glossary additions: Review Topic, Document Review Mode, Prefill (+ provenance rule), Cross-reference documents, Target document access, Cross-reference retrieval.
- **Mistake made and corrected mid-session, worth knowing**: first pass wrote the provenance rule inline in both ADR-006 and ADR-007 *in addition to* CONTEXT.md — same duplication problem it was meant to solve, caught on the 4th `/scrutinize` pass and trimmed to references only. If the provenance rule ever needs to change, edit `CONTEXT.md` only.

**Also fixed this session (unrelated to ADR-006/007, already applied to `generate_docx.py`, not yet committed):**
- `generate_docx.py` didn't render markdown tables at all — `_add_markdown_line()` had no table handling, so `| col | col |` / `| :--- |` lines were dumped in as literal paragraph text (visible pipe/dash garbage in exported `.docx`). Fixed by adding `_add_markdown_table()` + `_is_table_row()` / `_is_table_separator_row()` / `_split_table_row()` — detects a table block, builds a real Word table (`Table Grid` style, bold header), converts `<br>` in cells to real line breaks, reuses `_add_runs_with_bold()` for bold/`[ต้องระบุ: ...]` inside cells. Verified with a standalone test in the sandbox (not the user's venv).

## 3. Next Steps

1. ~~**Commit the still-uncommitted fixes first**~~ **Stale as of 2026-07-05 — these were already
   committed** (`e5cd6f3`, `83bd90b`, confirmed via `git log`), no action needed here. What's
   actually uncommitted right now is the architecture-refactor split (`worker_*.py` new files +
   modified `rag_worker.py`/`app.py`) from section "0a" — see "0b" above for what to commit instead.
2. **Read `ADR.md` ADR-006 and ADR-007 in full** (this handoff is a summary, the ADRs have the actual decisions with rationale).
3. **Implement in this order** (ADR-006 first, ADR-007 reuses its plumbing):
   a. `build_index.py` + `rag_worker.py`: add metadata filtering to the retriever so **Cross-reference retrieval** can be scoped to a specific file list (doesn't exist today — retrieval is always corpus-wide).
   b. `rag_worker.py`: **Target document access** — direct-parse path for the review target, separate from the FAISS retriever entirely. Markdown: read `##`/`###` headings directly. `.docx`: use python-docx, read paragraph `style.name` for `Heading 1/2/3`. Must detect "no parseable heading structure" and return a rejection signal (not silently fall back).
   c. New worker endpoints: `POST /review/target` (submit target doc → Review Topics + auto-suggested cross-reference docs, one response) and `POST /review/topic` (stateless — client sends full topic list + answers-so-far, gets prefill + next question back).
   d. `app.py`: new "Document Review" UI section, one-topic-at-a-time with back button, separate from the existing draft-mode 3-stage flow.
   e. Then ADR-007: new `POST /draft/questions/interactive` endpoint reusing the same one-at-a-time/prefill mechanics built in (c)/(d), expand the clarifying-question prompt to 10-25 categorized questions with follow-ups, upgrade its model to `GEMINI_MODEL_DRAFT`. Leave `/draft/questions` completely untouched.
   f. Add new tests to `test_rag_pipeline.py` for the new endpoints — do not modify the existing 6 tests (they cover `/draft/questions`'s old single-shot contract, which must keep working).
4. Confirm with the user whether to enable the fallback model in production now (`GEMINI_MODEL_CHAT_FALLBACK` / `GEMINI_MODEL_DRAFT_FALLBACK` in real `.env`) — unrelated to ADR-006/007, just an old open item.

## 4. Open Questions / Blockers

None block starting implementation — ADR-006/007 scrutinized clean. Implementation-level judgment calls the next agent will still need to make (normal detail work, not design gaps):
- Exact checklist content per document type (ADR-006 decision 4 says "checklist เฉพาะประเภทเอกสาร" but doesn't enumerate one per type — will need at least one worked example, e.g. IT risk policy, to validate the approach).
- Exact category names for grouping ADR-007's 10-25 questions (ADR gives examples like "ขอบเขต / บทบาทและความรับผิดชอบ / ตัวชี้วัด / การอนุมัติ" but implementer should adapt per topic).
- Go-live date for fallback models — old open item, not technical, not urgent.

## 5. Context & Constraints

- **Architecture (must not change)**: two-process split — `app.py` (thin Streamlit UI) ↔ `rag_worker.py` (holds torch/faiss/embedding/reranker/LLM) over local HTTP `127.0.0.1:8765`. Fixes a Windows WINHTTP.dll access-violation crash from native-library conflicts — never recombine into one process.
- **RAG stack**: BGE-M3 embedding + BGE-reranker-v2-m3 (local, `HF_HUB_OFFLINE=1`), FAISS vector store, LlamaIndex `condense_plus_context` chat engine, `llama_index.llms.google_genai.GoogleGenAI`.
- **"Iron rule" grounding** (`_build_sys_prompt()`) — Q&A mode must never answer outside retrieved documents. Draft mode and Review mode are explicitly exempt (they compose new content) but must always self-critique / never present output as approved.
- **Endpoint discipline established this session**: new features get new endpoints, never modify existing ones that have passing tests (`/draft`, `/draft/questions`) — this is now a standing pattern, not just an ADR-006/007-specific choice.
- **Sandbox constraint**: the assistant's bash tool cannot reach the user's `localhost:8765`, their venv, or run `git push`/`pip install`/`test_rag_pipeline.py` for them — hand over exact PowerShell commands, don't attempt to run directly. (python-docx testing for the `generate_docx.py` fix was done in the assistant's own isolated sandbox venv, not the user's — still valid since that module has no torch/faiss dependency.)
- **Write-tool truncation risk**: writing large (~300+ line) Thai/UTF-8 files in one call can silently truncate. Split into ~100-200 line chunks if writing something that big.
- **venv-vs-bare-python gotcha**: `ModuleNotFoundError` despite correct `requirements.txt` usually means the user ran bare `python` instead of `venv\Scripts\python.exe`.
- **Model-name verification pattern**: never trust an LLM-guessed Gemini model identifier — verify against official docs or `test_fallback_model.py <model_name>` before writing it anywhere.

## 6. Key Files

- `ADR.md` — **read ADR-006 and ADR-007 in full before coding anything**, source of truth for all decisions in this handoff.
- `CONTEXT.md` — glossary; **Prefill provenance rule lives here only**, don't duplicate it back into the ADRs.
- `rag_worker.py` (~782 lines) — worker process; ADR-006/007 work adds new endpoints here alongside existing `_handle_chat`/`_handle_draft`/`_handle_clarify_questions`.
- `app.py` — Streamlit UI; ADR-006/007 work adds a new Review Mode UI section here alongside the existing 3-stage draft flow.
- `build_index.py` — index builder; needs metadata-filtering support added for Cross-reference retrieval (ADR-006).
- `generate_docx.py` (~90 lines) — Markdown→docx conversion; table rendering just fixed (see Current State), not yet committed.
- `test_rag_pipeline.py` — 6-test E2E suite, currently 6/6 PASS covering `/draft`, `/draft/questions`, `/chat` — add new tests for ADR-006/007 endpoints, don't touch these.
- `.env` / `.env.example` — `GEMINI_MODEL_CHAT_FALLBACK`, `GEMINI_MODEL_DRAFT_FALLBACK`, `SESSION_IDLE_TIMEOUT_SECONDS`. `.env` is gitignored.
- `README.md` / `README_EN.md` / `README_MANAGEMENT*.md` — docs current through ADR-005; will need an update pass once ADR-006/007 ship (not done yet, not urgent until implementation lands).
