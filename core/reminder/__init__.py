"""
Aria Reminder System
====================
Voice-triggered timed reminders with Chinese natural language time parsing.

Components:
- time_parser: Chinese time expression → datetime
- store: JSON persistence with atomic writes
- scheduler: Background polling thread (15-second interval)
"""

from .time_parser import parse_reminder_text
from .store import ReminderStore
from .scheduler import ReminderScheduler

__all__ = ["parse_reminder_text", "ReminderStore", "ReminderScheduler"]
