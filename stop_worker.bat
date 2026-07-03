@echo off
chcp 65001 >nul
echo กำลังหา RAG worker process (port 8765)...

for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8765 ^| findstr LISTENING') do call :killpid %%a

echo เสร็จแล้ว
pause
exit /b

:killpid
echo พบ PID %1 - กำลังปิด...
taskkill /PID %1 /F
exit /b
