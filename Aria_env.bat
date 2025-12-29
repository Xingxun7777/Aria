@echo off
REM 后台启动Aria（无控制台窗口）
cd /d "%~dp0"
start "" /B ".venv\Scripts\pythonw.exe" "launcher.py"
