"""
Aria v1.1-dev Package Alias

This package redirects all imports to the project root directory.
Allows `from aria.xxx import yyy` to work when the project
directory is not named 'aria'.

How it works:
- Setting __path__ to parent directory makes Python search there for submodules
- e.g., `from aria.ui.qt.main import main` finds `../ui/qt/main.py`
"""

import os

# Get the project root (parent of this aria/ directory)
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Redirect submodule search to project root
__path__ = [_project_root]


# Re-export version info from root __init__.py by reading the file directly.
# Avoids stale hardcoded version when root bumps are forgotten to mirror here.
def _read_root_version() -> str:
    try:
        with open(os.path.join(_project_root, "__init__.py"), encoding="utf-8") as f:
            for line in f:
                if "__version__" in line and "=" in line:
                    return line.split("=", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return "0.0.0"


__version__ = _read_root_version()
__author__ = "Aria Team"
