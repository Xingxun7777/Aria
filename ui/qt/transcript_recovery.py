"""Explicit recovery actions for the most recent speech transcript.

The popup UI owns only intent. Transcript state stays in the backend/history,
and paste always re-enters the guarded OutputInjector transaction instead of
blindly sending Ctrl+V or Enter from a Qt callback.

The implementation lives in ``aria.system.transcript_recovery`` so the
backend's "粘贴" voice command can share it without importing the Qt layer;
this module keeps the historical import path for the popup.
"""

from aria.system.transcript_recovery import (
    RecoveryOutcome,
    RecoveryStatus,
    copy_last_transcript,
    copy_recovery_text,
    paste_last_transcript,
    paste_recovery_outcome,
    paste_recovery_text,
    resolve_last_transcript,
)

__all__ = [
    "RecoveryOutcome",
    "RecoveryStatus",
    "copy_last_transcript",
    "copy_recovery_text",
    "paste_last_transcript",
    "paste_recovery_outcome",
    "paste_recovery_text",
    "resolve_last_transcript",
]
