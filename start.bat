@echo off
REM ===========================================================================
REM  Akilli Sicak Satis Yonetim Sistemi - Baslatici
REM  Smart Van Sales Management System - Launcher
REM ===========================================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  [X] Sanal ortam bulunamadi. / Virtual environment not found.
    echo      Once kurulumu calistirin: / Run setup first:
    echo.
    echo      powershell -ExecutionPolicy Bypass -File .\setup.ps1
    echo.
    pause
    exit /b 1
)

echo.
echo  ===========================================================
echo   Akilli Sicak Satis Yonetim Sistemi
echo  ===========================================================
echo.
echo   Backend  : http://127.0.0.1:8000
echo   API Docs : http://127.0.0.1:8000/docs
echo   Frontend : http://localhost:5173
echo.
echo   Durdurmak icin bu pencereleri kapatin. / Close windows to stop.
echo  ===========================================================
echo.

start "Van Sales - Backend" cmd /k "cd /d "%~dp0backend" && "%~dp0.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload"

if exist "frontend\node_modules" (
    start "Van Sales - Frontend" cmd /k "cd /d "%~dp0frontend" && npm run dev"
    timeout /t 6 /nobreak >nul
    start "" "http://localhost:5173"
) else (
    echo  [!] frontend\node_modules yok - sadece backend baslatildi.
    echo      Frontend'i kurmak icin: cd frontend ^&^& npm install
    timeout /t 4 /nobreak >nul
    start "" "http://127.0.0.1:8000/docs"
)

endlocal
