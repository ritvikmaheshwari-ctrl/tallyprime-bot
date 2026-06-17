@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  call install_bot.bat
)

echo Installing optional OCR fallback. This can be slow and heavy on 8 GB RAM PCs.
".venv\Scripts\python.exe" -m pip install -r requirements-ocr-optional.txt

echo.
echo Optional OCR install complete.
pause
