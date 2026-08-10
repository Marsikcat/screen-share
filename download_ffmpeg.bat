@echo off
cd /d "%~dp0"
call venv\Scripts\python screen_share.py download-ffmpeg
pause
