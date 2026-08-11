"""
Global Hotkey Manager
=====================
System-level hotkey registration using Win32 RegisterHotKey.
"""

import ctypes
from ctypes import wintypes
import queue
import threading
import time
from typing import Any, Callable, Dict, Optional
from dataclasses import dataclass
from enum import IntFlag

from ..core.logging import get_system_logger

logger = get_system_logger()


# Windows API - use WinDLL with use_last_error for proper error handling
user32 = ctypes.WinDLL("user32", use_last_error=True)

# Define proper function signatures for Win32 API
# RegisterHotKey(HWND hWnd, int id, UINT fsModifiers, UINT vk) -> BOOL
user32.RegisterHotKey.argtypes = [
    wintypes.HWND,
    ctypes.c_int,
    wintypes.UINT,
    wintypes.UINT,
]
user32.RegisterHotKey.restype = wintypes.BOOL

# UnregisterHotKey(HWND hWnd, int id) -> BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL

# PeekMessageW(LPMSG lpMsg, HWND hWnd, UINT wMsgFilterMin, UINT wMsgFilterMax, UINT wRemoveMsg) -> BOOL
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
]
user32.PeekMessageW.restype = wintypes.BOOL

# TranslateMessage(const MSG *lpMsg) -> BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL

# DispatchMessageW(const MSG *lpMsg) -> LRESULT
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = wintypes.LPARAM

# GetAsyncKeyState(int vKey) -> SHORT (MSB set = key currently down).
# Used by press+release bindings to poll for key release: RegisterHotKey
# only delivers key-down (WM_HOTKEY), never key-up.
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


# Modifier keys
class Modifiers(IntFlag):
    ALT = 0x0001
    CTRL = 0x0002
    SHIFT = 0x0004
    WIN = 0x0008
    NOREPEAT = 0x4000  # Prevent repeated events when held


# Common virtual key codes
VK_CODES = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    # Special keys
    "capslock": 0x14,
    "caps": 0x14,  # Alias
    "numlock": 0x90,
    "scrolllock": 0x91,
    "pause": 0x13,
    "printscreen": 0x2C,
    # OEM keys (below ESC)
    "grave": 0xC0,  # ` ~ key
    "backtick": 0xC0,  # Alias
    "tilde": 0xC0,  # Alias
    "`": 0xC0,  # Raw backtick character (backward compat with old configs)
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
    # Letters
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
    # Numbers
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
}

WM_HOTKEY = 0x0312


@dataclass
class HotkeyBinding:
    """A registered hotkey binding.

    Two flavors share this record:
    - Plain (legacy): `callback` fires on every WM_HOTKEY press.
    - Press+release (`on_press` is not None): `on_press(ts)` fires on the
      first WM_HOTKEY, then the message loop polls GetAsyncKeyState until
      the main key goes up and fires `on_release(ts)`. Timestamps are
      time.monotonic() seconds. WM_HOTKEY autorepeats while `held` are
      swallowed (registration also adds MOD_NOREPEAT as first defense).
    """

    id: int
    modifiers: int
    vk_code: int
    callback: Optional[Callable[[], None]]
    description: str = ""
    on_press: Optional[Callable[[float], None]] = None
    on_release: Optional[Callable[[float], None]] = None
    held: bool = False


