"""
Entry point for running Aria as a module.

Usage:
    python -m aria
    python -m aria --hotkey ctrl+shift+space
    python -m aria --list-devices
"""

from .app import main

if __name__ == "__main__":
    main()
