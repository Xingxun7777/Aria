"""Debug DLL loading - try torch's bundled DLLs."""

import os
import ctypes

torch_lib = r"F:\anaconda\Lib\site-packages\torch\lib"

# Add torch lib to DLL search path FIRST
os.add_dll_directory(torch_lib)

# Also add CUDA 12.8
cuda_bin = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\bin"
os.add_dll_directory(cuda_bin)

print("DLL directories added")
print(f"  - {torch_lib}")
print(f"  - {cuda_bin}")
print()

# Try loading torch's bundled DLLs in dependency order
dlls = [
    "cudart64_12.dll",
    "cublas64_12.dll",
    "cublasLt64_12.dll",
    "libiomp5md.dll",
    "uv.dll",
    "c10.dll",
]

for dll_name in dlls:
    dll_path = os.path.join(torch_lib, dll_name)
    print(f"Loading {dll_name}...", end=" ")
    try:
        if os.path.exists(dll_path):
            dll = ctypes.CDLL(dll_path)
            print("OK")
        else:
            print("NOT FOUND")
    except OSError as e:
        print(f"FAILED: {e}")

print()
print("Trying torch import...")
try:
    import torch

    print(f"SUCCESS: torch {torch.__version__}")
except Exception as e:
    print(f"FAILED: {e}")
