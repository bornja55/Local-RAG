# Handoff to Claude Code

## The Context
The user is building a Local RAG application using Streamlit, LlamaIndex, Gemini API (`GoogleGenAI`), FAISS, and `sentence_transformers` (BGE-M3 and BGE-Reranker-v2-M3). 

## The Problem
1. **Initial Issue (Browser Timeout)**: The heavy PyTorch models (embedding and reranker) take ~4 minutes to load and allocate CUDA/RAM on their very first inference run. When this happened during the user's first chat interaction, the browser WebSocket timed out, resulting in `WinError 10054`.
2. **Warmup Fix**: We added a warmup routine (`embed_model.get_text_embedding("warmup")` and `reranker.predict(...)`) during the initial `st.spinner` page load to hide this delay.
3. **Silent Crash (0xc000071c)**: After the warmup, the user could type a chat message. But as soon as `chat_engine.chat()` was called, the Streamlit Tornado server silently crashed with `Windows fatal exception: code 0xc000071c` (STATUS_UNWIND_CONSOLIDATE).
4. **Root Cause of Silent Crash**: Streamlit creates a new thread for every interaction. Caching `GoogleGenAI` (which uses gRPC) or the `chat_engine` inside `st.session_state` or `@st.cache_resource` caused C++ cross-thread exception unwinding failures on Windows. When the second thread tried to use the gRPC channel created in the first thread, it segfaulted.
5. **Botched Refactoring**: To fix this, I attempted to refactor `app.py` so that only the heavy PyTorch tensors are cached via `@st.cache_resource`, while the lightweight objects (`GoogleGenAI`, `chat_engine`) are instantiated fresh on every Streamlit script run. However, my tool call botched the file replacement, deleting `load_embedding_model()` and leaving `NameError` and other syntax errors.

## What Claude Needs to Do
1. Fix `app.py`. 
2. Ensure `@st.cache_resource` is used **only** for `HuggingFaceEmbedding` and `CrossEncoder`, and include the warmup calls inside those cached functions.
3. Instantiate `GoogleGenAI`, `ChatMemoryBuffer` (stored in `session_state.memory`), and `index.as_chat_engine` freshly in the main script flow so they are bound to the current `ScriptRunner` thread, preventing the C++ thread crash.
4. Ensure `st.session_state.chat_engine` is NOT used. Just assign it to a local variable `chat_engine` in the script.
5. Verify the syntax and run `run_app.bat` to test.
