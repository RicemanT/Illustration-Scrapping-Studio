@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run Setup Studio.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" studio.py run
if errorlevel 1 pause
