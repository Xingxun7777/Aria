# __main__.py
# Entry point for running Qt frontend as module
# Usage: python -m aria.ui.qt [--hotkey <key>]

from .main import main
import sys

if __name__ == "__main__":
    sys.exit(main())
