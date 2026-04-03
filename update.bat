@echo off
chcp 65001 >nul 2>&1
title Aria 一键升级

:: 检查是否在正确目录
if not exist "_internal\app\aria\app.py" (
    echo.
    echo  [错误] 请将此文件放在 Aria 安装根目录下运行。
    echo         与 Aria.cmd 同一目录
    echo.
    pause
    exit /b 1
)

:: 用内置 Python 运行升级脚本
"_internal\python.exe" -u "_internal\app\aria\update_tool.py" %*
if %ERRORLEVEL% neq 0 (
    echo.
    echo  升级过程中出现错误，请查看上方信息。
    echo.
)
pause
