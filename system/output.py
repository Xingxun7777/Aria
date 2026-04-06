"""
Output Injection Module
=======================
Handles inserting transcribed text into the active application.

Strategy (Layered):
- Layer 0: Permission check (detect elevated target windows)
- Layer 1: Clipboard + Ctrl+V (default, fast)
- Layer 2: Typewriter mode (Unicode character-by-character, for apps that don't support paste)
- Layer 3: Fallback (copy to clipboard + prompt user to paste manually)

Based on POC#1 validation + Game Input Compatibility Fix (2026-01).
"""

import ctypes
from ctypes import wintypes
import time
from typing import Dict, Optional, Tuple, Callable
from dataclasses import dataclass, field

from ..core.logging import get_system_logger

logger = get_system_logger()

# ============================================================================
# Windows API declarations (MUST be at top - before any functions that use them)
# use_last_error=True for accurate ctypes.get_last_error()
# ============================================================================

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

# ============================================================================
# Layer 0: Permission Detection (using ctypes to avoid pywin32 dependency)
# ============================================================================

# Process access rights
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Token access rights
TOKEN_QUERY = 0x0008

# Token information class
TokenElevation = 20


class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


def is_process_elevated(pid: int) -> bool:
    """
    Check if a process is running with elevated (admin) privileges.
    Uses module-level WinDLL instances with proper argtypes/restype for 64-bit safety.
    (local ctypes.windll causes 64-bit handle truncation)
    """
    ERROR_ACCESS_DENIED = 5  # treat access-denied as elevated

    # Use module-level kernel32 (configured with argtypes/restype below)
    # Try to open the process
    hProcess = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not hProcess:
        # Access-denied means target is elevated/protected, not "not elevated"
        last_error = ctypes.get_last_error()
        if last_error == ERROR_ACCESS_DENIED:
            return True  # Treat access-denied as elevated
        return False

    try:
        # Open the process token
        hToken = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(hProcess, TOKEN_QUERY, ctypes.byref(hToken)):
            # Access-denied on token also means elevated/protected
            last_error = ctypes.get_last_error()
            if last_error == ERROR_ACCESS_DENIED:
                return True
            return False

        try:
            # Query token elevation
            elevation = TOKEN_ELEVATION()
            cbSize = wintypes.DWORD(ctypes.sizeof(TOKEN_ELEVATION))
            if not advapi32.GetTokenInformation(
                hToken,
                TokenElevation,
                ctypes.byref(elevation),
                cbSize,
                ctypes.byref(cbSize),
            ):
                return False

            return elevation.TokenIsElevated != 0
        finally:
            kernel32.CloseHandle(hToken)
    finally:
        kernel32.CloseHandle(hProcess)


def is_current_process_elevated() -> bool:
    """Check if the current process (Aria) is running elevated."""
    # Use module-level kernel32 for 64-bit safety
    return is_process_elevated(kernel32.GetCurrentProcessId())


def get_foreground_window_pid() -> Tuple[int, int]:
    """
    Get the foreground window handle and its process ID.
    Returns (hwnd, pid).
    Uses module-level user32 for 64-bit safety .
    """
    # Use module-level user32 (configured with argtypes/restype below)
    hwnd = user32.GetForegroundWindow()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return hwnd, pid.value


def get_foreground_window_info() -> Dict:
    """
    v1.2: Get foreground window info for screen context awareness.

    Returns dict with keys: hwnd, pid, window_title, process_name.
    Never raises — returns empty/default values on failure.
    """
    result = {"hwnd": 0, "pid": 0, "window_title": "", "process_name": ""}
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return result
        result["hwnd"] = hwnd

        # Get PID
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        result["pid"] = pid.value

        # Get window title
        try:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            result["window_title"] = buf.value or ""
        except Exception:
            pass

        # Get process name via QueryFullProcessImageNameW
        try:
            hProcess = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
            )
            if hProcess:
                try:
                    exe_buf = ctypes.create_unicode_buffer(512)
                    exe_size = wintypes.DWORD(512)
                    if kernel32.QueryFullProcessImageNameW(
                        hProcess, 0, exe_buf, ctypes.byref(exe_size)
                    ):
                        import os

                        result["process_name"] = os.path.basename(exe_buf.value)
                finally:
                    kernel32.CloseHandle(hProcess)
        except Exception:
            pass  # Protected/elevated process — fallback to empty

    except Exception as e:
        logger.debug(f"get_foreground_window_info failed: {e}")

    return result


def is_target_elevated() -> bool:
    """
    Check if the current foreground window's process is elevated.
    Used to warn user when Aria can't inject into elevated apps.
    """
    try:
        _, pid = get_foreground_window_pid()
        return is_process_elevated(pid)
    except Exception as e:
        logger.debug(f"Failed to check target elevation: {e}")
        return False  # Assume not elevated on error


