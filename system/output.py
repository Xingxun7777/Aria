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

# Virtual key codes
VK_CONTROL = 0x11
VK_V = 0x56

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
    paste_delay_ms: int = 50      # Delay between clipboard set and paste
    restore_clipboard: bool = True  # Restore original clipboard after paste
    restore_delay_ms: int = 100   # Delay before restoring clipboard


class OutputInjector:
    """
    Injects text into the active application via clipboard and paste.

    Usage:
        injector = OutputInjector()
        injector.insert_text("Hello, world!")
    """

    def __init__(self, config: Optional[OutputConfig] = None):
        self.config = config or OutputConfig()

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
                text_bytes = (text + '\0').encode('utf-16-le')
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
            logger.warning(f"SendInput returned {result}/4, error={ctypes.get_last_error()}")

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


def create_output_injector(config: Optional[OutputConfig] = None) -> OutputInjector:
    """Factory function to create output injector."""
    return OutputInjector(config)
