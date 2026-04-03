@echo off
REM Aria - 静默启动
cd /d "%~dp0"
REM Use Aria.exe (copy of pythonw.exe) so Task Manager shows "Aria" not "pythonw"
start "" /B ".venv\Scripts\Aria.exe" "launcher.py"
