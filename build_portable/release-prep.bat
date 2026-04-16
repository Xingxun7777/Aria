@echo off
setlocal
chcp 65001 >nul
echo ================================================
echo    Aria 发布前清理脚本
echo ================================================
echo.

cd /d %~dp0..

.venv\Scripts\python.exe build_portable\release_prep.py
if errorlevel 1 (
    echo 发布前清理失败！
    pause
    exit /b 1
)

echo.
echo 发布前清理完成。
echo 下一步可执行:
echo   build_portable\release-lite.bat
echo   build_portable\release-full.bat
echo.
pause
exit /b 0