class HotkeyManager:
    """
    Manages global hotkey registration and event handling.

    Usage:
        manager = HotkeyManager()
        manager.register("ctrl+shift+space", on_trigger, "Voice trigger")
        manager.start()  # Starts message loop in background thread
        ...
        manager.stop()
    """

    def __init__(self):
        self._bindings: Dict[int, HotkeyBinding] = {}
        self._next_id = 1
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._action_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._thread_ready = threading.Event()
        self._thread_id: Optional[int] = None

    def parse_hotkey(self, hotkey_str: str) -> tuple[int, int]:
        """
        Parse hotkey string to modifiers and vk_code.

        Examples:
            "ctrl+shift+space" -> (CTRL|SHIFT, VK_SPACE)
            "alt+f9" -> (ALT, VK_F9)
        """
        parts = hotkey_str.lower().replace(" ", "").split("+")
        modifiers = 0
        vk_code = 0

        for part in parts:
            if part == "ctrl":
                modifiers |= Modifiers.CTRL
            elif part == "shift":
                modifiers |= Modifiers.SHIFT
            elif part == "alt":
                modifiers |= Modifiers.ALT
            elif part == "win":
                modifiers |= Modifiers.WIN
            elif part in VK_CODES:
                vk_code = VK_CODES[part]
            else:
                raise ValueError(f"Unknown key: {part}")

        if vk_code == 0:
            raise ValueError(f"No key specified in hotkey: {hotkey_str}")

        return modifiers, vk_code

    def register(
        self, hotkey_str: str, callback: Callable[[], None], description: str = ""
    ) -> int:
        """
        Register a global hotkey.

        Args:
            hotkey_str: Hotkey string like "ctrl+shift+space"
            callback: Function to call when hotkey is pressed
            description: Human-readable description

        Returns:
            Hotkey ID for later unregistration

        Raises:
            ValueError: If hotkey string is invalid
            RuntimeError: If registration fails (hotkey in use)
        """
        modifiers, vk_code = self.parse_hotkey(hotkey_str)

        # Debug logging helper
        import datetime
        from pathlib import Path

        debug_log = Path(__file__).parent.parent / "DebugLog" / "hotkey_debug.log"

        def _log(msg: str):
            from ..core.debug import append_log_line

            ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            append_log_line(debug_log, f"[{ts}] {msg}")

        _log(
            f"register() called: hotkey='{hotkey_str}', mods={modifiers}, vk={vk_code:#x}"
        )

        def _register_on_thread() -> int:
            _log(f"_register_on_thread() executing on thread {threading.get_ident()}")
            with self._lock:
                hotkey_id = self._next_id
                self._next_id += 1

            _log(
                f"Calling RegisterHotKey(None, {hotkey_id}, {modifiers}, {vk_code:#x})"
            )
            result = user32.RegisterHotKey(None, hotkey_id, modifiers, vk_code)
            _log(f"RegisterHotKey returned: {result}")

            if not result:
                error = ctypes.get_last_error()
                _log(f"RegisterHotKey FAILED! error={error}")
                if error == 1409:
                    raise RuntimeError(
                        f"Hotkey '{hotkey_str}' already in use by another application"
                    )
                raise RuntimeError(f"Failed to register hotkey: error {error}")

            binding = HotkeyBinding(
                id=hotkey_id,
                modifiers=modifiers,
                vk_code=vk_code,
                callback=callback,
                description=description,
            )
            with self._lock:
                self._bindings[hotkey_id] = binding
            _log(f"Hotkey registered successfully: ID={hotkey_id}")
            logger.info(f"Registered hotkey: {hotkey_str} (ID={hotkey_id})")
            return hotkey_id

        # Register on the same thread that owns the message loop
        return self._run_on_hotkey_thread(_register_on_thread)

    def register_press_release(
        self,
        hotkey_str: str,
        on_press: Callable[[float], None],
        on_release: Callable[[float], None],
        description: str = "",
    ) -> int:
        """
        Register a global hotkey that reports BOTH press and release.

        RegisterHotKey only delivers key-down events, so release is detected
        by polling GetAsyncKeyState on the message-loop thread (~10ms cadence,
        the loop's existing idle sleep). No global keyboard hook is installed.

        Args:
            hotkey_str: Hotkey string like "grave" or "ctrl+shift+space".
                For combos, release = the MAIN key going up (modifiers are
                not tracked).
            on_press: Called with a time.monotonic() timestamp (seconds) on
                key down. Runs on the hotkey thread — must not block.
            on_release: Called with a time.monotonic() timestamp (seconds)
                on key up. Runs on the hotkey thread — must not block.
            description: Human-readable description

        Returns:
            Hotkey ID for later unregistration
        """
        modifiers, vk_code = self.parse_hotkey(hotkey_str)
        # First defense against autorepeat WM_HOTKEY while held (the `held`
        # flag in the message loop is the second). Never added on the legacy
        # register() path to keep its behavior frozen.
        modifiers |= Modifiers.NOREPEAT

        def _register_on_thread() -> int:
            with self._lock:
                hotkey_id = self._next_id
                self._next_id += 1

            result = user32.RegisterHotKey(None, hotkey_id, modifiers, vk_code)
            if not result:
                error = ctypes.get_last_error()
                if error == 1409:
                    raise RuntimeError(
                        f"Hotkey '{hotkey_str}' already in use by another application"
                    )
                raise RuntimeError(f"Failed to register hotkey: error {error}")

            binding = HotkeyBinding(
                id=hotkey_id,
                modifiers=modifiers,
                vk_code=vk_code,
                callback=None,
                description=description,
                on_press=on_press,
                on_release=on_release,
            )
            with self._lock:
                self._bindings[hotkey_id] = binding
            logger.info(
                f"Registered press+release hotkey: {hotkey_str} (ID={hotkey_id})"
            )
            return hotkey_id

        return self._run_on_hotkey_thread(_register_on_thread)

    @staticmethod
    def _key_is_down(vk_code: int) -> bool:
        """Physical key state via GetAsyncKeyState (MSB = currently down)."""
        return bool(user32.GetAsyncKeyState(vk_code) & 0x8000)

    def _handle_press_release_press(self, binding: HotkeyBinding) -> None:
        """WM_HOTKEY arrived for a press+release binding (hotkey thread)."""
        if binding.held:
            # Autorepeat while held (or MOD_NOREPEAT unsupported): swallow.
            return
        binding.held = True
        try:
            binding.on_press(time.monotonic())
        except Exception as e:
            logger.error(f"Hotkey on_press error: {e}")

    def _poll_press_release_releases(self) -> None:
        """Fire on_release for held bindings whose key went up (hotkey thread)."""
        with self._lock:
            held = [
                b
                for b in self._bindings.values()
                if b.on_press is not None and b.held
            ]
        for binding in held:
            if not self._key_is_down(binding.vk_code):
                binding.held = False
                try:
                    binding.on_release(time.monotonic())
                except Exception as e:
                    logger.error(f"Hotkey on_release error: {e}")

    def unregister(self, hotkey_id: int) -> bool:
        """Unregister a hotkey by ID."""

        def _unregister_on_thread() -> bool:
            with self._lock:
                if hotkey_id not in self._bindings:
                    return False
                del self._bindings[hotkey_id]

            user32.UnregisterHotKey(None, hotkey_id)
            logger.info(f"Unregistered hotkey ID={hotkey_id}")
            return True

        return self._run_on_hotkey_thread(_unregister_on_thread)

    def unregister_all(self) -> None:
        """Unregister all hotkeys."""
        self._run_on_hotkey_thread(self._unregister_all_internal)

    def _message_loop(self) -> None:
        """Windows message loop to receive hotkey events."""
        import datetime
        from pathlib import Path

        # Debug log file (works with pythonw.exe where stdout is suppressed)
        debug_log = Path(__file__).parent.parent / "DebugLog" / "hotkey_debug.log"

        def _log(msg: str):
            from ..core.debug import append_log_line

            ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            append_log_line(debug_log, f"[{ts}] {msg}")  # Silent fail for logging

        msg = wintypes.MSG()

        self._thread_id = threading.get_ident()
        self._thread_ready.set()

        logger.info("Hotkey message loop started")
        _log(f"Message loop started (thread_id={self._thread_id})")
        _log(f"Registered bindings: {list(self._bindings.keys())}")

        msg_count = 0
        last_heartbeat = time.time()
        HEARTBEAT_INTERVAL = 300  # Log heartbeat every 5 minutes
        while self._running:
            self._process_actions()
            # PeekMessage with PM_REMOVE (1)
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                msg_count += 1
                # Log every message for debugging (first 10 only to avoid spam)
                if msg_count <= 10:
                    _log(
                        f"Message received: msg={msg.message}, wParam={msg.wParam}, lParam={msg.lParam}"
                    )

                if msg.message == WM_HOTKEY:
                    hotkey_id = msg.wParam
                    _log(f">>> WM_HOTKEY detected! ID={hotkey_id}")

                    with self._lock:
                        binding = self._bindings.get(hotkey_id)

                    if binding:
                        try:
                            logger.debug(f"Hotkey {hotkey_id} triggered")
                            if binding.on_press is not None:
                                # Press+release binding (hold-to-talk):
                                # dispatch press, release comes via polling.
                                _log(f"Press event for hotkey {hotkey_id}")
                                self._handle_press_release_press(binding)
                            else:
                                _log(f"Calling callback for hotkey {hotkey_id}")
                                cb_start = time.time()
                                binding.callback()
                                cb_ms = (time.time() - cb_start) * 1000
                                _log(f"Callback completed ({cb_ms:.0f}ms)")
                        except Exception as e:
                            logger.error(f"Hotkey callback error: {e}")
                            _log(f"Callback error: {e}")
                    else:
                        _log(f"No binding found for hotkey ID={hotkey_id}")

                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                # No message, sleep briefly to reduce CPU
                time.sleep(0.01)

            # Release detection for press+release bindings rides the loop's
            # ~10ms cadence (quantizes release timestamps by <=10ms, which is
            # negligible against the 300ms tap/hold threshold).
            self._poll_press_release_releases()

            # Periodic heartbeat to confirm message loop is alive
            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                _log(
                    f"[HEARTBEAT] alive, msgs={msg_count}, bindings={list(self._bindings.keys())}"
                )
                last_heartbeat = now

        # Process any remaining actions (e.g., unregister) before exit
        self._process_actions()
        logger.info("Hotkey message loop stopped")
        _log(f"Message loop stopped (total messages: {msg_count})")

    def start(self) -> None:
        """Start the hotkey listener in a background thread."""
        with self._lock:
            if self._running:
                return

            self._running = True
            self._thread_ready.clear()
            self._thread = threading.Thread(target=self._message_loop, daemon=True)
            self._thread.start()

        # Wait for the message loop thread to signal readiness
        self._thread_ready.wait(timeout=1.0)

    def stop(self) -> None:
        """Stop the hotkey listener."""
        if not self._running:
            return

        def _shutdown():
            self._unregister_all_internal()
            self._running = False

        # Run shutdown on the hotkey thread to keep Register/Unregister on same thread
        self._run_on_hotkey_thread(_shutdown)

        # Only join if called from a different thread
        if self._thread and threading.get_ident() != self._thread_id:
            self._thread.join(timeout=1.0)

        self._thread = None
        self._thread_id = None

    @property
    def is_running(self) -> bool:
        """Check if the hotkey listener is running."""
        return self._running

    def __enter__(self) -> "HotkeyManager":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()

    def _process_actions(self) -> None:
        """Process any pending cross-thread actions."""
        import datetime
        from pathlib import Path

        debug_log = Path(__file__).parent.parent / "DebugLog" / "hotkey_debug.log"

        def _log(msg: str):
            from ..core.debug import append_log_line

            ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            append_log_line(debug_log, f"[{ts}] {msg}")

        processed = 0
        while True:
            try:
                action = self._action_queue.get_nowait()
            except queue.Empty:
                if processed > 0:
                    _log(
                        f"Processed {processed} actions, bindings now: {list(self._bindings.keys())}"
                    )
                return
            try:
                _log(f"Processing action: {action}")
                action()
                processed += 1
            except Exception as e:
                _log(f"Action error: {e}")
                raise
            finally:
                self._action_queue.task_done()

    def _run_on_hotkey_thread(self, func: Callable[[], Any]):
        """
        Ensure a callable runs on the hotkey thread (required for WM_HOTKEY delivery).
        """
        # If we're already on the hotkey thread, run immediately to avoid deadlock
        if self._thread and threading.get_ident() == self._thread_id:
            return func()

        # Ensure the hotkey thread is running
        if not self._running:
            self.start()

        done = threading.Event()
        result: Dict[str, Any] = {}

        def wrapper():
            try:
                result["value"] = func()
            except Exception as e:
                result["error"] = e
            finally:
                done.set()

        self._action_queue.put(wrapper)
        # Use timeout to prevent indefinite blocking
        if not done.wait(timeout=5.0):
            raise RuntimeError("Hotkey thread did not respond in time")

        if "error" in result:
            raise result["error"]
        return result.get("value")

    def _unregister_all_internal(self) -> None:
        """Internal helper to unregister hotkeys on the hotkey thread."""
        import datetime
        import traceback
        from pathlib import Path

        debug_log = Path(__file__).parent.parent / "DebugLog" / "hotkey_debug.log"

        def _log(msg: str):
            from ..core.debug import append_log_line

            ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            append_log_line(debug_log, f"[{ts}] {msg}")

        # Log who called this with stack trace
        stack = "".join(traceback.format_stack()[:-1])
        _log(f">>> _unregister_all_internal() called!")
        _log(f"Stack trace:\n{stack}")

        with self._lock:
            ids = list(self._bindings.keys())
            self._bindings.clear()

        _log(f"Unregistering hotkeys: {ids}")

        for hotkey_id in ids:
            user32.UnregisterHotKey(None, hotkey_id)

        if ids:
            logger.info("All hotkeys unregistered")
