@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  call install_bot.bat
  if not exist ".venv\Scripts\python.exe" (
    exit /b 1
  )
)

if exist ".git" (
  git pull
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  echo Update complete.
) else (
  echo This folder is not connected to Git.
  echo Copy the latest bot folder from your main PC, then run this file again.
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  echo Requirements refreshed for the copied files.
)

pause
