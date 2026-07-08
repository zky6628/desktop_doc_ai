@echo off
setlocal

REM Change to script directory to ensure correct working path
pushd "%~dp0"

echo ========================================
echo   RAG Document AI - Flutter App
echo ========================================
echo.

cd flutter_app

REM ========== 1. Check Flutter ==========
echo [1/3] Checking Flutter...
call flutter --version
if errorlevel 1 (
    echo.
    echo [ERROR] Flutter not found! Please install Flutter SDK first.
    echo Install guide: https://docs.flutter.dev/get-started/install
    echo.
    popd
    pause
    exit /b 1
)
echo [OK] Flutter is available.
echo.

REM ========== 2. Get dependencies ==========
echo [2/3] Getting Flutter dependencies...
call flutter pub get
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to get dependencies!
    echo.
    popd
    pause
    exit /b 1
)
echo [OK] Dependencies ready.
echo.

REM ========== 3. Run app ==========
echo [3/3] Starting Flutter desktop app...
echo.
echo ========================================
echo   Make sure backend is running first!
echo   Run start_backend.bat to start backend.
echo ========================================
echo.

call flutter run -d windows

REM If flutter exits with error, show message and pause
if errorlevel 1 (
    echo.
    echo [ERROR] App stopped with error!
    echo.
    popd
    pause
    exit /b 1
)

popd
endlocal
