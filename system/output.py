"""
Output Injection Module
=======================
Handles inserting transcribed text into the active application.

Strategy (Layered):
- Layer 0: Permission check (detect elevated target windows)
- Layer 1: Clipboard + Ctrl+V (default, fast)
- Layer 2: Typewriter mode (Unicode character-by-character, for apps that don't support paste)
- Layer 3: Fallback (copy to clipboard + prompt user to paste manually)

Implements paste / typewriter / clipboard fallback in priority order, with
extra handling for game inputs that ignore standard paste shortcuts.
"""

import ctypes
from collections import deque
from ctypes import wintypes
import threading
import time
from typing import Dict, Optional, Tuple, Callable
from dataclasses import dataclass, field, replace

from ..core.logging import get_system_logger
from .target_surface import (
    ELECTRON_WINDOW_CLASS,
    STANDARD_TEXT_CONTROL_CLASSES,
    NewlinePolicy,
    SurfaceKind,
    TargetSnapshot,
    TargetSurfaceProfile,
    classify_target_surface,
    supports_word_compatible_ranges,
)

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


def foreground_belongs_to_current_process() -> bool:
    """
    Check if the foreground window belongs to the Aria process itself.

    Used by UI-triggered injection (e.g. history "re-inject") to refuse
    pasting into Aria's own windows: with no external target focused the
    text would land in the history browser / settings instead of the app
    the user meant.
    """
    try:
        import os

        _, pid = get_foreground_window_pid()
        return pid != 0 and pid == os.getpid()
    except Exception as e:
        logger.debug(f"Failed to check foreground ownership: {e}")
        return False


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
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_BACK = 0x08
VK_INSERT = 0x2D
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
    "slash": 0xBF,  # VK_OEM_2 physical /? key (layout-dependent character)
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

# GetAsyncKeyState: used by the deferred-restore worker to notice the USER
# physically pressing Ctrl+V during the settle window (our own injected
# Ctrl+V chord is fully released before the worker parks, so a down-state
# here can only come from the keyboard).
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
_KEY_DOWN_BIT = 0x8000

# Chromium hosts normally need a long post-paste settle because their renderer
# may consume Ctrl+V asynchronously. Per-process overrides can shorten that
# hold when a client is known to be responsive without dropping all the way to
# the aggressive 200ms native-window floor. ChatGPT.exe uses a 1000ms override,
# cutting the visible hold while retaining a 5x margin over
# the floor. Cursor/Chrome/other Electron targets keep the safer 3000ms delay.
ELECTRON_SETTLE_DELAY_OVERRIDES_MS = {"chatgpt.exe": 1000}

user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT

# Private clipboard format stamped alongside every text we inject. Windows
# keeps it exactly as long as our content owns the clipboard: any other app
# that copies something calls EmptyClipboard first, wiping the marker. So a
# clipboard (or a backup taken from it) carrying this format is guaranteed to
# be an Aria injection — across processes, restarts and package upgrades —
# which the self-echo guard uses to avoid restoring our own previous sentence
# after a paste (see _backup_is_own_injection). Registration is idempotent
# and session-wide; 0 (failure) degrades to the in-process history fallback.
try:
    _ARIA_MARKER_FORMAT = int(
        user32.RegisterClipboardFormatW("Aria.VoiceInput.Injected") or 0
    )
except Exception:
    _ARIA_MARKER_FORMAT = 0

# Window class names that support EM_REPLACESEL (standard text controls).
# For these controls, EM_REPLACESEL goes through the native text rendering
# pipeline (including font linking for CJK), avoiding the white-box issue
# that SendInput KEYEVENTF_UNICODE causes in RichEdit controls.
_EM_REPLACESEL_CLASSES = STANDARD_TEXT_CONTROL_CLASSES

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

user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_size_t),
]
user32.SendMessageTimeoutW.restype = wintypes.LPARAM

user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long

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

    # Native Word COM delivery is enabled after the real isolated-document
    # matrix proved formatting, revisions and single-step undo. Explicit false
    # remains a supported rollback switch.
    word_native_enabled: bool = True

    # Terminal profiles are explicit and conservative. The default reproduces
    # the existing single Ctrl+V paste with flattened newlines.
    terminal_profile: str = "safe"
    terminal_chunking_enabled: bool = False
    terminal_chunk_chars: int = 1000
    terminal_chunk_delay_ms: int = 120

    # Games are never guessed. Only exact, enabled executable profiles enter
    # the game-chat state machine; malformed profiles fail to Draft Box.
    game_chat_profiles: Dict[str, Dict] = field(default_factory=dict)

    # Layer 0: Permission handling
    check_elevation: bool = True  # Check if target window is elevated
    elevation_callback: Optional[Callable[[str], None]] = (
        None  # Callback to show warning
    )

    # Called (with a short user-facing message) when the clipboard paste
    # failed: either the set-verify gate aborted it (nothing pasted) or the
    # Ctrl+V SendInput failed (possibly nothing pasted). The user should
    # re-check / re-dictate instead of assuming the text landed. Must not
    # raise/block.
    paste_abort_callback: Optional[Callable[[str], None]] = None


class _PendingRestore:
    """Deferred clipboard-restore state for one injection (PERF-7).

    `outcome` is written once by the worker thread: "ran" when the settle
    delay elapsed and the worker proceeded to its guard/restore phase,
    "canceled" when a follow-up injection canceled the wait first. After
    join(), "canceled" means the backup was NOT restored and may be carried
    forward to the next injection session.

    `backup` may be an EMPTY dict: the pre-injection clipboard held nothing
    worth keeping (truly empty, or our own previous injection), and the
    restore phase then CLEARS the clipboard instead of leaving the dictated
    sentence on it forever.

    `injected_text` is the text this injection verifiably placed on the
    clipboard — the seq guard uses it to tell a genuine user copy from an
    external tool echoing our own content back (IME clipboard managers,
    cross-device sync, Electron internals).
    """

    def __init__(
        self,
        backup: Dict[int, bytes],
        seq_after_set: Optional[int],
        injected_text: Optional[str] = None,
    ):
        self.backup = backup
        self.seq_after_set = seq_after_set
        self.injected_text = injected_text
        self.cancel = threading.Event()
        self.outcome = "pending"  # -> "ran" | "canceled"
        self.thread: Optional[threading.Thread] = None