# Cache for elevation status (avoid repeated checks)
_aria_elevated: Optional[bool] = None


def is_aria_elevated() -> bool:
    """Check if Aria is running with elevated privileges (cached)."""
    global _aria_elevated
    if _aria_elevated is None:
        _aria_elevated = is_current_process_elevated()
        if _aria_elevated:
            logger.info("Aria is running with elevated privileges")
        else:
            logger.debug("Aria is running without elevation")
    return _aria_elevated


# ============================================================================
# Constants and structures
# ============================================================================
# NOTE: user32/kernel32/advapi32 are declared at top of file (lines 30-32)
# for 64-bit safety - functions defined before argtypes need the configured instances

# Clipboard formats
CF_TEXT = 1  # ANSI text
CF_BITMAP = 2  # Bitmap handle (HBITMAP)
CF_DIB = 8  # Device Independent Bitmap (packed DIB)
CF_UNICODETEXT = 13  # Unicode text
CF_HDROP = 15  # File list (HDROP handle)
CF_DIBV5 = 17  # BITMAPV5 DIB

# Memory allocation
GMEM_MOVEABLE = 0x0002

# SendInput structures
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_EXTENDEDKEY = 0x0001  # For extended keys

# Extended keys that need KEYEVENTF_EXTENDEDKEY flag
# These keys have 0xE0 prefix in their scan codes
EXTENDED_VK_CODES = {
    0x2D,  # VK_INSERT
    0x2E,  # VK_DELETE
    0x24,  # VK_HOME
    0x23,  # VK_END
    0x21,  # VK_PRIOR (Page Up)
    0x22,  # VK_NEXT (Page Down)
    0x25,  # VK_LEFT
    0x26,  # VK_UP
    0x27,  # VK_RIGHT
    0x28,  # VK_DOWN
}

# Virtual key codes - Basic
VK_CONTROL = 0x11
VK_V = 0x56

# Virtual key codes - Extended for commands
VK_CODES = {
    "enter": 0x0D,
    "return": 0x0D,
    "backspace": 0x08,
    "delete": 0x2E,
    "tab": 0x09,
    "escape": 0x1B,
    "space": 0x20,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    # Letter keys (A-Z)
    "a": 0x41,
    "b": 0x42,
    "c": 0x43,
    "d": 0x44,
    "e": 0x45,
    "f": 0x46,
    "g": 0x47,
    "h": 0x48,
    "i": 0x49,
    "j": 0x4A,
    "k": 0x4B,
    "l": 0x4C,
    "m": 0x4D,
    "n": 0x4E,
    "o": 0x4F,
    "p": 0x50,
    "q": 0x51,
    "r": 0x52,
    "s": 0x53,
    "t": 0x54,
    "u": 0x55,
    "v": 0x56,
    "w": 0x57,
    "x": 0x58,
    "y": 0x59,
    "z": 0x5A,
    # Number keys (0-9)
    "0": 0x30,
    "1": 0x31,
    "2": 0x32,
    "3": 0x33,
    "4": 0x34,
    "5": 0x35,
    "6": 0x36,
    "7": 0x37,
    "8": 0x38,
    "9": 0x39,
    # Function keys
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
}

# Modifier key codes
VK_MODIFIERS = {
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "win": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
}

# ULONG_PTR is 8 bytes on 64-bit Windows, 4 bytes on 32-bit
ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION),
    ]


# Define SendInput function signature for proper 64-bit handling
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT

# Fix types for 64-bit Windows - handle-returning functions need explicit types
# Without these, 64-bit handles may be truncated to 32-bit
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.restype = ctypes.c_void_p

# Clipboard functions
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardSequenceNumber.argtypes = []
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.EnumClipboardFormats.argtypes = [ctypes.c_uint]
user32.EnumClipboardFormats.restype = ctypes.c_uint
user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL

# GlobalSize for getting clipboard data size
kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
kernel32.GlobalSize.restype = ctypes.c_size_t

# Window/Process functions - critical for handle correctness
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

# Process/Token functions for elevation detection
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetCurrentProcessId.argtypes = []
kernel32.GetCurrentProcessId.restype = wintypes.DWORD
advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,  # TokenHandle
    ctypes.c_int,  # TokenInformationClass
    ctypes.c_void_p,  # TokenInformation
    wintypes.DWORD,  # TokenInformationLength
    ctypes.POINTER(wintypes.DWORD),  # ReturnLength
]
advapi32.GetTokenInformation.restype = wintypes.BOOL

# v1.2: Window info APIs for screen context awareness
user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.c_wchar_p,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

# Typewriter mode: PostMessage + GetGUIThreadInfo for cross-thread focus detection
WM_CHAR = 0x0102
EM_REPLACESEL = 0x00C2  # Insert text at cursor in Edit/RichEdit controls

