"""Debug DLL loading issues."""

import os
import ctypes
import sys

# Set CUDA path
cuda_path = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8"
cuda_bin = os.path.join(cuda_path, "bin")

# Add to PATH
os.environ["CUDA_PATH"] = cuda_path
os.environ["PATH"] = cuda_bin + ";" + os.environ["PATH"]

print("=" * 60)
print("DLL Loading Debug")
print("=" * 60)
print(f"CUDA_PATH: {cuda_path}")
print(f"CUDA bin in PATH: {cuda_bin in os.environ['PATH']}")
print()

# torch lib path
torch_lib = r"F:\anaconda\Lib\site-packages\torch\lib"
print(f"torch lib path: {torch_lib}")
print()

# Try loading DLLs in order
dlls_to_try = [
    os.path.join(cuda_bin, "cudart64_12.dll"),  # CUDA runtime
    os.path.join(torch_lib, "c10.dll"),
    os.path.join(torch_lib, "torch_cpu.dll"),
    os.path.join(torch_lib, "c10_cuda.dll"),
]

for dll_path in dlls_to_try:
    print(f"Loading: {os.path.basename(dll_path)}...")
    try:
        if os.path.exists(dll_path):
            dll = ctypes.CDLL(dll_path)
            print(f"  SUCCESS: {dll}")
        else:
            print(f"  NOT FOUND: {dll_path}")
    except OSError as e:
        print(f"  FAILED: {e}")
    print()

print("=" * 60)
print("Now trying torch import...")
print("=" * 60)
try:
    import torch

    print(f"SUCCESS: torch {torch.__version__}")
    print(f"CUDA available: {torch.cuda.is_available()}")
except Exception as e:
    print(f"FAILED: {e}")
