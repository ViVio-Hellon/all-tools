@echo off
rem 看板発注システム 起動用
rem 引数はそのまま main.py へ渡します (例: run_kanban.bat --mode warehouse)
setlocal
cd /d "%~dp0"
start "" pythonw main.py %*
endlocal
