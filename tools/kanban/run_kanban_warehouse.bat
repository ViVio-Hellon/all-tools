@echo off
rem 看板発注システム 倉庫モード起動用
setlocal
cd /d "%~dp0"
start "" pythonw main.py --mode warehouse
endlocal
