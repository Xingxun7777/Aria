"""
Quick test: compare SendInput vs PostMessage WM_CHAR for typewriter mode.
Run this, switch to Notepad within 5 seconds, and see which method works.
"""

import ctypes
from ctypes import wintypes
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)

# API declarations
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.PostMessageW.restype = wintypes.BOOL

WM_CHAR = 0x0102


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


def get_focused_control():
    """Get focused control via GetGUIThreadInfo (cross-thread safe)."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, None
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    if not tid:
        return hwnd, hwnd

    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(GUITHREADINFO)
    if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
        focus = gti.hwndFocus or gti.hwndActive or hwnd
        return hwnd, focus
    return hwnd, hwnd


def test_postmessage_wm_char(text, target_hwnd, delay=0.015):
    """Send text via PostMessage WM_CHAR."""
    print(f"\n--- Method: PostMessage WM_CHAR ---")
    for i, char in enumerate(text):
        cp = ord(char)
        if char == "\n":
            cp = 0x0D
        if cp > 0xFFFF:
            high = 0xD800 + ((cp - 0x10000) >> 10)
            low = 0xDC00 + ((cp - 0x10000) & 0x3FF)
            cps = [high, low]
        else:
            cps = [cp]

        for c in cps:
            result = user32.PostMessageW(target_hwnd, WM_CHAR, c, 0)
            if not result:
                err = ctypes.get_last_error()
                print(f"  [{i+1}] '{char}' FAILED (error={err})")
                return
        print(f"  [{i+1}/{len(text)}] '{char}' (U+{ord(char):04X}) -> OK")
        time.sleep(delay)
    print("Done!")


text = "你好世界Hello123"
print(f"Will type: {text}")
print("Switch to target window (e.g. Notepad) within 5 seconds...")
for i in range(5, 0, -1):
    print(f"  {i}...")
    time.sleep(1)

fg_hwnd, focus_hwnd = get_focused_control()
print(f"Foreground: {fg_hwnd:#x}, Focused control: {focus_hwnd:#x}")

test_postmessage_wm_char(text, focus_hwnd)
