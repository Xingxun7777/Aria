"""
Entry point for running VoiceType as a module.

Usage:
    python -m voicetype
    python -m voicetype --hotkey ctrl+shift+space
    python -m voicetype --list-devices
"""

from .app import main

if __name__ == "__main__":
    main()
