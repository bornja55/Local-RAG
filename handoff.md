# Handoff

## 1. Goal
A local RAG (Retrieval-Augmented Generation) Streamlit app that answers questions about an organization's internal policy documents in Thai, citing document codes and sections. Runs fully on the user's machine except for LLM calls (Gemini API). Designed to be reusable across different companies/clients without code changes.

## 2. Current State

### Architecture (stable, working)
The app is split into two processes communicating over local HTTP (`127.0.0.1:8765`):
- **`app.py`** — thin Streamlit UI client. No torch/faiss/llama_index/transformers imports. Starts the worker if it isn't running, polls `/health`, sends chat requests to `/chat`.
- **`rag_worker.py`** — standalone background process holding all the heavy stuff: HuggingFace embedding (BGE-M3), CrossEncoder reranker (BGE-reranker-v2-m3), FAISS vector store, LlamaIndex chat engine, GoogleGenAI (Gemini) calls. Runs `ThreadingHTTPServer`, exposes `/health` (GET) and `/chat` (POST, body `{session_id, prompt}`).

This split exists because running the ML libraries in-process with Streamlit caused a reproducible Windows access violation (0xc0000005, faulting module WINHTTP.dll) after the app sat idle. Multiple in-process fixes were tried and falsified (KMP_DUPLICATE_LIB_OK + import ordering, asyncio event loop policy, disabling Streamlit telemetry) before isolating the cause to native libraries (torch/faiss) sharing a process with Streamlit's threading model. `test_minimal_streamlit.py`-style testing proved bare Streamlit never crashes; adding the ML stack in-process always did. Full process isolation fixed it — confirmed working via a real chat exchange after the rewrite. **Not yet reconfirmed by the user that the idle-crash scenario specifically no longer reproduces** (only that chat works) — worth asking if it resurfaces.

### Multi-tenant / no-hardcoded-company-name (just done this session)
The user works for multiple companies and wants this codebase reusable, not tied to one org. Removed all hardcoded company name/branding:
- Added `COMPANY_NAME` env var (read via a hand-rolled `_load_dotenv()` in both `app.py` and `rag_worker.py`, default fallback `"องค์กรของคุณ"` if unset).
- `rag_worker.py`: `_build_sys_prompt()` now interpolates `COMPANY_NAME` instead of a previously hardcoded organization name, and dropped the hardcoded example doc-code format.
- `app.py`: page title changed from a client-specific title to generic "Policy RAG Assistant"; caption now interpolates `COMPANY_NAME`.
- `.env.example` documents the new `COMPANY_NAME` key.
- `.env` (gitignored, local only) was given the actual org name for the current deployment so it keeps working unchanged. Never put a real org name in any file that gets committed to git.

### Security / git hygiene (done previous session)
- `GOOGLE_API_KEY` externalized to `.env` (gitignored), `.env.example` committed as template.
- `.gitignore` excludes: `.env`, `venv/`, `__pycache__/`, `models/`, `storage/`, and all proprietary document folders (`Policies/`, `Procedures/`, `Manuals/`, `Forms/`, `Forms_Raw/`).
- All debug/one-off `test_*.py` scripts (14 of them) consolidated into a single `test_rag_pipeline.py` covering worker start, health check, and two chained chat calls (verifies session memory).
- `app_monolithic_backup.py` (pre-rewrite backup) deleted after the new architecture was confirmed working.
- Repo pushed to GitHub: `https://github.com/bornja55/Local-RAG` (already has an initial commit + push done by the user from PowerShell).

### Just deleted this session
- `Next_Steps_Plan.md` and old `handoff.md` (both stale, described an earlier Ollama-based prototype phase that no longer reflects the current architecture). This file replaces them.

### Done, same session as the COMPANY_NAME work
- `README.md` rewritten: company name removed, more engaging tone (emoji section headers, shields.io badges, features section) inspired by the style of `https://github.com/bornja55/Media-Downloader/blob/main/README.md`, plus a "เครดิต" section crediting LlamaIndex, Streamlit, FAISS, sentence-transformers/BGE-M3/BGE-reranker-v2-m3, and the Gemini API. No bilingual `README_EN.md` was created — user only referenced the example's visual style, not its bilingual structure; revisit only if the user asks for an English version.
- Verified via grep that no company-name references remain in any committed file (only `.env`, which is gitignored, still has the real org name — expected and fine).

## 3. Next Steps
1. Stage, commit, and push:
   ```
   cd "D:\Review Policy\Local  RAG"
   git add -A
   git status   # confirm .env, models/, storage/, document folders still excluded; confirm Next_Steps_Plan.md and old handoff.md show as deleted, README.md/app.py/rag_worker.py/.env.example/handoff.md show as modified or new
   git commit -m "Genericize company branding via COMPANY_NAME env var; refresh README and handoff docs"
   git push
   ```
