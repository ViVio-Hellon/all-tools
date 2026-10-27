@echo off
rem  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
rem  Everything above the chcp line must stay ASCII (see start.bat).
chcp 932 >nul 2>&1
rem 看板発注システム 起動用
rem 引数はそのまま main.py へ渡します (例: run_kanban.bat --mode warehouse)。
rem --import-only などが無ければ、main.py が start_app.py へ渡して画面を開きます
rem 共有フォルダ(\\サーバ\...)に置いても動くよう、cd /d で移らずに start /D でこのフォルダを渡す
rem (cmd.exe は共有フォルダを今いる場所にできない。cd /d は失敗し、main.py が見つからない)
setlocal
start "" /D "%~dp0." pythonw "%~dp0main.py" %*
endlocal
