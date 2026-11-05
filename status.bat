@echo off
cd /d "%~dp0"
set "PY=%~dp0python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" install_autostart.py status
pause
