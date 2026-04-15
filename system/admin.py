"""
Admin Restart Utility
=====================
Provides functionality to restart Aria with administrator privileges.
Uses Windows ShellExecuteW with "runas" verb to trigger UAC prompt.
"""

import ctypes
import sys
import os
from pathlib import Path

from ..core.logging import get_system_logger

logger = get_system_logger()


def restart_as_admin() -> bool:
    """
    Restart Aria with administrator privileges.

    This function:
    1. Detects whether running as frozen exe (PyInstaller) or development mode
    2. Uses ShellExecuteW with "runas" verb to trigger UAC elevation prompt
    3. Returns True if the new process was launched successfully

    Note: The current process should exit after calling this if it returns True.

    Returns:
        True if the elevated process was started successfully, False otherwise.
        A return value > 32 from ShellExecuteW indicates success.
    """
    if sys.platform != "win32":
        logger.error("restart_as_admin is only supported on Windows")
        return False

    try:
        shell32 = ctypes.windll.shell32

        if getattr(sys, "frozen", False):
            # PyInstaller frozen executable
            exe_path = sys.executable
            # For frozen apps, no additional params needed - exe launches directly
            params = ""
            logger.info(f"Restarting frozen executable as admin: {exe_path}")
        else:
            # Development mode - running via python -m
            exe_path = sys.executable  # python.exe path

            # Reconstruct the module path
            # Aria is launched as: python -m aria.ui.qt
            # We need to find the package root and use the same module path
            params = "-m aria.ui.qt"
            logger.info(f"Restarting dev mode as admin: {exe_path} {params}")

        # Get working directory
        work_dir = os.getcwd()

        # ShellExecuteW parameters:
        # hwnd: None (no parent window)
        # lpOperation: "runas" (request elevation via UAC)
        # lpFile: executable path
        # lpParameters: command line arguments
        # lpDirectory: working directory
        # nShowCmd: SW_SHOWNORMAL (1) - show window normally
        SW_SHOWNORMAL = 1

        result = shell32.ShellExecuteW(
            None,  # hwnd
            "runas",  # lpOperation - triggers UAC
            exe_path,  # lpFile
            params,  # lpParameters
            work_dir,  # lpDirectory
            SW_SHOWNORMAL,  # nShowCmd
        )

        # ShellExecuteW returns a value > 32 on success
        # Values <= 32 are error codes
        if result > 32:
            logger.info(f"Successfully launched elevated process (result={result})")
            return True
        else:
            # Common error codes:
            # 0 = Out of memory
            # 2 = File not found
            # 3 = Path not found
            # 5 = Access denied (user cancelled UAC)
            # 31 = No association
            error_messages = {
                0: "Out of memory",
                2: "File not found",
                3: "Path not found",
                5: "Access denied (UAC cancelled)",
                31: "No file association",
            }
            error_msg = error_messages.get(result, f"Unknown error code: {result}")
            logger.warning(f"Failed to launch elevated process: {error_msg}")
            return False

    except Exception as e:
        logger.error(f"Exception in restart_as_admin: {e}")
        return False


def is_admin() -> bool:
    """
    Check if the current process is running with administrator privileges.

    Returns:
        True if running as admin, False otherwise.
    """
    if sys.platform != "win32":
        return False

    try:
        # Use ctypes to call IsUserAnAdmin
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False
