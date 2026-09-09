"""Explicit recovery actions for the most recent speech transcript.

Callers (the popup UI, the "上屏" voice command) own only intent. Transcript
state stays in the backend/history, and paste always re-enters the guarded
OutputInjector transaction — full-format clipboard backup/restore, target
verification, no auto-send — instead of blindly sending Ctrl+V or Enter.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

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


@dataclass(frozen=True)
class RecoveryOutcome:
    """Paste result plus the target the text was bound to (None unless used)."""

    status: RecoveryStatus
    target: Optional[TargetSnapshot] = None


# User-facing feedback for the voice-triggered re-inject ("上屏"). Kept
# content-free: the transcript itself never appears in a notice.
VOICE_REPASTE_NOTICES = {
    RecoveryStatus.INSERTED: ("已重新上屏上一句", "success"),
    RecoveryStatus.NO_TEXT: ("暂无可上屏的语音转写", "info"),
    RecoveryStatus.TARGET_REQUIRED: ("请先把光标放到目标窗口，再说“上屏”", "warning"),
    RecoveryStatus.TARGET_UNAVAILABLE: ("无法确认目标窗口，未重新上屏", "warning"),
    RecoveryStatus.OUTPUT_UNAVAILABLE: ("输入通道未就绪，无法重新上屏", "warning"),
    RecoveryStatus.INSERT_FAILED: (
        "目标窗口已变化或输入失败，未重新上屏；把光标放好后再说一次",
        "warning",
    ),
}


def describe_voice_repaste(status: RecoveryStatus) -> tuple[str, str]:
    """Return (message, level) for the voice re-inject command."""
    return VOICE_REPASTE_NOTICES.get(
        status, ("重新上屏失败，转写仍保留", "warning")
    )


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
    expected_target: Optional[TargetSnapshot] = None,
    current_process_foreground: Callable[[], bool] = (
        foreground_belongs_to_current_process
    ),
) -> RecoveryStatus:
    """Insert explicit draft text through a guarded target transaction."""
    return paste_recovery_outcome(
        backend,
        text,
        expected_target=expected_target,
        current_process_foreground=current_process_foreground,
    ).status


def paste_recovery_outcome(
    backend,
    text: str,
    *,
    expected_target: Optional[TargetSnapshot] = None,
    current_process_foreground: Callable[[], bool] = (
        foreground_belongs_to_current_process
    ),
) -> RecoveryOutcome:
    """Insert recovery text and report which target it was bound to.

    ``expected_target`` lets a voice command reuse the snapshot taken when the
    spoken request ended, so the paste obeys the same "focus moved → fail
    closed" rule as ordinary dictation. Without it a fresh snapshot of the
    current foreground is captured (popup behaviour). An explicitly supplied
    but invalid snapshot means capture already failed at the commit boundary
    and is refused rather than replaced by a guess.
    """
    text = str(text or "")
    if not text.strip():
        return RecoveryOutcome(RecoveryStatus.NO_TEXT)
    try:
        if current_process_foreground():
            return RecoveryOutcome(RecoveryStatus.TARGET_REQUIRED)
    except Exception:
        return RecoveryOutcome(RecoveryStatus.TARGET_UNAVAILABLE)

    injector = getattr(backend, "output_injector", None)
    if injector is None:
        return RecoveryOutcome(RecoveryStatus.OUTPUT_UNAVAILABLE)

    if expected_target is not None:
        target = expected_target
    else:
        capture = getattr(injector, "capture_target_snapshot", None)
        if not callable(capture):
            return RecoveryOutcome(RecoveryStatus.OUTPUT_UNAVAILABLE)
        try:
            target = capture()
        except Exception:
            return RecoveryOutcome(RecoveryStatus.TARGET_UNAVAILABLE)
    if not isinstance(target, TargetSnapshot) or not target.is_valid:
        return RecoveryOutcome(RecoveryStatus.TARGET_UNAVAILABLE)

    try:
        inserted = injector.insert_text(text, expected_target=target)
    except Exception:
        return RecoveryOutcome(RecoveryStatus.INSERT_FAILED, target)
    if inserted:
        return RecoveryOutcome(RecoveryStatus.INSERTED, target)
    return RecoveryOutcome(RecoveryStatus.INSERT_FAILED, target)


__all__ = [
    "RecoveryOutcome",
    "RecoveryStatus",
    "VOICE_REPASTE_NOTICES",
    "copy_last_transcript",
    "copy_recovery_text",
    "describe_voice_repaste",
    "paste_last_transcript",
    "paste_recovery_outcome",
    "paste_recovery_text",
    "resolve_last_transcript",
]
