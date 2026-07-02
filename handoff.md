# Handoff

## 1. Goal
Local RAG Streamlit app answering questions about org policy documents in Thai. Currently being extended with a new "draft policy + auto-scrutinize" mode so it can compose brand-new policies (not just retrieve existing ones), grounded in and cross-checked against the existing policy corpus.

## 2. Current State

### Shipped and working (prior session, confirmed by user)
- Two-process architecture: `app.py` (thin Streamlit UI) + `rag_worker.py` (background worker holding torch/faiss/embedding/reranker/LLM), talking over `127.0.0.1:8765`. This fixed a reproducible Windows access violation (WINHTTP.dll) that occurred when torch+faiss lived in-process with Streamlit.
- Secrets externalized to `.env` (gitignored), `.env.example` committed as template.
- `COMPANY_NAME` env var added to both `app.py` and `rag_worker.py` (with a hand-rolled `_load_dotenv()`, no python-dotenv dependency) so the codebase has zero hardcoded company branding — reusable across clients. Default fallback: `"องค์กรของคุณ"`.
- Repo pushed to GitHub: `https://github.com/bornja55/Local-RAG` (user pushes manually from PowerShell; I cannot reach their machine's git or localhost:8765 from my sandbox — network-isolated).
- `requirements.txt` was found badly broken (UTF-16 encoded, truncated — missing torch, llama-index*, streamlit, sentence-transformers, google-genai, pandas, python-docx entirely). Rewritten clean, ASCII, complete. **Root cause note**: writing Thai text into `requirements.txt` via the Write tool caused it to silently re-encode as UTF-16 (matches whatever corrupted the original) — worked around by keeping this file's comments in English and writing it via bash heredoc instead of the Write tool. If this file needs edits again, avoid non-ASCII content and prefer bash heredoc over Write/Edit for it specifically.
- README.md + new README_EN.md written (bilingual, styled after https://github.com/bornja55/Media-Downloader README as a visual/format reference — engaging tone, badges, credits section). Added hardware notes (~15GB disk, GPU optional/CPU works, CUDA torch must be installed separately if wanted) and clarified `build_index.py` needs no extra deps beyond `requirements.txt`.
- Old `Next_Steps_Plan.md` and old `handoff.md` (stale, pre-dated the process-isolation rewrite) deleted and replaced.
- Tested the Q&A system's reasoning against two real uploaded policy docs (`IT Policy Ball V.1.docx`, `Risk Management Ball V.1.docx`, both IT-department-scoped, written by the user as IT Manager). Confirmed: current system prompt is strict retrieval-only ("ห้ามคิดคำตอบขึ้นมาเองโดยเด็ดขาด") — correct for Q&A, but means it refuses to draft new policies that don't exist yet in the corpus. This is by design, not a bug.

### Just completed this session: design phase for "Draft + Scrutinize" mode (NOT YET IMPLEMENTED)
Ran a full `/grill-with-docs` session. All open design questions resolved via user's explicit choices (all "recommended" options):
1. Draft generation and scrutinize critique run as **one combined flow** (not two separate buttons) — user clicks "ร่างนโยบาย" once, gets both the draft and a self-critique in the same response.
2. Triggered via a **separate toggle/button in the Streamlit sidebar** ("โหมดร่างเอกสาร"), not a slash-command in the existing chat box — keeps normal Q&A behavior completely unchanged and avoids accidental mode-switching.
3. Output: **markdown in the chat + a "download as .docx" button**. The docx is a plain structured document (headings/paragraphs/bullets) — explicitly **not** a replica of the company's real letterhead/signature-table/revision-history template (scoped down for v1 to reduce build effort; user can paste into the real template before circulating).
4. If auto-scrutinize finds a CRITICAL-severity issue, the app **shows a prominent warning but does not block the .docx download** — the draft is a human-in-the-loop starting point, not a finished document.
5. Model research (via WebSearch, official Google DeepMind model cards, current as of May 2026): confirmed `gemini-3.1-flash-lite` (currently hardcoded in `rag_worker.py`) is fine for the existing pure-retrieval Q&A but too weak for drafting/scrutiny (FACTS factuality only 40.6%, no agentic/expert-task benchmarks). `gemini-3.5-flash` was picked for the new draft/scrutinize mode — on Finance Agent v2 (expert document analysis) and MCP Atlas (multi-step workflows) it actually **beats** `gemini-3.1-pro` while costing about half as much ($1.5/$9 per 1M tokens vs $2-4/$12-18), and is still on the free tier (Pro was removed from Google's free tier in April 2026).

### Files created this session (design artifacts, not yet wired into the app)
- **`CONTEXT.md`** (NEW) — project glossary. Defines: Worker, Grounding, Iron-rule system prompt, Draft mode, Scrutinize/Auto-scrutinize, CRITICAL/WARNING/NITPICK/VERDICT severity levels, CRITICAL gate policy, COMPANY_NAME, GEMINI_MODEL_CHAT/GEMINI_MODEL_DRAFT (planned).
- **`ADR.md`** (NEW) — ADR-001 records the full decision above (context, decision, consequences) for the draft+scrutinize feature.

## 3. Next Steps
Implementation has NOT started — user ended the session right after the plan was presented and approved in principle, saying they're starting a fresh chat. The next agent should pick up here and build:

1. **`rag_worker.py`**:
   - Add `GEMINI_MODEL_CHAT = os.environ.get("GEMINI_MODEL_CHAT", "gemini-3.1-flash-lite")` and `GEMINI_MODEL_DRAFT = os.environ.get("GEMINI_MODEL_DRAFT", "gemini-3.5-flash")` near the existing `COMPANY_NAME` line (~line 81).
   - Update `_handle_chat()` (~line 247) to use `GEMINI_MODEL_CHAT` instead of the hardcoded `"gemini-3.1-flash-lite"` string.
   - Add `_build_draft_sys_prompt()` — permissive version of `_build_sys_prompt()` that allows composing new content but must ground claims in retrieved context and instruct the model to clearly mark output as a draft requiring human review.
   - Add `_build_scrutinize_sys_prompt()` — critique prompt, CRITICAL/WARNING/NITPICK/VERDICT format (same convention as the `/scrutinize` skill), comparing the draft against the same retrieved context.
   - Add `_handle_draft(topic: str, instructions: str) -> dict`: retrieve+rerank relevant existing-policy context for the topic (reuse `_index` + `_reranker`, don't build a separate index), call `GoogleGenAI(model=GEMINI_MODEL_DRAFT)` with the draft prompt to get `draft_markdown`, then call it again with the scrutinize prompt (draft + same context) to get the critique. Return `{"draft_markdown": ..., "scrutiny": ..., "sources": [...]}`. Reuse the existing quota-retry pattern from `_handle_chat` (`_is_quota_error`, 3 attempts, backoff).
   - Add `POST /draft` route in `Handler.do_POST` (currently only handles `/chat`, 404s everything else — see ~line 307).
   - Update the module docstring's endpoint list (~line 12-16).

2. **`app.py`**:
   - Add a sidebar toggle "โหมดร่างเอกสาร (ทดลอง)".
   - When on: replace the chat UI with a form (topic text input + instructions textarea + "ร่างนโยบาย" button) that POSTs to `/draft` (mirror the existing `_call_worker_chat` HTTP-call pattern, e.g. `_call_worker_draft(topic, instructions)`).
   - Render the returned draft markdown, the scrutiny report (highlight/warn if CRITICAL present, per ADR-001 — warn but never block), and a "ดาวน์โหลดเป็น Word" download button.

3. **New file `generate_docx.py`**: `markdown_to_docx(markdown_text: str, title: str) -> bytes` using `python-docx` (already in `requirements.txt` as `python-docx`, imported as `from docx import Document` elsewhere in this codebase — see `extract_forms.py`). Simple mapping: `#`/`##` → Heading levels, `-` → bullet paragraph, else plain paragraph. No corporate template fidelity (per ADR-001 decision 5).

4. **`.env.example`** and the user's local **`.env`**: add `GEMINI_MODEL_CHAT=gemini-3.1-flash-lite` and `GEMINI_MODEL_DRAFT=gemini-3.5-flash`.

5. **`test_rag_pipeline.py`**: add a test calling `/draft` with a sample topic (e.g. "นโยบายจัดซื้อ"), asserting both `draft_markdown` and `scrutiny` come back non-empty with no `error` key.

6. **README.md / README_EN.md**: once built, document the new draft mode (brief section, consistent with existing tone/format — user has a standing instruction to run marketing/user-facing copy through `/management-talk` before writing it, see Context section below).

7. After implementation: verify locally (`python test_rag_pipeline.py`, then manual UI check), then the user commits/pushes from their own PowerShell (I cannot run git push — see Constraints).

## 4. Open Questions / Blockers
- None outstanding on the design — all v1 scope decisions were explicitly confirmed by the user (see Current State above). Only remaining unknown is real-world quality of the draft/scrutinize output once built — recommend testing with the actual "นโยบายจัดซื้อ" (procurement policy) scenario the user originally asked about, using the two uploaded reference docs (`IT Policy Ball V.1.docx`, `Risk Management Ball V.1.docx`) as the grounding corpus.
- Not asked yet, worth raising if relevant later: should `GEMINI_MODEL_DRAFT` calls (2x per draft request, pricier model) have any rate-limiting/cost-guardrail given this is a small ~10-person org? Not blocking, just a future consideration.

## 5. Context & Constraints
- **Stack**: Python, Streamlit, LlamaIndex (`llama-index-core`, `llama-index-embeddings-huggingface`, `llama-index-vector-stores-faiss`, `llama-index-llms-google-genai`), HuggingFace `sentence-transformers` (BGE-M3 embedding + BGE-reranker-v2-m3 reranker, loaded from local `models/` folder, `HF_HUB_OFFLINE=1`), FAISS (`faiss-cpu`), Google GenAI SDK (HTTP-based, not gRPC). Windows host.
- **No python-dotenv dependency** — `.env` parsing is a hand-rolled ~10-line `_load_dotenv()` duplicated in `app.py`, `rag_worker.py`, `extract_forms.py`. Keep this pattern for any new env vars.
- **Sandbox tooling caveat (still applies)**: `mcp__workspace__bash`'s mount of the connected folder can be a stale snapshot right after Read/Write/Edit tool calls — trust the Read/Write/Edit tool results over bash `cat`. Separately, and specifically for `requirements.txt`: the Write/Edit tools appear to silently convert to UTF-16 if the file was previously UTF-16 (unclear root cause, reproduced twice) — use bash heredoc + ASCII-only content for that file if it needs changes again.
- **Cannot reach the user's machine over network** — my sandbox is isolated from their `127.0.0.1:8765` (the running worker) and from their local git config. I can edit files directly (real Windows filesystem via Read/Write/Edit tools), but the user must run `pip install`, `python build_index.py`, `run_app.bat`, and all `git` commands themselves in their own PowerShell.
- **User preferences**: concise responses, Thai primary language, technical terms in English OK, bullet points for long/technical content, cites sources/links when research is involved. Explicit standing instruction: use `/management-talk` skill whenever writing marketing copy or user-facing explanations (e.g. README sections).
- **Established convention**: don't hardcode anything organization-specific in code — always externalize via `.env` with a sensible generic default (established with `COMPANY_NAME`, now extending to `GEMINI_MODEL_CHAT`/`GEMINI_MODEL_DRAFT`).
- **Do NOT** modify the existing `_build_sys_prompt()` / `_handle_chat()` Q&A behavior — it's tested and working; the new draft/scrutinize feature m