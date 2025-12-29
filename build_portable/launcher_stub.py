"""
Aria Launcher Stub
=======================
A minimal launcher that starts Aria using the embedded Python.
This script is compiled to EXE using PyInstaller to avoid VBS security issues.
"""

import os
import sys
import subprocess
from pathlib import Path


def main():
    # Get the directory where this EXE is located
    if getattr(sys, "frozen", False):
        # Running as compiled EXE
        exe_dir = Path(sys.executable).parent
    else:
        # Running as script (for testing)
        exe_dir = Path(__file__).parent

    # Paths
    internal_dir = exe_dir / "_internal"
    pythonw = internal_dir / "pythonw.exe"

    # Validate
    if not pythonw.exists():
        # Try to show error message
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0,
                f"找不到 Python 运行环境:\n{pythonw}\n\n请确保 _internal 文件夹完整。",
                "Aria 启动错误",
                0x10,  # MB_ICONERROR
            )
        except Exception:
            pass
        sys.exit(1)

    # Launch Aria
    # Use pythonw.exe for no console window
    # -s: don't add user site-packages
    # -m aria.launcher: run as module
    cmd = [str(pythonw), "-s", "-m", "aria.launcher"]

    try:
        # Start without waiting (detached process)
        # CREATE_NO_WINDOW = 0x08000000
        # DETACHED_PROCESS = 0x00000008
        subprocess.Popen(
            cmd, cwd=str(exe_dir), creationflags=0x08000000 | 0x00000008, close_fds=True
        )
    except Exception as e:
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0,
                f"启动失败:\n{e}\n\n请尝试使用 Aria_debug.bat 启动查看详细错误。",
                "Aria 启动错误",
                0x10,
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