class OutputInjector:
    """
    Injects text into the active application via clipboard and paste.

    Usage:
        injector = OutputInjector()
        injector.insert_text("Hello, world!")
    """

    # Retry delays for clipboard backup/restore failures.
    BACKUP_RETRY_DELAY_MS = 50
    RESTORE_RETRY_DELAY_MS = 100
    # Second (last-chance) retry delay for the deferred restore: a failed
    # restore permanently loses the user's clipboard content, so it is worth
    # one more, longer-spaced attempt against transient contention.
    RESTORE_RETRY_DELAY_2_MS = 300
    # Delay before re-attempting a failed clipboard set in the verify loop.
    SET_RETRY_DELAY_MS = 50
    # Re-set attempts after the initial set when the readback verify fails.
    SET_VERIFY_RETRIES = 2
    # Post-paste settle delay for Chromium/Electron targets. Their paste
    # pipeline reads the clipboard asynchronously (browser↔renderer IPC) and
    # field reports show 400ms can still be too early under load; restoring
    # at the default 200ms made Cursor paste a previously-copied image
    # instead of the dictated text (observed 2026-07-20, backup keys=[8]).
    # 1000ms also proved insufficient on a machine under GPU+game load
    # (observed 2026-07-20 06:06: Cursor consumed the paste after the +1000ms
    # restore and injected the restored previous sentence). No fixed delay
    # truly wins this race — the self-echo guard (_backup_is_own_injection)
    # removes the dominant failure mode; this delay only protects genuine
    # user content (images, files, foreign text) and is free for dictation
    # bursts thanks to the cancel/carry-forward path.
    ELECTRON_SETTLE_DELAY_MS = 3000
    # Poll step for the settle wait. Between polls the worker checks for a
    # cancel (follow-up injection) and — when the backup holds real user
    # content — for a physical Ctrl+V, which triggers an immediate restore
    # so the user pastes THEIR content instead of the dictated sentence.
    FAST_RESTORE_POLL_S = 0.025
    # Ignore the paste chord for the first part of the settle window: a
    # chord seen that early can only be a key the user was already holding
    # when the injection fired (auto-repeat paste), not a deliberate
    # "paste my clipboard back" press.
    FAST_RESTORE_GRACE_S = 0.25
    # How many recent injected texts to remember for self-echo detection.
    RECENT_INJECTION_HISTORY = 8
    # Max bytes captured per clipboard format in a backup. The old 10MB cap
    # silently dropped copied screenshots (a 2560x1440 32bpp CF_DIB is
    # ~14.7MB, 4K ~33MB), so the paste overwrote the user's image with no
    # restore. 64MB covers 4K-and-beyond DIBs; the buffer is transient.
    MAX_CLIPBOARD_FORMAT_BYTES = 64 * 1024 * 1024

    def __init__(self, config: Optional[OutputConfig] = None):
        self.config = config or OutputConfig()
        self._clipboard_lock = None  # Optional thread lock for clipboard operations
        # PERF-7: deferred clipboard-restore worker of the previous injection.
        # The restore (settle delay + write-back) runs on a background thread
        # so insert_text() can return right after the paste is confirmed.
        self._pending_restore: Optional[_PendingRestore] = None
        # Texts we recently placed on the clipboard (verified sets), used to
        # recognize self-echo backups — see _backup_is_own_injection().
        self._recent_injections: deque = deque(maxlen=self.RECENT_INJECTION_HISTORY)
        # Privacy-safe, content-free result for the most recent synchronous
        # insert_text() call.  The ASR history layer reads this immediately
        # after insertion so failed delivery remains recoverable without
        # storing foreground app names or window titles.
        self._last_delivery_metadata: Dict = {}
        self._word_adapter = None
        # Logical terminal-tail receipts.  They never claim screen-buffer
        # readback; they only bind Aria's own latest single-line paste to a
        # bounded Backspace+paste transaction on the same foreground target.
        self._terminal_recent_lock = threading.Lock()
        self._terminal_recent_offsets: Dict[tuple, int] = {}
        self._terminal_pending_capture = None
        # Capability-gated receipts for Qt/Electron/custom text composers.
        # The pending tuple contains only target identity plus hashes/lengths;
        # copied field text never survives the synchronous transaction.
        self._generic_recent_lock = threading.Lock()
        self._generic_pending_capture = None
        self._last_paste_partial_possible = False

    def set_clipboard_lock(self, lock) -> None:
        """Set a threading lock for thread-safe clipboard operations."""
        self._clipboard_lock = lock

    def capture_target_snapshot(self) -> TargetSnapshot:
        """Capture a content-free identity and profile for the current target.

        UI Automation is intentionally not invoked here: an output hot path
        must not hang on a broken provider.  Standard Win32 control classes
        are detected directly; richer Word/CLI/game adapters can add bounded
        probes above this stable baseline.
        """

        last_snapshot = None
        for attempt in range(2):
            fg_info = get_foreground_window_info()
            hwnd = int(fg_info.get("hwnd") or 0)
            pid = int(fg_info.get("pid") or 0)
            window_class = ""
            focused_class = ""
            focused_hwnd = 0

            if hwnd:
                try:
                    cls_buf = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(hwnd, cls_buf, 256)
                    window_class = cls_buf.value or ""
                except Exception:
                    pass

                try:
                    focused_hwnd = int(self._get_focused_control() or 0)
                    if focused_hwnd:
                        cls_buf = ctypes.create_unicode_buffer(256)
                        user32.GetClassNameW(focused_hwnd, cls_buf, 256)
                        focused_class = cls_buf.value or ""
                except Exception:
                    pass

            process_name = fg_info.get("process_name", "")
            game_profile = self._resolve_game_chat_profile(process_name)
            last_snapshot = TargetSnapshot(
                hwnd=hwnd,
                pid=pid,
                focused_hwnd=focused_hwnd,
                profile=classify_target_surface(
                    process_name=process_name,
                    window_class=window_class,
                    focused_class=focused_class,
                    explicit_kind=(
                        SurfaceKind.GAME if game_profile is not None else None
                    ),
                ),
            )

            # Compatibility callers sometimes supply only process metadata to
            # inspect routing policy. A real commit snapshot, however, must be
            # stable across the whole read so an Alt+Tab cannot mix the old
            # top-level window with the new focused child control.
            if not last_snapshot.is_valid:
                return last_snapshot
            try:
                end_hwnd, end_pid = get_foreground_window_pid()
            except Exception:
                logger.debug("Could not verify target snapshot stability", exc_info=True)
                break
            if (int(end_hwnd or 0), int(end_pid or 0)) == (hwnd, pid):
                enriched_snapshot = self._attach_adapter_token(last_snapshot)
                if (
                    getattr(self.config, "word_native_enabled", False)
                    and last_snapshot.profile.kind == SurfaceKind.DOCUMENT
                    and supports_word_compatible_ranges(
                        last_snapshot.profile.process_name
                    )
                ):
                    # Native bookmark capture can cross a COM boundary. Re-read
                    # the foreground afterwards so a slow Word call cannot bind
                    # a range from one window to the HWND identity of another.
                    try:
                        adapter_end_hwnd, adapter_end_pid = get_foreground_window_pid()
                    except Exception:
                        break
                    if (int(adapter_end_hwnd or 0), int(adapter_end_pid or 0)) != (
                        hwnd,
                        pid,
                    ):
                        logger.debug(
                            "Foreground changed while capturing native adapter bookmark "
                            f"(attempt {attempt + 1}/2); retrying"
                        )
                        continue
                return enriched_snapshot
            logger.debug(
                "Foreground changed while capturing output target "
                f"(attempt {attempt + 1}/2); retrying"
            )

        # Repeated focus churn (or inability to complete the identity read)
        # must not produce a mixed snapshot that can later authorize delivery.
        assert last_snapshot is not None
        return TargetSnapshot(
            hwnd=0,
            pid=0,
            focused_hwnd=0,
            profile=last_snapshot.profile,
        )

    def inspect_target_surface(self) -> TargetSurfaceProfile:
        """Return a conservative profile for the current focused carrier."""

        return self.capture_target_snapshot().profile

    def get_last_delivery_metadata(self) -> Dict:
        """Return a copy of the latest content-free delivery report."""

        delivery = self._last_delivery_metadata.get("delivery")
        return {"delivery": dict(delivery)} if isinstance(delivery, dict) else {}

    def _get_word_adapter(self):
        """Lazy-load the Word adapter without attaching to or launching Word."""
        if self._word_adapter is None:
            from .word_adapter import WordAdapter

            self._word_adapter = WordAdapter()
        return self._word_adapter

    def _resolve_game_chat_profile(self, process_name: str):
        """Return an exact enabled profile, including invalid fail-closed ones."""
        from .game_chat_adapter import resolve_game_chat_profile

        return resolve_game_chat_profile(
            getattr(self.config, "game_chat_profiles", {}), process_name
        )

    def _attach_adapter_token(self, snapshot: TargetSnapshot) -> TargetSnapshot:
        """Attach a content-free native/profile bookmark when applicable."""
        if snapshot.is_valid and snapshot.profile.kind == SurfaceKind.GAME:
            game_profile = self._resolve_game_chat_profile(
                snapshot.profile.process_name
            )
            if game_profile is not None:
                return replace(snapshot, adapter_token=game_profile)
        if (
            not getattr(self.config, "word_native_enabled", False)
            or not snapshot.is_valid
            or snapshot.profile.kind != SurfaceKind.DOCUMENT
            or not supports_word_compatible_ranges(snapshot.profile.process_name)
        ):
            return snapshot
        try:
            bookmark = self._get_word_adapter().capture_bookmark(snapshot)
        except Exception:
            bookmark = None
        if bookmark is None:
            return snapshot
        return replace(snapshot, adapter_token=bookmark)

    def _attempt_word_native_insert(
        self,
        text: str,
        expected_target: Optional[TargetSnapshot],
        *,
        expected_selection_text: Optional[str] = None,
    ):
        """Return a WordInsertResult, or None when native routing is inapplicable."""
        if not getattr(self.config, "word_native_enabled", False):
            return None

        word_target = expected_target
        if word_target is None:
            try:
                word_target = self.capture_target_snapshot()
            except Exception:
                return None
        if (
            word_target is None
            or not word_target.is_valid
            or word_target.profile.kind != SurfaceKind.DOCUMENT
            or not supports_word_compatible_ranges(word_target.profile.process_name)
        ):
            return None

        try:
            from .word_adapter import WordInsertResult, WordInsertStatus
        except Exception:
            return None

        try:
            adapter = self._get_word_adapter()
        except Exception:
            # Construction/import failures happen before a Word range write and
            # may conservatively fall back to the existing guarded clipboard.
            return WordInsertResult(
                success=False,
                status=WordInsertStatus.COM_UNAVAILABLE,
                safe_to_fallback=True,
            )

        try:
            if expected_selection_text is None:
                return adapter.insert_text(
                    text,
                    word_target,
                    target_is_current=self.is_target_snapshot_current,
                )
            return adapter.insert_text(
                text,
                word_target,
                target_is_current=self.is_target_snapshot_current,
                expected_selection_text=expected_selection_text,
            )
        except Exception:
            # An unexpected adapter exception has unknown write state. Never
            # turn it into an automatic clipboard retry that could duplicate.
            return WordInsertResult(
                success=False,
                status=WordInsertStatus.INSERT_FAILED,
                safe_to_fallback=False,
                partial_possible=True,
            )

    def _plan_terminal_delivery(self, text: str):
        """Build one content-free terminal plan from explicit output config."""
        from .terminal_adapter import TerminalAdapter

        adapter = TerminalAdapter(
            getattr(self.config, "terminal_profile", "safe"),
            chunking_enabled=getattr(
                self.config, "terminal_chunking_enabled", False
            ),
            chunk_chars=getattr(self.config, "terminal_chunk_chars", 1000),
        )
        return adapter.plan(text)

    @staticmethod
    def _terminal_target_key(target: TargetSnapshot) -> tuple:
        profile = getattr(target, "profile", None)
        return (
            int(getattr(target, "hwnd", 0) or 0),
            int(getattr(target, "pid", 0) or 0),
            int(getattr(target, "focused_hwnd", 0) or 0),
            str(getattr(profile, "process_name", "") or "").lower(),
        )

    @staticmethod
    def _generic_target_key(target: TargetSnapshot) -> tuple:
        profile = getattr(target, "profile", None)
        return (
            int(getattr(target, "hwnd", 0) or 0),
            int(getattr(target, "pid", 0) or 0),
            int(getattr(target, "focused_hwnd", 0) or 0),
            str(getattr(profile, "process_name", "") or "").lower(),
            str(getattr(getattr(profile, "kind", None), "value", "") or ""),
        )

    def _record_game_delivery(
        self,
        status: str,
        *,
        transport: str = "none",
        reason: str = "",
        partial_possible: bool = False,
        open_chat_attempted: bool = False,
        auto_submit_configured: bool = False,
        submit_status: str = "disabled",
    ) -> None:
        """Store one content-free game-chat transaction report."""
        delivery = {
            "status": status,
            "surface": SurfaceKind.GAME.value,
            "transport": transport,
            "confidence": "best_effort",
            "addressable_text": False,
            "allow_auto_send": False,
            "partial_possible": bool(partial_possible),
            "open_chat_attempted": bool(open_chat_attempted),
            "auto_submit_configured": bool(auto_submit_configured),
            "submit_status": str(submit_status or "disabled"),
        }
        if reason:
            delivery["reason"] = str(reason)
        self._last_delivery_metadata = {"delivery": delivery}

    def _record_voice_edit_delivery(
        self,
        result,
        expected_target: Optional[TargetSnapshot] = None,
        *,
        reason_override: str = "",
        operation: str = "voice_edit",
    ) -> None:
        """Store a content-free result for one deterministic edit command."""
        from .standard_text_adapter import StandardTextEditStatus

        status_value = str(
            getattr(getattr(result, "status", None), "value", "rejected")
        )
        success = bool(getattr(result, "success", False))
        partial = bool(getattr(result, "partial_possible", False))
        status_prefix = (
            "voice_edit_undo" if operation == "voice_edit_undo" else "voice_edit"
        )
        if success:
            delivery_status = f"{status_prefix}_confirmed"
        elif partial:
            delivery_status = f"{status_prefix}_partial"
        else:
            delivery_status = f"{status_prefix}_{status_value}"

        profile = getattr(expected_target, "profile", None)
        surface = getattr(getattr(profile, "kind", None), "value", "custom")
        addressable = bool(getattr(profile, "addressable_text", False))
        transport = (
            "win32_edit"
            if surface == SurfaceKind.STANDARD_TEXT.value
            else "none"
        )
        delivery = {
            "status": delivery_status,
            "surface": surface,
            "transport": transport,
            "operation": operation,
            "confidence": "native" if success else "manual_only",
            "addressable_text": addressable,
            "allow_auto_send": False,
            "partial_possible": partial,
            "match_count": max(0, int(getattr(result, "match_count", 0) or 0)),
            "undo_available": bool(getattr(result, "undo_available", False)),
            "reason": reason_override or status_value,
        }
        if getattr(result, "status", None) == StandardTextEditStatus.CONFIRMED:
            delivery["confirmed"] = True
        self._last_delivery_metadata = {"delivery": delivery}

    def reject_voice_edit(
        self,
        reason_code: str,
        expected_target: Optional[TargetSnapshot] = None,
    ) -> None:
        """Clear stale delivery state for a malformed explicit edit command."""
        from .standard_text_adapter import (
            StandardTextEditResult,
            StandardTextEditStatus,
        )

        self._record_voice_edit_delivery(
            StandardTextEditResult(
                False, StandardTextEditStatus.INVALID_ARGUMENT
            ),
            expected_target,
            reason_override=str(reason_code or "voice_edit_rejected"),
        )

    def apply_standard_text_edit(
        self,
        source: str,
        replacement: str,
        expected_target: Optional[TargetSnapshot],
    ):
        """Replace all safe exact matches in one native text transaction."""
        from .standard_text_adapter import (
            StandardTextAdapter,
            StandardTextEditResult,
            StandardTextEditStatus,
            Win32StandardTextBackend,
        )

        if expected_target is None or not expected_target.is_valid:
            result = StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_UNAVAILABLE
            )
            self._record_voice_edit_delivery(result, expected_target)
            return result

        if not self.is_target_snapshot_current(expected_target):
            result = StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )
            self._record_voice_edit_delivery(result, expected_target)
            return result

        if self.config.check_elevation:
            try:
                elevation_blocked = is_target_elevated() and not is_aria_elevated()
            except Exception:
                elevation_blocked = False
            if elevation_blocked:
                result = StandardTextEditResult(
                    False, StandardTextEditStatus.ELEVATION_REQUIRED
                )
                self._record_voice_edit_delivery(result, expected_target)
                return result

        try:
            result = StandardTextAdapter(
                Win32StandardTextBackend(user32),
                target_is_current=self.is_target_snapshot_current,
            ).replace_all(expected_target, source, replacement)
        except Exception:
            logger.exception("Standard text voice edit raised; write state unknown")
            result = StandardTextEditResult(
                False,
                StandardTextEditStatus.WRITE_PARTIAL,
                partial_possible=True,
                undo_available=True,
            )
        self._record_voice_edit_delivery(result, expected_target)
        return result

    def apply_standard_text_edit_candidate(
        self,
        source: str,
        replacement: str,
        expected_target: Optional[TargetSnapshot],
        candidate_token,
        occurrence: int,
    ):
        """Commit one numbered exact match from a prior content snapshot."""

        from .standard_text_adapter import (
            StandardTextAdapter,
            StandardTextEditResult,
            StandardTextEditStatus,
            Win32StandardTextBackend,
        )

        if expected_target is None or not expected_target.is_valid:
            result = StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_UNAVAILABLE
            )
            self._record_voice_edit_delivery(result, expected_target)
            return result
        if not self.is_target_snapshot_current(expected_target):
            result = StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )
            self._record_voice_edit_delivery(result, expected_target)
            return result
        if self.config.check_elevation:
            try:
                elevation_blocked = is_target_elevated() and not is_aria_elevated()
            except Exception:
                elevation_blocked = False
            if elevation_blocked:
                result = StandardTextEditResult(
                    False, StandardTextEditStatus.ELEVATION_REQUIRED
                )
                self._record_voice_edit_delivery(result, expected_target)
                return result
        try:
            result = StandardTextAdapter(
                Win32StandardTextBackend(user32),
                target_is_current=self.is_target_snapshot_current,
            ).replace_candidate(
                expected_target,
                candidate_token,
                int(occurrence),
                source,
                replacement,
            )
        except Exception:
            logger.exception(
                "Numbered standard text edit raised; write state unknown"
            )
            result = StandardTextEditResult(
                False,
                StandardTextEditStatus.WRITE_PARTIAL,
                partial_possible=True,
                undo_available=True,
            )
        self._record_voice_edit_delivery(result, expected_target)
        return result

    def revert_standard_text_edit(
        self,
        source: str,
        replacement: str,
        expected_target: Optional[TargetSnapshot],
        undo_token,
    ):
        """Compensate one still-identical confirmed native voice edit."""

        from .standard_text_adapter import (
            StandardTextAdapter,
            StandardTextEditResult,
            StandardTextEditStatus,
            Win32StandardTextBackend,
        )

        if expected_target is None or not expected_target.is_valid:
            result = StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_UNAVAILABLE
            )
            self._record_voice_edit_delivery(
                result, expected_target, operation="voice_edit_undo"
            )
            return result
        if not self.is_target_snapshot_current(expected_target):
            result = StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )
            self._record_voice_edit_delivery(
                result, expected_target, operation="voice_edit_undo"
            )
            return result
        if self.config.check_elevation:
            try:
                elevation_blocked = is_target_elevated() and not is_aria_elevated()
            except Exception:
                elevation_blocked = False
            if elevation_blocked:
                result = StandardTextEditResult(
                    False, StandardTextEditStatus.ELEVATION_REQUIRED
                )
                self._record_voice_edit_delivery(
                    result, expected_target, operation="voice_edit_undo"
                )
                return result
        try:
            result = StandardTextAdapter(
                Win32StandardTextBackend(user32),
                target_is_current=self.is_target_snapshot_current,
            ).revert_edit(
                expected_target,
                undo_token,
                source,
                replacement,
            )
        except Exception:
            logger.exception("Standard text edit compensation raised; state unknown")
            result = StandardTextEditResult(
                False,
                StandardTextEditStatus.WRITE_PARTIAL,
                partial_possible=True,
                undo_available=True,
            )
        self._record_voice_edit_delivery(
            result, expected_target, operation="voice_edit_undo"
        )
        return result

    @staticmethod
    def _selection_transaction_status(reason):
        """Normalize adapter-specific failures into the public transaction model."""
        from .selection_transaction import SelectionTransactionStatus

        value = str(getattr(reason, "value", reason) or "native_failed")
        mapping = {
            "confirmed": SelectionTransactionStatus.CONFIRMED,
            "inserted": SelectionTransactionStatus.CONFIRMED,
            "no_change": SelectionTransactionStatus.NO_CHANGE,
            "invalid_argument": SelectionTransactionStatus.INVALID_ARGUMENT,
            "target_unavailable": SelectionTransactionStatus.TARGET_UNAVAILABLE,
            "target_changed": SelectionTransactionStatus.TARGET_CHANGED,
            "window_mismatch": SelectionTransactionStatus.TARGET_CHANGED,
            "unsupported_surface": SelectionTransactionStatus.UNSUPPORTED_SURFACE,
            "unsupported_control": SelectionTransactionStatus.UNSUPPORTED_SURFACE,
            "non_main_story": SelectionTransactionStatus.UNSUPPORTED_SURFACE,
            "unsafe_selection": SelectionTransactionStatus.UNSUPPORTED_SURFACE,
            "bookmark_unavailable": SelectionTransactionStatus.UNSUPPORTED_SURFACE,
            "com_unavailable": SelectionTransactionStatus.UNSUPPORTED_SURFACE,
            "selection_unavailable": SelectionTransactionStatus.SELECTION_UNAVAILABLE,
            "selection_failed": SelectionTransactionStatus.SELECTION_UNAVAILABLE,
            "selection_changed": SelectionTransactionStatus.SELECTION_CHANGED,
            "content_changed": SelectionTransactionStatus.CONTENT_CHANGED,
            "protected_control": SelectionTransactionStatus.PROTECTED_CONTROL,
            "protected_view": SelectionTransactionStatus.PROTECTED_CONTROL,
            "document_protected": SelectionTransactionStatus.PROTECTED_CONTROL,
            "content_control_locked": SelectionTransactionStatus.PROTECTED_CONTROL,
            "read_only": SelectionTransactionStatus.READ_ONLY,
            "document_read_only": SelectionTransactionStatus.READ_ONLY,
            "undo_unavailable": SelectionTransactionStatus.UNDO_UNAVAILABLE,
            "undo_busy": SelectionTransactionStatus.UNDO_UNAVAILABLE,
            "undo_start_failed": SelectionTransactionStatus.UNDO_UNAVAILABLE,
            "elevation_required": SelectionTransactionStatus.ELEVATION_REQUIRED,
            "write_rejected": SelectionTransactionStatus.WRITE_REJECTED,
            "write_partial": SelectionTransactionStatus.WRITE_PARTIAL,
            "verify_failed": SelectionTransactionStatus.WRITE_PARTIAL,
            "insert_failed": SelectionTransactionStatus.WRITE_PARTIAL,
        }
        return mapping.get(value, SelectionTransactionStatus.NATIVE_FAILED)

    def capture_selection_transaction(
        self,
        selected_text: str,
        expected_target: Optional[TargetSnapshot],
    ):
        """Bind copied text to one addressable native selection before a wait."""
        from .selection_transaction import (
            SelectionCaptureResult,
            SelectionTransactionStatus,
        )

        if not selected_text:
            return SelectionCaptureResult(
                False, SelectionTransactionStatus.INVALID_ARGUMENT
            )
        if expected_target is None or not expected_target.is_valid:
            return SelectionCaptureResult(
                False, SelectionTransactionStatus.TARGET_UNAVAILABLE
            )
        try:
            current_target = self.capture_target_snapshot()
        except Exception:
            return SelectionCaptureResult(
                False, SelectionTransactionStatus.TARGET_UNAVAILABLE
            )
        if (
            not current_target.is_valid
            or not expected_target.matches(current_target)
            or expected_target.profile.kind != current_target.profile.kind
        ):
            return SelectionCaptureResult(
                False, SelectionTransactionStatus.TARGET_CHANGED
            )

        if current_target.profile.kind == SurfaceKind.STANDARD_TEXT:
            from .standard_text_adapter import (
                StandardTextAdapter,
                Win32StandardTextBackend,
            )

            try:
                capture = StandardTextAdapter(
                    Win32StandardTextBackend(user32),
                    target_is_current=self.is_target_snapshot_current,
                ).capture_selection(current_target, selected_text)
            except Exception:
                logger.exception("Native selection capture failed")
                return SelectionCaptureResult(
                    False, SelectionTransactionStatus.NATIVE_FAILED
                )
            if not capture.success or capture.bookmark is None:
                return SelectionCaptureResult(
                    False, self._selection_transaction_status(capture.status)
                )
            return SelectionCaptureResult(
                True,
                SelectionTransactionStatus.READY,
                target=replace(current_target, adapter_token=capture.bookmark),
                transport="win32_edit",
            )

        if (
            current_target.profile.kind == SurfaceKind.DOCUMENT
            and supports_word_compatible_ranges(current_target.profile.process_name)
            and getattr(self.config, "word_native_enabled", False)
        ):
            from .word_adapter import WordBookmark

            current_bookmark = current_target.adapter_token
            previous_bookmark = expected_target.adapter_token
            if not isinstance(current_bookmark, WordBookmark):
                return SelectionCaptureResult(
                    False, SelectionTransactionStatus.UNSUPPORTED_SURFACE
                )
            if (
                not isinstance(previous_bookmark, WordBookmark)
                or previous_bookmark != current_bookmark
            ):
                return SelectionCaptureResult(
                    False, SelectionTransactionStatus.SELECTION_CHANGED
                )
            return SelectionCaptureResult(
                True,
                SelectionTransactionStatus.READY,
                target=current_target,
                transport="word_com",
            )

        return SelectionCaptureResult(
            False, SelectionTransactionStatus.UNSUPPORTED_SURFACE
        )

    def capture_recent_voice_insert(
        self,
        inserted_text: str,
        expected_target: Optional[TargetSnapshot],
    ):
        """Bind one confirmed insertion to its exact native post-insert range."""

        from .selection_transaction import (
            SelectionCaptureResult,
            SelectionTransactionStatus,
        )

        if not inserted_text or expected_target is None or not expected_target.is_valid:
            return SelectionCaptureResult(
                False, SelectionTransactionStatus.INVALID_ARGUMENT
            )
        if not self.is_target_snapshot_current(expected_target):
            return SelectionCaptureResult(
                False, SelectionTransactionStatus.TARGET_CHANGED
            )

        if expected_target.profile.kind == SurfaceKind.TERMINAL:
            from .terminal_adapter import (
                PasteChord,
                TerminalRecentTextBookmark,
                TerminalTailProbeStatus,
                terminal_backspace_units,
            )

            units = terminal_backspace_units(inserted_text)
            if units is None:
                return SelectionCaptureResult(
                    False, SelectionTransactionStatus.UNSUPPORTED_SURFACE
                )
            key = self._terminal_target_key(expected_target)
            with self._terminal_recent_lock:
                pending = self._terminal_pending_capture
                if pending is not None and pending[0] != key:
                    self._terminal_pending_capture = None
                    pending = None
                if pending is not None:
                    bookmark = pending[1]
                    if int(bookmark.end) - int(bookmark.start) != units:
                        self._terminal_pending_capture = None
                        return SelectionCaptureResult(
                            False, SelectionTransactionStatus.CONTENT_CHANGED
                        )
                    self._terminal_pending_capture = None
                    return SelectionCaptureResult(
                        True,
                        SelectionTransactionStatus.READY,
                        target=replace(expected_target, adapter_token=bookmark),
                        transport="terminal_tail",
                    )
                start = int(self._terminal_recent_offsets.get(key, 0))

            delivery = dict(self._last_delivery_metadata.get("delivery", {}))
            transport = str(delivery.get("transport") or "")
            if (
                str(delivery.get("surface") or "") != SurfaceKind.TERMINAL.value
                or str(delivery.get("status") or "") != "sent"
                or bool(delivery.get("partial_possible"))
                or bool(delivery.get("newlines_flattened"))
                or transport not in {"clipboard", "clipboard_shift_insert"}
            ):
                return SelectionCaptureResult(
                    False, SelectionTransactionStatus.UNSUPPORTED_SURFACE
                )
            chord = PasteChord(
                str(delivery.get("paste_chord") or PasteChord.CTRL_V.value)
            )
            # UIA can take a few rendered frames after Aria's paste.  Do not
            # hold the recent-range state lock while waiting for readback.
            tail_probe = self._capture_terminal_tail_guard_with_retry(
                expected_target.hwnd, inserted_text
            )
            if (
                tail_probe.status != TerminalTailProbeStatus.MATCH
                or tail_probe.anchor is None
            ):
                with self._terminal_recent_lock:
                    if (
                        self._terminal_pending_capture is None
                        and int(self._terminal_recent_offsets.get(key, 0)) == start
                    ):
                        self._terminal_recent_offsets.pop(key, None)
                return SelectionCaptureResult(
                    False,
                    (
                        SelectionTransactionStatus.CONTENT_CHANGED
                        if tail_probe.status == TerminalTailProbeStatus.MISMATCH
                        else SelectionTransactionStatus.UNSUPPORTED_SURFACE
                    ),
                )
            if not self.is_target_snapshot_current(expected_target):
                with self._terminal_recent_lock:
                    if (
                        self._terminal_pending_capture is None
                        and int(self._terminal_recent_offsets.get(key, 0)) == start
                    ):
                        self._terminal_recent_offsets.pop(key, None)
                return SelectionCaptureResult(
                    False, SelectionTransactionStatus.TARGET_CHANGED
                )
            with self._terminal_recent_lock:
                if (
                    self._terminal_pending_capture is not None
                    or int(self._terminal_recent_offsets.get(key, 0)) != start
                ):
                    return SelectionCaptureResult(
                        False, SelectionTransactionStatus.CONTENT_CHANGED
                    )
                bookmark = TerminalRecentTextBookmark(
                    hwnd=int(expected_target.hwnd),
                    start=start,
                    end=start + units,
                    pid=int(expected_target.pid),
                    focused_hwnd=int(expected_target.focused_hwnd),
                    paste_chord=chord,
                    uia_verified=True,
                    anchor_chars=int(tail_probe.anchor.chars),
                    anchor_sha256=str(tail_probe.anchor.sha256),
                )
                self._terminal_recent_offsets[key] = int(bookmark.end)
            return SelectionCaptureResult(
                True,
                SelectionTransactionStatus.READY,
                target=replace(expected_target, adapter_token=bookmark),
                transport="terminal_tail",
            )

        if expected_target.profile.kind == SurfaceKind.STANDARD_TEXT:
            from .standard_text_adapter import (
                StandardTextAdapter,
                Win32StandardTextBackend,
            )

            try:
                capture = StandardTextAdapter(
                    Win32StandardTextBackend(user32),
                    target_is_current=self.is_target_snapshot_current,
                ).capture_recent_insert(expected_target, inserted_text)
            except Exception:
                logger.exception("Recent standard-text range capture failed")
                return SelectionCaptureResult(
                    False, SelectionTransactionStatus.NATIVE_FAILED
                )
            if not capture.success or capture.bookmark is None:
                return SelectionCaptureResult(
                    False, self._selection_transaction_status(capture.status)
                )
            return SelectionCaptureResult(
                True,
                SelectionTransactionStatus.READY,
                target=replace(expected_target, adapter_token=capture.bookmark),
                transport="win32_edit",
            )

        if (
            expected_target.profile.kind == SurfaceKind.DOCUMENT
            and supports_word_compatible_ranges(expected_target.profile.process_name)
            and getattr(self.config, "word_native_enabled", False)
        ):
            try:
                bookmark = self._get_word_adapter().capture_recent_insert(
                    inserted_text,
                    expected_target,
                    target_is_current=self.is_target_snapshot_current,
                )
            except Exception:
                bookmark = None
            if bookmark is None:
                return SelectionCaptureResult(
                    False, SelectionTransactionStatus.SELECTION_UNAVAILABLE
                )
            return SelectionCaptureResult(
                True,
                SelectionTransactionStatus.READY,
                target=replace(expected_target, adapter_token=bookmark),
                transport="word_com",
            )

        if expected_target.profile.kind in {
            SurfaceKind.CUSTOM,
            SurfaceKind.ELECTRON,
        }:
            from .generic_text_adapter import (
                GenericTextFieldAdapter,
                WindowsGenericTextBackend,
                fingerprint_field_text,
            )

            key = self._generic_target_key(expected_target)
            inserted_fingerprint = fingerprint_field_text(inserted_text)
            with self._generic_recent_lock:
                pending = self._generic_pending_capture
                if pending is not None and pending[0] != key:
                    self._generic_pending_capture = None
                    pending = None
                if pending is not None:
                    self._generic_pending_capture = None
                    if pending[2] != inserted_fingerprint:
                        return SelectionCaptureResult(
                            False, SelectionTransactionStatus.CONTENT_CHANGED
                        )
                    return SelectionCaptureResult(
                        True,
                        SelectionTransactionStatus.READY,
                        target=replace(
                            expected_target, adapter_token=pending[1]
                        ),
                        transport="generic_text_field",
                    )

            delivery = dict(self._last_delivery_metadata.get("delivery", {}))
            if (
                str(delivery.get("surface") or "")
                != expected_target.profile.kind.value
                or str(delivery.get("status") or "") != "sent"
                or str(delivery.get("transport") or "") != "clipboard"
                or bool(delivery.get("partial_possible"))
                or bool(delivery.get("newlines_flattened"))
            ):
                return SelectionCaptureResult(
                    False, SelectionTransactionStatus.UNSUPPORTED_SURFACE
                )
            # Do not turn the asynchronous clipboard restore into a 0.2-3s
            # post-dictation stall.  A recent rewrite performs the same guarded
            # capture lazily after the user's command, by which time the target
            # has normally consumed the paste and the worker has restored the
            # user's clipboard.
            pending_restore = self._pending_restore
            if (
                pending_restore is not None
                and pending_restore.thread is not None
                and pending_restore.thread.is_alive()
            ):
                return SelectionCaptureResult(
                    False, SelectionTransactionStatus.SELECTION_UNAVAILABLE
                )
            try:
                captured = GenericTextFieldAdapter(
                    WindowsGenericTextBackend(self)
                ).capture_recent_insert(expected_target, inserted_text)
            except Exception:
                logger.exception("Generic recent-text capture raised")
                return SelectionCaptureResult(
                    False, SelectionTransactionStatus.NATIVE_FAILED
                )
            if not captured.success or captured.bookmark is None:
                return SelectionCaptureResult(
                    False, self._selection_transaction_status(captured.status)
                )
            return SelectionCaptureResult(
                True,
                SelectionTransactionStatus.READY,
                target=replace(
                    expected_target, adapter_token=captured.bookmark
                ),
                transport="generic_text_field",
            )

        return SelectionCaptureResult(
            False, SelectionTransactionStatus.UNSUPPORTED_SURFACE
        )

    def capture_recent_voice_group(
        self,
        inserted_segments,
        expected_target: Optional[TargetSnapshot],
    ):
        """Lazily bind a custom composer group without delaying dictation.

        This path is used only for segments that the application tracker has
        already recorded after successful output delivery.  It still proves
        the same target, exact whole-field suffix and one fingerprint state per
        Aria paste before making the group addressable.
        """

        from .selection_transaction import (
            SelectionCaptureResult,
            SelectionTransactionStatus,
        )

        segments = tuple(str(item or "") for item in tuple(inserted_segments or ()))
        if (
            not segments
            or any(not item for item in segments)
            or expected_target is None
            or not expected_target.is_valid
        ):
            return SelectionCaptureResult(
                False, SelectionTransactionStatus.INVALID_ARGUMENT
            )
        if expected_target.profile.kind not in {
            SurfaceKind.CUSTOM,
            SurfaceKind.ELECTRON,
        }:
            return SelectionCaptureResult(
                False, SelectionTransactionStatus.UNSUPPORTED_SURFACE
            )
        if not self.is_target_snapshot_current(expected_target):
            return SelectionCaptureResult(
                False, SelectionTransactionStatus.TARGET_CHANGED
            )

        from .generic_text_adapter import (
            GenericTextFieldAdapter,
            WindowsGenericTextBackend,
        )

        try:
            captured = GenericTextFieldAdapter(
                WindowsGenericTextBackend(self)
            ).capture_recent_segments(expected_target, segments)
        except Exception:
            logger.exception("Lazy generic recent-text capture raised")
            return SelectionCaptureResult(
                False, SelectionTransactionStatus.NATIVE_FAILED
            )
        if not captured.success or captured.bookmark is None:
            return SelectionCaptureResult(
                False, self._selection_transaction_status(captured.status)
            )
        return SelectionCaptureResult(
            True,
            SelectionTransactionStatus.READY,
            target=replace(expected_target, adapter_token=captured.bookmark),
            transport="generic_text_field",
        )

    def _record_selection_transaction_delivery(
        self, result, *, operation: str = "selection_process"
    ) -> None:
        status_value = str(getattr(result.status, "value", result.status))
        if status_value == "no_change":
            delivery_status = "selection_no_change"
        elif result.success and status_value == "sent":
            delivery_status = "selection_sent"
        elif result.success:
            delivery_status = "selection_confirmed"
        elif result.partial_possible:
            delivery_status = "selection_partial"
        else:
            delivery_status = f"selection_{status_value}"
        self._last_delivery_metadata = {
            "delivery": {
                "status": delivery_status,
                "operation": str(operation),
                "transport": str(result.transport or "none"),
                "confidence": (
                    "native"
                    if result.transport in {"win32_edit", "word_com"}
                    else "best_effort"
                    if result.transport
                    in {"terminal_tail_replace", "generic_text_undo"}
                    else "manual_only"
                ),
                "addressable_text": result.transport
                in {
                    "win32_edit",
                    "word_com",
                    "terminal_tail_replace",
                    "generic_text_undo",
                },
                "allow_auto_send": False,
                "partial_possible": bool(result.partial_possible),
                "undo_available": bool(result.undo_available),
                "reason": status_value,
            }
        }

    def replace_captured_selection(
        self,
        replacement: str,
        original_text: str,
        expected_target: Optional[TargetSnapshot],
    ):
        """Commit one native selection replacement; never blind-paste fallback."""
        from .selection_transaction import (
            SelectionReplaceResult,
            SelectionTransactionStatus,
        )

        def finish(result):
            self._record_selection_transaction_delivery(result)
            return result

        if (
            not replacement
            or not original_text
            or expected_target is None
            or not expected_target.is_valid
        ):
            return finish(
                SelectionReplaceResult(
                    False, SelectionTransactionStatus.INVALID_ARGUMENT
                )
            )
        if replacement == original_text:
            return finish(
                SelectionReplaceResult(
                    True, SelectionTransactionStatus.NO_CHANGE,
                    transport="none",
                )
            )
        if not self.is_target_snapshot_current(expected_target):
            return finish(
                SelectionReplaceResult(
                    False, SelectionTransactionStatus.TARGET_CHANGED
                )
            )
        if self.config.check_elevation:
            try:
                elevation_blocked = is_target_elevated() and not is_aria_elevated()
            except Exception:
                elevation_blocked = False
            if elevation_blocked:
                return finish(
                    SelectionReplaceResult(
                        False, SelectionTransactionStatus.ELEVATION_REQUIRED
                    )
                )

        if expected_target.profile.kind == SurfaceKind.STANDARD_TEXT:
            from .standard_text_adapter import (
                StandardTextAdapter,
                StandardTextSelectionBookmark,
                Win32StandardTextBackend,
            )

            bookmark = expected_target.adapter_token
            if not isinstance(bookmark, StandardTextSelectionBookmark):
                return finish(
                    SelectionReplaceResult(
                        False, SelectionTransactionStatus.SELECTION_UNAVAILABLE
                    )
                )
            try:
                native_result = StandardTextAdapter(
                    Win32StandardTextBackend(user32),
                    target_is_current=self.is_target_snapshot_current,
                ).replace_captured_selection(
                    expected_target,
                    bookmark,
                    original_text,
                    replacement,
                )
            except Exception:
                logger.exception("Native selection replacement raised")
                return finish(
                    SelectionReplaceResult(
                        False,
                        SelectionTransactionStatus.WRITE_PARTIAL,
                        transport="win32_edit",
                        partial_possible=True,
                        undo_available=True,
                    )
                )
            return finish(
                SelectionReplaceResult(
                    native_result.success,
                    self._selection_transaction_status(native_result.status),
                    transport="win32_edit",
                    partial_possible=bool(native_result.partial_possible),
                    undo_available=bool(native_result.undo_available),
                )
            )

        if (
            expected_target.profile.kind == SurfaceKind.DOCUMENT
            and supports_word_compatible_ranges(expected_target.profile.process_name)
            and getattr(self.config, "word_native_enabled", False)
        ):
            try:
                word_result = self._attempt_word_native_insert(
                    replacement,
                    expected_target,
                    expected_selection_text=original_text,
                )
            except Exception:
                word_result = None
            if word_result is None:
                return finish(
                    SelectionReplaceResult(
                        False, SelectionTransactionStatus.WRITE_PARTIAL,
                        transport="word_com", partial_possible=True,
                        undo_available=True,
                    )
                )
            mapped_status = self._selection_transaction_status(word_result.status)
            return finish(
                SelectionReplaceResult(
                    bool(word_result.success),
                    mapped_status,
                    transport="word_com",
                    partial_possible=bool(word_result.partial_possible),
                    undo_available=bool(
                        word_result.success or word_result.partial_possible
                    ),
                )
            )

        return finish(
            SelectionReplaceResult(
                False, SelectionTransactionStatus.UNSUPPORTED_SURFACE
            )
        )

    def replace_recent_voice_range(
        self,
        replacement: str,
        original_text: str,
        expected_target: Optional[TargetSnapshot],
    ):
        """Rewrite one recent dictated range; never blind-paste a fallback."""

        from .selection_transaction import (
            SelectionReplaceResult,
            SelectionTransactionStatus,
        )

        def finish(result):
            self._record_selection_transaction_delivery(
                result, operation="recent_voice_rewrite"
            )
            return result

        if (
            not replacement
            or not original_text
            or expected_target is None
            or not expected_target.is_valid
        ):
            return finish(
                SelectionReplaceResult(
                    False, SelectionTransactionStatus.INVALID_ARGUMENT
                )
            )
        if replacement == original_text:
            return finish(
                SelectionReplaceResult(
                    True, SelectionTransactionStatus.NO_CHANGE,
                    transport="none",
                )
            )
        if not self.is_target_snapshot_current(expected_target):
            return finish(
                SelectionReplaceResult(
                    False, SelectionTransactionStatus.TARGET_CHANGED
                )
            )
        if self.config.check_elevation:
            try:
                elevation_blocked = is_target_elevated() and not is_aria_elevated()
            except Exception:
                elevation_blocked = False
            if elevation_blocked:
                return finish(
                    SelectionReplaceResult(
                        False, SelectionTransactionStatus.ELEVATION_REQUIRED
                    )
                )

        if expected_target.profile.kind == SurfaceKind.TERMINAL:
            return finish(
                self._replace_terminal_recent_tail(
                    replacement, original_text, expected_target
                )
            )

        if expected_target.profile.kind == SurfaceKind.STANDARD_TEXT:
            from .standard_text_adapter import (
                StandardTextAdapter,
                StandardTextSelectionBookmark,
                Win32StandardTextBackend,
            )

            bookmark = expected_target.adapter_token
            if not isinstance(bookmark, StandardTextSelectionBookmark):
                return finish(
                    SelectionReplaceResult(
                        False, SelectionTransactionStatus.SELECTION_UNAVAILABLE
                    )
                )
            try:
                native = StandardTextAdapter(
                    Win32StandardTextBackend(user32),
                    target_is_current=self.is_target_snapshot_current,
                ).replace_bookmarked_range(
                    expected_target,
                    bookmark,
                    original_text,
                    replacement,
                )
            except Exception:
                logger.exception("Recent standard-text rewrite raised")
                return finish(
                    SelectionReplaceResult(
                        False,
                        SelectionTransactionStatus.WRITE_PARTIAL,
                        transport="win32_edit",
                        partial_possible=True,
                        undo_available=True,
                    )
                )
            return finish(
                SelectionReplaceResult(
                    bool(native.success),
                    self._selection_transaction_status(native.status),
                    transport="win32_edit",
                    partial_possible=bool(native.partial_possible),
                    undo_available=bool(native.undo_available),
                    undo_token=native.undo_token,
                )
            )

        if (
            expected_target.profile.kind == SurfaceKind.DOCUMENT
            and supports_word_compatible_ranges(expected_target.profile.process_name)
            and getattr(self.config, "word_native_enabled", False)
        ):
            try:
                native = self._get_word_adapter().replace_bookmarked_range(
                    replacement,
                    original_text,
                    expected_target,
                    target_is_current=self.is_target_snapshot_current,
                )
            except Exception:
                native = None
            if native is None:
                return finish(
                    SelectionReplaceResult(
                        False,
                        SelectionTransactionStatus.WRITE_PARTIAL,
                        transport="word_com",
                        partial_possible=True,
                        undo_available=True,
                    )
                )
            return finish(
                SelectionReplaceResult(
                    bool(native.success),
                    self._selection_transaction_status(native.status),
                    transport="word_com",
                    partial_possible=bool(native.partial_possible),
                    undo_available=bool(native.success or native.partial_possible),
                )
            )

        if expected_target.profile.kind in {
            SurfaceKind.CUSTOM,
            SurfaceKind.ELECTRON,
        }:
            from .generic_text_adapter import (
                GenericRecentTextBookmark,
                GenericTextFieldAdapter,
                WindowsGenericTextBackend,
                fingerprint_field_text,
            )

            bookmark = expected_target.adapter_token
            if not isinstance(bookmark, GenericRecentTextBookmark):
                return finish(
                    SelectionReplaceResult(
                        False, SelectionTransactionStatus.SELECTION_UNAVAILABLE
                    )
                )
            key = self._generic_target_key(expected_target)
            with self._generic_recent_lock:
                self._generic_pending_capture = None
            try:
                native = GenericTextFieldAdapter(
                    WindowsGenericTextBackend(self)
                ).replace_bookmarked_range(
                    expected_target,
                    bookmark,
                    original_text,
                    replacement,
                )
            except Exception:
                logger.exception("Generic recent-text rewrite raised")
                return finish(
                    SelectionReplaceResult(
                        False,
                        SelectionTransactionStatus.WRITE_PARTIAL,
                        transport="generic_text_undo",
                        partial_possible=True,
                    )
                )
            if native.success and native.replacement_bookmark is not None:
                with self._generic_recent_lock:
                    self._generic_pending_capture = (
                        key,
                        native.replacement_bookmark,
                        fingerprint_field_text(replacement),
                    )
            return finish(
                SelectionReplaceResult(
                    bool(native.success),
                    self._selection_transaction_status(native.status),
                    transport="generic_text_undo",
                    partial_possible=bool(native.partial_possible),
                    # Ctrl+Z now undoes only the replacement paste and would
                    # leave the verified prefix, not restore the old passage.
                    undo_available=False,
                )
            )

        return finish(
            SelectionReplaceResult(
                False, SelectionTransactionStatus.UNSUPPORTED_SURFACE
            )
        )

    @staticmethod
    def _release_vk_key(vk_code: int) -> None:
        key_up = (INPUT * 1)()
        key_up[0].type = INPUT_KEYBOARD
        key_up[0].union.ki.wVk = int(vk_code)
        key_up[0].union.ki.wScan = 0
        key_up[0].union.ki.dwFlags = KEYEVENTF_KEYUP
        key_up[0].union.ki.time = 0
        key_up[0].union.ki.dwExtraInfo = 0
        user32.SendInput(1, key_up, ctypes.sizeof(INPUT))

    @staticmethod
    def _capture_terminal_tail_guard(hwnd: int, expected_text: str):
        from .terminal_adapter import capture_terminal_tail_guard

        return capture_terminal_tail_guard(hwnd, expected_text)

    @staticmethod
    def _verify_terminal_tail_anchor(anchor):
        from .terminal_adapter import verify_terminal_tail_anchor

        return verify_terminal_tail_anchor(anchor)

    def _capture_terminal_tail_guard_with_retry(
        self, hwnd: int, expected_text: str
    ):
        from .terminal_adapter import TerminalTailProbeStatus

        probe = self._capture_terminal_tail_guard(hwnd, expected_text)
        if probe.status == TerminalTailProbeStatus.MATCH:
            return probe
        # Used only immediately after Aria pasted text; retry readback, never
        # the write.  UIA can trail the rendered terminal by one frame.
        for delay in (0.02, 0.05, 0.10):
            time.sleep(delay)
            probe = self._capture_terminal_tail_guard(hwnd, expected_text)
            if probe.status == TerminalTailProbeStatus.MATCH:
                return probe
        return probe

    def _capture_terminal_tail_guard_stable(
        self, hwnd: int, expected_text: str
    ):
        from .terminal_adapter import (
            TerminalTailProbeResult,
            TerminalTailProbeStatus,
        )

        first = self._capture_terminal_tail_guard(hwnd, expected_text)
        if first.status != TerminalTailProbeStatus.MATCH or first.anchor is None:
            return first
        # A second equal sample closes the observed Windows Terminal caret/UIA
        # render lag before the irreversible Backspace batch.
        time.sleep(0.05)
        second = self._capture_terminal_tail_guard(hwnd, expected_text)
        if (
            second.status == TerminalTailProbeStatus.MATCH
            and second.anchor == first.anchor
        ):
            return second
        if second.status == TerminalTailProbeStatus.UNAVAILABLE:
            return second
        return TerminalTailProbeResult(TerminalTailProbeStatus.MISMATCH)

    def _terminal_tail_matches_after_paste(
        self, hwnd: int, expected_text: str, expected_anchor=None
    ) -> bool:
        from .terminal_adapter import TerminalTailProbeStatus

        probe = self._capture_terminal_tail_guard_with_retry(hwnd, expected_text)
        if probe.status != TerminalTailProbeStatus.MATCH or probe.anchor is None:
            return False
        return expected_anchor is None or probe.anchor == expected_anchor

    @staticmethod
    def _terminal_edit_keys_are_clear() -> bool:
        """Fail closed while any physical keyboard key is held."""

        try:
            return all(
                not bool(user32.GetAsyncKeyState(vk) & _KEY_DOWN_BIT)
                for vk in range(VK_BACK, 0xFF)
            )
        except Exception:
            return False

    def _send_terminal_backspaces(
        self, count: int, expected_target: TargetSnapshot
    ) -> tuple[int, bool]:
        """Send bounded Backspace batches, reporting accepted full presses.

        A partial SendInput or target change after the first accepted event is
        an unknown mutation state; callers must not paste or auto-retry it.
        """

        requested = max(0, int(count or 0))
        if requested <= 0 or requested > 2000:
            return 0, False
        completed = 0
        while completed < requested:
            if (
                not self.is_target_snapshot_current(expected_target)
                or not self._terminal_edit_keys_are_clear()
            ):
                return completed, completed > 0
            batch = min(64, requested - completed)
            inputs = (INPUT * (batch * 2))()
            for index in range(batch):
                down = index * 2
                up = down + 1
                inputs[down].type = INPUT_KEYBOARD
                inputs[down].union.ki.wVk = VK_BACK
                inputs[down].union.ki.dwFlags = 0
                inputs[up].type = INPUT_KEYBOARD
                inputs[up].union.ki.wVk = VK_BACK
                inputs[up].union.ki.dwFlags = KEYEVENTF_KEYUP
            sent = int(
                user32.SendInput(batch * 2, inputs, ctypes.sizeof(INPUT)) or 0
            )
            completed += sent // 2
            if sent != batch * 2:
                if sent % 2:
                    try:
                        self._release_vk_key(VK_BACK)
                    except Exception:
                        pass
                return completed, sent > 0 or completed > 0
        if (
            not self.is_target_snapshot_current(expected_target)
            or not self._terminal_edit_keys_are_clear()
        ):
            return completed, True
        return completed, False

    def _replace_terminal_recent_tail(
        self,
        replacement: str,
        original_text: str,
        expected_target: TargetSnapshot,
    ):
        """Best-effort direct rewrite of Aria's latest terminal input tail."""

        from .selection_transaction import (
            SelectionReplaceResult,
            SelectionTransactionStatus,
        )
        from .terminal_adapter import (
            TerminalRecentTextBookmark,
            TerminalTailProbeStatus,
            terminal_backspace_units,
        )

        bookmark = expected_target.adapter_token
        if not isinstance(bookmark, TerminalRecentTextBookmark):
            return SelectionReplaceResult(
                False, SelectionTransactionStatus.SELECTION_UNAVAILABLE
            )
        if (
            int(bookmark.hwnd) != int(expected_target.hwnd)
            or int(bookmark.pid) != int(expected_target.pid)
            or int(bookmark.focused_hwnd) != int(expected_target.focused_hwnd)
        ):
            return SelectionReplaceResult(
                False, SelectionTransactionStatus.TARGET_CHANGED
            )

        source_units = terminal_backspace_units(original_text)
        replacement_units = terminal_backspace_units(replacement)
        span_units = int(bookmark.end) - int(bookmark.start)
        if source_units is None or source_units != span_units:
            return SelectionReplaceResult(
                False, SelectionTransactionStatus.CONTENT_CHANGED
            )
        if replacement_units is None or replacement_units > 2000:
            return SelectionReplaceResult(
                False, SelectionTransactionStatus.UNSUPPORTED_SURFACE
            )
        try:
            plan = self._plan_terminal_delivery(replacement)
        except Exception:
            return SelectionReplaceResult(
                False, SelectionTransactionStatus.UNSUPPORTED_SURFACE
            )
        if (
            plan.requires_manual
            or plan.flattened_newlines
            or len(plan.chunks) != 1
            or plan.chunks[0] != replacement
            or plan.profile.paste_chord != bookmark.paste_chord
        ):
            return SelectionReplaceResult(
                False, SelectionTransactionStatus.UNSUPPORTED_SURFACE
            )

        key = self._terminal_target_key(expected_target)
        with self._terminal_recent_lock:
            if int(self._terminal_recent_offsets.get(key, -1)) != int(bookmark.end):
                return SelectionReplaceResult(
                    False, SelectionTransactionStatus.CONTENT_CHANGED
                )
            self._terminal_pending_capture = None

        if not self._terminal_edit_keys_are_clear():
            return SelectionReplaceResult(
                False, SelectionTransactionStatus.CONTENT_CHANGED
            )
        if (
            not bool(bookmark.uia_verified)
            or not str(bookmark.anchor_sha256 or "")
        ):
            return SelectionReplaceResult(
                False, SelectionTransactionStatus.UNSUPPORTED_SURFACE
            )
        tail_probe = self._capture_terminal_tail_guard_stable(
            expected_target.hwnd, original_text
        )
        if tail_probe.status == TerminalTailProbeStatus.UNAVAILABLE:
            return SelectionReplaceResult(
                False, SelectionTransactionStatus.UNSUPPORTED_SURFACE
            )
        if (
            tail_probe.status != TerminalTailProbeStatus.MATCH
            or tail_probe.anchor is None
        ):
            return SelectionReplaceResult(
                False, SelectionTransactionStatus.CONTENT_CHANGED
            )
        if (
            int(tail_probe.anchor.chars) != int(bookmark.anchor_chars)
            or str(tail_probe.anchor.sha256) != str(bookmark.anchor_sha256)
        ):
            return SelectionReplaceResult(
                False, SelectionTransactionStatus.CONTENT_CHANGED
            )

        deleted, partial = self._send_terminal_backspaces(
            source_units, expected_target
        )
        if partial or deleted != source_units:
            with self._terminal_recent_lock:
                self._terminal_recent_offsets.pop(key, None)
                self._terminal_pending_capture = None
            return SelectionReplaceResult(
                False,
                SelectionTransactionStatus.WRITE_PARTIAL,
                transport="terminal_tail_replace",
                partial_possible=True,
            )

        anchor_status = self._verify_terminal_tail_anchor(tail_probe.anchor)
        if (
            anchor_status != TerminalTailProbeStatus.MATCH
            or not self.is_target_snapshot_current(expected_target)
            or not self._terminal_edit_keys_are_clear()
        ):
            with self._terminal_recent_lock:
                self._terminal_recent_offsets.pop(key, None)
                self._terminal_pending_capture = None
            return SelectionReplaceResult(
                False,
                SelectionTransactionStatus.WRITE_PARTIAL,
                transport="terminal_tail_replace",
                partial_possible=True,
            )

        self._last_delivery_metadata = {
            "delivery": {
                "status": "terminal_rewrite_paste_pending",
                "surface": SurfaceKind.TERMINAL.value,
                "transport": "clipboard",
                "partial_possible": False,
            }
        }
        paste_ok = self._insert_text_clipboard(
            replacement,
            expected_target=expected_target,
            paste_chord=bookmark.paste_chord.value,
            allow_typewriter_fallback=False,
        )
        if not paste_ok:
            low_level = self._last_delivery_metadata.get("delivery", {})
            paste_partial = bool(low_level.get("partial_possible"))
            rollback_ok = False
            if (
                not paste_partial
                and self.is_target_snapshot_current(expected_target)
                and self._terminal_edit_keys_are_clear()
                and self._verify_terminal_tail_anchor(tail_probe.anchor)
                == TerminalTailProbeStatus.MATCH
            ):
                rollback_ok = self._insert_text_clipboard(
                    original_text,
                    expected_target=expected_target,
                    paste_chord=bookmark.paste_chord.value,
                    allow_typewriter_fallback=False,
                )
                if rollback_ok:
                    rollback_ok = self._terminal_tail_matches_after_paste(
                        expected_target.hwnd,
                        original_text,
                        tail_probe.anchor,
                    )
            with self._terminal_recent_lock:
                self._terminal_pending_capture = None
                if rollback_ok:
                    self._terminal_recent_offsets[key] = int(bookmark.end)
                else:
                    self._terminal_recent_offsets.pop(key, None)
            return SelectionReplaceResult(
                False,
                (
                    SelectionTransactionStatus.WRITE_REJECTED
                    if rollback_ok
                    else SelectionTransactionStatus.WRITE_PARTIAL
                ),
                transport="terminal_tail_replace",
                partial_possible=not rollback_ok,
            )

        if not self._terminal_tail_matches_after_paste(
            expected_target.hwnd,
            replacement,
            tail_probe.anchor,
        ):
            with self._terminal_recent_lock:
                self._terminal_recent_offsets.pop(key, None)
                self._terminal_pending_capture = None
            return SelectionReplaceResult(
                False,
                SelectionTransactionStatus.WRITE_PARTIAL,
                transport="terminal_tail_replace",
                partial_possible=True,
            )

        replacement_bookmark = replace(
            bookmark,
            start=int(bookmark.start),
            end=int(bookmark.start) + replacement_units,
        )
        with self._terminal_recent_lock:
            self._terminal_recent_offsets[key] = int(replacement_bookmark.end)
            self._terminal_pending_capture = (key, replacement_bookmark)
        return SelectionReplaceResult(
            True,
            SelectionTransactionStatus.SENT,
            transport="terminal_tail_replace",
            partial_possible=False,
            undo_available=False,
        )

    def _notify_game_delivery_issue(self, message: str) -> None:
        callback = getattr(self.config, "paste_abort_callback", None)
        if not callable(callback):
            return
        try:
            callback(message)
        except Exception:
            logger.debug("Game delivery notice callback failed", exc_info=True)

    def _insert_game_chat(
        self,
        text: str,
        expected_target: Optional[TargetSnapshot],
    ) -> bool:
        """Execute one explicit profile-bound game chat transaction.

        The only side effects are ordinary SendInput/clipboard/typewriter
        operations already owned by OutputInjector.  No process memory,
        hooks, drivers, scan-code spoofing or anti-cheat workarounds exist in
        this path.
        """
        from .game_chat_adapter import (
            GameChatAdapter,
            GameChatProfile,
            GameChatTransport,
        )

        process_name = getattr(
            getattr(expected_target, "profile", None), "process_name", ""
        )
        if not process_name:
            try:
                process_name = self.inspect_target_surface().process_name
            except Exception:
                process_name = ""
        profile = self._resolve_game_chat_profile(process_name)
        if profile is None:
            self._record_game_delivery(
                "game_profile_unavailable", reason="game_profile_unavailable"
            )
            return False

        try:
            plan = GameChatAdapter(profile).plan(text)
        except Exception:
            self._record_game_delivery(
                "game_plan_failed", reason="game_plan_failed"
            )
            logger.exception("Game chat planning failed; transaction aborted")
            return False
        if plan.requires_manual:
            self._record_game_delivery(
                "game_manual_required",
                reason=plan.reason_code,
                auto_submit_configured=profile.auto_submit,
            )
            return False

        transaction_target = expected_target
        if transaction_target is None:
            try:
                transaction_target = self.capture_target_snapshot()
            except Exception:
                transaction_target = None
        if (
            transaction_target is None
            or not transaction_target.is_valid
            or transaction_target.profile.kind != SurfaceKind.GAME
        ):
            self._record_game_delivery(
                "game_target_unavailable", reason="game_target_unavailable"
            )
            return False
        if not isinstance(transaction_target.adapter_token, GameChatProfile):
            self._record_game_delivery(
                "game_profile_unavailable", reason="game_profile_token_missing"
            )
            return False
        if transaction_target.adapter_token != profile:
            self._record_game_delivery(
                "game_profile_changed", reason="game_profile_changed"
            )
            return False

        try:
            start_target = self.capture_target_snapshot()
        except Exception:
            start_target = None
        if (
            start_target is None
            or not start_target.is_valid
            or not transaction_target.matches(start_target)
            or start_target.profile.kind != SurfaceKind.GAME
        ):
            self._record_game_delivery(
                "game_target_changed", reason="game_target_changed"
            )
            return False
        if start_target.adapter_token != profile:
            self._record_game_delivery(
                "game_profile_changed", reason="game_profile_changed"
            )
            return False

        open_attempted = profile.open_chat_key is not None
        post_open_target = start_target
        if profile.open_chat_key is not None:
            if not self.send_key(
                profile.open_chat_key, expected_target=start_target
            ):
                self._record_game_delivery(
                    "game_open_failed",
                    reason="game_open_key_unconfirmed",
                    open_chat_attempted=True,
                    auto_submit_configured=profile.auto_submit,
                )
                self._notify_game_delivery_issue(
                    "游戏聊天键发送未确认，文字未输入，请重新打开聊天框后再试"
                )
                return False
            if profile.open_delay_ms:
                time.sleep(profile.open_delay_ms / 1000)
            try:
                post_open_target = self.capture_target_snapshot()
            except Exception:
                post_open_target = None
            if (
                post_open_target is None
                or not post_open_target.is_valid
                or post_open_target.hwnd != start_target.hwnd
                or post_open_target.pid != start_target.pid
                or post_open_target.profile.kind != SurfaceKind.GAME
            ):
                self._record_game_delivery(
                    "game_target_changed",
                    reason="game_target_changed_after_open",
                    open_chat_attempted=True,
                    auto_submit_configured=profile.auto_submit,
                )
                return False
            if post_open_target.adapter_token != profile:
                self._record_game_delivery(
                    "game_profile_changed",
                    reason="game_profile_changed_after_open",
                    open_chat_attempted=True,
                    auto_submit_configured=profile.auto_submit,
                )
                return False
            if not profile.allow_same_focus_after_open and (
                start_target.focused_hwnd <= 0
                or post_open_target.focused_hwnd <= 0
                or post_open_target.focused_hwnd == start_target.focused_hwnd
            ):
                self._record_game_delivery(
                    "game_chat_unverified",
                    reason="game_focus_did_not_change_after_open",
                    open_chat_attempted=True,
                    auto_submit_configured=profile.auto_submit,
                )
                self._notify_game_delivery_issue(
                    "无法确认游戏聊天框已打开，文字未输入，请手动打开后再试"
                )
                return False

        transport = profile.transport.value
        # Fresh current-call baseline prevents a previous partial transaction
        # from contaminating this failure classification if a low-level
        # transport returns False without writing its own report.
        self._record_game_delivery(
            "failed",
            transport=transport,
            reason="game_text_pending",
            open_chat_attempted=open_attempted,
            auto_submit_configured=profile.auto_submit,
        )
        if profile.transport == GameChatTransport.CLIPBOARD:
            text_inserted = self._insert_text_clipboard(
                plan.text,
                expected_target=post_open_target,
                allow_typewriter_fallback=False,
            )
        else:
            text_inserted = self._insert_text_typewriter(
                plan.text, expected_target=post_open_target
            )

        if not text_inserted:
            previous = self._last_delivery_metadata.get("delivery", {})
            partial_possible = bool(previous.get("partial_possible"))
            previous_status = str(previous.get("status") or "failed")
            self._record_game_delivery(
                "game_text_partial" if partial_possible else "game_text_failed",
                transport=transport,
                reason=previous_status,
                partial_possible=partial_possible,
                open_chat_attempted=open_attempted,
                auto_submit_configured=profile.auto_submit,
            )
            return False

        submit_status = "disabled"
        partial_possible = False
        if profile.auto_submit and profile.submit_key is not None:
            if profile.submit_delay_ms:
                time.sleep(profile.submit_delay_ms / 1000)
            try:
                submit_target = self.capture_target_snapshot()
            except Exception:
                submit_target = None
            submit_target_valid = bool(
                submit_target is not None
                and submit_target.is_valid
                and post_open_target.matches(submit_target)
                and submit_target.profile.kind == SurfaceKind.GAME
                and submit_target.adapter_token == profile
            )
            if submit_target_valid and self.send_key(
                profile.submit_key, expected_target=submit_target
            ):
                submit_status = "sent"
            else:
                # Text is already staged in the game's chat input. Do not open
                # Draft Box and invite a duplicate; surface the uncertain
                # submit and leave the user one manual key press from recovery.
                submit_status = "unknown"
                partial_possible = True
                self._notify_game_delivery_issue(
                    "游戏文字已输入，但提交键未确认；请检查聊天框后手动发送"
                )

        self._record_game_delivery(
            "sent",
            transport=transport,
            reason=plan.reason_code,
            partial_possible=partial_possible,
            open_chat_attempted=open_attempted,
            auto_submit_configured=profile.auto_submit,
            submit_status=submit_status,
        )
        return True

    def _wait_for_pending_restore(self, timeout: float = 4.0) -> None:
        """Block until the pending deferred restore finished, without
        canceling it. Used when the caller must not take over the backup
        (restore_clipboard disabled mid-session, tests)."""
        state = self._pending_restore
        t = state.thread if state is not None else None
        if t is not None and t.is_alive():
            t.join(timeout)
            if t.is_alive():
                logger.warning(
                    "Pending clipboard restore still running after "
                    f"{timeout:.1f}s — proceeding anyway"
                )

    def _resolve_pending_restore(
        self, cancel: bool = True, timeout: float = 3.0
    ) -> Optional[Dict[int, bytes]]:
        """Settle the previous injection's deferred restore before starting a
        new injection session (PERF-7 ordering guarantee).

        With cancel=True (normal restore-enabled sessions) the pending worker
        is asked to skip its restore instead of being waited out. If it was
        canceled in time AND the clipboard sequence number still matches the
        previous verified set (no external change since), its backup is
        returned and MUST be reused by the caller: the clipboard currently
        holds our own pasted text, so a fresh backup would capture that
        instead of the user's content. In a rapid dictation burst the user's
        clipboard is therefore restored once, after the last utterance, and
        never flashes back mid-burst — and follow-up injections don't block
        on the settle delay at all.

        Returns the carried-forward backup, or None when there is nothing to
        carry (no pending worker / restore already ran / external clipboard
        change detected — a fresh backup then captures the newest content).
        """
        state = self._pending_restore
        self._pending_restore = None
        if state is None or state.thread is None:
            return None
        if cancel:
            state.cancel.set()
        if state.thread.is_alive():
            state.thread.join(timeout)
            if state.thread.is_alive():
                logger.warning(
                    "Pending clipboard restore still running after "
                    f"{timeout:.1f}s — proceeding anyway"
                )
                return None
        if not cancel or state.outcome != "canceled":
            return None
        current_seq = user32.GetClipboardSequenceNumber()
        if (
            state.seq_after_set is not None
            and current_seq == state.seq_after_set
        ):
            return state.backup
        logger.info(
            "Pending restore canceled but clipboard changed externally — "
            "not carrying its backup forward"
        )
        return None

    def flush_pending_restore(self) -> None:
        """Cancel a parked post-paste settle wait and restore the user's
        clipboard immediately. Called at shutdown: the deferred-restore
        worker is a daemon thread, so quitting inside the (up to 3s) settle
        delay would otherwise drop the user's clipboard backup with it.
        No more injections are coming, so restoring right away is safe.
        """
        carried = self._resolve_pending_restore()
        if carried is None:
            return
        if not self._restore_clipboard_all_formats(carried):
            time.sleep(self.RESTORE_RETRY_DELAY_MS / 1000)
            if not self._restore_clipboard_all_formats(carried):
                logger.warning("Shutdown clipboard restore failed")
                return
        logger.info("Deferred clipboard restore flushed at shutdown")

    def _settle_clipboard_before_generic_probe(self) -> bool:
        """Wait for the preceding paste/restore before Ctrl+A/C readback.

        Canceling the restore here would swap the clipboard while a Qt or
        Chromium renderer may still be consuming the paste.  Waiting on the
        already-running worker preserves the user's original clipboard and
        gives the target its configured settle window.  This method never
        starts another write and returns False if the worker did not finish in
        a bounded interval.
        """

        state = self._pending_restore
        if state is None or state.thread is None:
            return True
        try:
            timeout = max(1.0, float(self._restore_settle_delay_s()) + 1.0)
        except Exception:
            timeout = 4.0
        self._resolve_pending_restore(cancel=False, timeout=timeout)
        return not state.thread.is_alive()

    def _restore_settle_delay_s(
        self, debug_log: Optional[Callable[[str], None]] = None
    ) -> float:
        """Settle delay between Ctrl+V and the clipboard restore, in seconds.

        Chromium/Electron hosts (window class Chrome_WidgetWin_1: Cursor,
        VS Code, Slack, browsers) consume the paste lazily via IPC, so they
        get ELECTRON_SETTLE_DELAY_MS by default. A measured per-process map
        may choose a shorter but still buffered delay (currently 1000ms for
        ChatGPT.exe). Native windows keep the
        classic max(restore_delay_ms, 200). A follow-up dictation cancels the
        wait (carry-forward), and the sequence-number guard protects anything
        the user copies meanwhile.
        """
        base_ms = max(self.config.restore_delay_ms, 200)
        process_name = ""
        window_class = ""
        try:
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                buf = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, buf, 64)
                window_class = buf.value
                if window_class == ELECTRON_WINDOW_CLASS:
                    fg_info = get_foreground_window_info()
                    if fg_info.get("hwnd") == hwnd:
                        process_name = fg_info.get("process_name", "").lower()
                if window_class == ELECTRON_WINDOW_CLASS:
                    target_delay_ms = ELECTRON_SETTLE_DELAY_OVERRIDES_MS.get(
                        process_name, self.ELECTRON_SETTLE_DELAY_MS
                    )
                    base_ms = max(base_ms, target_delay_ms)
        except Exception:
            pass
        if debug_log is not None:
            debug_log(
                "Restore settle profile: "
                f"process={process_name or 'unknown'}, "
                f"class={window_class or 'unknown'}, delay={base_ms}ms"
            )
        return base_ms / 1000

    def _paste_chord_down(self) -> bool:
        """True while a supported physical paste chord is held down.

        Only meaningful inside the deferred-restore worker: our own injected
        paste chord (down+up in one SendInput call) is fully released before
        the worker is scheduled, and a follow-up injection cancels the worker
        before sending its own Ctrl+V — so a down-state seen here comes from
        the user's keyboard (covers Ctrl+Shift+V terminal paste too).
        """
        try:
            ctrl_v = bool(
                user32.GetAsyncKeyState(VK_V) & _KEY_DOWN_BIT
            ) and bool(user32.GetAsyncKeyState(VK_CONTROL) & _KEY_DOWN_BIT)
            shift_insert = bool(
                user32.GetAsyncKeyState(VK_INSERT) & _KEY_DOWN_BIT
            ) and bool(user32.GetAsyncKeyState(VK_SHIFT) & _KEY_DOWN_BIT)
            return ctrl_v or shift_insert
        except Exception:
            return False

    def _schedule_clipboard_restore(
        self,
        backup: Dict[int, bytes],
        seq_after_set: Optional[int],
        debug_log: Callable[[str], None],
        injected_text: Optional[str] = None,
    ) -> None:
        """Restore the user's clipboard on a background thread (PERF-7).

        The worker waits out the settle delay (cancelable by the next
        injection, which then carries `backup` forward), then puts the
        pre-injection clipboard state back — including the EMPTY state: an
        empty `backup` means the clipboard held nothing worth keeping before
        the dictation, and the worker clears our pasted sentence instead of
        leaving it behind forever.

        Two escapes shorten/harden the wait:
        - A physical Ctrl+V during the settle window (user about to paste
          their own content) triggers the restore immediately, so they get
          their clipboard back instead of the dictated sentence. Only armed
          when `backup` holds real content — clearing early would make the
          user paste nothing.
        - On a sequence-number change the worker no longer gives up outright:
          if the clipboard still holds exactly our injected text, the change
          was an echo of our own content (IME clipboard history, cross-device
          sync, Electron internals rewriting on paste — observed seq +7/+8
          during Electron settles) and the user's backup is restored anyway.
          Only genuinely new content (a real user copy) skips the restore.

        Clipboard writes stay race-free: _restore_clipboard_all_formats
        acquires _clipboard_lock internally, and _resolve_pending_restore()
        serializes whole injection sessions.
        """
        settle_s = self._restore_settle_delay_s(debug_log)
        state = _PendingRestore(backup, seq_after_set, injected_text)
        # Fast-restore only hands back real user content (image/files/text).
        allow_fast_restore = bool(backup)

        def _restore_worker() -> None:
            # Give the target app time to fully consume the paste before the
            # clipboard content is swapped back. A follow-up injection may
            # cancel this wait and take over the backup.
            start = time.monotonic()
            deadline = start + settle_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if state.cancel.wait(min(self.FAST_RESTORE_POLL_S, remaining)):
                    state.outcome = "canceled"
                    debug_log(
                        "Restore canceled by follow-up injection "
                        "(backup carried forward)"
                    )
                    return
                if (
                    allow_fast_restore
                    and time.monotonic() - start >= self.FAST_RESTORE_GRACE_S
                    and self._paste_chord_down()
                ):
                    debug_log(
                        "Physical Ctrl+V during settle — restoring the "
                        "user's clipboard immediately"
                    )
                    logger.info(
                        "User pressed Ctrl+V during the post-paste settle "
                        "window — restoring their clipboard immediately"
                    )
                    break

            if seq_after_set is not None:
                current_seq = user32.GetClipboardSequenceNumber()
                if current_seq != seq_after_set:
                    # External write during the settle. Echo check: if the
                    # clipboard still reads back as OUR injected text, the
                    # writer merely re-wrote our own content (sync/IME echo)
                    # and skipping would permanently strand the user's
                    # backup — restore over the echo instead.
                    echo = False
                    if state.injected_text is not None:
                        try:
                            echo = (
                                self._get_clipboard_text()
                                == state.injected_text
                            )
                        except Exception:
                            echo = False
                    if not echo:
                        state.outcome = "ran"
                        debug_log(
                            "Restore skipped: clipboard changed externally "
                            f"(seq {seq_after_set} -> {current_seq})"
                        )
                        logger.info(
                            "Clipboard changed externally after paste — "
                            "skipping restore to preserve the new content"
                        )
                        return
                    debug_log(
                        "External clipboard write was an echo of our own "
                        f"text (seq {seq_after_set} -> {current_seq}) — "
                        "restoring anyway"
                    )

            # Late-cancel recheck: a follow-up injection may have set cancel
            # in the instant after wait() returned. Honoring it here (before
            # any clipboard write) keeps the burst invariant — the clipboard
            # still holds our verified text, so the backup stays carriable.
            if state.cancel.is_set():
                state.outcome = "canceled"
                debug_log(
                    "Restore canceled at the last moment by follow-up "
                    "injection (backup carried forward)"
                )
                return
            state.outcome = "ran"

            if backup:
                debug_log(f"Restoring {len(backup)} formats (async)...")
            else:
                debug_log(
                    "Clearing clipboard (async) — pre-dictation state was "
                    "empty / our own previous injection"
                )
            restore_success = self._restore_clipboard_all_formats(backup)
            if not restore_success:
                # Transient contention — a failure here permanently loses
                # the user's content, so retry twice with growing delays.
                for retry_delay_ms in (
                    self.RESTORE_RETRY_DELAY_MS,
                    self.RESTORE_RETRY_DELAY_2_MS,
                ):
                    debug_log("Restore failed, retrying...")
                    time.sleep(retry_delay_ms / 1000)
                    restore_success = self._restore_clipboard_all_formats(
                        backup
                    )
                    if restore_success:
                        break
            if restore_success:
                debug_log("RESTORED successfully")
                logger.info("Clipboard restored successfully (all formats)")
            else:
                debug_log("RESTORE FAILED")
                logger.warning("Failed to restore original clipboard")

        t = threading.Thread(
            target=_restore_worker, name="clipboard-restore", daemon=True
        )
        state.thread = t
        self._pending_restore = state
        t.start()

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

    def _verify_clipboard_text(self, expected: str) -> bool:
        """Read the clipboard back and confirm it holds exactly `expected`.

        Guards the window between SetClipboardData and Ctrl+V: if the set
        silently failed or another process (clipboard manager, sync tool)
        replaced our text, pasting now would inject stale content — e.g. a
        previously copied image — into the target app.
        """
        actual = self._get_clipboard_text()
        if actual == expected:
            return True
        if actual is None:
            logger.warning("Clipboard verify failed: could not read clipboard back")
        else:
            logger.warning(
                "Clipboard verify failed: content mismatch "
                f"(expected {len(expected)} chars, got {len(actual)} chars)"
            )
        return False

    def _backup_is_own_injection(self, backup: Optional[Dict[int, bytes]]) -> bool:
        """True when the backed-up clipboard content is one of our own
        injections (self-echo) rather than genuine user content.

        After a paste whose restore never ran (empty pre-dictation clipboard,
        skipped/failed restore, app restart mid-session), the clipboard keeps
        holding the previous dictation. The next injection would back that
        text up as "user content" and restore it after the settle delay — and
        a slow Electron target that reads the clipboard late then pastes the
        PREVIOUS sentence instead of the current one (observed 2026-07-20
        06:06, first injection after a package swap). Such a backup carries
        nothing of the user's worth protecting, so the caller replaces it
        with an empty snapshot: after the settle delay the clipboard is
        CLEARED instead of re-armed with the stale sentence — and the
        dictated text does not linger on the user's clipboard either.

        Two detection channels:
        1. Marker format — the injection path stamps _ARIA_MARKER_FORMAT
           next to every injected text; any other app's copy wipes it
           (EmptyClipboard). Works across processes, restarts and package
           upgrades (the observed failure was the FIRST injection of a fresh
           process, where an in-process history is necessarily empty). When
           the marker system is available, it is AUTHORITATIVE both ways:
           marker present = ours, marker absent = someone else deliberately
           placed this content. Text matching a recent injection without the
           marker is the user (or a history-popup copy) re-copying our
           sentence to keep it — that copy must be restored like any other
           user content, not treated as self-echo (2026-07-22 field report:
           treating it as ours silently destroyed deliberate copies).
        2. In-process history — fallback ONLY when the marker format could
           not be registered on this system.

        Any non-text format (image, files) vetoes both channels: that is
        real user content even if a marker somehow sits next to it.
        """
        if not backup:
            return False
        text_like = {CF_UNICODETEXT, CF_TEXT}
        if _ARIA_MARKER_FORMAT:
            text_like.add(_ARIA_MARKER_FORMAT)
        if not set(backup.keys()) <= text_like:
            return False
        if _ARIA_MARKER_FORMAT:
            return _ARIA_MARKER_FORMAT in backup
        raw = backup.get(CF_UNICODETEXT)
        if raw is None:
            return False
        try:
            # Clipboard text is null-terminated; ignore allocation slack.
            text = raw.decode("utf-16-le", errors="ignore").split("\x00", 1)[0]
        except Exception:
            return False
        return bool(text) and text in self._recent_injections

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
        if _ARIA_MARKER_FORMAT:
            # Our own injection marker: carried through backup/restore so the
            # self-echo knowledge survives an abort's synchronous write-back.
            SAFE_FORMATS.append(_ARIA_MARKER_FORMAT)
        # Max size per format — see MAX_CLIPBOARD_FORMAT_BYTES.
        MAX_FORMAT_SIZE = self.MAX_CLIPBOARD_FORMAT_BYTES

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

    def _clear_clipboard(self) -> bool:
        """Empty the clipboard — restores the 'clipboard was empty' state.

        Used when the pre-injection snapshot was empty (or held our own
        previous injection): faithfully returning to that state means
        removing the dictated sentence we pasted, not leaving it behind.
        """
        lock_acquired = False
        try:
            if self._clipboard_lock:
                self._clipboard_lock.acquire()
                lock_acquired = True
            if not self._open_clipboard_with_retry():
                return False
            try:
                user32.EmptyClipboard()
                return True
            finally:
                user32.CloseClipboard()
        except Exception as e:
            logger.error(f"Failed to clear clipboard: {e}")
            return False
        finally:
            if lock_acquired:
                self._clipboard_lock.release()

    def _restore_clipboard_all_formats(self, backup: Dict[int, bytes]) -> bool:
        """
        Restore all clipboard formats from backup.

        Args:
            backup: Dict mapping format_id -> raw bytes. An EMPTY dict is an
                explicit snapshot of an empty clipboard and CLEARS the
                clipboard; None (no snapshot) is a no-op.

        Returns:
            True if restore succeeded, False otherwise.
        """
        if backup is None:
            logger.debug("No backup snapshot to restore")
            return True
        if not backup:
            logger.debug("Restoring empty-clipboard state (clearing)")
            return self._clear_clipboard()

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

    def _stamp_injection_marker(self) -> None:
        """Add _ARIA_MARKER_FORMAT (1-byte payload) to the OPEN clipboard.

        Must be called between OpenClipboard and CloseClipboard, after the
        text was set. A copy by any other app runs EmptyClipboard first,
        wiping the marker — so its presence proves the content is ours.
        Failure is non-fatal: the in-process history still covers the
        common single-process case.
        """
        if not _ARIA_MARKER_FORMAT:
            return
        marker_handle = None
        try:
            marker_handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, 1)
            if not marker_handle:
                return
            mptr = kernel32.GlobalLock(marker_handle)
            if mptr:
                try:
                    ctypes.memset(mptr, 1, 1)
                finally:
                    kernel32.GlobalUnlock(marker_handle)
            if user32.SetClipboardData(_ARIA_MARKER_FORMAT, marker_handle):
                marker_handle = None  # clipboard owns it now
        except Exception:
            logger.debug("Injection marker set failed", exc_info=True)
        finally:
            if marker_handle:
                try:
                    kernel32.GlobalFree(marker_handle)
                except Exception:
                    pass

    def _set_clipboard_text(
        self, text: str, mark_as_injection: bool = False
    ) -> Tuple[bool, Optional[int]]:
        """
        Set clipboard text content.

        Args:
            text: Text to place on the clipboard.
            mark_as_injection: Stamp _ARIA_MARKER_FORMAT next to the text.
                ONLY the actual dictation-injection path (and the selection
                sentinel probe, whose leftover is garbage worth discarding)
                may set this. Restore paths that write USER content back
                (SelectionDetector.restore_clipboard & friends) must leave it
                False: stamping restored user content made the self-echo
                guard treat it as Aria's own injection and permanently stop
                restoring the user's clipboard (2026-07-22 field report —
                every post-open_path dictation logged "self-echo … restore
                disabled").

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

                # Stamp our private marker next to the text so ANY Aria
                # process (including a future one after restart/upgrade) can
                # recognize this clipboard content as its own injection.
                # Restore-type writes skip this — see docstring.
                if mark_as_injection:
                    self._stamp_injection_marker()

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

    def _send_paste(self, paste_chord: str = "ctrl_v") -> bool:
        """
        Send a supported paste chord using one SendInput batch.

        Returns:
            True if all 4 inputs were sent successfully, False otherwise.
        """
        if paste_chord == "shift_insert":
            modifier_vk = VK_SHIFT
            key_vk = VK_INSERT
            key_flag = KEYEVENTF_EXTENDEDKEY
        else:
            modifier_vk = VK_CONTROL
            key_vk = VK_V
            key_flag = 0
        inputs = (INPUT * 4)()

        # Initialize all inputs
        for i in range(4):
            inputs[i].type = INPUT_KEYBOARD
            inputs[i].union.ki.wScan = 0
            inputs[i].union.ki.time = 0
            inputs[i].union.ki.dwExtraInfo = 0

        # Modifier down
        inputs[0].union.ki.wVk = modifier_vk
        inputs[0].union.ki.dwFlags = 0

        # Paste key down
        inputs[1].union.ki.wVk = key_vk
        inputs[1].union.ki.dwFlags = key_flag

        # Paste key up
        inputs[2].union.ki.wVk = key_vk
        inputs[2].union.ki.dwFlags = key_flag | KEYEVENTF_KEYUP

        # Modifier up
        inputs[3].union.ki.wVk = modifier_vk
        inputs[3].union.ki.dwFlags = KEYEVENTF_KEYUP

        self._last_paste_partial_possible = False
        result = user32.SendInput(4, inputs, ctypes.sizeof(INPUT))
        if result != 4:
            self._last_paste_partial_possible = bool(result)
            logger.warning(
                f"SendInput returned {result}/4, error={ctypes.get_last_error()}"
            )
            # Cleanup stuck keys to prevent modifier state carrying over
            # Order: [0]=Ctrl down, [1]=V down, [2]=V up, [3]=Ctrl up
            self._cleanup_stuck_keys(
                result,
                modifier_vk=modifier_vk,
                key_vk=key_vk,
                key_extended=bool(key_flag),
            )
            return False
        return True

    def _cleanup_stuck_keys(
        self,
        sent_count: int,
        *,
        modifier_vk: int = VK_CONTROL,
        key_vk: int = VK_V,
        key_extended: bool = False,
    ) -> None:
        """
        Send key-up events for any keys that might be stuck after partial SendInput.
        (partial SendInput can leave modifier keys down)

        Args:
            sent_count: Number of events that were actually sent (0-3 for _send_paste)
        """
        cleanup_keys = []

        # _send_paste order: modifier down, key down, key up, modifier up.
        if sent_count >= 1:
            cleanup_keys.append((modifier_vk, False))
        if sent_count >= 2 and sent_count < 3:
            cleanup_keys.insert(0, (key_vk, key_extended))

        if not cleanup_keys:
            return

        # Send key-up events for stuck keys
        cleanup_inputs = (INPUT * len(cleanup_keys))()
        for i, (vk, extended) in enumerate(cleanup_keys):
            cleanup_inputs[i].type = INPUT_KEYBOARD
            cleanup_inputs[i].union.ki.wVk = vk
            cleanup_inputs[i].union.ki.wScan = 0
            cleanup_inputs[i].union.ki.dwFlags = KEYEVENTF_KEYUP | (
                KEYEVENTF_EXTENDEDKEY if extended else 0
            )
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

    def _insert_text_typewriter(
        self, text: str, expected_target: Optional[TargetSnapshot] = None
    ) -> bool:
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

        # Normalize newlines to \n.
        # - EM_REPLACESEL path: \n → \r\n in the char loop (safe for Edit/RichEdit)
        # - SendInput path: \n skipped (can't be safely sent as keypress)
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        method = "EM_REPLACESEL" if use_em_replacesel else "SendInput UNICODE"
        logger.info(
            f"Typewriter mode: sending {len(text)} chars via {method} "
            f"to hwnd={target_hwnd:#x} (fg={initial_hwnd:#x})"
        )

        for char in text:
            # A guarded ASR commit checks the lightweight HWND/PID/focused
            # control identity before *every* character.  A typewriter send is
            # not atomic; checking only at the start could leak the remaining
            # suffix into a window the user focused mid-delivery.
            if expected_target is not None:
                if not self._guard_target_before_transport(
                    expected_target,
                    partial_possible=chars_sent > 0,
                    transport="typewriter",
                ):
                    logger.warning(
                        f"Typewriter target changed after {chars_sent} chars; aborting"
                    )
                    return False
            # Unguarded/manual callers retain the legacy cheap top-level check.
            elif chars_sent % 5 == 0:
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
                # LPARAM is c_longlong on 64-bit — not a pointer type — so
                # ctypes.cast(buf, LPARAM) raises TypeError. Pass the buffer
                # address as an int; ctypes converts it via the LPARAM argtype.
                user32.SendMessageW(
                    target_hwnd,
                    EM_REPLACESEL,
                    wintypes.WPARAM(1),
                    ctypes.addressof(text_buf),
                )
            else:
                # SendInput UNICODE: for custom controls, remote desktop, etc.
                # Skip newlines — they can't be safely sent via SendInput
                # (VK_RETURN triggers "send" in chat apps, form submit on websites).
                # Structured text with \n is handled by the clipboard auto-switch
                # in insert_text() before reaching this method.
                if char == "\n":
                    chars_sent += 1
                    continue

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

    def _typewriter_newline_safe(self, text: str) -> Tuple[bool, Optional[str]]:
        """
        Check whether typewriter injection would silently drop newlines for
        the current focus target.

        Safe when the text is single-line, or when the focused control is a
        standard Edit/RichEdit (EM_REPLACESEL inserts \\n as \\r\\n). Unsafe
        otherwise: the SendInput UNICODE path skips \\n char-by-char, so
        multi-line text would be flattened. Shared by insert_text() routing
        and the backup-failure fallback in _insert_text_clipboard().

        Does not log — callers decide the log level/wording for their path.

        Returns:
            (safe, target_class):
            - (True, None): no newline risk
            - (False, class_name): unsafe, focused control class name
            - (False, None): unsafe, class detection failed (conservative)
        """
        if "\n" not in text:
            return True, None
        try:
            hwnd = user32.GetForegroundWindow()
            tid = user32.GetWindowThreadProcessId(hwnd, None)
            gti = GUITHREADINFO()
            gti.cbSize = ctypes.sizeof(GUITHREADINFO)
            focus_hwnd = hwnd
            if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)) and gti.hwndFocus:
                focus_hwnd = gti.hwndFocus
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(focus_hwnd, cls_buf, 256)
            if cls_buf.value.lower() in _EM_REPLACESEL_CLASSES:
                return True, None
            return False, cls_buf.value
        except Exception:
            return False, None

    def _insert_text_clipboard(
        self,
        text: str,
        expected_target: Optional[TargetSnapshot] = None,
        paste_chord: str = "ctrl_v",
        allow_typewriter_fallback: bool = True,
    ) -> bool:
        """
        Insert text using clipboard plus an explicit supported paste chord.

        Returns:
            True if paste was sent successfully, False if clipboard or SendInput failed.
        """

        # Debug: Write directly to pipeline log for visibility
        def _debug_log(msg: str):
            import datetime
            from pathlib import Path

            from ..core.debug import append_log_line

            log_path = Path(__file__).parent.parent / "DebugLog" / "pipeline_debug.log"
            ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            append_log_line(log_path, f"[{ts}] [CLIPBOARD] {msg}")

        _debug_log(f"restore_clipboard={self.config.restore_clipboard}")

        # PERF-7: settle the previous injection's deferred restore first.
        # If it hasn't run yet it is canceled and its backup carried forward
        # (the clipboard still holds OUR previous text, so a fresh backup
        # would capture that instead of the user's content). When restore is
        # disabled we must not take over a backup — just wait it out.
        carried_backup: Optional[Dict[int, bytes]] = None
        if self.config.restore_clipboard:
            carried_backup = self._resolve_pending_restore()
        else:
            self._wait_for_pending_restore()

        # Backup ALL clipboard formats (text, images, files, etc.)
        # Fix: Previously only backed up text, causing image/file loss
        clipboard_backup: Optional[Dict[int, bytes]] = None
        if self.config.restore_clipboard and carried_backup is not None:
            clipboard_backup = carried_backup
            _debug_log(
                f"Carrying over backup from canceled pending restore: "
                f"{len(clipboard_backup)} formats, "
                f"keys={list(clipboard_backup.keys())}"
            )
        elif self.config.restore_clipboard:
            clipboard_backup = self._backup_clipboard_all_formats()
            if clipboard_backup is None:
                # Transient contention (clipboard managers, RDP) — retry once
                # before giving up on the user's clipboard content.
                _debug_log("Backup FAILED (returned None), retrying once...")
                time.sleep(self.BACKUP_RETRY_DELAY_MS / 1000)
                clipboard_backup = self._backup_clipboard_all_formats()
            if clipboard_backup is None:
                _debug_log("Backup FAILED after retry")
                # Backup failed twice: overwriting the clipboard now would
                # permanently lose the user's content. If the target window
                # supports typewriter injection AND typewriter won't drop
                # newlines (single-line text, or Edit/RichEdit target), use
                # it and leave the clipboard untouched. Otherwise (terminals,
                # multi-line text into non-Edit controls) proceed with the
                # clipboard path and a warning.
                # Known accepted race: the focus target may change during the
                # 50ms retry window (user Alt+Tab); we re-detect here and
                # accept that tiny window.
                if (
                    allow_typewriter_fallback
                    and self.detect_output_mode() == "typewriter"
                    and self._typewriter_newline_safe(text)[0]
                ):
                    _debug_log("Switching to typewriter to protect clipboard")
                    logger.warning(
                        "Clipboard backup failed twice — switching to "
                        "typewriter injection to avoid losing user clipboard"
                    )
                    if expected_target is None:
                        return self._insert_text_typewriter(text)
                    return self._insert_text_typewriter(
                        text, expected_target=expected_target
                    )
                logger.warning(
                    "Clipboard backup failed twice and target requires "
                    "clipboard paste — proceeding without restore, user "
                    "clipboard content will be lost"
                )
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

        # Self-echo guard: when the backed-up "user content" is literally one
        # of our own recent injections (typically the previous dictated
        # sentence), never put it BACK on the clipboard after the paste —
        # restoring it re-arms the stale-paste race: a loaded Electron target
        # may read the clipboard after the settle delay and would then paste
        # the PREVIOUS sentence instead of this one (observed 2026-07-20
        # 06:06, settle=1000ms). Instead of keeping the current sentence on
        # the clipboard forever (which reads as "Aria replaced my clipboard
        # with what I said" — 2026-07-26 field report), the restore phase now
        # CLEARS the clipboard after the settle delay: a pathologically late
        # read then pastes nothing (visible, recoverable) rather than the
        # wrong sentence (silent corruption). The abort path below still uses
        # the original backup: without a Ctrl+V there is no race, and putting
        # the clipboard back exactly as found is strictly safer.
        restore_backup = clipboard_backup
        if clipboard_backup and self._backup_is_own_injection(clipboard_backup):
            restore_backup = {}
            _debug_log(
                "Backup is our own previous injection (self-echo) — "
                "will clear the clipboard after the settle delay"
            )
            logger.info(
                "Clipboard held Aria's own previous injection — clearing "
                "after the settle delay instead of restoring it"
            )

        # Set text to clipboard, then VERIFY the clipboard really holds our
        # text before sending Ctrl+V. A failed or externally clobbered set
        # must never paste: the target would receive whatever sits on the
        # clipboard (e.g. a previously copied image) instead of the dictated
        # text. Rather fail loudly than inject stale content.
        set_verified = False
        for attempt in range(1 + self.SET_VERIFY_RETRIES):
            set_success, _ = self._set_clipboard_text(text, mark_as_injection=True)
            if not set_success:
                _debug_log(f"Set clipboard FAILED (attempt {attempt + 1})")
                if attempt < self.SET_VERIFY_RETRIES:
                    time.sleep(self.SET_RETRY_DELAY_MS / 1000)
                continue
            # Small delay to ensure clipboard is ready, then read it back.
            time.sleep(self.config.paste_delay_ms / 1000)
            if self._verify_clipboard_text(text):
                set_verified = True
                break
            _debug_log(f"Set-verify MISMATCH (attempt {attempt + 1})")
        if not set_verified:
            _debug_log("PASTE ABORTED: clipboard never verified as our text")
            logger.error(
                "Clipboard set/verify failed — aborting paste so stale "
                "clipboard content is not injected"
            )
            # Our set attempts may have emptied/overwritten the clipboard;
            # put the user's content (or its empty state) back before
            # reporting failure.
            if clipboard_backup is not None:
                if self._restore_clipboard_all_formats(clipboard_backup):
                    _debug_log("Backup restored after aborted paste")
                else:
                    _debug_log("Backup restore FAILED after aborted paste")
            # Nothing was pasted — surface that to the user (notice + sound)
            # so they re-dictate instead of assuming the text landed.
            if self.config.paste_abort_callback:
                try:
                    self.config.paste_abort_callback(
                        "剪贴板被占用，这句没有粘贴，请重说一次"
                    )
                except Exception:
                    logger.debug("paste_abort_callback failed", exc_info=True)
            return False

        # Re-check at the actual commit edge. Clipboard backup, retry and
        # verification above can take hundreds of milliseconds; a guard only
        # at insert_text() entry still allows Ctrl+V to land in a newly focused
        # window. Nothing has been pasted yet, so fail closed and restore the
        # exact pre-injection clipboard snapshot synchronously.
        if expected_target is not None and not self._guard_target_before_transport(
            expected_target, transport="clipboard"
        ):
            _debug_log("PASTE ABORTED: target changed before Ctrl+V")
            if clipboard_backup is not None:
                if self._restore_clipboard_all_formats(clipboard_backup):
                    _debug_log("Backup restored after target-guard abort")
                else:
                    _debug_log("Backup restore FAILED after target-guard abort")
            return False

        # Remember what we verifiably placed on the clipboard: the next
        # injection uses this to recognize self-echo backups.
        self._recent_injections.append(text)

        # Baseline for the restore guard: the verified content is ours, so
        # any sequence-number change after this point is an external write
        # (user copy, clipboard manager) that the restore must not clobber.
        seq_after_set = user32.GetClipboardSequenceNumber()

        # Send the selected paste chord. Preserve the historical no-argument
        # call shape for Ctrl+V so existing subclasses/mocks remain compatible.
        paste_success = (
            self._send_paste()
            if paste_chord == "ctrl_v"
            else self._send_paste(paste_chord)
        )
        if not paste_success:
            logger.error("Failed to send clipboard paste command")
            delivery = self._last_delivery_metadata.setdefault("delivery", {})
            delivery["partial_possible"] = bool(
                self._last_paste_partial_possible
            )
            # Same guard philosophy as the verify abort: the user must FEEL
            # that the sentence may not have landed, instead of assuming it
            # did. (A partial SendInput may still have pasted — hence
            # "可能".) The deferred restore below stays scheduled, so the
            # clipboard eventually returns to the user's content either way.
            if self.config.paste_abort_callback:
                try:
                    self.config.paste_abort_callback(
                        "粘贴按键发送失败，这句可能没有输入，请检查或重说一次"
                    )
                except Exception:
                    logger.debug("paste_abort_callback failed", exc_info=True)
            # Still try to restore clipboard even on paste failure

        # Restore original clipboard — deferred to a background thread
        # (PERF-7). The paste above already landed in the target app; the
        # settle delay + restore only protect the user's clipboard content
        # and don't need to block the transcription worker. The backup-failure
        # branch above never reaches here with a backup, so it stays
        # synchronous. An EMPTY restore_backup (clipboard was empty / held
        # our own previous injection) is still scheduled: its restore phase
        # clears the pasted sentence off the clipboard after the settle
        # delay, returning the clipboard to its true pre-dictation state.
        if self.config.restore_clipboard and restore_backup is not None:
            self._schedule_clipboard_restore(
                restore_backup, seq_after_set, _debug_log, injected_text=text
            )
        else:
            _debug_log(
                f"Restore skipped: restore_clipboard={self.config.restore_clipboard}, "
                f"backup_missing={restore_backup is None}"
            )

        return paste_success

    def detect_output_mode(self) -> str:
        """
        Predict which output path insert_text() will take for the current
        foreground window.

        Returned before polish starts so the caller can pick streaming-friendly
        typewriter path vs atomic-paste clipboard path. Mirrors the terminal
        detection in insert_text() — but skips the newline-class check because
        that requires the final polished text.

        Returns:
            'typewriter' — can accept incremental chunks
            'clipboard'  — needs full text for atomic paste
        """
        if not self.config.typewriter_mode:
            return "clipboard"
        try:
            if self.inspect_target_surface().force_clipboard:
                return "clipboard"
        except Exception:
            pass
        return "typewriter"

    def is_terminal_target(self) -> bool:
        """Return True iff the foreground window is a terminal emulator.

        Distinct from `detect_output_mode() == 'clipboard'` because that also
        returns 'clipboard' when typewriter_mode is globally disabled (which
        is NOT a terminal). Used by the polish-bypass policy.
        """
        try:
            return self.inspect_target_surface().kind == SurfaceKind.TERMINAL
        except Exception:
            return False

    def insert_text_typewriter_chunk(self, chunk: str) -> bool:
        """
        Insert a chunk of text in typewriter mode. Used by streaming polish to
        append each LLM delta as it arrives.

        Thin wrapper around _insert_text_typewriter so the streaming path has
        a public entry point without re-running terminal/mode detection on
        every chunk (caller already decided mode via detect_output_mode).
        """
        if not chunk:
            return True
        return self._insert_text_typewriter(chunk)

    def _abort_guarded_delivery(
        self,
        expected_target: TargetSnapshot,
        status: str,
        message: str,
        *,
        partial_possible: bool = False,
        transport: str = "none",
    ) -> bool:
        """Record and surface a target-guard failure, including partial sends."""
        surface = expected_target.profile
        reported_status = f"{status}_partial" if partial_possible else status
        self._last_delivery_metadata = {
            "delivery": {
                "status": reported_status,
                "surface": surface.kind.value,
                "transport": transport,
                "confidence": surface.confidence.value,
                "addressable_text": surface.addressable_text,
                "partial_possible": partial_possible,
            }
        }
        logger.warning(message)
        if self.config.paste_abort_callback:
            try:
                if partial_possible:
                    notice = (
                        "焦点已变化，逐字输入已中止；可能已有部分文字进入原窗口，"
                        "请检查历史"
                    )
                else:
                    notice = "目标窗口已变化，这句未自动上屏，已保存在历史中"
                self.config.paste_abort_callback(notice)
            except Exception:
                logger.debug("target-guard callback failed", exc_info=True)
        return False

    def _target_identity_status(self, expected_target: TargetSnapshot) -> str:
        """Return ``current``, ``target_changed`` or ``target_unavailable``.

        This deliberately avoids process-name lookup and UI Automation so it
        is cheap enough for the per-character typewriter guard.
        """
        if not expected_target.is_valid:
            return "target_unavailable"
        try:
            hwnd, pid = get_foreground_window_pid()
            if not hwnd or not pid:
                return "target_unavailable"
            focused_hwnd = int(self._get_focused_control() or 0)
            # The focus lookup itself crosses thread boundaries. Verify that
            # the top-level identity stayed stable across that observation so
            # a fast Alt+Tab cannot produce a mixed old-window/new-control
            # snapshot that accidentally passes the guard.
            hwnd_after, pid_after = get_foreground_window_pid()
        except Exception:
            return "target_unavailable"
        if hwnd_after != hwnd or pid_after != pid:
            return "target_changed"
        if hwnd != expected_target.hwnd or pid != expected_target.pid:
            return "target_changed"
        if expected_target.focused_hwnd > 0:
            if focused_hwnd <= 0:
                return "target_unavailable"
            if focused_hwnd != expected_target.focused_hwnd:
                return "target_changed"
        return "current"

    def is_target_snapshot_current(self, expected_target: TargetSnapshot) -> bool:
        """Content-free guard used before post-insert actions such as Enter."""
        return self._target_identity_status(expected_target) == "current"

    def _guard_target_before_transport(
        self,
        expected_target: TargetSnapshot,
        *,
        partial_possible: bool = False,
        transport: str = "none",
    ) -> bool:
        status = self._target_identity_status(expected_target)
        if status == "current":
            return True
        return self._abort_guarded_delivery(
            expected_target,
            status,
            "Foreground target identity changed at the transport commit edge; "
            "automatic delivery aborted",
            partial_possible=partial_possible,
            transport=transport,
        )

    def insert_text(
        self, text: str, expected_target: Optional[TargetSnapshot] = None
    ) -> bool:
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

        if expected_target is not None:
            if not expected_target.is_valid:
                return self._abort_guarded_delivery(
                    expected_target,
                    "target_unavailable",
                    "No valid foreground target existed at the output commit "
                    "boundary; automatic delivery aborted",
                )
            try:
                current_target = self.capture_target_snapshot()
            except Exception:
                return self._abort_guarded_delivery(
                    expected_target,
                    "target_unavailable",
                    "Could not re-inspect foreground target before output commit; "
                    "automatic delivery aborted",
                )
            target_surface = current_target.profile
            if not current_target.is_valid or (
                expected_target.focused_hwnd > 0
                and current_target.focused_hwnd <= 0
            ):
                return self._abort_guarded_delivery(
                    expected_target,
                    "target_unavailable",
                    "Foreground target identity could not be fully re-inspected; "
                    "automatic delivery aborted",
                )
            if not expected_target.matches(current_target):
                return self._abort_guarded_delivery(
                    expected_target,
                    "target_changed",
                    (
                        "Foreground target changed before output commit; "
                        "automatic delivery aborted"
                    ),
                )
            if expected_target.profile.kind != current_target.profile.kind:
                return self._abort_guarded_delivery(
                    expected_target,
                    "target_changed",
                    "Foreground carrier policy changed before output commit; "
                    "automatic delivery aborted",
                )
        else:
            try:
                target_surface = self.inspect_target_surface()
            except Exception:
                target_surface = classify_target_surface()

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

        # Explicit game profiles own the whole open-chat -> text -> optional
        # submit state machine. They must never fall through to the global
        # clipboard/typewriter switch or global auto-send behavior.
        if target_surface.kind == SurfaceKind.GAME:
            return self._insert_game_chat(text, expected_target)

        # ========================================
        # Layer 1 or 2: Choose input method
        # ========================================
        use_typewriter = self.config.typewriter_mode
        if target_surface.force_clipboard:
            use_typewriter = False

        terminal_plan = None
        terminal_chunks: tuple[str, ...] = ()
        terminal_chord = "ctrl_v"
        terminal_chunk_delay_s = 0.0
        if target_surface.kind == SurfaceKind.TERMINAL:
            try:
                terminal_plan = self._plan_terminal_delivery(text)
            except Exception:
                self._last_delivery_metadata = {
                    "delivery": {
                        "status": "terminal_plan_failed",
                        "surface": target_surface.kind.value,
                        "transport": "none",
                        "confidence": "manual_only",
                        "addressable_text": False,
                        "allow_auto_send": False,
                    }
                }
                logger.exception(
                    "Terminal delivery planning failed; automatic delivery aborted"
                )
                return False
            if terminal_plan is not None and terminal_plan.requires_manual:
                self._last_delivery_metadata = {
                    "delivery": {
                        "status": "terminal_manual_required",
                        "surface": target_surface.kind.value,
                        "transport": "none",
                        "confidence": "manual_only",
                        "addressable_text": False,
                        "terminal_mode": terminal_plan.profile.mode.value,
                        "reason": terminal_plan.reason_code,
                        "allow_auto_send": False,
                    }
                }
                logger.warning("Terminal profile requires manual Draft Box delivery")
                return False
            if terminal_plan is not None:
                terminal_chunks = terminal_plan.chunks
                terminal_chord = terminal_plan.profile.paste_chord.value
                text = "".join(terminal_chunks)
                try:
                    delay_ms = int(
                        getattr(self.config, "terminal_chunk_delay_ms", 120)
                    )
                except (TypeError, ValueError):
                    delay_ms = 120
                terminal_chunk_delay_s = min(5.0, max(0, delay_ms) / 1000)

        # Some carriers force atomic clipboard delivery.  Terminals need it
        # because SendInput can fail silently and newlines are executable;
        # Word needs it until a native Selection/Range adapter owns the edit.
        # This is independent of the global typewriter switch.
        #
        # Note: WordPad/RichEdit no longer here — handled by EM_REPLACESEL path
        # in _insert_text_typewriter() which avoids the white-box issue.
        if target_surface.newline_policy == NewlinePolicy.FLATTEN:
            # A terminal process does not tell us whether the caret is at a
            # shell prompt, Vim insert mode, WSL/tmux, or an AI CLI.  Newlines
            # can execute commands, so automatic delivery stays single-line
            # until an explicit sub-profile proves a safer contract.
            if terminal_plan is None:
                text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
            logger.info("Clipboard forced for terminal surface (newlines stripped)")

        # Auto-switch to clipboard if text has newlines and target is NOT a
        # known text control (Edit/RichEdit). Clipboard paste (Ctrl+V) inserts
        # newlines correctly in all apps without triggering "send" actions.
        if use_typewriter:
            newline_safe, target_class = self._typewriter_newline_safe(text)
            if not newline_safe:
                use_typewriter = False
                if target_class is None:
                    logger.info(
                        "Clipboard forced: text has newlines (class check failed)"
                    )
                else:
                    logger.info(
                        "Clipboard forced: text has newlines, "
                        f"target class '{target_class}' not Edit/RichEdit"
                    )

        word_result = None
        word_fallback_reason = ""
        if (
            target_surface.kind == SurfaceKind.DOCUMENT
            and supports_word_compatible_ranges(target_surface.process_name)
        ):
            word_result = self._attempt_word_native_insert(text, expected_target)
            if word_result is not None and word_result.success:
                self._last_delivery_metadata = {
                    "delivery": {
                        "status": "confirmed" if word_result.verified else "sent",
                        "surface": target_surface.kind.value,
                        "transport": "word_com",
                        "confidence": "native",
                        "addressable_text": True,
                        "track_revisions": bool(word_result.track_revisions),
                    }
                }
                logger.info("Text inserted through native Word range")
                return True
            if word_result is not None and not word_result.safe_to_fallback:
                status_value = getattr(word_result.status, "value", word_result.status)
                if word_result.partial_possible:
                    delivery_status = "word_com_partial"
                elif str(status_value) == "target_changed":
                    delivery_status = "target_changed"
                else:
                    delivery_status = "word_com_failed"
                self._last_delivery_metadata = {
                    "delivery": {
                        "status": delivery_status,
                        "surface": target_surface.kind.value,
                        "transport": "word_com",
                        "confidence": "native",
                        "addressable_text": True,
                        "partial_possible": bool(word_result.partial_possible),
                        "reason": str(status_value),
                        "track_revisions": bool(word_result.track_revisions),
                    }
                }
                logger.warning("Native Word transaction did not complete")
                return False
            if word_result is not None:
                word_fallback_reason = str(
                    getattr(word_result.status, "value", word_result.status)
                )

        # Start with a current-call failure report before touching the target.
        # If an unexpected low-level exception escapes, the history layer sees
        # this call as failed instead of inheriting a stale success report from
        # the previous utterance.
        self._last_delivery_metadata = {
            "delivery": {
                "status": "failed",
                "surface": target_surface.kind.value,
                "transport": "typewriter" if use_typewriter else "clipboard",
                "confidence": target_surface.confidence.value,
                "addressable_text": target_surface.addressable_text,
            }
        }
        if terminal_plan is not None:
            delivery = self._last_delivery_metadata["delivery"]
            delivery["transport"] = (
                "clipboard_chunks"
                if len(terminal_chunks) > 1
                else (
                    "clipboard_shift_insert"
                    if terminal_chord == "shift_insert"
                    else "clipboard"
                )
            )
            delivery["terminal_mode"] = terminal_plan.profile.mode.value
            delivery["paste_chord"] = terminal_chord
            delivery["newlines_flattened"] = bool(
                terminal_plan.flattened_newlines
            )
            delivery["reason"] = terminal_plan.reason_code
        if target_surface.kind == SurfaceKind.TERMINAL:
            # Inserting terminal text never authorizes a following Enter key.
            # This policy survives planner fallback and is consumed by Aria's
            # application-level global auto-send guard.
            self._last_delivery_metadata["delivery"]["allow_auto_send"] = False
        if word_fallback_reason:
            self._last_delivery_metadata["delivery"]["native_fallback_reason"] = (
                word_fallback_reason
            )

        if use_typewriter:
            # Layer 2: Typewriter mode
            if expected_target is None:
                success = self._insert_text_typewriter(text)
            else:
                success = self._insert_text_typewriter(
                    text, expected_target=expected_target
                )
        else:
            # Layer 1: Clipboard mode (default)
            if terminal_plan is not None and terminal_chunks:
                success = True
                chunks_sent = 0
                for index, chunk in enumerate(terminal_chunks):
                    if expected_target is None:
                        if terminal_chord == "ctrl_v":
                            chunk_success = self._insert_text_clipboard(chunk)
                        else:
                            chunk_success = self._insert_text_clipboard(
                                chunk, paste_chord=terminal_chord
                            )
                    elif terminal_chord == "ctrl_v":
                        chunk_success = self._insert_text_clipboard(
                            chunk, expected_target=expected_target
                        )
                    else:
                        chunk_success = self._insert_text_clipboard(
                            chunk,
                            expected_target=expected_target,
                            paste_chord=terminal_chord,
                        )
                    if not chunk_success:
                        success = False
                        break
                    chunks_sent += 1
                    if (
                        index + 1 < len(terminal_chunks)
                        and terminal_chunk_delay_s
                    ):
                        time.sleep(terminal_chunk_delay_s)
                if not success and chunks_sent:
                    delivery = self._last_delivery_metadata["delivery"]
                    delivery["status"] = "terminal_chunk_partial"
                    delivery["partial_possible"] = True
                    delivery["chunks_sent"] = chunks_sent
                    delivery["chunks_total"] = len(terminal_chunks)
            elif expected_target is None:
                success = self._insert_text_clipboard(text)
            else:
                success = self._insert_text_clipboard(
                    text, expected_target=expected_target
                )

        # "sent" means the selected Windows API accepted the send; it is not
        # a claim that an arbitrary custom control applied the text.  Full
        # verification is a later adapter capability.
        if success:
            self._last_delivery_metadata["delivery"]["status"] = "sent"

        if success:
            logger.info("Text inserted successfully")
        else:
            logger.warning("Text insertion may have failed")

        return success

    def _cleanup_partial_key_send(
        self,
        sent_count: int,
        modifier_vks: list[int],
        key_vk: int,
        key_extended: bool,
    ) -> None:
        """Release keys left down by a partially accepted send_key batch."""
        modifier_count = len(modifier_vks)
        total_inputs = 2 + (modifier_count * 2)
        sent = min(total_inputs, max(0, int(sent_count or 0)))

        held_modifiers = [
            vk for index, vk in enumerate(modifier_vks) if sent > index
        ]
        key_down_index = modifier_count
        key_up_index = modifier_count + 1
        key_held = sent > key_down_index and sent <= key_up_index

        for release_index, vk in enumerate(reversed(modifier_vks)):
            event_index = modifier_count + 2 + release_index
            if sent > event_index:
                try:
                    reverse_position = (
                        len(held_modifiers)
                        - 1
                        - held_modifiers[::-1].index(vk)
                    )
                    held_modifiers.pop(reverse_position)
                except ValueError:
                    pass

        cleanup_keys = []
        if key_held:
            cleanup_keys.append((key_vk, key_extended))
        cleanup_keys.extend((vk, False) for vk in reversed(held_modifiers))
        if not cleanup_keys:
            return

        cleanup_inputs = (INPUT * len(cleanup_keys))()
        for index, (vk, extended) in enumerate(cleanup_keys):
            cleanup_inputs[index].type = INPUT_KEYBOARD
            cleanup_inputs[index].union.ki.wVk = vk
            cleanup_inputs[index].union.ki.wScan = 0
            cleanup_inputs[index].union.ki.dwFlags = KEYEVENTF_KEYUP | (
                KEYEVENTF_EXTENDEDKEY if extended else 0
            )
            cleanup_inputs[index].union.ki.time = 0
            cleanup_inputs[index].union.ki.dwExtraInfo = 0
        user32.SendInput(
            len(cleanup_keys), cleanup_inputs, ctypes.sizeof(INPUT)
        )

    def send_key(
        self,
        key: str,
        modifiers: list = None,
        expected_target: Optional[TargetSnapshot] = None,
    ) -> bool:
        """
        Send a single keystroke with optional modifiers.

        Args:
            key: Key name (e.g., 'enter', 'backspace', 'a', 'z')
            modifiers: List of modifier keys (e.g., ['ctrl'], ['ctrl', 'shift'])
            expected_target: Optional focus identity checked immediately before
                the key is sent. Used for destructive post-actions such as the
                auto-send Enter that follows ASR insertion.

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

        # Send all inputs. The check lives here, at the actual SendInput edge,
        # rather than only in the caller before key-array construction.
        if expected_target is not None and not self.is_target_snapshot_current(
            expected_target
        ):
            logger.warning("Key send aborted because target focus changed")
            return False
        result = user32.SendInput(total_inputs, inputs, ctypes.sizeof(INPUT))
        if result != total_inputs:
            logger.warning(
                f"SendInput returned {result}/{total_inputs}, error={ctypes.get_last_error()}"
            )
            self._cleanup_partial_key_send(
                result,
                modifier_vks,
                vk_key,
                bool(key_extended_flag),
            )
            return False

        mod_str = "+".join(modifiers) + "+" if modifiers else ""
        logger.info(f"Key sent: {mod_str}{key}")
        return True


def create_output_injector(config: Optional[OutputConfig] = None) -> OutputInjector:
    """Factory function to create output injector."""
    return OutputInjector(config)
