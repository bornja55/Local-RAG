# Handoff — Policy RAG Assistant (2026-07-03, ปรับปรุงล่าสุด 2026-07-05)

## 0f. Session Handoff (2026-07-05, สิ้นสุด session นี้) ← อ่านส่วนนี้ก่อนสุด

**Goal**: Policy RAG Assistant — worker แยกโปรเซส (Streamlit `app.py` ↔ `rag_worker.py` HTTP
`127.0.0.1:8765`) แก้บั๊ก timeout/fallback ที่ `/scrutinize` เจอ, ปิด gap unit test, ทำเอกสารให้ตรงกับ
พฤติกรรมจริง, commit ขึ้น git — งานหลักทั้งหมดเสร็จแล้ว เหลือแค่ผู้ใช้ `git push` เอง

**Current State (ทำเสร็จ, verify แล้วทุกจุด)**:
- Client-timeout math + misleading fallback log + `_handle_chat` test-coverage gap (ผ่าน
  `llm_fallback.run_with_fallback()` กลาง) — commit `5acd316`
- ลบ `README_MANAGEMENT.md`/`README_MANAGEMENT_EN.md` (เอกสาร "ฉบับผู้บริหาร" ที่ไม่มีผู้อ่านจริง
  ตามที่ผู้ใช้ยืนยัน) ทั้งดิสก์และ git — commit `844f91b`
- รวม `test_llm_fallback.py`(29)+`test_handle_chat_fallback.py`(5)+`test_session_store.py`(5) →
  `test_all.py` ไฟล์เดียว (39 เทส, pure stdlib unittest) verify 39/39 PASS ในสภาพแวดล้อมสะอาด —
  `test_rag_pipeline.py` (E2E) กับ `test_fallback_model.py` (manual CLI) ตั้งใจไม่รวมด้วย (คนละประเภท
  เทส) — เก็บ prose reference เก่าใน ADR.md/HANDOFF.md ไว้เป็นบันทึกประวัติ ไม่แก้ย้อนหลัง แต่เพิ่ม
  หมายเหตุกำกับชัดเจนว่าไฟล์ปัจจุบันคือ `test_all.py` — commit `14a6b73`
- ยืนยันด้วย `git diff --stat HEAD` ว่าไม่มีไฟล์ production ใดถูกแก้ใน commit `14a6b73` เลย
  (`llm_fallback.py`/`worker_handlers.py`/`worker_client.py`/`README*.md`/`test_rag_pipeline.py`
  ตรงกับ HEAD เป๊ะ — diff ที่ `git status --short` เคยโชว์เป็นแค่ artifact ของ default index ที่พังมา
  ตั้งแต่ก่อน session นี้ ดู Constraints ด้านล่าง)

**Next Steps**:
1. ผู้ใช้รัน `git push origin main` จากเครื่องจริง (Windows, `D:\Review Policy\Local  RAG`) —
   push commit `5acd316`, `844f91b`, `14a6b73` ที่ยังไม่ขึ้น `origin/main` (ปัจจุบันค้างที่ `0bdb639`)
   — sandbox นี้ไม่มี credential ทำเองไม่ได้
2. (แนะนำ ไม่บังคับ) รัน `venv\Scripts\python.exe test_all.py` และ `test_rag_pipeline.py` บนเครื่องจริง
   อีกครั้งเพื่อยืนยันนอก sandbox — โค้ด production ไม่เปลี่ยนเลยรอบนี้ ความเสี่ยง regression ต่ำมาก

**Open Questions / Blockers**:
- ยังไม่ยืนยันว่าผู้ใช้ push จริงหรือยัง
- `test_rag_pipeline.py` ยังไม่ได้รันซ้ำบน Windows หลัง commit `14a6b73` (แต่ไม่ได้แก้ไฟล์ production
  เลยในรอบนี้ ความเสี่ยงต่ำ)

**Context & Constraints สำคัญสำหรับ agent ถัดไป**:
- Sandbox bash mount serve เนื้อหา**เก่า/truncate**สำหรับไฟล์ที่เพิ่งแก้ — Read/Write/Edit tool คือ
  source of truth เสมอ ห้ามเชื่อ `wc -l`/`cat` ผ่าน bash เฉยๆ กับไฟล์ที่เพิ่งแก้ ต้อง Read แล้ว Write
  ไปที่ outputs ก่อน แล้วค่อย cp เข้า `/tmp` มา compile/test
- **Default git index ในนี้พังมาตั้งแต่ก่อน session นี้** (staged content ไม่ตรงความจริง) —
  `git status --short`/`git diff HEAD` (ไม่ระบุ path) ให้ผลลัพธ์หลอกได้ ("MM" ทั้งที่ไฟล์ไม่ได้แก้จริง)
  วิธี verify จริง: `git diff --stat HEAD -- <path>` หรือ `diff <(git show HEAD:<path>) <path>`
  วิธี commit ที่ปลอดภัย: ใช้ alternate index เสมอ —
  `GIT_INDEX_FILE=/tmp/xxx git read-tree HEAD` → `git hash-object -w <file>` ต่อไฟล์ →
  `git update-index --cacheinfo 100644,<hash>,<path>` (หรือ `--remove` สำหรับไฟล์ที่ลบ) →
  `git write-tree` → `git commit-tree <tree> -p HEAD -F <msgfile>` → `git update-ref refs/heads/main <sha>`
  ห้ามใช้ `git add`/`git commit` ธรรมดาใน sandbox นี้
