@echo off
cd /d G:\AIBOX
echo VoiceType Starting (独立环境 + RTX 5090 GPU)
echo =============================================
echo Hotkey: ` (grave/backtick)
echo Model: large-v3-turbo on CUDA
echo.

set PYTHONUNBUFFERED=1
"C:\Users\84238\.conda\envs\voicetype\python.exe" -u -m voicetype --hotkey grave

echo.
echo VoiceType exited.
pause
