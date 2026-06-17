@echo off
setlocal
cd /d "%~dp0"

echo Installing TallyPrime Entry Prep Bot...
echo.

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m venv .venv
) else (
  python -m venv .venv
)

if not exist ".venv\Scripts\python.exe" (
  echo Python was not found. Install Python 3.11 or 3.12 from https://www.python.org/downloads/windows/
  echo During install, tick "Add python.exe to PATH".
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo.
echo Install complete.
echo For scanned/photo bills, also install Tesseract OCR and Poppler. See LOCAL_SETUP.md.
pause
