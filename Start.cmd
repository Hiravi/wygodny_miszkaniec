@echo off

cd /d "%~dp0"

"runtime\python\python.exe" -B -m tools.release.minimal_launcher %*

if errorlevel 1 pause

