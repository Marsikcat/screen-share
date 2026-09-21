@echo off
:: Builds dist\MoyDiscord\ (exe) and dist\MoyDiscord-Setup-X.Y.Z.exe (installer).
:: Needs Python 3.10+ and Inno Setup 6 (winget install JRSoftware.InnoSetup).
cd /d "%~dp0.."
if not exist ".build\Scripts\python.exe" (
    echo Creating build environment .build ...
    python -m venv .build || (echo Python not found & pause & exit /b 1)
)
.build\Scripts\python -m pip install -q --upgrade pip
.build\Scripts\python -m pip install -q -r requirements.txt pyinstaller || (pause & exit /b 1)
.build\Scripts\python tools\build.py
if errorlevel 1 pause & exit /b 1
explorer dist
