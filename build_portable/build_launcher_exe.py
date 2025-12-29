"""
Build VoiceType.exe Launcher
============================
Compiles launcher_stub.py to a small EXE using PyInstaller.

Usage:
    python build_portable/build_launcher_exe.py

Output:
    dist_portable/VoiceType/VoiceType.exe
"""

import subprocess
import sys
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
BUILD_DIR = PROJECT_ROOT / "build_portable"
DIST_DIR = PROJECT_ROOT / "dist_portable" / "VoiceType"


def main():
    print("[BUILD] Building VoiceType.exe launcher...")

    # Check PyInstaller
    try:
        import PyInstaller

        print(f"[BUILD] PyInstaller version: {PyInstaller.__version__}")
    except ImportError:
        print("[BUILD] ERROR: PyInstaller not installed!")
        print("[BUILD] Run: pip install pyinstaller")
        return 1

    # Paths
    launcher_script = BUILD_DIR / "launcher_stub.py"
    icon_file = PROJECT_ROOT / "assets" / "voicetype.ico"
    output_exe = DIST_DIR / "VoiceType.exe"

    if not launcher_script.exists():
        print(f"[BUILD] ERROR: {launcher_script} not found!")
        return 1

    # Build command
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",  # Single EXE
        "--windowed",  # No console window
        "--clean",  # Clean build
        "--noconfirm",  # Overwrite without asking
        f"--name=VoiceType",  # Output name
        f"--distpath={DIST_DIR}",  # Output to dist_portable/VoiceType/
        f"--workpath={BUILD_DIR}/.pyinstaller_build",
        f"--specpath={BUILD_DIR}",
    ]

    # Add icon if exists
    if icon_file.exists():
        cmd.append(f"--icon={icon_file}")
        print(f"[BUILD] Using icon: {icon_file}")

    # Add the script
    cmd.append(str(launcher_script))

    print(f"[BUILD] Running: {' '.join(cmd)}")

    # Run PyInstaller
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))

    if result.returncode != 0:
        print("[BUILD] ERROR: PyInstaller failed!")
        return 1

    # Verify output
    if output_exe.exists():
        size_kb = output_exe.stat().st_size / 1024
        print(f"[BUILD] SUCCESS: {output_exe}")
        print(f"[BUILD] Size: {size_kb:.1f} KB")
    else:
        print("[BUILD] ERROR: Output EXE not found!")
        return 1

    # Cleanup PyInstaller artifacts
    pyinstaller_build = BUILD_DIR / ".pyinstaller_build"
    if pyinstaller_build.exists():
        shutil.rmtree(pyinstaller_build)
        print("[BUILD] Cleaned up build artifacts")

    print("[BUILD] Done!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
