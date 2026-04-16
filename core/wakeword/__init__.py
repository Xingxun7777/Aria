"""
Wakeword Module
===============
Detects and executes application-level commands via wakeword.

Usage:
    from core.wakeword import WakewordDetector, WakewordExecutor

    detector = WakewordDetector()
    executor = WakewordExecutor(app_instance, bridge)

    result = detector.detect("小助手，开启自动发送")
    if result:
        cmd_id, action, value, response, following_text, command_text = result
        executor.execute(cmd_id, action, value, response, following_text)
"""

from .detector import WakewordDetector
from .executor import WakewordExecutor

__all__ = ["WakewordDetector", "WakewordExecutor"]
