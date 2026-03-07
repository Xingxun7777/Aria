@echo off
setlocal
chcp 65001 >nul

if /I "%~1"=="full" (
    call "%~dp0release-full.bat" %~2
    exit /b %errorlevel%
)

if /I "%~1"=="lite" (
    call "%~dp0release-lite.bat" %~2
    exit /b %errorlevel%
)

if "%~1"=="" (
    echo [INFO] 未指定模式，默认构建 lite 包。
    echo [INFO] 如需完整离线傻瓜包，请运行: build_portable\release-full.bat
    echo.
    call "%~dp0release-lite.bat" %~2
    exit /b %errorlevel%
)

if /I "%~1"=="help" goto :usage_ok
if /I "%~1"=="--help" goto :usage_ok
if /I "%~1"=="-h" goto :usage_ok

echo [ERROR] 未知模式: %~1
echo.
:usage
echo 用法:
echo   build_portable\release.bat lite
echo   build_portable\release.bat full
echo   build_portable\release.bat lite dry-run
echo   build_portable\release.bat full dry-run
echo.
echo 或直接使用:
echo   build_portable\release-lite.bat
echo   build_portable\release-full.bat
exit /b 1

:usage_ok
echo 用法:
echo   build_portable\release.bat lite
echo   build_portable\release.bat full
echo   build_portable\release.bat lite dry-run
echo   build_portable\release.bat full dry-run
echo.
echo 或直接使用:
echo   build_portable\release-lite.bat
echo   build_portable\release-full.bat
exit /b 0
