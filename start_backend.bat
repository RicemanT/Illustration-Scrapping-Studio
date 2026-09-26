@echo off
setlocal
set "PROJECT_ROOT=%~dp0"
set "BACKEND_PYTHON=%PROJECT_ROOT%.venv\Scripts\python.exe"

echo Starting Illustration Scrapping Studio Backend...
if not exist "%BACKEND_PYTHON%" (
    echo Backend virtual environment not found.
    echo Run: python -m venv .venv
    pause
    exit /b 1
)

"%BACKEND_PYTHON%" -c "import fastapi, httpx, PIL, imagehash, imageio_ffmpeg, cv2" >nul 2>&1
if errorlevel 1 (
    echo Installing backend dependencies...
    "%BACKEND_PYTHON%" -m pip install -r "%PROJECT_ROOT%backend\requirements.txt"
    if errorlevel 1 (
        echo Dependency installation failed.
        pause
        exit /b 1
    )
)

cd /d "%PROJECT_ROOT%backend"
set "PYTHONPATH=%PROJECT_ROOT%backend"
"%BACKEND_PYTHON%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
exit /b %errorlevel%
