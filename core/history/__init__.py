"""
History Module
==============
Unified history storage for all Aria interactions.
"""

from .models import RecordType, HistoryRecord, RECORD_TYPE_LABELS, RECORD_TYPE_COLORS
from .store import HistoryStore
from .asr_entry import AsrHistoryEntry, compose_asr_history_entry

__all__ = [
    "RecordType",
    "HistoryRecord",
    "HistoryStore",
    "RECORD_TYPE_LABELS",
    "RECORD_TYPE_COLORS",
    "AsrHistoryEntry",
    "compose_asr_history_entry",
]
