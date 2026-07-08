@echo off
setlocal

REM Change to script directory to ensure correct working path
pushd "%~dp0"

echo ========================================
echo   RAG Document AI - Backend Server
echo ========================================
echo.

cd python_rag

REM ========== 1. Check Python ==========
echo [1/4] Checking Python...
python --version
if errorlevel 1 (
    echo.
    echo [ERROR] Python not found! Please install Python 3.10 or higher.
    echo Download: https://www.python.org/downloads/
    echo.
    popd
    pause
    exit /b 1
)
echo [OK] Python is installed.
echo.

REM ========== 2. Check .env file ==========
echo [2/4] Checking .env file...
if not exist ".env" (
    echo .env not found, copying from .env.example...
    copy ".env.example" ".env"
    echo.
    echo [WARNING] Please edit python_rag\.env and set your DASHSCOPE_API_KEY!
    echo Get API key: https://dashscope.console.aliyun.com/
    echo.
) else (
    echo [OK] .env file exists.
)
echo.

REM ========== 3. Install dependencies ==========
echo [3/4] Checking dependencies...
pip show fastapi >nul 2>&1
if errorlevel 1 (
    echo Dependencies not installed. Installing...
    echo This may take a few minutes...
    echo.
    pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to install dependencies!
        echo Please check your network connection and try again.
        echo.
        popd
        pause
        exit /b 1
    )
    echo [OK] Dependencies installed successfully.
) else (
    echo [OK] Dependencies already installed.
)
echo.

REM ========== 4. Start server ==========
echo [4/4] Starting FastAPI server...
echo.
echo ========================================
echo   Server URL: http://127.0.0.1:8000
echo   API Docs:   http://127.0.0.1:8000/docs
echo   Press Ctrl+C to stop
echo ========================================
echo.

python main.py

REM If python exits with error, show message and pause
if errorlevel 1 (
    echo.
    echo [ERROR] Server stopped with error!
    echo.
    popd
    pause
    exit /b 1
)

popd
endlocal
