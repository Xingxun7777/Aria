# __main__.py
# Entry point for running Qt frontend as module
# Usage: python -m voicetype.ui.qt [--hotkey <key>] [--demo]

from .main import main
import sys

if __name__ == "__main__":
    sys.exit(main())
