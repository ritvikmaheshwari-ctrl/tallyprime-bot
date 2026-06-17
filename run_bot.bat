@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  call install_bot.bat
)

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
  taskkill /PID %%a /F >nul 2>nul
)

start "" "http://127.0.0.1:8765/bills"
".venv\Scripts\python.exe" app.py
pause
