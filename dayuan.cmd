@echo off
chcp 65001 >nul
rem Use the WeCom "Smart Assistant" (DaYuan) as a command line tool.
rem   dayuan ask "check my unread messages"   send, wait, print the reply
rem   dayuan auto-reply                      daemon: timed check-unread + auto-reply
rem   dayuan read                            dump the current conversation
rem   dayuan status                          busy / pending approval / landmarks (JSON)
rem   dayuan wake                            bring the DaYuan window up (double-tap Ctrl)
rem   dayuan doctor                          diagnose permissions / window / landmarks
rem NOTE: keep this file ASCII-only. cmd.exe parses .bat in the OEM codepage,
rem so UTF-8 comments here get executed as garbage commands.
python "%~dp0dayuan_watch.py" %*
