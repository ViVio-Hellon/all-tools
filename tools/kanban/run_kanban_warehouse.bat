@echo off
rem  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
rem  Everything above the chcp line must stay ASCII (see start.bat).
chcp 932 >nul 2>&1
rem 看板発注システム 倉庫モードで開く(この端末に保存したモードは変えない。この 1 回だけ)
rem main.py は --import-only などが無ければ start_app.py へ渡して画面を開く(Start.vbs と同じ道)。
rem 共有フォルダ(\\サーバ\...)に置いても動くよう、cd /d で移らずに start /D でこのフォルダを渡す
rem (cmd.exe は共有フォルダを今いる場所にできない。cd /d は失敗し、main.py が見つからない)
setlocal
start "" /D "%~dp0." pythonw "%~dp0main.py" --mode warehouse
endlocal
