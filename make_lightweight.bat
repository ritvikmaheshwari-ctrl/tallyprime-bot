@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found. Run install_bot.bat first.
  pause
  exit /b 1
)

echo Removing optional heavy OCR packages from this bot environment...
".venv\Scripts\python.exe" -m pip uninstall -y easyocr torch torchvision torchaudio opencv-python opencv-python-headless

echo.
echo Reinstalling lightweight required packages...
".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo.
echo Lightweight mode is ready.
pause
