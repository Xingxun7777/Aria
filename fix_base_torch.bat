@echo off
echo ============================================
echo 修复全局环境 PyTorch (支持RTX 5090)
echo ============================================
echo.
echo 当前: torch 2.5.1+cpu (无CUDA)
echo 目标: torch 2.5.1+cu124 (CUDA 12.4)
echo.
echo 这只会影响全局base环境，不会影响其他独立环境。
echo.
pause

echo.
echo [1/2] 卸载旧版本...
pip uninstall torch torchvision torchaudio -y

echo.
echo [2/2] 安装CUDA 12.4版本...
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124

echo.
echo ============================================
echo 验证安装...
python -c "import torch; print(f'PyTorch: {torch.__version__}'); print(f'CUDA: {torch.cuda.is_available()}'); print(f'GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"N/A\"}')"
echo.
echo 完成！现在可以直接用原来的 VoiceType.bat 启动了。
echo ============================================
pause
