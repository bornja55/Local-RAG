# Handoff — Policy RAG Assistant (2026-07-03)

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
   **ยังไม่ได้รัน `test_rag_pipeline.py` ซ้ำเพื่อยืนยันว่าแก้ได้จริง** ต้องรัน
   `venv\Scripts\python.exe test_rag_pipeline.py` อีกครั้งก่อนเชื่อว่าโหมดรีวิวเอกสารใช้งานได้จริง
   (เทสต์ที่เหลือ 9 ข้อ รวมถึง 1-6 เดิมทั้งหมด ผ่านหมดในรันแรกแล้ว ไม่ต้องกังวลเรื่องนั้น)
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
6. **`generate_docx.py` table-rendering fix + README update ที่ค้างจาก session ก่อนหน้า
   (ดูส่วน "Next Steps" ข้อ 1 ด้านล่าง) ยังไม่ได้ commit** — ไม่เกี่ยวกับงานเซสชันนี้ แค่ยังค้างอยู่เหมือนเดิม

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

1. **Commit the still-uncommitted fixes first** (exact commands for the user's PowerShell):
   ```
   git add generate_docx.py
   git commit -m "fix: render markdown tables as real docx tables instead of literal pipe text"
   git add README_MANAGEMENT.md README_MANAGEMENT_EN.md
   git commit -m "docs: update leadership README to v2.1 (fallback verified, session cleanup, docx highlighting)"
   git push
   ```
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
