"""
Voice Command Module
====================
Detects and executes voice commands from transcribed text.

Usage:
    from core.command import CommandDetector, CommandExecutor

    detector = CommandDetector()
    executor = CommandExecutor(output_injector)

    cmd = detector.detect("小助手，发送")
    if cmd:
        executor.execute(cmd)
"""

from .detector import CommandDetector
from .executor import CommandExecutor

__all__ = ['CommandDetector', 'CommandExecutor']
