"""Explicit recovery actions for the most recent speech transcript.

The popup UI owns only intent. Transcript state stays in the backend/history,
and paste always re-enters the guarded OutputInjector transaction instead of
blindly sending Ctrl+V or Enter from a Qt callback.
"""

from enum import Enum
from typing import Callable

from aria.system.output import foreground_belongs_to_current_process
from aria.system.target_surface import TargetSnapshot


class RecoveryStatus(str, Enum):
    COPIED = "copied"
    INSERTED = "inserted"
    NO_TEXT = "no_text"
    COPY_FAILED = "copy_failed"
    TARGET_REQUIRED = "target_required"
    OUTPUT_UNAVAILABLE = "output_unavailable"
    TARGET_UNAVAILABLE = "target_unavailable"
    INSERT_FAILED = "insert_failed"


def resolve_last_transcript(backend) -> str:
    """Resolve runtime transcript first, then a HistoryStore fallback."""
    getter = getattr(backend, "get_last_transcript", None)
    if callable(getter):
        try:
            text = str(getter() or "").strip()
        except Exception:
            text = ""
        if text:
            return text

    store = getattr(backend, "history_store", None)
    latest = getattr(store, "latest_asr_text", None)
    if not callable(latest):
        return ""
    try:
        return str(latest() or "").strip()
    except Exception:
        return ""


def copy_last_transcript(backend, clipboard) -> RecoveryStatus:
    """Copy the last transcript and verify the clipboard accepted it."""
    text = resolve_last_transcript(backend)
    return copy_recovery_text(text, clipboard)


def copy_recovery_text(text: str, clipboard) -> RecoveryStatus:
    """Copy explicit recovery text and verify the clipboard accepted it."""
    text = str(text or "")
    if not text.strip():
        return RecoveryStatus.NO_TEXT
    try:
        clipboard.setText(text)
        if str(clipboard.text() or "") != text:
            return RecoveryStatus.COPY_FAILED
    except Exception:
        return RecoveryStatus.COPY_FAILED
    return RecoveryStatus.COPIED


def paste_last_transcript(
    backend,
    *,
    current_process_foreground: Callable[[], bool] = (
        foreground_belongs_to_current_process
    ),
) -> RecoveryStatus:
    """Paste the last transcript through a fresh guarded target transaction.

    The caller should invoke this after the popup has closed and focus had a
    chance to return. No auto-send action is performed.
    """
    text = resolve_last_transcript(backend)
    return paste_recovery_text(
        backend,
        text,
        current_process_foreground=current_process_foreground,
    )


def paste_recovery_text(
    backend,
    text: str,
    *,
    current_process_foreground: Callable[[], bool] = (
        foreground_belongs_to_current_process
    ),
) -> RecoveryStatus:
    """Insert explicit draft text through a fresh guarded target transaction."""
    text = str(text or "")
    if not text.strip():
        return RecoveryStatus.NO_TEXT
    try:
        if current_process_foreground():
            return RecoveryStatus.TARGET_REQUIRED
    except Exception:
        return RecoveryStatus.TARGET_UNAVAILABLE

    injector = getattr(backend, "output_injector", None)
    if injector is None:
        return RecoveryStatus.OUTPUT_UNAVAILABLE
    capture = getattr(injector, "capture_target_snapshot", None)
    if not callable(capture):
        return RecoveryStatus.OUTPUT_UNAVAILABLE
    try:
        target = capture()
    except Exception:
        return RecoveryStatus.TARGET_UNAVAILABLE
    if not isinstance(target, TargetSnapshot) or not target.is_valid:
        return RecoveryStatus.TARGET_UNAVAILABLE

    try:
        inserted = injector.insert_text(text, expected_target=target)
    except Exception:
        return RecoveryStatus.INSERT_FAILED
    return RecoveryStatus.INSERTED if inserted else RecoveryStatus.INSERT_FAILED
