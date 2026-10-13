@echo off
chcp 65001 >nul
cd /d "%~dp0"
python dayuan_watch.py watch %*
pause
