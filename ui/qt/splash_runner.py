#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Standalone splash screen runner for Aria.
This script is launched as a subprocess to display the splash screen
while the main application loads.

Usage: python splash_runner.py <port>
"""

import sys
import os
import traceback

# Set up path for imports - must be before any aria imports
# Calculate paths relative to this script's location
# This script is at: aria/ui/qt/splash_runner.py
_script_dir = os.path.dirname(os.path.abspath(__file__))
_project_dir = os.path.dirname(
    os.path.dirname(_script_dir)
)  # Go up from ui/qt to project root

# CRITICAL: Insert project dir FIRST to ensure we import from THIS project,
# not from a sibling stable version (e.g., /AIBOX/aria vs /AIBOX/aria-v1.1-dev)
if _project_dir not in sys.path:
    sys.path.insert(0, _project_dir)
# Also add script directory so we can import splash.py directly
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)
os.chdir(_project_dir)

# Log file for splash errors
SPLASH_LOG = os.path.join(os.path.dirname(__file__), "..", "..", "splash_error.log")


def log_error(msg):
    try:
        with open(SPLASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"{msg}\n")
            f.flush()
            os.fsync(f.fileno())  # Force write to disk
    except:
        pass


try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer

    # Direct import from current directory to avoid package resolution issues
    from splash import SplashWindow

    # Import progress_ipc from project root
    sys.path.insert(0, _project_dir)
    from progress_ipc import ProgressListener
except Exception as e:
    log_error(f"Import error: {e}\n{traceback.format_exc()}")
    sys.exit(1)


def main():
    try:
        log_error(f"=== Splash starting: {sys.argv} ===")

        if len(sys.argv) < 2:
            log_error("Usage: splash_runner.py <port>")
            sys.exit(1)

        port = int(sys.argv[1])
        address = ("localhost", port)
        log_error(f"Listening on port {port}")

        # Create listener FIRST and start it BEFORE Qt app
        # This ensures the server is ready when main process tries to connect
        log_error("Creating ProgressListener...")
        try:
            listener = ProgressListener(address)
            log_error("ProgressListener created OK")
        except Exception as e:
            log_error(f"ProgressListener creation failed: {e}")
            sys.exit(1)

        log_error("Starting listener.start() (waiting for main process)...")

        # Start listener with longer timeout to wait for main process
        try:
            result = listener.start(timeout=30.0)
            log_error(f"listener.start() returned: {result}")
            if not result:
                log_error("Listener failed to get connection, exiting")
                sys.exit(1)
        except Exception as e:
            log_error(f"listener.start() exception: {e}")
            sys.exit(1)

        log_error("Listener connected to main process!")

        app = QApplication(sys.argv)
        app.setApplicationName("Aria Splash")

        # Read version directly from file (not import — AriaRuntime.exe's
        # PyInstaller hooks may return the bundled old version instead of
        # the updated __init__.py on disk).
        _version = ""
        try:
            _init_path = os.path.join(_project_dir, "__init__.py")
            with open(_init_path, "r", encoding="utf-8") as _f:
                for _line in _f:
                    if "__version__" in _line and "=" in _line:
                        _version = _line.split("=")[1].strip().strip("\"'")
                        break
        except Exception:
            pass
        splash = SplashWindow(version=_version)
        splash.show()
        log_error("Splash window shown")

        # Poll timer - check for progress updates
        poll_timer = QTimer()

        def on_poll():
            # Poll for events
            event = listener.poll(timeout=0.05)
            if event:
                splash.set_progress(event.percent, event.message)
                log_error(f"Progress: {event.stage} {event.percent}% - {event.message}")

                if event.stage in ("done", "error"):
                    poll_timer.stop()
                    listener.close()
                    log_error(f"Received {event.stage}, closing splash")
                    # Close after brief delay to show final state
                    QTimer.singleShot(400, splash.fade_out_and_close)

        poll_timer.timeout.connect(on_poll)
        poll_timer.start(50)  # 20 Hz polling

        # Safety timeout - close splash after 30 minutes regardless
        # (first-launch model download can take 10+ minutes on slow connections)
        def on_timeout():
            if not splash._closing:
                log_error("Timeout - closing splash")
                poll_timer.stop()
                listener.close()
                splash.fade_out_and_close()

        QTimer.singleShot(1_800_000, on_timeout)

        # Run event loop
        splash.closed.connect(app.quit)
        log_error("Entering event loop")
        sys.exit(app.exec())

    except Exception as e:
        log_error(f"Main error: {e}\n{traceback.format_exc()}")
        sys.exit(1)


if __name__ == "__main__":
    main()
