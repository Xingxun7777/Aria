@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title VoiceType GPU Verification

:: Get script directory
set "PROJECT_ROOT=%~dp0"
set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"
set "VENV_DIR=%PROJECT_ROOT%\.venv"

:: Check if venv exists
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found!
    echo Please run setup_env.bat first.
    pause
    exit /b 1
)

:: PATH isolation
set "PATH=%VENV_DIR%\Scripts;%SystemRoot%\system32;%SystemRoot%;%SystemRoot%\System32\Wbem"
set "PYTHONPATH="
set "PYTHONHOME="

echo ===================================================
echo  VoiceType GPU Verification
echo ===================================================
echo.

"%VENV_DIR%\Scripts\python.exe" -c "
import torch
print('=== PyTorch Info ===')
print(f'PyTorch Version: {torch.__version__}')
print(f'CUDA Available: {torch.cuda.is_available()}')
print(f'CUDA Version: {torch.version.cuda if torch.cuda.is_available() else \"N/A\"}')

if torch.cuda.is_available():
    print(f'GPU Device: {torch.cuda.get_device_name(0)}')
    print(f'GPU Memory: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB')
    
    arch_list = torch.cuda.get_arch_list() if hasattr(torch.cuda, 'get_arch_list') else []
    print(f'Supported Architectures: {arch_list}')
    
    # Check if sm_120 (Blackwell) is supported
    if any('sm_120' in arch or 'sm_12' in arch for arch in arch_list):
        print('✓ RTX 50-Series (Blackwell) supported!')
    elif any('sm_89' in arch for arch in arch_list):
        print('✓ RTX 40-Series (Ada) supported!')
    
    # Quick tensor test
    print()
    print('=== Quick CUDA Test ===')
    try:
        x = torch.rand(1000, 1000, device='cuda')
        y = torch.rand(1000, 1000, device='cuda')
        z = torch.matmul(x, y)
        print(f'Matrix multiply test: PASSED')
    except Exception as e:
        print(f'Matrix multiply test: FAILED - {e}')
else:
    print('Running in CPU mode (no CUDA)')
"

echo.
echo ===================================================
pause
