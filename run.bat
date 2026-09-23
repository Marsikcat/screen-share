@echo off
title MarinCall
cd /d "%~dp0"

:: venv made by setup.bat on this PC
if exist "venv\Scripts\pythonw.exe" (
    venv\Scripts\python.exe -c "import PySide6, sounddevice, av" >nul 2>nul
    if not errorlevel 1 (
        start "" venv\Scripts\pythonw.exe app.py %*
        exit /b
    )
)

:: portable Python from setup.bat
if exist "python\pythonw.exe" (
    python\python.exe -c "import PySide6, sounddevice, av" >nul 2>nul
    if not errorlevel 1 (
        start "" python\pythonw.exe app.py %*
        exit /b
    )
)

echo Dependencies are missing (or the venv came from another PC). Running setup...
call setup.bat
