@echo off
REM Aria - 静默启动
cd /d "%~dp0"
start "" /B ".venv\Scripts\pythonw.exe" "launcher.py"
