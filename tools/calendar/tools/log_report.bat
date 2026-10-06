@echo off
rem  Summarize logs for root-cause analysis (see tools\log_report.py).
rem  Usage: log_report.bat [ref] [--dir folder]
rem  ASCII only: cmd.exe reads .bat with the console code page.
pushd "%~dp0.." || exit /b 1
python tools\log_report.py %*
set RC=%ERRORLEVEL%
popd
pause
exit /b %RC%
