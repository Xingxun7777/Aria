"""
Cross-install single-instance guard — Windows global named mutex.

Why this exists: launcher.py's per-install singleton (named mutex + temp lock
file) deliberately uses DIFFERENT names for dev and portable trees, so a dev
checkout and an unpacked release could run side by side. Field forensics
(2026-07-19 slim trial) showed what that costs: two live instances both capture
the mic and insert the same sentence twice, while the second instance loses the
F11 hotkey race. This module adds an OUTER, install-agnostic layer: one
``Global\\`` kernel namespace mutex shared by every Aria install on the
machine, so the second instance — whatever directory it launched from — bails
out with a clear message instead of double-listening.

Stdlib-only and importable both as ``core.singleton`` (launcher bootstrap,
before sys.path setup) and ``aria.core.singleton`` (tests, app code) — same
contract as core/update_migration.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

# Kernel object name. "Global\\" spans all terminal-services sessions; the
# privilege check for Global\\ creation applies to file mappings only, not
# mutexes, so a normal (non-elevated) user process can create this. Do not
# rename casually: every install that should mutually exclude must agree on it.
GLOBAL_MUTEX_NAME = "Global\\AriaVoiceInput"

# Deliberately verbose and undocumented in the user UI: this is an operator
# escape hatch for a local PERSONAL build, not a supported multi-instance mode.
# Public packages cannot satisfy the marker gate because the release privacy
# scanner rejects PERSONAL_BUILD.txt.
PARALLEL_PERSONAL_TEST_ENV = "ARIA_PERSONAL_TEST_ALLOW_PARALLEL"
PERSONAL_BUILD_MARKER = "PERSONAL_BUILD.txt"

ALREADY_RUNNING_MESSAGE = (
    "Aria 已在运行（可能是另一个安装目录的实例）。\n"
    "请先退出已运行的 Aria（检查系统托盘），再启动本实例。"
)

ERROR_ALREADY_EXISTS = 183

_global_mutex_handle = None


def allow_parallel_personal_test_instance(
    install_root: str | os.PathLike[str] | None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether an explicitly requested PERSONAL portable test may overlap.

    This does not acquire or release any mutex.  The launcher uses it to skip
    both singleton layers only when all fail-closed gates pass:

    * the operator-only environment flag is exactly ``"1"``;
    * the process has a concrete portable install root;
    * that root contains the personal-build distribution blocker; and
    * the expected portable source tree exists.

    Ordinary starts, development checkouts and public packages therefore keep
    strict cross-install single-instance behavior.
    """

    env = os.environ if environ is None else environ
    if env.get(PARALLEL_PERSONAL_TEST_ENV) != "1" or not install_root:
        return False
    try:
        root = Path(install_root)
        return (root / PERSONAL_BUILD_MARKER).is_file() and (
            root / "_internal" / "app" / "aria"
        ).is_dir()
    except (OSError, TypeError, ValueError):
        return False


def _kernel32():
    """kernel32 with use_last_error=True so get_last_error() is trustworthy.

    (ctypes.windll.kernel32 does NOT maintain the ctypes-private LastError
    copy; reading get_last_error() after calls through it returns stale
    values. Proper argtypes also prevent 64-bit HANDLE truncation.)
    """
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    k32.ReleaseMutex.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def try_acquire_global_mutex() -> bool:
    """Acquire the machine-wide Aria mutex. False = another install runs.

    Fail-open on unexpected errors (same policy as the per-install mutex):
    a broken singleton check must never block a legitimate boot — the inner
    per-install lock still provides same-directory protection.
    """
    global _global_mutex_handle
    if sys.platform != "win32":
        return True
    if _global_mutex_handle:
        # This process already holds it (retry loop re-entry).
        return True
    try:
        import ctypes

        k32 = _kernel32()
        handle = k32.CreateMutexW(None, True, GLOBAL_MUTEX_NAME)
        if not handle:
            print("[MUTEX] Warning: CreateMutexW(global) returned NULL")
            return True
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            k32.CloseHandle(handle)
            return False
        _global_mutex_handle = handle
        return True
    except Exception as e:
        print(f"[MUTEX] Warning: Could not create global mutex: {e}")
        return True


def release_global_mutex() -> None:
    """Release + close the machine-wide mutex (no-op if not held)."""
    global _global_mutex_handle
    if _global_mutex_handle and sys.platform == "win32":
        try:
            k32 = _kernel32()
            k32.ReleaseMutex(_global_mutex_handle)
            k32.CloseHandle(_global_mutex_handle)
        except Exception:
            pass
        _global_mutex_handle = None


def _show_dialog(message: str) -> None:
    """Best-effort native MessageBox — works before Qt, under pythonw too."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        MB_OK = 0x0
        MB_ICONWARNING = 0x30
        MB_SETFOREGROUND = 0x10000
        MB_TOPMOST = 0x40000
        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "Aria",
            MB_OK | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST,
        )
    except Exception:
        pass


def notify_already_running_and_exit(message: str = ALREADY_RUNNING_MESSAGE) -> None:
    """Second-instance exit path: visible dialog (stdout is devnull under
    pythonw, so print alone is invisible) then sys.exit(1)."""
    print("=" * 50)
    print(message)
    print("=" * 50)
    _show_dialog(message)
    sys.exit(1)
