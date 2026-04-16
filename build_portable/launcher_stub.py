"""
Aria Launcher Stub
=======================
A minimal launcher that starts Aria using the embedded Python.
This script is compiled to EXE using PyInstaller to avoid VBS security issues.

v1.0.5 addition: recovery for interrupted auto-update swap.
Runs BEFORE the aria/ package is imported so it can heal a missing live dir.
Uses only stdlib to stay robust when aria/ is gone.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def _read_state(state_path: Path) -> dict:
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_write_state(state_path: Path, state: dict) -> None:
    try:
        tmp = state_path.with_suffix(state_path.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, state_path)
    except OSError:
        pass


def _heal_interrupted_swap(exe_dir: Path) -> str:
    """Recover if a swap was interrupted. Returns status string.

    States when launcher_stub runs (before AriaRuntime.exe + aria.launcher):
      - live present         → normal or orphan, no action
      - live missing + stage → complete swap (stage → live)
      - live missing + backup → restore (backup → live)
      - none                 → fail
    """
    app_dir = exe_dir / "_internal" / "app"
    live = app_dir / "aria"
    stage = app_dir / "aria.new"
    state_path = exe_dir / ".update_state.json"

    if live.exists():
        return "ok"

    state = _read_state(state_path)
    # Missing live tree — attempt recovery
    if stage.exists():
        try:
            os.rename(stage, live)
            state.update({"status": "swapped", "failed_boots": 0, "error": ""})
            _atomic_write_state(state_path, state)
            return "stage_promoted"
        except OSError:
            pass
    backup_dir_str = state.get("backup_dir", "")
    if backup_dir_str and Path(backup_dir_str).exists():
        try:
            os.rename(backup_dir_str, live)
            state.update(
                {
                    "status": "rollback",
                    "failed_boots": 0,
                    "error": "stub 发现 live 丢失，已从 backup 恢复",
                }
            )
            _atomic_write_state(state_path, state)
            return "backup_restored"
        except OSError:
            pass
    # Nothing we can do — signal failure
    state.update({"status": "failed", "error": "aria/ 目录丢失且无可用 stage/backup"})
    _atomic_write_state(state_path, state)
    return "failed"


def main():
    # Get the directory where this EXE is located
    if getattr(sys, "frozen", False):
        # Running as compiled EXE
        exe_dir = Path(sys.executable).parent
    else:
        # Running as script (for testing)
        exe_dir = Path(__file__).parent

    # === Auto-update recovery (v1.0.5): runs before interpreter spawn ===
    heal_status = _heal_interrupted_swap(exe_dir)
    if heal_status == "failed":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0,
                "Aria 核心文件缺失，自动更新可能中断且无备份。\n\n"
                "请从 GitHub 重新下载完整便携版覆盖安装：\n"
                "https://github.com/Xingxun7777/Aria",
                "Aria 启动错误",
                0x10,
            )
        except Exception:
            pass
        sys.exit(1)

    # Paths
    internal_dir = exe_dir / "_internal"
    runtime_exe = internal_dir / "AriaRuntime.exe"
    pythonw = internal_dir / "pythonw.exe"
    python_exe = runtime_exe if runtime_exe.exists() else pythonw

    # Validate
    if not python_exe.exists():
        # Try to show error message
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0,
                f"找不到运行环境:\n{python_exe}\n\n请确保 _internal 文件夹完整。",
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
    cmd = [str(python_exe), "-s", "-m", "aria.launcher"]

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
