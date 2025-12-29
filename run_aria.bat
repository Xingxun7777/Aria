@echo off
cd /d "%~dp0"
echo Aria Starting...
echo =====================
echo Hotkey: ` (grave/backtick)
echo Mode: Toggle (press to start, press again to stop)
echo.

set PYTHONUNBUFFERED=1
".venv\Scripts\python.exe" -u -m aria --hotkey grave

echo.
echo Aria exited.
pause
