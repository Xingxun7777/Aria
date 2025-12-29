"""
Path utilities for Aria.

Handles path resolution for both development (running as script) and
production (frozen PyInstaller executable) environments.
"""

import sys
from pathlib import Path
from typing import Optional


def get_base_path() -> Path:
    """
    Get the base path for the application.

    Returns:
        Path: Base directory path
            - For frozen (PyInstaller): directory containing the executable
            - For script: aria package directory
    """
    if getattr(sys, "frozen", False):
        # Running as compiled executable
        return Path(sys.executable).parent
    else:
        # Running as script - return the aria package directory
        return Path(__file__).parent.parent.parent


def get_config_path(filename: str = "hotwords.json") -> Path:
    """
    Get the path to a configuration file.

    Args:
        filename: Name of the config file (default: hotwords.json)

    Returns:
        Path: Full path to the config file
    """
    return get_base_path() / "config" / filename


def get_models_path(model_name: Optional[str] = None) -> Path:
    """
    Get the path to models directory or a specific model.

    Args:
        model_name: Optional name of specific model subdirectory

    Returns:
        Path: Full path to models directory or specific model
    """
    models_dir = get_base_path() / "models"
    if model_name:
        return models_dir / model_name
    return models_dir


def get_debug_log_path() -> Path:
    """
    Get the path to debug log directory.

    Returns:
        Path: Full path to DebugLog directory
    """
    return get_base_path() / "DebugLog"


def ensure_directory(path: Path) -> Path:
    """
    Ensure a directory exists, creating it if necessary.

    Args:
        path: Directory path to ensure

    Returns:
        Path: The same path (for chaining)
    """
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_frozen() -> bool:
    """
    Check if running as frozen (PyInstaller) executable.

    Returns:
        bool: True if running as frozen executable
    """
    return getattr(sys, "frozen", False)
