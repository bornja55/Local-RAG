@echo off
echo กำลังหา RAG worker process (port 8765)...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8765 ^| findstr LISTENING') do (
    echo พบ PID %%a - กำลังปิด...
    taskkill /PID %%a /F
)
echo เสร็จแล้ว
pause
