@echo off
REM Aria Debug - 显示控制台输出以便调试
cd /d "%~dp0"
".venv\Scripts\python.exe" launcher.py
pause
