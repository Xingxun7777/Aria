@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

REM Aria updater runner shim (v1.0.5 spec)
REM Usage: updater_runner.bat <old_pythonw_pid>
REM Invoked detached from Aria app before it exits.

cd /d "%~dp0"

REM Wait for old process to exit (max 30s).
REM Match by PID existence (any image name). Covers pythonw.exe (dev) AND
REM AriaRuntime.exe (portable build's renamed interpreter).
set OLD_PID=%1
set /a WAIT_COUNT=0
:WAITLOOP
if "%OLD_PID%"=="" goto :SWAP
if %WAIT_COUNT% GEQ 30 goto :KILLSTALE
tasklist /FI "PID eq %OLD_PID%" /NH 2>nul | findstr /I /R "pythonw\.exe AriaRuntime\.exe python\.exe" >nul 2>&1
if errorlevel 1 goto :SWAP
timeout /t 1 /nobreak >nul
set /a WAIT_COUNT+=1
goto :WAITLOOP

:KILLSTALE
REM Old process still alive after 30s — verify it's an Aria interpreter before killing
tasklist /FI "PID eq %OLD_PID%" /NH 2>nul | findstr /I /R "pythonw\.exe AriaRuntime\.exe python\.exe" >nul 2>&1
if errorlevel 1 goto :SWAP
taskkill /F /PID %OLD_PID% >nul 2>&1
timeout /t 1 /nobreak >nul

:SWAP
REM Invoke the swap runner via portable python
set PYEXE="_internal\python.exe"
if not exist %PYEXE% set PYEXE="_internal\pythonw.exe"
if not exist %PYEXE% (
    echo [ERROR] Python interpreter not found in _internal\
    exit /b 99
)

%PYEXE% "updater_runner.py"
set SWAP_RC=%ERRORLEVEL%

endlocal & exit /b %SWAP_RC%
