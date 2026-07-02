*Read this in other languages: [🇹🇭 ภาษาไทย](README.md), [🇬🇧 English](README_EN.md)*

# 🤖 Policy RAG Assistant

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python)
![Streamlit](https://img.shields.io/badge/UI-Streamlit-FF4B4B?style=for-the-badge&logo=streamlit)
![Gemini](https://img.shields.io/badge/LLM-Gemini-8E75B2?style=for-the-badge&logo=googlegemini)

An AI assistant that reads all of your organization's policies and procedures for you. Ask a question in plain language and get an answer instantly — complete with the source document code and section — no more opening files one by one or hunting with Ctrl+F.

Built to work with **any organization**, not tied to a specific company. Just point it at your documents and set your organization's name in a `.env` file, and it's ready to go.

---

## ✨ Key Features

- **🔍 Understands meaning, not just keywords** — Uses BGE-M3 (embedding) together with BGE-reranker-v2-m3 (reranker), so it finds the right answer even when your question doesn't match the document's exact wording.
- **🧠 Remembers the conversation** — Ask follow-up questions without repeating context; the system keeps track of what's already been discussed in the session.
- **📎 Always cites its sources** — Every answer comes with the document code and section it was drawn from, so you can trace it back to the original. No guessing, no made-up answers.
- **🏢 Rebrandable without touching code** — Set your organization's name via the `COMPANY_NAME` variable in `.env`. Deploy the same codebase for as many organizations as you need.
- **🛡️ Built to survive long sessions** — The AI processing runs in a completely separate process from the web UI (see architecture below), so the page doesn't freeze or crash after sitting idle.
- **💾 Your documents never leave your machine** — Everything runs locally except the call to the Gemini API used to compose the final answer.
- **📝 Draft new policies with automatic self-review (experimental)** — Not just retrieval: compose a brand-new policy document in one click, grounded in your existing policies for consistency, with an instant report flagging anything worth double-checking before you use it. See details below.
- **💬 Discuss your draft in chat (same session only)** — After drafting, switch back to regular Q&A mode and ask follow-up questions about it (e.g. "explain section 3 of the draft"). Clearly labeled as unapproved AI-generated content so it never gets confused with real policy.
- **🔁 Automatic model fallback on quota limits (optional)** — Configure a fallback model in `.env`; if the primary model hits its quota repeatedly, the system automatically retries with the fallback — no manual intervention needed.

---

## 🚀 Getting Started

**Prerequisites:**
- Python 3.10+
- A `models/` folder (embedding + reranker models)
- Your policy document folders (`Policies/`, `Procedures/`, `Manuals/`, `Forms/`)
- A Gemini API key ([get one free at Google AI Studio](https://aistudio.google.com/apikey))

None of the above are stored in git (see why below) — you'll need to provide them yourself in the project folder.

**⚠️ Check before installing — free up at least 15 GB of disk space**

This system uses noticeably more disk space than a typical app, because it runs AI models locally instead of just calling an API:

| Component | Approx. size |
|---|---|
| All libraries in `requirements.txt` (mainly PyTorch + LlamaIndex) | ~5-7 GB |
| Embedding model (BGE-M3) + reranker (BGE-reranker-v2-m3) in `models/` | ~4-5 GB |
| Search index `storage/` built by `build_index.py` | Tens of MB for a typical policy document set (grows with more documents) |

**On GPUs:** not required — the system is designed to run fine on CPU (that's why the first launch waits ~4-5 minutes for models to load, as noted below). If you have an NVIDIA GPU and want `build_index.py` and model loading to run faster, install a CUDA-enabled build of PyTorch yourself **before** running `pip install -r requirements.txt` — the version pulled in by that file is CPU-only by default. Pick the install command that matches your GPU at [pytorch.org/get-started](https://pytorch.org/get-started/locally/).

1. Copy `.env.example` to `.env` and fill in your API key and organization name:
   ```
   GOOGLE_API_KEY=your-gemini-api-key-here
   COMPANY_NAME=Your Company Ltd.
   ```
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Build the search index from your documents (no extra install needed — uses the same libraries from step 2; run once before first use, and again any time documents are added or edited):
   ```
   python build_index.py
   ```
   This takes about 4-5 minutes and produces a `storage/` folder that the app loads on startup.
4. Run `run_app.bat`
5. Wait about 4 minutes on the very first launch while the models load. Subsequent launches are much faster since the models stay loaded in the background.

To fully shut the system down (forcing a fresh model load next time — e.g. after updating documents), run `stop_worker.bat`.

---

## 📝 Draft Mode (Experimental)

Normal Q&A mode only answers from what's actually in your documents (to prevent hallucination). But sometimes you need to **draft a policy that doesn't exist yet** — say, a procurement policy or a PDPA privacy policy. Draft mode does that: it asks for the details it actually needs first, then drafts, and reviews its own work before handing it to you.

**How to use it:** turn on the "📝 Draft Mode (Experimental)" toggle in the sidebar, then follow two steps:

1. Enter the policy topic (and any extra instructions) → click **"❓ Generate Clarifying Questions."** The system compares your topic against existing policies and asks only about what's actually missing (e.g. a "PDPA Policy" topic will trigger questions about what personal data you collect, whether you have a DPO, etc. — things your existing IT/Risk policies don't cover). Answer what you know; skip anything you're unsure about, nothing is required.
2. Click **"✅ Confirm Draft."** The system drafts the document from what you provided, then critiques it immediately.

**What you get:**
1. A full policy draft (Markdown), grounded in the format and principles of your existing policies. Anywhere you skipped a question, the draft inserts a `[Needs input: ...]` marker instead of guessing an answer.
2. An immediate review — the system critiques its own draft against your existing policies, in four severity levels: **CRITICAL** (clear conflict / high risk), **WARNING** (worth checking before use — including any leftover `[Needs input: ...]` markers), **NITPICK** (minor), **VERDICT** (one-line summary).
3. A "Download as Word (.docx)" button.

**Good to know:**
- ⚠️ **This draft is not a finished document.** A human must always review and approve it before real use — even if the review found no CRITICAL issues. The download button stays active even when CRITICAL issues are found, because the draft is meant as a starting point for you to revise, not a final ruling.
- The .docx file is a plain structured document (headings/paragraphs/bullets) — it **does not match your organization's real template** (no signature/approval tables, revision history, or letterhead). Copy the content into your actual template before submitting it for approval.
- Draft mode uses a different Gemini model from regular Q&A for the drafting and critique steps (configurable via `GEMINI_MODEL_DRAFT` in `.env`, default `gemini-3.5-flash`), since composing and critiquing a document needs more reasoning than plain retrieval. The clarifying-questions step reuses the regular Q&A model (`GEMINI_MODEL_CHAT`) since it's a lighter task — so each full draft request costs more than a regular question, but less than if every step used the heavier model.
- Once a draft is confirmed, switch the toggle back off and return to regular chat — you'll see a banner letting you know this session has a draft you can ask about. This only works within the same session (same browser tab) — closing or refreshing the page clears it from chat memory (it does not delete any .docx file you already downloaded).

---

## 🔁 Automatic Model Fallback on Quota Limits (Optional)

If heavy usage causes your primary model to hit its quota (429 / RESOURCE_EXHAUSTED) often, configure a fallback model in `.env`:

```
GEMINI_MODEL_CHAT_FALLBACK=gemma-4-26b
GEMINI_MODEL_DRAFT_FALLBACK=gemma-4-31b
```

The system only switches to the fallback **after the primary model has exhausted all 3 retries and is still hitting a quota error** — it never falls back on the first error, and never falls back for non-quota errors. The default is empty, which disables this feature entirely (original behavior unchanged).

**Good to know:** responses within the same session may occasionally come from a different model without any explicit notice. Test the quality of whichever fallback model you choose before relying on it in production (see ADR-003).

---

## 🏗️ Architecture

The app is split into two separate processes that talk to each other over local HTTP:

- **`app.py`** — the Streamlit web page the user sees. Very lightweight; no AI processing happens in this process at all.
- **`rag_worker.py`** — the background process that does all the heavy lifting (document search + calling Gemini), completely separate from Streamlit.

**Why split it up?** During development, we found that running the AI models in the same process as Streamlit on Windows caused the app to randomly freeze or crash after some period of use. Splitting them into separate processes fixed this — and as a bonus, the web page now restarts much faster since it doesn't need to reload the models every time.

Other files in the project:
- `build_index.py` — builds/rebuilds the search index (`storage/`) from documents in `Policies/Procedures/Manuals/Forms` — re-run any time documents are added.
- `extract_forms.py`, `convert_forms_to_txt.py`, `dump_raw_forms.py` — tools for converting raw form files into Markdown before ingestion.
- `generate_docx.py` — converts Markdown to a .docx file, used by Draft Mode (see above).
- `test_rag_pipeline.py` — an end-to-end test that verifies the system works correctly (no need to launch Streamlit first). Run with `python test_rag_pipeline.py`.

---

## 🚫 What's Not Tracked in Git (Intentionally)

| Excluded | Reason |
|---|---|
| `.env` | Contains your real API key and organization name |
| `models/` | Model weight files are too large to manage well in git |
| `storage/` | Search index — can be rebuilt anytime with `build_index.py` |
| `Policies/`, `Procedures/`, `Manuals/`, `Forms/`, `Forms_Raw/` | Internal organization documents — shouldn't live in a git repo |

---

## 🩺 Troubleshooting

- **The page is stuck on "Starting RAG worker process..." for too long** — Check the `rag_worker.log` file in the project folder; it will tell you why (e.g. missing `.env` or model files).
- **I edited a document but the answers didn't update** — Re-run `build_index.py`, then run `stop_worker.bat` before reopening the app (the worker doesn't know documents changed until it restarts).
- **How do I know if the worker is still running?** — Open Task Manager and look for a `python.exe` process that isn't attached to any terminal window.

---

## 🤝 Credits

This project is built on top of excellent open-source technology. Special thanks to:

- **[LlamaIndex](https://github.com/run-llama/llama_index)** — the core framework powering the RAG pipeline, chat engine, and vector store integration.
- **[Streamlit](https://github.com/streamlit/streamlit)** — the framework behind the web UI, making the system accessible through a browser.
- **[FAISS](https://github.com/facebookresearch/faiss)** (Meta AI) — the fast vector similarity search engine at the core of the document retrieval system.
- **[sentence-transformers](https://github.com/UKPLab/sentence-transformers)** and the **BGE-M3** / **BGE-reranker-v2-m3** models (BAAI) — the embedding and reranking models that make semantic search accurate.
- **[Google Gemini API](https://ai.google.dev/)** — the language model that composes and answers questions from the retrieved documents.

---

*Want to deploy this for a different organization? Just change `COMPANY_NAME` and the documents in `Policies/Procedures/Manuals/Forms` — no code changes needed.*
