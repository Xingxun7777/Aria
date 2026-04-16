@echo off
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
title Aria Update
if not exist "_internal\app\aria\app.py" (
    echo.
    echo  [Error] Please run this file in the Aria install directory.
    echo.
    pause
    exit /b 1
)
"_internal\python.exe" -u "_internal\app\aria\update_tool.py" %*
if %ERRORLEVEL% neq 0 (
    echo.
    echo  Update failed. See above for details.
    echo.
)
pause
