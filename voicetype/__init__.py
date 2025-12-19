"""
VoiceType v1.1-dev Package Alias

This package redirects all imports to the project root directory.
Allows `from voicetype.xxx import yyy` to work when the project
directory is not named 'voicetype'.

How it works:
- Setting __path__ to parent directory makes Python search there for submodules
- e.g., `from voicetype.ui.qt.main import main` finds `../ui/qt/main.py`
"""

import os

# Get the project root (parent of this voicetype/ directory)
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Redirect submodule search to project root
__path__ = [_project_root]

# Re-export version info from root __init__.py
__version__ = "1.1.0-dev"
__author__ = "VoiceType Team"