- `.git/index.lock`/`.git/HEAD.lock` ค้างได้ `rm` ตรงๆ ไม่ผ่าน ("Operation not permitted") — เรียก
  `mcp__cowork__allow_cowork_file_delete` กับ path ของ lock file ก่อนเสมอ
- `git push` รันจาก sandbox นี้ไม่ได้เลย (ไม่มี credential helper) — ต้องส่ง command ให้ผู้ใช้รันเอง
  เสมอ ห้ามพยายามรันตรงๆ
- Convention เดิมของโปรเจกต์: docstring/comment/log ภาษาไทย, module ชื่อ `worker_*` แบบ flat,
  test ใช้ stdlib `unittest` (ไม่ใช่ pytest), คงชื่อ underscore เดิมข้ามโมดูลเพื่อลด diff
- ADR.md/HANDOFF.md เป็น**บันทึกประวัติแบบ append-only** — พบอะไรใหม่ให้เพิ่ม section ใหม่ที่มี
  วันที่กำกับไว้บนสุด ไม่แก้ entry เก่าย้อนหลัง (ยกเว้น "current state" pointer list ที่จะทำให้เข้าใจผิด
  จริงๆ ถึงแก้ในจุดนั้นได้)

**Key Files**:
- `test_all.py` — unit test suite เดียวรวมทั้งหมด (39 เทส, pure unittest, รันได้โดยไม่ต้องมี API
  key/โมเดล/worker จริง) — `venv\Scripts\python.exe test_all.py`
- `test_rag_pipeline.py` — E2E (11 เทส, ต้องมี worker จริง+API key), ไม่แตะรอบนี้
- `test_fallback_model.py` — manual CLI ตรวจชื่อโมเดลสำรองทีละตัว, ไม่แตะรอบนี้
- `llm_fallback.py`/`worker_handlers.py`/`worker_client.py`/`worker_state.py`/`worker_config.py` —
  โค้ด production, **ไม่เปลี่ยนเลยรอบนี้** (verify แล้วตรงกับ HEAD เป๊ะ)
- `ADR.md`/`HANDOFF.md` — แก้รอบนี้ (เพิ่ม section ใหม่ + ล้าง reference เก่าที่ตายแล้ว)
- `README.md`/`README_EN.md` — ไม่แตะรอบนี้
- `README_MANAGEMENT.md`/`README_MANAGEMENT_EN.md` — **ลบแล้ว** (ทั้งดิสก์+git) ห้ามสร้างใหม่

---

## 0e. Update (2026-07-05 — รวม unit test 3 ไฟล์เป็น `test_all.py` ไฟล์เดียว)

ตามคำขอผู้ใช้ให้เหลือไฟล์ทดสอบเดียวที่รวมทุกฟังก์ชันที่ต้องทดสอบ — `test_llm_fallback.py` (29 เทส) +
`test_handle_chat_fallback.py` (5 เทส) + `test_session_store.py` (5 เทส) **ลบทิ้งแล้ว รวมเป็น
`test_all.py` (39 เทส) ไฟล์เดียว** เนื้อหาเทสทุกตัวย้ายมาทั้งก้อน ไม่ตัดทอน แค่รวม fake object ที่ซ้ำกัน
ระหว่าง 2 ไฟล์เดิม (`_FakeChatResponse`, `_FakeChatEngine`) เป็นชุดเดียว — verify แล้ว 39/39 PASS
(ดู ADR-003 หมายเหตุ 2026-07-05 ส่วนล่างสุดสำหรับรายละเอียด)

**`test_rag_pipeline.py`** (E2E, ต้อง worker จริง+API key) **และ `test_fallback_model.py`**
(สคริปต์ตรวจชื่อโมเดลด้วยมือ) **ยังคงแยกไฟล์เหมือนเดิม** — ไม่ใช่ pure unit test แบบเดียวกัน จึงไม่รวม
เข้ามาด้วย วิธีรันตอนนี้:
```
venv\Scripts\python.exe test_all.py
venv\Scripts\python.exe test_rag_pipeline.py
```

ทุกจุดด้านล่างที่อ้างชื่อ `test_llm_fallback.py`/`test_handle_chat_fallback.py`/`test_session_store.py`
เป็นบันทึกประวัติ ณ ตอนที่เขียน (ไฟล์เหล่านั้นมีอยู่จริงตอนนั้น) — ไม่ได้แก้ย้อนหลัง ให้ถือว่าชื่อไฟล์ปัจจุบัน
คือ `test_all.py` เสมอ

---

## 0d. Update (2026-07-05 — ปิด gap `_handle_chat` ไม่มี unit test คุ้ม)

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
  free tier ไม่ใช่บั๊ก) — README_MANAGEMENT.md/README_MANAGEMENT_EN.md **ลบทิ้งแล้ว (2026-07-05)**
  แทนการแก้ (ผู้ใช้ระบุว่าเป็นเอกสาร "ฉบับส่งผู้บริหาร" ที่ไม่มีผู้อ่านจริง เกิดจากความผิดพลาดตอนสั่งให้เขียน)
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
- `CONTEXT.md` glossary additions: Review Topic, Document Review Mode, Prefill (+ provenance rule), Cross-reference documents, Target documen