# Window class names that support EM_REPLACESEL (standard text controls).
# For these controls, EM_REPLACESEL goes through the native text rendering
# pipeline (including font linking for CJK), avoiding the white-box issue
# that SendInput KEYEVENTF_UNICODE causes in RichEdit controls.
_EM_REPLACESEL_CLASSES = {
    "edit",
    "richedit",
    "richedit20a",
    "richedit20w",
    "richedit50w",
}

user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.PostMessageW.restype = wintypes.BOOL

user32.SendMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.SendMessageW.restype = wintypes.LPARAM

user32.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype = wintypes.BOOL


@dataclass
class OutputConfig:
    """Output injection configuration."""

    # Layer 1: Clipboard mode settings
    paste_delay_ms: int = 50  # Delay between clipboard set and paste
    restore_clipboard: bool = True  # Restore original clipboard after paste
    restore_delay_ms: int = 100  # Delay before restoring clipboard

    # Layer 2: Typewriter mode settings
    typewriter_mode: bool = True  # Use character-by-character input (SendInput UNICODE)
    typewriter_delay_ms: int = 15  # Delay between characters (fixed, not random)

    # Layer 0: Permission handling
    check_elevation: bool = True  # Check if target window is elevated
    elevation_callback: Optional[Callable[[str], None]] = (
        None  # Callback to show warning
    )


