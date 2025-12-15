@echo off
chcp 65001 >nul
title VoiceType DEBUG

set "PROJECT_ROOT=%~dp0"
set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"
set "VENV_DIR=%PROJECT_ROOT%\.venv"

:: PATH Isolation
set "TORCH_LIB=%VENV_DIR%\Lib\site-packages\torch\lib"
set "PATH=%TORCH_LIB%;%VENV_DIR%\Scripts;%SystemRoot%\system32;%SystemRoot%;%SystemRoot%\System32\Wbem"
for %%I in ("%PROJECT_ROOT%") do set "PARENT_DIR=%%~dpI"
set "PYTHONPATH=%PARENT_DIR%"
set "PYTHONHOME="
set "KMP_DUPLICATE_LIB_OK=TRUE"

echo ========================================
echo VoiceType DEBUG Mode
echo ========================================
echo.

cd /d "%PROJECT_ROOT%"
"%VENV_DIR%\Scripts\python.exe" launcher.py %*

echo.
echo ========================================
echo Press any key to exit...
pause >nul
