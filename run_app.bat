@echo off
cd /d "D:\Review Policy\Local  RAG"
title Local RAG Application
echo ==================================================
echo Starting Local RAG Application...
echo Please wait, your browser will open automatically.
echo ==================================================
cmd /k ".\venv\Scripts\streamlit.exe run app.py"
