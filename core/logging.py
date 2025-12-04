"""
VoiceType Logging System
========================
Centralized logging for all modules.
"""

import logging
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

# Log directory
LOG_DIR = Path(__file__).parent.parent / "logs"


def setup_logging(
    level: int = logging.INFO,
    log_file: bool = True,
    console: bool = True,
    name: str = "voicetype"
) -> logging.Logger:
    """
    Setup logging for VoiceType.

    Args:
        level: Logging level (default INFO)
        log_file: Whether to write to file
        console: Whether to output to console
        name: Logger name

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Clear existing handlers
    logger.handlers.clear()

    # Format
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Console handler
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    # File handler
    if log_file:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"{name}_{datetime.now().strftime('%Y%m%d')}.log"

        file_handler = logging.FileHandler(log_path, encoding='utf-8')
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str = "voicetype") -> logging.Logger:
    """Get a logger instance."""
    return logging.getLogger(name)


# Module-specific loggers
def get_system_logger() -> logging.Logger:
    """Get logger for system integration module."""
    return logging.getLogger("voicetype.system")


def get_scheduler_logger() -> logging.Logger:
    """Get logger for scheduler module."""
    return logging.getLogger("voicetype.scheduler")


def get_asr_logger() -> logging.Logger:
    """Get logger for ASR module."""
    return logging.getLogger("voicetype.asr")


def get_audio_logger() -> logging.Logger:
    """Get logger for audio module."""
    return logging.getLogger("voicetype.audio")


def get_ui_logger() -> logging.Logger:
    """Get logger for UI module."""
    return logging.getLogger("voicetype.ui")
