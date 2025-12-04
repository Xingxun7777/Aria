@echo off
REM 后台启动VoiceType（无控制台窗口）- 使用独立环境
cd /d G:\AIBOX
start "" /B "C:\Users\84238\.conda\envs\voicetype\pythonw.exe" "G:\AIBOX\voicetype\launcher.py"
