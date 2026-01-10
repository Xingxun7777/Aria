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
from typing import Optional, Tuple, Callable
from dataclasses import dataclass, field

from ..core.logging import get_system_logger

logger = get_system_logger()

# ============================================================================
# Layer 0: Permission Detection (using ctypes to avoid pywin32 dependency)
# ============================================================================

advapi32 = ctypes.windll.advapi32

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
    Uses ctypes directly to avoid pywin32 dependency.
    """
    kernel32 = ctypes.windll.kernel32

    # Try to open the process
    hProcess = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not hProcess:
        # Can't open process, assume not elevated (or we don't have permission)
        return False

    try:
        # Open the process token
        hToken = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(hProcess, TOKEN_QUERY, ctypes.byref(hToken)):
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
    kernel32 = ctypes.windll.kernel32
    return is_process_elevated(kernel32.GetCurrentProcessId())


def get_foreground_window_pid() -> Tuple[int, int]:
    """
    Get the foreground window handle and its process ID.
    Returns (hwnd, pid).
    """
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return hwnd, pid.value


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
# Original constants and structures
# ============================================================================

# Windows API
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Clipboard formats
CF_UNICODETEXT = 13

# Memory allocation
GMEM_MOVEABLE = 0x0002

# SendInput structures
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

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
# Without these, 64-bit handles may be truncated to 32-bit (Codex review finding)
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


@dataclass
class OutputConfig:
    """Output injection configuration."""

    # Layer 1: Clipboard mode settings
    paste_delay_ms: int = 50  # Delay between clipboard set and paste
    restore_clipboard: bool = True  # Restore original clipboard after paste
    restore_delay_ms: int = 100  # Delay before restoring clipboard

    # Layer 2: Typewriter mode settings
    typewriter_mode: bool = False  # Use character-by-character input instead of paste
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

    def _get_clipboard_text(self) -> Optional[str]:
        """Get current clipboard text content."""
        try:
            if not user32.OpenClipboard(None):
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

    def _set_clipboard_text(self, text: str) -> bool:
        """
        Set clipboard text content.

        Memory management notes (from Codex/Gemini review):
        - GlobalAlloc allocates memory that we own
        - If SetClipboardData succeeds, clipboard takes ownership (don't free)
        - If SetClipboardData fails, we must free the handle ourselves
        - If GlobalLock fails, we must free the handle ourselves
        """
        handle = None  # Track handle for cleanup on failure
        try:
            if not user32.OpenClipboard(None):
                logger.error("Failed to open clipboard")
                return False

            try:
                user32.EmptyClipboard()

                # Allocate memory for text (including null terminator)
                text_bytes = (text + "\0").encode("utf-16-le")
                size = len(text_bytes)

                handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
                if not handle:
                    logger.error("Failed to allocate memory for clipboard")
                    return False

                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    logger.error("Failed to lock memory")
                    # Memory leak fix: free handle if lock fails
                    kernel32.GlobalFree(handle)
                    handle = None
                    return False

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
                    return False

                # Success - clipboard now owns handle, don't free it
                handle = None
                return True
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
            return False

    def _send_paste(self) -> None:
        """Send Ctrl+V keystroke using SendInput."""
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

    def _send_vk_key(self, vk_code: int) -> bool:
        """Send a single virtual key press (down + up)."""
        inputs = (INPUT * 2)()

        # Key down
        inputs[0].type = INPUT_KEYBOARD
        inputs[0].union.ki.wVk = vk_code
        inputs[0].union.ki.wScan = 0
        inputs[0].union.ki.dwFlags = 0
        inputs[0].union.ki.time = 0
        inputs[0].union.ki.dwExtraInfo = 0

        # Key up
        inputs[1].type = INPUT_KEYBOARD
        inputs[1].union.ki.wVk = vk_code
        inputs[1].union.ki.wScan = 0
        inputs[1].union.ki.dwFlags = KEYEVENTF_KEYUP
        inputs[1].union.ki.time = 0
        inputs[1].union.ki.dwExtraInfo = 0

        result = user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
        return result == 2

    def _insert_text_typewriter(self, text: str) -> bool:
        """
        Insert text character-by-character using KEYEVENTF_UNICODE.

        This is Layer 2 - for applications that don't support Ctrl+V paste.

        Features:
        - Handles surrogate pairs (emoji) correctly
        - Detects focus loss and aborts
        - Fixed delay (random delay is security theater)
        - Control characters mapped to VK keys (Gemini review)
        - Aborts on SendInput failure (Codex/Gemini review)

        Limitations (by design, not bugs):
        - Does NOT work with DirectInput/RawInput games
        - Does NOT bypass anti-cheat (LLKHF_INJECTED flag is kernel-level)
        - Slower than clipboard paste
        """
        if not text:
            return True

        # Control characters that should be sent as VK keys, not Unicode
        # (Gemini review: some apps don't interpret Unicode control chars)
        CONTROL_CHAR_TO_VK = {
            "\n": VK_CODES["enter"],  # 0x0D - VK_RETURN
            "\r": VK_CODES["enter"],  # 0x0D - VK_RETURN
            "\t": VK_CODES["tab"],  # 0x09 - VK_TAB
        }

        # Record initial window for focus loss detection
        initial_hwnd = user32.GetForegroundWindow()
        delay_s = self.config.typewriter_delay_ms / 1000
        chars_sent = 0

        logger.info(f"Typewriter mode: sending {len(text)} chars")

        for char in text:
            # Focus loss detection (every character for safety)
            current_hwnd = user32.GetForegroundWindow()
            if current_hwnd != initial_hwnd:
                logger.warning(
                    f"Focus lost after {chars_sent} chars, aborting typewriter input"
                )
                return False

            # Check for control characters that need VK key handling
            if char in CONTROL_CHAR_TO_VK:
                vk_code = CONTROL_CHAR_TO_VK[char]
                if not self._send_vk_key(vk_code):
                    logger.error(
                        f"SendInput failed for control char '\\x{ord(char):02x}' "
                        f"after {chars_sent} chars, aborting"
                    )
                    return False
                chars_sent += 1
                time.sleep(delay_s)
                continue

            codepoint = ord(char)

            # Handle BMP and non-BMP characters differently
            # SendInput's wScan is 16-bit, so characters outside BMP need surrogate pairs
            if codepoint > 0xFFFF:
                # Convert to UTF-16 surrogate pair
                high_surrogate = 0xD800 + ((codepoint - 0x10000) >> 10)
                low_surrogate = 0xDC00 + ((codepoint - 0x10000) & 0x3FF)
                scancodes = [high_surrogate, low_surrogate]
            else:
                scancodes = [codepoint]

            # Send each scancode
            for scan in scancodes:
                inputs = (INPUT * 2)()

                # Key down
                inputs[0].type = INPUT_KEYBOARD
                inputs[0].union.ki.wVk = 0
                inputs[0].union.ki.wScan = scan
                inputs[0].union.ki.dwFlags = KEYEVENTF_UNICODE
                inputs[0].union.ki.time = 0
                inputs[0].union.ki.dwExtraInfo = 0

                # Key up
                inputs[1].type = INPUT_KEYBOARD
                inputs[1].union.ki.wVk = 0
                inputs[1].union.ki.wScan = scan
                inputs[1].union.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
                inputs[1].union.ki.time = 0
                inputs[1].union.ki.dwExtraInfo = 0

                result = user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
                if result != 2:
                    # Abort on failure instead of continuing (Codex/Gemini review)
                    logger.error(
                        f"SendInput failed ({result}/2) for char '{char}' "
                        f"after {chars_sent} chars, error={ctypes.get_last_error()}, aborting"
                    )
                    return False

            chars_sent += 1
            time.sleep(delay_s)

        logger.info(f"Typewriter mode: successfully sent {chars_sent} chars")
        return True

    def _insert_text_clipboard(self, text: str) -> bool:
        """
        Insert text using clipboard + Ctrl+V (Layer 1 - default fast mode).
        """
        # Backup clipboard
        original_clipboard = None
        if self.config.restore_clipboard:
            original_clipboard = self._get_clipboard_text()

        # Set text to clipboard
        if not self._set_clipboard_text(text):
            return False

        # Small delay to ensure clipboard is ready
        time.sleep(self.config.paste_delay_ms / 1000)

        # Send Ctrl+V
        self._send_paste()

        # Restore original clipboard
        if self.config.restore_clipboard and original_clipboard is not None:
            time.sleep(self.config.restore_delay_ms / 1000)
            self._set_clipboard_text(original_clipboard)
            logger.debug("Clipboard restored")

        return True

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

        logger.info(f"Inserting text: {text[:50]}{'...' if len(text) > 50 else ''}")

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
        if self.config.typewriter_mode:
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

        # Press key down
        inputs[idx].union.ki.wVk = vk_key
        inputs[idx].union.ki.dwFlags = 0
        idx += 1

        # Release key up
        inputs[idx].union.ki.wVk = vk_key
        inputs[idx].union.ki.dwFlags = KEYEVENTF_KEYUP
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
