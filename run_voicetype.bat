@echo off
cd /d G:\AIBOX
echo VoiceType Starting...
echo =====================
echo Hotkey: ` (grave/backtick)
echo Mode: Toggle (press to start, press again to stop)
echo.

set PYTHONUNBUFFERED=1
F:\anaconda\python.exe -u -m voicetype --hotkey grave

echo.
echo VoiceType exited.
pause
