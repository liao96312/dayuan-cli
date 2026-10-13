@echo off
chcp 65001 >nul
rem Daemon: every N minutes ask DaYuan to check unread and reply.
rem CLI reads the confirm card (recipient + content) and decides approve/cancel.
rem   start-auto-reply.cmd                 default 300s
rem   start-auto-reply.cmd --interval 120
rem   start-auto-reply.cmd --review        read the card, then CANCEL (never sends)
rem   start-auto-reply.cmd --allow 张三 --deny 老板
rem ASCII-only on purpose: cmd.exe parses .bat in the OEM codepage.
python "%~dp0dayuan_watch.py" auto-reply %*
pause
