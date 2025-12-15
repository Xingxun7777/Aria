@echo off
REM VoiceType EXE Build Script
REM ==========================
REM Builds VoiceType as a portable EXE application.

echo ========================================
echo VoiceType Build Script
echo ========================================
echo.

REM Check if PyInstaller is installed
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
)

REM Check if UPX is available (optional, for compression)
where upx >nul 2>&1
if errorlevel 1 (
    echo NOTE: UPX not found. Build will work but may be larger.
    echo       Install UPX for smaller executables.
    echo.
)

REM Clean previous build
echo Cleaning previous build...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul

REM Build
echo.
echo Building VoiceType...
echo This may take several minutes...
echo.

pyinstaller voicetype.spec --clean

if errorlevel 1 (
    echo.
    echo BUILD FAILED!
    echo Check the error messages above.
    pause
    exit /b 1
)

echo.
echo ========================================
echo BUILD SUCCESSFUL!
echo ========================================
echo.
echo Output: dist\VoiceType\
echo.
echo To run: dist\VoiceType\VoiceType.exe
echo.
echo NOTE: This is the LITE version.
echo       Models will be downloaded on first run.
echo.

pause
