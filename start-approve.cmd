@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Resident: click confirm cards only. Does not inject new prompts.
rem Close this window to stop.
python "%~dp0dayuan_watch.py" watch --no-inject %*
pause