class OutputInjector:
    """
    Injects text into the active application via clipboard and paste.

    Usage:
        injector = OutputInjector()
        injector.insert_text("Hello, world!")
    """

    def __init__(self, config: Optional[OutputConfig] = None):
        self.config = config or OutputConfig()
        self._clipboard_lock = None  # Optional thread lock for clipboard operations

    def set_clipboard_lock(self, lock) -> None:
        """Set a threading lock for thread-safe clipboard operations."""
        self._clipboard_lock = lock

    def _open_clipboard_with_retry(
        self, max_retries: int = 5, retry_delay_ms: int = 20
    ) -> bool:
        """
        Open clipboard with retry mechanism for contention handling.

        Windows clipboard can fail to open if another application (clipboard
        managers, RDP, etc.) is momentarily accessing it. This adds a retry
        loop to handle such transient failures.

        Args:
            max_retries: Maximum number of retry attempts
            retry_delay_ms: Delay between retries in milliseconds

        Returns:
            True if clipboard opened successfully, False otherwise.
        """
        for attempt in range(max_retries):
            if user32.OpenClipboard(None):
                return True
            if attempt < max_retries - 1:
                time.sleep(retry_delay_ms / 1000)
        logger.warning(f"Failed to open clipboard after {max_retries} attempts")
        return False

    def _get_clipboard_text(self) -> Optional[str]:
        """Get current clipboard text content."""
        # Track if we acquired the lock (ensure release in finally)
        lock_acquired = False
        try:
            # Acquire lock if available ( lock was unused)
            if self._clipboard_lock:
                self._clipboard_lock.acquire()
                lock_acquired = True
            if not self._open_clipboard_with_retry():
                return None

            try:
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return None

                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    return None

                try:
                    text = ctypes.wstring_at(ptr)
                    return text
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        except Exception as e:
            logger.error(f"Failed to get clipboard: {e}")
            return None
        finally:
            if lock_acquired:
                self._clipboard_lock.release()

    def _backup_clipboard_all_formats(self) -> Optional[Dict[int, bytes]]:
        """
        Backup common clipboard formats (text, images, files).

        Returns:
            Dict mapping format_id -> raw bytes, or None on failure.
            Empty dict means clipboard was empty.
        """
        # Only backup safe, common formats to avoid crashes from
        # exotic formats like delayed rendering or OLE objects
        SAFE_FORMATS = [
            CF_UNICODETEXT,  # 13: Unicode text (most common)
            CF_TEXT,  # 1: ANSI text
            CF_DIB,  # 8: DIB (images)
            CF_HDROP,  # 15: File list
        ]
        # Max size per format (10MB - images can be large)
        MAX_FORMAT_SIZE = 10 * 1024 * 1024

        lock_acquired = False
        try:
            if self._clipboard_lock:
                self._clipboard_lock.acquire()
                lock_acquired = True

            if not self._open_clipboard_with_retry():
                return None

            try:
                backup: Dict[int, bytes] = {}

                # Only backup safe formats
                for fmt in SAFE_FORMATS:
                    try:
                        if not user32.IsClipboardFormatAvailable(fmt):
                            continue

                        handle = user32.GetClipboardData(fmt)
                        if handle:
                            size = kernel32.GlobalSize(handle)
                            if size > 0 and size < MAX_FORMAT_SIZE:
                                ptr = kernel32.GlobalLock(handle)
                                if ptr:
                                    try:
                                        data = ctypes.string_at(ptr, size)
                                        backup[fmt] = data
                                    finally:
                                        kernel32.GlobalUnlock(handle)
                            elif size >= MAX_FORMAT_SIZE:
                                logger.debug(f"Skip large format {fmt}: {size}B")
                    except Exception as e:
                        logger.debug(f"Skip format {fmt}: {e}")

                if backup:
                    logger.debug(
                        f"Clipboard backup: {len(backup)} formats "
                        f"({list(backup.keys())})"
                    )
                else:
                    logger.debug("Clipboard was empty")

                return backup

            finally:
                user32.CloseClipboard()

        except Exception as e:
            logger.error(f"Failed to backup clipboard: {e}")
            return None
        finally:
            if lock_acquired:
                self._clipboard_lock.release()

    def _restore_clipboard_all_formats(self, backup: Dict[int, bytes]) -> bool:
        """
        Restore all clipboard formats from backup.

        Args:
            backup: Dict mapping format_id -> raw bytes

        Returns:
            True if restore succeeded, False otherwise.
        """
        if not backup:
            logger.debug("No backup to restore (was empty)")
            return True

        lock_acquired = False
        try:
            if self._clipboard_lock:
                self._clipboard_lock.acquire()
                lock_acquired = True

            if not self._open_clipboard_with_retry():
                return False

            try:
                # Must empty clipboard before setting new data
                user32.EmptyClipboard()

                restored_count = 0
                for fmt, data in backup.items():
                    try:
                        # Allocate global memory
                        size = len(data)
                        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
                        if not handle:
                            logger.warning(f"GlobalAlloc failed for format {fmt}")
                            continue

                        ptr = kernel32.GlobalLock(handle)
                        if not ptr:
                            kernel32.GlobalFree(handle)
                            logger.warning(f"GlobalLock failed for format {fmt}")
                            continue

                        try:
                            # Copy data to global memory
                            ctypes.memmove(ptr, data, size)
                        finally:
                            kernel32.GlobalUnlock(handle)

                        # Set clipboard data - clipboard takes ownership on success
                        result = user32.SetClipboardData(fmt, handle)
                        if result:
                            restored_count += 1
                        else:
                            # SetClipboardData failed, we must free the handle
                            kernel32.GlobalFree(handle)
                            logger.warning(f"SetClipboardData failed for format {fmt}")

                    except Exception as e:
                        logger.warning(f"Failed to restore format {fmt}: {e}")

                logger.debug(
                    f"Clipboard restored: {restored_count}/{len(backup)} formats"
                )
                return restored_count > 0

            finally:
                user32.CloseClipboard()

        except Exception as e:
            logger.error(f"Failed to restore clipboard: {e}")
            return False
        finally:
            if lock_acquired:
                self._clipboard_lock.release()

    def _set_clipboard_text(self, text: str) -> Tuple[bool, Optional[int]]:
        """
        Set clipboard text content.

        Returns:
            Tuple of (success, sequence_number).
            sequence_number is the clipboard sequence right after SetClipboardData,
            used for race detection .

        Memory management notes :
        - GlobalAlloc allocates memory that we own
        - If SetClipboardData succeeds, clipboard takes ownership (don't free)
        - If SetClipboardData fails, we must free the handle ourselves
        - If GlobalLock fails, we must free the handle ourselves
        """
        # Track if we acquired the lock (ensure release in finally)
        lock_acquired = False
        handle = None  # Track handle for cleanup on failure
        try:
            # Acquire lock if available ( lock was unused)
            if self._clipboard_lock:
                self._clipboard_lock.acquire()
                lock_acquired = True
            if not self._open_clipboard_with_retry():
                logger.error("Failed to open clipboard")
                return False, None

            try:
                user32.EmptyClipboard()

                # Allocate memory for text (including null terminator)
                text_bytes = (text + "\0").encode("utf-16-le")
                size = len(text_bytes)

                handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
                if not handle:
                    logger.error("Failed to allocate memory for clipboard")
                    return False, None

                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    logger.error("Failed to lock memory")
                    # Memory leak fix: free handle if lock fails
                    kernel32.GlobalFree(handle)
                    handle = None
                    return False, None

                try:
                    ctypes.memmove(ptr, text_bytes, size)
                finally:
                    kernel32.GlobalUnlock(handle)

                # Set clipboard data - if successful, clipboard owns the handle
                result = user32.SetClipboardData(CF_UNICODETEXT, handle)
                if not result:
                    logger.error("Failed to set clipboard data")
                    # Memory leak fix: free handle if SetClipboardData fails
                    kernel32.GlobalFree(handle)
                    handle = None
                    return False, None

                # Get sequence number BEFORE CloseClipboard to avoid race
                # (other app could modify clipboard between Close and GetSequence)
                seq_number = user32.GetClipboardSequenceNumber()

                # Success - clipboard now owns handle, don't free it
                handle = None
                return True, seq_number
            finally:
                user32.CloseClipboard()
        except Exception as e:
            logger.error(f"Failed to set clipboard: {e}")
            # Memory leak fix: ensure cleanup on exception
            if handle:
                try:
                    kernel32.GlobalFree(handle)
                except Exception:
                    pass
            return False, None
        finally:
            if lock_acquired:
                self._clipboard_lock.release()

    def _send_paste(self) -> bool:
        """
        Send Ctrl+V keystroke using SendInput.

        Returns:
            True if all 4 inputs were sent successfully, False otherwise.
        """
        inputs = (INPUT * 4)()

        # Initialize all inputs
        for i in range(4):
            inputs[i].type = INPUT_KEYBOARD
            inputs[i].union.ki.wScan = 0
            inputs[i].union.ki.time = 0
            inputs[i].union.ki.dwExtraInfo = 0

        # Ctrl down
        inputs[0].union.ki.wVk = VK_CONTROL
        inputs[0].union.ki.dwFlags = 0

        # V down
        inputs[1].union.ki.wVk = VK_V
        inputs[1].union.ki.dwFlags = 0

        # V up
        inputs[2].union.ki.wVk = VK_V
        inputs[2].union.ki.dwFlags = KEYEVENTF_KEYUP

        # Ctrl up
        inputs[3].union.ki.wVk = VK_CONTROL
        inputs[3].union.ki.dwFlags = KEYEVENTF_KEYUP

        result = user32.SendInput(4, inputs, ctypes.sizeof(INPUT))
        if result != 4:
            logger.warning(
                f"SendInput returned {result}/4, error={ctypes.get_last_error()}"
            )
            # Cleanup stuck keys to prevent modifier state carrying over
            # Order: [0]=Ctrl down, [1]=V down, [2]=V up, [3]=Ctrl up
            self._cleanup_stuck_keys(result)
            return False
        return True

    def _cleanup_stuck_keys(self, sent_count: int) -> None:
        """
        Send key-up events for any keys that might be stuck after partial SendInput.
        (partial SendInput can leave modifier keys down)

        Args:
            sent_count: Number of events that were actually sent (0-3 for _send_paste)
        """
        cleanup_keys = []

        # Determine which keys need cleanup based on what was sent
        # _send_paste order: Ctrl down, V down, V up, Ctrl up
        if sent_count >= 1:  # Ctrl down was sent
            cleanup_keys.append(VK_CONTROL)
        if sent_count >= 2:  # V down was sent (V up is at index 2)
            if sent_count < 3:  # V up was NOT sent
                cleanup_keys.insert(0, VK_V)  # Release V before Ctrl

        if not cleanup_keys:
            return

        # Send key-up events for stuck keys
        cleanup_inputs = (INPUT * len(cleanup_keys))()
        for i, vk in enumerate(cleanup_keys):
            cleanup_inputs[i].type = INPUT_KEYBOARD
            cleanup_inputs[i].union.ki.wVk = vk
            cleanup_inputs[i].union.ki.wScan = 0
            cleanup_inputs[i].union.ki.dwFlags = KEYEVENTF_KEYUP
            cleanup_inputs[i].union.ki.time = 0
            cleanup_inputs[i].union.ki.dwExtraInfo = 0

        cleanup_result = user32.SendInput(
            len(cleanup_keys), cleanup_inputs, ctypes.sizeof(INPUT)
        )
        if cleanup_result != len(cleanup_keys):
            logger.error(
                f"Failed to cleanup stuck keys: {cleanup_result}/{len(cleanup_keys)}"
            )

    def _send_vk_key(self, vk_code: int) -> bool:
        """Send a single virtual key press (down + up)."""
        inputs = (INPUT * 2)()

        # Determine if this is an extended key
        extended_flag = KEYEVENTF_EXTENDEDKEY if vk_code in EXTENDED_VK_CODES else 0

        # Key down
        inputs[0].type = INPUT_KEYBOARD
        inputs[0].union.ki.wVk = vk_code
        inputs[0].union.ki.wScan = 0
        inputs[0].union.ki.dwFlags = extended_flag
        inputs[0].union.ki.time = 0
        inputs[0].union.ki.dwExtraInfo = 0

        # Key up
        inputs[1].type = INPUT_KEYBOARD
        inputs[1].union.ki.wVk = vk_code
        inputs[1].union.ki.wScan = 0
        inputs[1].union.ki.dwFlags = KEYEVENTF_KEYUP | extended_flag
        inputs[1].union.ki.time = 0
        inputs[1].union.ki.dwExtraInfo = 0

        result = user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
        return result == 2

    def _get_focused_control(self) -> Optional[int]:
        """
        Get the focused control in the foreground window's thread (cross-thread safe).

        Uses GetGUIThreadInfo instead of GetFocus — GetFocus only works within
        the calling thread, but GetGUIThreadInfo works for any thread.

        Returns:
            HWND of the focused control, or None if detection failed.
        """
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        # Get the thread ID of the foreground window
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        if not tid:
            return hwnd  # Fallback to foreground window itself

        # Get GUI thread info (cross-thread)
        gti = GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(GUITHREADINFO)
        if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
            # hwndFocus is the actual focused control (edit box, text area, etc.)
            if gti.hwndFocus:
                return gti.hwndFocus
            # hwndActive as fallback
            if gti.hwndActive:
                return gti.hwndActive

        return hwnd  # Ultimate fallback

    def _insert_text_typewriter(self, text: str) -> bool:
        """
        Insert text character-by-character (typewriter mode).

        Two strategies based on target control type:
        - Standard text controls (Edit/RichEdit): EM_REPLACESEL message, which
          goes through the control's native text pipeline including font linking.
        - Other controls: SendInput + KEYEVENTF_UNICODE (works for custom
          controls and remote desktop).

        SendInput UNICODE bypasses font linking in RichEdit controls, causing
        CJK characters to render as white boxes □ (data is correct — copy/paste
        works — but display is broken). EM_REPLACESEL avoids this.

        Cross-thread focus detection via GetGUIThreadInfo (not GetFocus).
        """
        if not text:
            return True

        # Record initial foreground window for focus loss detection
        initial_hwnd = user32.GetForegroundWindow()
        delay_s = self.config.typewriter_delay_ms / 1000
        chars_sent = 0

        # Resolve the actual focused control
        target_hwnd = self._get_focused_control()
        if not target_hwnd:
            logger.error("Typewriter mode: no focused window found")
            return False

        # Detect if target is a standard text control (Edit/RichEdit).
        # For these, use EM_REPLACESEL to avoid white-box rendering issue.
        use_em_replacesel = False
        try:
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(target_hwnd, cls_buf, 256)
            target_class = cls_buf.value.lower()
            use_em_replacesel = target_class in _EM_REPLACESEL_CLASSES
        except Exception:
            pass

        # Newline handling depends on target control type:
        # - Edit/RichEdit (EM_REPLACESEL): normalize to \n, send as \r\n per char
        # - SendInput path (chat apps, custom controls): strip newlines
        #   (Enter = send message in chat apps, dangerous)
        if use_em_replacesel:
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        else:
            text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")

        method = "EM_REPLACESEL" if use_em_replacesel else "SendInput UNICODE"
        logger.info(
            f"Typewriter mode: sending {len(text)} chars via {method} "
            f"to hwnd={target_hwnd:#x} (fg={initial_hwnd:#x})"
        )

        for char in text:
            # Focus loss detection (every 5 chars to reduce overhead, plus first char)
            if chars_sent % 5 == 0:
                current_hwnd = user32.GetForegroundWindow()
                if current_hwnd != initial_hwnd:
                    logger.warning(
                        f"Focus lost after {chars_sent} chars "
                        f"(initial={initial_hwnd:#x}, current={current_hwnd:#x}), "
                        f"aborting typewriter input"
                    )
                    return False

            if use_em_replacesel:
                # EM_REPLACESEL: insert at cursor via the control's native text
                # pipeline. Handles font linking correctly for CJK characters.
                # wParam=1 enables undo support.
                # Newline: send \r\n (Windows line ending for Edit/RichEdit).
                insert_str = "\r\n" if char == "\n" else char
                text_buf = ctypes.create_unicode_buffer(insert_str)
                user32.SendMessageW(
                    target_hwnd,
                    EM_REPLACESEL,
                    wintypes.WPARAM(1),
                    ctypes.cast(text_buf, wintypes.LPARAM),
                )
            else:
                # SendInput UNICODE: for custom controls, remote desktop, etc.
                codepoint = ord(char)
                if codepoint > 0xFFFF:
                    # Non-BMP: surrogate pair (emoji, etc.)
                    high = 0xD800 + ((codepoint - 0x10000) >> 10)
                    low = 0xDC00 + ((codepoint - 0x10000) & 0x3FF)
                    scan_codes = [high, low]
                else:
                    scan_codes = [codepoint]

                inputs = (INPUT * (len(scan_codes) * 2))()
                idx = 0
                for sc in scan_codes:
                    # Key down
                    inputs[idx].type = INPUT_KEYBOARD
                    inputs[idx].union.ki.wVk = 0
                    inputs[idx].union.ki.wScan = sc
                    inputs[idx].union.ki.dwFlags = KEYEVENTF_UNICODE
                    inputs[idx].union.ki.time = 0
                    inputs[idx].union.ki.dwExtraInfo = 0
                    idx += 1
                    # Key up
                    inputs[idx].type = INPUT_KEYBOARD
                    inputs[idx].union.ki.wVk = 0
                    inputs[idx].union.ki.wScan = sc
                    inputs[idx].union.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
                    inputs[idx].union.ki.time = 0
                    inputs[idx].union.ki.dwExtraInfo = 0
                    idx += 1

                result = user32.SendInput(idx, inputs, ctypes.sizeof(INPUT))
                if result != idx:
                    err = ctypes.get_last_error()
                    logger.error(
                        f"SendInput UNICODE failed for '{char}' (U+{codepoint:04X}) "
                        f"after {chars_sent} chars, sent={result}/{idx}, error={err}"
                    )
                    return False

            chars_sent += 1
            if delay_s > 0:
                time.sleep(delay_s)

        logger.info(
            f"Typewriter mode: successfully sent {chars_sent} chars via {method}"
        )
        return True

    def _insert_text_clipboard(self, text: str) -> bool:
        """
        Insert text using clipboard + Ctrl+V (Layer 1 - default fast mode).

        Returns:
            True if paste was sent successfully, False if clipboard or SendInput failed.
        """

        # Debug: Write directly to pipeline log for visibility
        def _debug_log(msg: str):
            import datetime
            from pathlib import Path

            log_path = Path(__file__).parent.parent / "DebugLog" / "pipeline_debug.log"
            try:
                ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"[{ts}] [CLIPBOARD] {msg}\n")
            except Exception:
                pass

        _debug_log(f"restore_clipboard={self.config.restore_clipboard}")

        # Backup ALL clipboard formats (text, images, files, etc.)
        # Fix: Previously only backed up text, causing image/file loss
        clipboard_backup: Optional[Dict[int, bytes]] = None
        if self.config.restore_clipboard:
            clipboard_backup = self._backup_clipboard_all_formats()
            if clipboard_backup is None:
                _debug_log("Backup FAILED (returned None)")
                logger.warning("Failed to backup clipboard, will not restore")
            elif not clipboard_backup:
                _debug_log("Backup OK but empty (clipboard was empty)")
                logger.debug("Clipboard was empty before paste")
            else:
                _debug_log(
                    f"Backup OK: {len(clipboard_backup)} formats, keys={list(clipboard_backup.keys())}"
                )
                logger.debug(f"Backed up {len(clipboard_backup)} clipboard formats")
        else:
            _debug_log("restore_clipboard is DISABLED - skipping backup")

        # Set text to clipboard - returns (success, sequence_number)
        # sequence captured inside _set_clipboard_text to avoid race
        set_success, seq_after_set = self._set_clipboard_text(text)
        if not set_success:
            return False

        # Small delay to ensure clipboard is ready
        time.sleep(self.config.paste_delay_ms / 1000)

        # Send Ctrl+V - check result
        paste_success = self._send_paste()
        if not paste_success:
            logger.error("Failed to send Ctrl+V paste command")
            # Still try to restore clipboard even on paste failure

        # Restore original clipboard
        # Note: Removed sequence number check - it was too conservative and blocked
        # restore when target app (e.g., editors, clipboard managers) touched clipboard
        if self.config.restore_clipboard and clipboard_backup:
            # Increase delay to ensure paste is fully processed
            time.sleep(max(self.config.restore_delay_ms, 200) / 1000)

            _debug_log(f"Restoring {len(clipboard_backup)} formats...")
            restore_success = self._restore_clipboard_all_formats(clipboard_backup)
            if restore_success:
                _debug_log("RESTORED successfully")
                logger.info("Clipboard restored successfully (all formats)")
            else:
                _debug_log("RESTORE FAILED")
                logger.warning("Failed to restore original clipboard")
        else:
            _debug_log(
                f"Restore skipped: restore_clipboard={self.config.restore_clipboard}, has_backup={clipboard_backup is not None and len(clipboard_backup) > 0}"
            )

        return paste_success

    def insert_text(self, text: str) -> bool:
        """
        Insert text into the active application using layered strategy.

        Layer 0: Permission check - warn if target is elevated but Aria isn't
        Layer 1: Clipboard + Ctrl+V (default, fast)
        Layer 2: Typewriter mode (character-by-character, for apps without paste)
        Layer 3: Fallback - copy to clipboard and let user paste manually

        Args:
            text: Text to insert

        Returns:
            True if successful, False otherwise
        """
        if not text:
            return True

        # Log text length only at INFO, content at DEBUG to avoid sensitive data exposure
        # ( sensitive data logging concern)
        logger.info(f"Inserting text ({len(text)} chars)")
        logger.debug(f"Text preview: {text[:50]}{'...' if len(text) > 50 else ''}")

        # ========================================
        # Layer 0: Permission check
        # ========================================
        if self.config.check_elevation:
            if is_target_elevated() and not is_aria_elevated():
                warning_msg = (
                    "目标窗口以管理员权限运行，但 Aria 没有。\n"
                    "输入可能失败。请尝试以管理员身份运行 Aria。"
                )
                logger.warning(
                    "Target window is elevated but Aria is not - input may fail"
                )
                if self.config.elevation_callback:
                    self.config.elevation_callback(warning_msg)
                # Continue anyway - might work for some apps

        # ========================================
        # Layer 1 or 2: Choose input method
        # ========================================
        use_typewriter = self.config.typewriter_mode

        # Force clipboard mode for terminals where SendInput silently fails.
        # Note: WordPad/RichEdit no longer here — handled by EM_REPLACESEL path
        # in _insert_text_typewriter() which avoids the white-box issue.
        if use_typewriter:
            _CLIPBOARD_FORCED_PROCESSES = {
                "windowsterminal.exe",
                "cmd.exe",
                "powershell.exe",
                "pwsh.exe",
                "conhost.exe",
                "wezterm-gui.exe",
                "alacritty.exe",
                "hyper.exe",
            }
            try:
                fg_info = get_foreground_window_info()
                proc = fg_info.get("process_name", "").lower()
                if proc in _CLIPBOARD_FORCED_PROCESSES:
                    use_typewriter = False
                    logger.info(
                        f"Clipboard forced for {proc} (typewriter incompatible)"
                    )
            except Exception:
                pass

        if use_typewriter:
            # Layer 2: Typewriter mode
            success = self._insert_text_typewriter(text)
        else:
            # Layer 1: Clipboard mode (default)
            success = self._insert_text_clipboard(text)

        if success:
            logger.info("Text inserted successfully")
        else:
            logger.warning("Text insertion may have failed")

        return success

    def send_key(self, key: str, modifiers: list = None) -> bool:
        """
        Send a single keystroke with optional modifiers.

        Args:
            key: Key name (e.g., 'enter', 'backspace', 'a', 'z')
            modifiers: List of modifier keys (e.g., ['ctrl'], ['ctrl', 'shift'])

        Returns:
            True if successful, False otherwise

        Example:
            send_key('enter')           # Press Enter
            send_key('z', ['ctrl'])     # Press Ctrl+Z
            send_key('v', ['ctrl'])     # Press Ctrl+V
        """
        modifiers = modifiers or []

        # Validate key
        key_lower = key.lower()
        if key_lower not in VK_CODES:
            logger.error(f"Unknown key: {key}")
            return False

        vk_key = VK_CODES[key_lower]

        # Check if main key is extended
        key_extended_flag = KEYEVENTF_EXTENDEDKEY if vk_key in EXTENDED_VK_CODES else 0

        # Validate and collect modifiers
        modifier_vks = []
        for mod in modifiers:
            mod_lower = mod.lower()
            if mod_lower not in VK_MODIFIERS:
                logger.error(f"Unknown modifier: {mod}")
                return False
            modifier_vks.append(VK_MODIFIERS[mod_lower])

        # Calculate total inputs needed: (modifier_down + key_down + key_up + modifier_up)
        num_modifiers = len(modifier_vks)
        total_inputs = 2 + (num_modifiers * 2)  # key down/up + modifier down/up pairs

        inputs = (INPUT * total_inputs)()

        # Initialize all inputs
        for i in range(total_inputs):
            inputs[i].type = INPUT_KEYBOARD
            inputs[i].union.ki.wScan = 0
            inputs[i].union.ki.time = 0
            inputs[i].union.ki.dwExtraInfo = 0

        idx = 0

        # Press modifiers down
        for vk_mod in modifier_vks:
            inputs[idx].union.ki.wVk = vk_mod
            inputs[idx].union.ki.dwFlags = 0
            idx += 1

        # Press key down (with extended flag if needed)
        inputs[idx].union.ki.wVk = vk_key
        inputs[idx].union.ki.dwFlags = key_extended_flag
        idx += 1

        # Release key up (with extended flag if needed)
        inputs[idx].union.ki.wVk = vk_key
        inputs[idx].union.ki.dwFlags = KEYEVENTF_KEYUP | key_extended_flag
        idx += 1

        # Release modifiers up (reverse order)
        for vk_mod in reversed(modifier_vks):
            inputs[idx].union.ki.wVk = vk_mod
            inputs[idx].union.ki.dwFlags = KEYEVENTF_KEYUP
            idx += 1

        # Send all inputs
        result = user32.SendInput(total_inputs, inputs, ctypes.sizeof(INPUT))
        if result != total_inputs:
            logger.warning(
                f"SendInput returned {result}/{total_inputs}, error={ctypes.get_last_error()}"
            )
            return False

        mod_str = "+".join(modifiers) + "+" if modifiers else ""
        logger.info(f"Key sent: {mod_str}{key}")
        return True


def create_output_injector(config: Optional[OutputConfig] = None) -> OutputInjector:
    """Factory function to create output injector."""
    return OutputInjector(config)
