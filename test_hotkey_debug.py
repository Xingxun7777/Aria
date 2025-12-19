# -*- coding: utf-8 -*-
"""
Hotkey Debug Test Script
Tests the hotkey system to diagnose registration/message loop issues.
"""
import sys
import os
import time
import threading

# CRITICAL: This directory IS the voicetype package
# Rename it in sys.modules to override G:\AIBOX\voicetype\
this_dir = os.path.dirname(os.path.abspath(__file__))

# Create a fake voicetype package that points to this directory
import types

voicetype_pkg = types.ModuleType("voicetype")
voicetype_pkg.__path__ = [this_dir]
voicetype_pkg.__file__ = os.path.join(this_dir, "__init__.py")
sys.modules["voicetype"] = voicetype_pkg


# Verify which module we're importing
def verify_import():
    import voicetype.system.hotkey as h

    expected = os.path.join(this_dir, "system", "hotkey.py")
    actual = os.path.abspath(h.__file__)
    if os.path.normpath(actual) != os.path.normpath(expected):
        print(f"[WARNING] Wrong module imported!")
        print(f"  Expected: {expected}")
        print(f"  Actual:   {actual}")
        return False
    return True


def test_hotkey():
    print("=" * 60)
    print("Hotkey Debug Test")
    print("=" * 60)

    # Step 1: Check if HotkeyManager can be imported
    print("\n[1] Importing HotkeyManager...")
    try:
        if not verify_import():
            print("    FAIL: Wrong module imported! Check sys.path")
            return

        from voicetype.system.hotkey import HotkeyManager, VK_CODES

        print("    OK: HotkeyManager imported successfully")
        print(f"    Available keys: {list(VK_CODES.keys())[:10]}...")
    except Exception as e:
        print(f"    FAIL: Import error: {e}")
        import traceback

        traceback.print_exc()
        return

    # Step 2: Get hotkey from command line or config
    print("\n[2] Loading hotkey...")
    hotkey = "f10"  # Test with F10 first - more reliable than grave
    if len(sys.argv) > 1:
        hotkey = sys.argv[1].lower()
        print(f"    Using command-line hotkey: '{hotkey}'")
    else:
        print(f"    Using test hotkey: '{hotkey}' (pass different key as argument)")

    # Step 3: Create manager
    print("\n[3] Creating HotkeyManager...")
    try:
        manager = HotkeyManager()
        print("    OK: Manager created")
    except Exception as e:
        print(f"    FAIL: {e}")
        import traceback

        traceback.print_exc()
        return

    # Step 4: Test callback
    callback_count = [0]
    callback_event = threading.Event()

    def test_callback():
        callback_count[0] += 1
        print(f"\n    >>> HOTKEY PRESSED! (count={callback_count[0]})")
        callback_event.set()

    # Step 5: Register hotkey
    print(f"\n[4] Registering hotkey '{hotkey}'...")
    try:
        hotkey_id = manager.register(hotkey, test_callback, "Test hotkey")
        print(f"    OK: Hotkey registered (ID={hotkey_id})")
    except RuntimeError as e:
        print(f"    FAIL: Registration error: {e}")
        print("\n    This usually means:")
        print("    - Another application has registered this hotkey")
        print("    - Or Windows is blocking registration")
        return
    except Exception as e:
        print(f"    FAIL: Unexpected error: {e}")
        import traceback

        traceback.print_exc()
        return

    # Step 6: Check manager state
    print("\n[5] Checking manager state...")
    print(f"    is_running: {manager.is_running}")
    print(f"    _thread_id: {manager._thread_id}")
    print(f"    _thread: {manager._thread}")
    print(f"    _bindings: {len(manager._bindings)} registered")

    # Check debug log
    log_path = os.path.join(this_dir, "DebugLog", "hotkey_debug.log")
    print(f"\n[6] Checking debug log...")
    time.sleep(0.3)  # Give thread time to write
    if os.path.exists(log_path):
        with open(log_path, "r") as f:
            content = f.read()
        print(f"    Log file exists! Content:")
        for line in content.strip().split("\n")[-5:]:
            print(f"      {line}")
    else:
        print(f"    No log file at {log_path}")

    # Step 7: Wait for hotkey press
    print("\n" + "=" * 60)
    print(f"  NOW PRESS THE '{hotkey.upper()}' KEY")
    print("  Waiting 10 seconds for hotkey press...")
    print("=" * 60)

    # Wait for callback or timeout
    pressed = callback_event.wait(timeout=10.0)

    if pressed:
        print(f"\n[SUCCESS] Hotkey '{hotkey}' is working!")
        print(f"    Callback was triggered {callback_count[0]} time(s)")
    else:
        print(f"\n[FAILURE] No hotkey press detected after 10 seconds")
        print("\n    Possible issues:")
        print("    1. The hotkey is registered by another app (try a different key)")
        print("    2. Windows message loop is not receiving events")
        print("    3. The key code may not match your keyboard layout")
        print(
            f"\n    Registered VK_CODE for '{hotkey}': {VK_CODES.get(hotkey.lower(), 'unknown')}"
        )

    # Cleanup
    print("\n[7] Cleaning up...")
    manager.stop()
    print("    OK: Manager stopped")

    print("\n" + "=" * 60)
    print("Test complete")
    print("=" * 60)


if __name__ == "__main__":
    test_hotkey()
