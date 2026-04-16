@echo off
setlocal
chcp 65001 >nul
echo ================================================
echo    Aria Full 便携版打包脚本
echo ================================================
echo.

cd /d %~dp0..

call :get_version
set "ARIA_DIST_NAME=Aria_release_full"
set "ARIA_ARCHIVE_NAME=Aria-v%ARIA_VERSION%-full.7z"

echo [INFO] 用途: 网盘/云盘离线傻瓜包（内置 Qwen3-ASR 0.6B + 1.7B）
echo [INFO] 输出目录: dist_portable\%ARIA_DIST_NAME%\
echo [INFO] 如需先清空本地记录与配置痕迹，请先运行: build_portable\release-prep.bat
echo.

if /I "%~1"=="dry-run" goto :dry_run
if /I "%ARIA_RELEASE_DRY_RUN%"=="1" (
    goto :dry_run
)

echo [1/3] 打包 Full 便携版...
.venv\Scripts\python.exe build_portable\build.py --full --dist-name %ARIA_DIST_NAME%
if errorlevel 1 (
    echo 打包失败！
    pause
    exit /b 1
)

echo.
echo [2/3] 编译 EXE 启动器...
.venv\Scripts\python.exe build_portable\build_launcher_exe.py --dist-name %ARIA_DIST_NAME%
if errorlevel 1 (
    echo EXE 编译失败！
    pause
    exit /b 1
)

echo.
echo [3/3] 验证敏感数据已清理...
findstr /c:"sk-or-v1" dist_portable\%ARIA_DIST_NAME%\_internal\app\aria\config\hotwords.json >nul 2>&1
if not errorlevel 1 (
    echo 警告：发现 API Key 未清理！
    pause
    exit /b 1
)
echo API Key: 已清理

echo.
echo ================================================
echo    Full 打包完成！
echo ================================================
echo.
echo 输出目录: dist_portable\%ARIA_DIST_NAME%\
echo.
echo 下一步:
echo   1. 测试: dist_portable\%ARIA_DIST_NAME%\Aria.exe
echo   2. 压缩: 7z a %ARIA_ARCHIVE_NAME% dist_portable\%ARIA_DIST_NAME%\
echo   3. 发布位置: 网盘/云盘离线包（full）
echo.
pause
exit /b 0

:dry_run
    echo [DRY-RUN] .venv\Scripts\python.exe build_portable\build.py --full --dist-name %ARIA_DIST_NAME%
    echo [DRY-RUN] .venv\Scripts\python.exe build_portable\build_launcher_exe.py --dist-name %ARIA_DIST_NAME%
    echo [DRY-RUN] findstr /c:"sk-or-v1" dist_portable\%ARIA_DIST_NAME%\_internal\app\aria\config\hotwords.json
    exit /b 0

:get_version
for /f "usebackq delims=" %%i in (`.venv\Scripts\python.exe -c "import pathlib; ns={}; exec(pathlib.Path('__init__.py').read_text(encoding='utf-8'), ns); print(ns.get('__version__','0.0.0'))"`) do set "ARIA_VERSION=%%i"
if not defined ARIA_VERSION set "ARIA_VERSION=0.0.0"
exit /b 0