2. Report back to the user with a concise summary of what changed (this user prefers short, direct responses — no full diff dumps).
4. Report back to the user with a summary of what changed (not a full diff dump — this user prefers concise responses).

## 4. Open Questions / Blockers
- Does the user want a bilingual `README_EN.md` too (matching the Media-Downloader example's bilingual pattern), or just the Thai README? Not asked yet — default to Thai-only unless requested, since only the style (not the bilingual structure) was cited as the thing to emulate.
- The idle-crash fix (process isolation) was validated via a working chat exchange but the user never explicitly confirmed "idle no longer crashes after N minutes" — flag this as an open item if the user mentions any future crash.
- Previously-flagged NITPICKs from a `/scrutinize` pass, still unaddressed (not currently requested, just tracked): no retry-in-progress UI feedback during Gemini quota backoff, no TTL/cleanup on the in-memory `_sessions` dict in `rag_worker.py` (slow leak on long-running deployments), possible race condition on concurrent requests within the same session, and a recommendation to rotate the `GOOGLE_API_KEY` since it was exposed in plaintext across ~10 files earlier in the project's history before being externalized to `.env`.
- No authentication/access control on the app — anyone with network access to the host machine can use it and consume the shared Gemini quota. Raised to the user once, not acted on.

## 5. Context & Constraints
- **Stack**: Python, Streamlit, LlamaIndex, HuggingFace `sentence-transformers` (BGE-M3 + BGE-reranker-v2-m3, loaded from local `models/` folder — not downloaded at runtime, `HF_HUB_OFFLINE=1`), FAISS (`faiss-cpu`), Google GenAI SDK via `llama_index.llms.google_genai.GoogleGenAI` (HTTP-based, NOT gRPC — no grpc package in requirements.txt, this was confirmed after an earlier incorrect handoff claimed otherwise).
- **Platform**: Windows. Several fixes are Windows-specific (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` for the worker subprocess, `KMP_DUPLICATE_LIB_OK` for MKL/OpenMP conflicts between faiss and torch).
- **No python-dotenv dependency** — `.env` loading is a hand-rolled ~10-line parser (`_load_dotenv()`), duplicated in `app.py`, `rag_worker.py`, and `extract_forms.py`. Keep this pattern consistent if adding new env-driven config — don't introduce python-dotenv as a new dependency without discussing it, since the user chose to avoid it.
- **Sandbox tooling caveat**: the `mcp__workspace__bash` mount of the connected folder can be a STALE snapshot for files edited via Read/Write/Edit tools during a session. Bash-based `cat`/`py_compile` checks on recently-edited files may show stale or corrupted content. Trust the Read/Write/Edit tool tool results as ground truth over bash `cat` output on this specific mount. New files written via `Write` sync initially; subsequent `Edit` calls may not reliably sync to the bash view.
- **User preferences**: wants concise responses, no unnecessary verbosity. Wants `/management-talk` skill used specifically for any marketing copy or user-facing explanations (like README). Responds in Thai; technical terms in English are fine.
- **Deletion of files in the mounted workspace folder** requires calling `mcp__cowork__allow_cowork_file_delete` first (permission denied otherwise) — already done for this session's deletions, but a fresh session will need to call it again.
- **What NOT to do**: don't re-introduce a hardcoded company name anywhere (defeats the point of this session's work); don't put the real `GOOGLE_API_KEY` value in anything that gets committed; don't touch `Policies/`, `Procedures/`, `Manuals/`, `Forms/`, `Forms_Raw/`, `models/`, or `storage/` in git operations (all intentionally gitignored, contain proprietary/large data).

## 6. Key Files
- `D:\Review Policy\Local  RAG\app.py` — Streamlit UI (thin client), just edited for COMPANY_NAME.
- `D:\Review Policy\Local  RAG\rag_worker.py` — background worker process, just edited for COMPANY_NAME.
- `D:\Review Policy\Local  RAG\.env` — local secrets (gitignored): `GOOGLE_API_KEY`, `COMPANY_NAME`.
- `D:\Review Policy\Local  RAG\.env.example` — committed template, documents both keys now.
- `D:\Review Policy\Local  RAG\README.md` — needs rewriting per Next Steps above.
- `D:\Review Policy\Local  RAG\.gitignore` — scoping rules, already correct, no changes needed.
- `D:\Review Policy\Local  RAG\requirements.txt` — check before writing README credits section.
- `D:\Review Policy\Local  RAG\test_rag_pipeline.py` — consolidated end-to-end test, run this to sanity-check the app still works after any change (`python test_rag_pipeline.py`).
- `D:\Review Policy\Local  RAG\stop_worker.bat` — kills the background worker process (needed after doc/index changes).
- Reference style examples (external, not local files): `https://github.com/bornja55/Media-Downloader/blob/main/README.md` and `README_EN.md`.
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             