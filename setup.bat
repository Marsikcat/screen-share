@echo off
title MarinCall Setup

set "ROOT=%~dp0"
cd /d "%ROOT%"

echo ==========================================
echo   MarinCall Setup
echo ==========================================
echo.

:: --- Step 1: Find Python ---
echo [1/4] Checking Python...

python --version >nul 2>nul
if %errorlevel% equ 0 goto :have_python

py --version >nul 2>nul
if %errorlevel% equ 0 set "PY_EXE=py" & goto :have_python

if exist "python\python.exe" set "PY_EXE=python\python.exe" & goto :have_python

echo   Python not found. Downloading Python 3.12.9...
if not exist "python" mkdir python
powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.9/python-3.12.9-embed-amd64.zip' -OutFile 'python_tmp.zip' -UseBasicParsing" >nul 2>nul
if not exist python_tmp.zip (
    echo   Download failed. Install Python 3.10+ from python.org and run setup again.
    pause
    exit /b 1
)
powershell -Command "Expand-Archive -Path python_tmp.zip -DestinationPath python -Force" >nul 2>nul
del python_tmp.zip 2>nul
:: Enable pip in embedded Python
if exist "python\python312._pth" (
    powershell -Command "(Get-Content 'python\python312._pth') -replace '#import site','import site' | Set-Content 'python\python312._pth'" >nul
)
if not exist "python\python.exe" (
    echo   Failed to extract Python.
    pause
    exit /b 1
)
powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile 'python\get-pip.py' -UseBasicParsing" >nul 2>nul
python\python.exe python\get-pip.py --quiet >nul 2>nul
set "PY_EXE=python\python.exe"
echo   Portable Python 3.12.9 installed

:have_python
if "%PY_EXE%"=="" set "PY_EXE=python"
%PY_EXE% --version 2>nul

:: --- Step 2: Install dependencies ---
echo [2/4] Installing dependencies (Qt, audio, Opus) - may take a few minutes...

if "%PY_EXE%"=="python\python.exe" (
    %PY_EXE% -m pip install --upgrade pip --quiet
    %PY_EXE% -m pip install -r requirements.txt --quiet
    set "PYW=%ROOT%python\pythonw.exe"
    goto :deps_done
)

:: A venv copied from another PC points at a Python that does not exist here
set "VENV_OK=0"
if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe --version >nul 2>nul
    if not errorlevel 1 set "VENV_OK=1"
)
if "%VENV_OK%"=="0" (
    if exist "venv" (
        echo   venv from another PC detected, re-creating...
        rmdir /s /q venv 2>nul
    )
    %PY_EXE% -m venv venv
)
call venv\Scripts\python -m pip install --upgrade pip --quiet
call venv\Scripts\python -m pip install -r requirements.txt --quiet
set "PYW=%ROOT%venv\Scripts\pythonw.exe"

:deps_done
venv\Scripts\python.exe -c "import PySide6, sounddevice, av" >nul 2>nul || python\python.exe -c "import PySide6, sounddevice, av" >nul 2>nul
if errorlevel 1 (
    echo   Some packages failed to install. If the error mentions long paths, move the
    echo   folder closer to the drive root (e.g. C:\MarinCall) and run setup again.
) else (
    echo   Done
)

:: --- Step 3: Download FFmpeg (screen share) ---
echo [3/4] Checking FFmpeg...
if not exist "ffmpeg\ffmpeg.exe" (
    echo   Downloading FFmpeg...
    if not exist "ffmpeg" mkdir ffmpeg
    powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'ffmpeg.zip' -UseBasicParsing" >nul 2>nul
    if exist ffmpeg.zip (
        powershell -Command "Expand-Archive -Path ffmpeg.zip -DestinationPath ffmpeg_tmp -Force" >nul 2>nul
        for /r ffmpeg_tmp %%f in (ffmpeg.exe ffplay.exe) do (
            if exist "%%f" copy "%%f" ffmpeg\ >nul 2>nul
        )
        rmdir /s /q ffmpeg_tmp 2>nul
        del ffmpeg.zip 2>nul
    ) else (
        echo   FFmpeg download failed. Screen share will not work until you run download_ffmpeg.bat
    )
)
if exist "ffmpeg\ffmpeg.exe" (
    ffmpeg\ffmpeg.exe -hide_banner -encoders 2>nul | findstr h264_nvenc >nul 2>nul
    if errorlevel 1 (echo   FFmpeg OK - CPU encoding) else (echo   FFmpeg OK - NVENC supported)
)

:: --- Step 4: Shortcuts ---
echo [4/4] Creating shortcuts...
del screen_share.lnk 2>nul
powershell -NoProfile -Command "$w=New-Object -ComObject WScript.Shell; foreach($p in @('%ROOT%MarinCall.lnk', [Environment]::GetFolderPath('Desktop')+'\MarinCall.lnk')){ $s=$w.CreateShortcut($p); $s.TargetPath='%PYW%'; $s.Arguments='\"%ROOT%app.py\"'; $s.WorkingDirectory='%ROOT%'; $s.Save() }" >nul 2>nul
echo   Done (MarinCall.lnk here and on the desktop)

echo.
echo ==========================================
echo   Setup complete! Start: run.bat or MarinCall.lnk
echo   Autostart and tray options are in Settings.
echo ==========================================
pause
