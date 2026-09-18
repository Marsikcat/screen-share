@echo off
:: The original ScreenShare window: stream to viewers by IP, no channels.
cd /d "%~dp0"
if exist "venv\Scripts\pythonw.exe" (
    start "" venv\Scripts\pythonw.exe screen_share.py
) else if exist "python\pythonw.exe" (
    start "" python\pythonw.exe screen_share.py
) else (
    echo Run setup.bat first
    pause
)
