@echo off
chcp 65001 >nul
setlocal

REM Change to script directory to ensure correct working path
pushd "%~dp0"

echo ========================================
echo   RAG Document AI - Backend Server
echo ========================================
echo.

cd python_rag

REM ========== 1. Check Python (3.12+) ==========
echo [1/5] Checking Python...
python --version
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if errorlevel 1 (
    echo.
    echo [ERROR] Python 3.12 or higher is required.
    echo Download: https://www.python.org/downloads/
    echo.
    popd
    pause
    exit /b 1
)
echo [OK] Python version is supported.
echo.

REM ========== 2. Check uv ==========
REM Dependencies are locked in requirements.lock and installed with uv
echo [2/5] Checking uv...
uv --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] uv not found. Dependencies are installed from requirements.lock via uv.
    echo Install: https://docs.astral.sh/uv/getting-started/installation/
    echo.
    popd
    pause
    exit /b 1
)
echo [OK] uv is installed.
echo.

REM ========== 3. Check .env file ==========
echo [3/5] Checking .env file...
if not exist ".env" (
    echo .env not found, copying from .env.example...
    copy ".env.example" ".env" >nul
    echo.
    echo [WARNING] Edit python_rag\.env and set DASHSCOPE_API_KEY
    echo           for embedding / rerank / generation.
    echo           MINERU_API_TOKEN is only needed for images and scanned PDFs.
    echo Get API key: https://dashscope.console.aliyun.com/
    echo.
) else (
    echo [OK] .env file exists.
)
echo.

REM ========== 4. Check dependencies (locked set) ==========
echo [4/5] Checking dependencies...
python -c "import fastapi, chromadb, jieba" >nul 2>&1
if errorlevel 1 (
    echo Dependencies missing. Installing from requirements.lock ...
    echo This may take a few minutes...
    echo.
    uv pip install --system -r requirements.lock
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to install dependencies!
        echo Check your network connection and try again.
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

REM ========== 5. Start server ==========
echo [5/5] Starting FastAPI server...
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