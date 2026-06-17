@echo off
setlocal
cd /d "%~dp0"

echo This will delete old runs, uploads, temp files, latest exports, and cache files.
echo Code files will stay safe.
echo.
set /p CONFIRM=Type YES to continue: 
if /I not "%CONFIRM%"=="YES" (
  echo Cancelled.
  pause
  exit /b 0
)

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" clear_bot_history.py
) else (
  python clear_bot_history.py
)

echo.
echo History cleanup complete.
pause
