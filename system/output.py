"""
Output Injection Module
=======================
Handles inserting transcribed text into the active application.

Strategy:
1. Backup current clipboard content
2. Set transcribed text to clipboard
3. SendInput Ctrl+V to paste
4. Restore original clipboard content

Based on POC#1 validation.
"""

import ctypes
from ctypes import wintypes
import time
from typing import Optional
from dataclasses import dataclass

from ..core.logging import get_system_logger

logger = get_system_logger()

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


# Fix types for 64-bit Windows
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.restype = ctypes.c_void_p


@dataclass
class OutputConfig:
    """Output injection configuration."""

    paste_delay_ms: int = 50  # Delay between clipboard set and paste
    restore_clipboard: bool = True  # Restore original clipboard after paste
    restore_delay_ms: int = 100  # Delay before restoring clipboard


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
        """Set clipboard text content."""
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
                    return False

                try:
                    ctypes.memmove(ptr, text_bytes, size)
                finally:
                    kernel32.GlobalUnlock(handle)

                # Set clipboard data
                result = user32.SetClipboardData(CF_UNICODETEXT, handle)
                if not result:
                    logger.error("Failed to set clipboard data")
                    return False

                return True
            finally:
                user32.CloseClipboard()
        except Exception as e:
            logger.error(f"Failed to set clipboard: {e}")
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

    def insert_text(self, text: str) -> bool:
        """
        Insert text into the active application.

        Args:
            text: Text to insert

        Returns:
            True if successful, False otherwise
        """
        if not text:
            return True

        logger.info(f"Inserting text: {text[:50]}{'...' if len(text) > 50 else ''}")

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

        logger.info("Text inserted successfully")
        return True

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
