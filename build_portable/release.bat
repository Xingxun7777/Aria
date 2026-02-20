@echo off
chcp 65001 >nul
echo ================================================
echo    Aria 便携版打包脚本
echo ================================================
echo.

cd /d %~dp0..

echo [1/3] 打包便携版...
.venv\Scripts\python.exe build_portable\build.py
if errorlevel 1 (
    echo 打包失败！
    pause
    exit /b 1
)

echo.
echo [2/3] 编译 EXE 启动器...
.venv\Scripts\python.exe build_portable\build_launcher_exe.py
if errorlevel 1 (
    echo EXE 编译失败！
    pause
    exit /b 1
)

echo.
echo [3/3] 验证敏感数据已清理...
findstr /c:"sk-or-v1" dist_portable\Aria\_internal\app\aria\config\hotwords.json >nul 2>&1
if not errorlevel 1 (
    echo 警告：发现 API Key 未清理！
    pause
    exit /b 1
)
echo API Key: 已清理 ✓

echo.
echo ================================================
echo    打包完成！
echo ================================================
echo.
echo 输出目录: dist_portable\Aria\
echo.
echo 下一步:
echo   1. 测试: dist_portable\Aria\Aria.exe
echo   2. 压缩: 7z a Aria-v1.1.2.7z dist_portable\Aria\
echo.
pause
