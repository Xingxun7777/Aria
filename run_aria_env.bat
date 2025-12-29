@echo off
cd /d G:\AIBOX
echo Aria Starting (独立环境 + RTX 5090 GPU)
echo =============================================
echo Hotkey: ` (grave/backtick)
echo Model: large-v3-turbo on CUDA
echo.

set PYTHONUNBUFFERED=1
"C:\Users\84238\.conda\envs\aria\python.exe" -u -m aria --hotkey grave

echo.
echo Aria exited.
pause
