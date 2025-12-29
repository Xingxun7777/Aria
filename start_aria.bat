@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Starting Aria...
".venv\Scripts\python.exe" launcher.py
pause
