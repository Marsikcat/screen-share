@echo off
title ScreenShare
cd /d "%~dp0"

:: Try venv first (fastest)
if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe --version >nul 2>nul
    if not errorlevel 1 (
        call venv\Scripts\python screen_share.py
        exit /b
    )
    echo venv broken (wrong path). Re-create with setup.bat
)

:: Try bundled Python (portable)
if exist "python\python.exe" (
    python\python.exe screen_share.py
    if errorlevel 1 pause
    exit /b
)

:: Try system Python directly
python --version >nul 2>nul
if not errorlevel 1 (
    python screen_share.py
    if errorlevel 1 pause
    exit /b
)

echo Run setup.bat first (Python not found)
pause
