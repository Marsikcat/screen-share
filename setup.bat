@echo off
title ScreenShare Setup

set "ROOT=%~dp0"
cd /d "%ROOT%"

echo ==========================================
echo   ScreenShare Setup
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
    echo   Download failed. Install Python 3.10+ manually.
    pause
    exit /b 1
)
powershell -Command "Expand-Archive -Path python_tmp.zip -DestinationPath python -Force" >nul 2>nul
del python_tmp.zip 2>nul
:: Enable pip in embedded Python
if exist "python\python._pth" (
    powershell -Command "(Get-Content 'python\python._pth') -replace '#import site','import site' | Set-Content 'python\python._pth'" >nul
)
if not exist "python\python.exe" (
    echo   Failed to extract Python.
    pause
    exit /b 1
)
python\python.exe -m ensurepip --upgrade --quiet >nul 2>nul
set "PY_EXE=python\python.exe"
echo   Python 3.12.9 downloaded and configured

:have_python
if "%PY_EXE%"=="" set "PY_EXE=python"

:: --- Show Python version ---
%PY_EXE% --version 2>nul

:: --- Step 2: Install dependencies ---
echo [2/4] Installing dependencies...

if "%PY_EXE%"=="python" goto :use_venv
if "%PY_EXE%"=="py" goto :use_venv

:: Using local/bundled Python - install directly
%PY_EXE% -m pip install --upgrade pip --quiet
%PY_EXE% -m pip install mss pillow numpy --quiet
goto :deps_done

:use_venv
:: Check if existing venv is broken (wrong Python path)
set "VENV_OK=0"
if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe --version >nul 2>nul
    if not errorlevel 1 set "VENV_OK=1"
)
if "%VENV_OK%"=="1" goto :venv_exists

:: Remove broken venv
if exist "venv\Scripts\python.exe" (
    echo   venv from another PC detected, re-creating...
    rmdir /s /q venv 2>nul
)

%PY_EXE% -m venv venv
if errorlevel 1 (
    echo   venv failed, installing directly
    %PY_EXE% -m pip install mss pillow numpy --quiet
    goto :deps_done
)
:venv_exists
call venv\Scripts\python -m pip install --upgrade pip --quiet
call venv\Scripts\pip install mss pillow numpy --quiet

:deps_done
echo   Done

:: --- Step 3: Download FFmpeg ---
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
        echo   FFmpeg download failed. Run download_ffmpeg.bat later.
        goto :skip_ffmpeg_check
    )
)
:: Verify FFmpeg works
ffmpeg\ffmpeg.exe -version >nul 2>nul
if errorlevel 1 (
    echo   FFmpeg binary is broken. Try deleting ffmpeg folder and re-run setup.
) else (
    for /f "tokens=1-3" %%a in ('ffmpeg\ffmpeg.exe -version 2^>nul') do set "FF_VER=%%a %%b %%c" & goto :ffmpeg_ok
    :ffmpeg_ok
    ffmpeg\ffmpeg.exe -encoders 2>nul | findstr h264_nvenc >nul 2>nul
    if errorlevel 1 (
        echo   FFmpeg OK (CPU only, no NVENC)
    ) else (
        echo   FFmpeg OK (NVENC supported)
    )
)
:skip_ffmpeg_check

:: --- Step 4: Create shortcut ---
echo [4/4] Creating shortcut...
del screen_share.lnk 2>nul

if "%PY_EXE%"=="python\python.exe" (
    set "TARGET=%ROOT%python\pythonw.exe"
) else (
    set "TARGET=%ROOT%venv\Scripts\pythonw.exe"
)
powershell -Command "Add-Type -AssemblyName System.Windows.Forms; $w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut('%ROOT%screen_share.lnk'); $s.TargetPath='%TARGET%'; $s.Arguments='%ROOT%screen_share.py'; $s.WorkingDirectory='%ROOT%'; $s.Save()" >nul 2>nul
echo   Done

echo.
echo ==========================================
echo   Setup complete!
echo   Run: run.bat or screen_share.lnk
echo ==========================================
pause
