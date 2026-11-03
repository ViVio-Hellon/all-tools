@echo off
rem  Make the distribution folder (see scripts\make_dist.py).
rem  Default output: next to the tool, in <dist>\VER<version>\<display name>\ .
rem  The version goes in the parent folder; the folder itself has no version
rem  (launcher rule: keep the path). With --zip, the zip name has the version
rem  but the top folder inside the zip does not.
rem  ASCII only: cmd.exe reads .bat with the console code page.
pushd "%~dp0.." || exit /b 1
python scripts\make_dist.py %*
set RC=%ERRORLEVEL%
popd
pause
exit /b %RC%
