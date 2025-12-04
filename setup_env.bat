@echo off
echo ============================================
echo VoiceType 环境配置脚本 (RTX 5090)
echo ============================================
echo.

REM 检查conda
where conda >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] 找不到conda，请确保Anaconda已安装并在PATH中
    pause
    exit /b 1
)

echo [1/4] 创建独立环境 voicetype_env...
call conda create -n voicetype_env python=3.11 -y
if %errorlevel% neq 0 (
    echo [ERROR] 创建环境失败
    pause
    exit /b 1
)

echo.
echo [2/4] 激活环境...
call conda activate voicetype_env

echo.
echo [3/4] 安装PyTorch (CUDA 12.4, 支持5090)...
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124

echo.
echo [4/4] 安装其他依赖...
pip install faster-whisper silero-vad sounddevice numpy pynput pyperclip PySide6

echo.
echo ============================================
echo 安装完成！
echo.
echo 使用方法:
echo   1. 运行 run_voicetype_env.bat 启动VoiceType
echo   2. 或手动: conda activate voicetype_env ^&^& python -m voicetype
echo ============================================
pause
