#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
VoiceType GUI Launcher (No Console)
Double-click to run with floating ball UI.
"""

import sys
import os

# Fix OpenMP conflict between PyTorch and faster-whisper (MUST be before any imports)
os.environ['KMP_DUPLICATE_LIB_OK'] = 'TRUE'

# Error logging since no console
def log_error(msg):
    with open(os.path.join(os.path.dirname(__file__), "launch_error.log"), "w", encoding="utf-8") as f:
        f.write(msg)

try:
    # Set working directory
    os.chdir(r"G:\AIBOX")
    sys.path.insert(0, r"G:\AIBOX")

    # Launch Qt frontend
    from voicetype.ui.qt.main import main
    sys.exit(main())

except Exception as e:
    import traceback
    log_error(traceback.format_exc())

    # Show error dialog
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication(sys.argv)
        QMessageBox.critical(None, "VoiceType Error", str(e))
    except:
        pass
    sys.exit(1)
