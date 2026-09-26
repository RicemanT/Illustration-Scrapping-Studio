@echo off
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 (
  python studio.py setup
) else (
  py -3 studio.py setup
)
pause
