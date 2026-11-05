@echo off
cd /d "%~dp0"
set "PY=%~dp0python\python.exe"
if not exist "%PY%" set "PY=python"
echo Starting service... open http://127.0.0.1:8765/ in your browser.
"%PY%" app.py
pause
