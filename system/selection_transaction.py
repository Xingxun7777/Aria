"""Content-free result models for guarded long-running selection edits."""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .target_surface import TargetSnapshot


class SelectionTransactionStatus(str, Enum):
    READY = "ready"
    CONFIRMED = "confirmed"
    SENT = "sent"
    NO_CHANGE = "no_change"
    INVALID_ARGUMENT = "invalid_argument"
    TARGET_UNAVAILABLE = "target_unavailable"
    TARGET_CHANGED = "target_changed"
    UNSUPPORTED_SURFACE = "unsupported_surface"
    SELECTION_UNAVAILABLE = "selection_unavailable"
    SELECTION_CHANGED = "selection_changed"
    CONTENT_CHANGED = "content_changed"
    PROTECTED_CONTROL = "protected_control"
    READ_ONLY = "read_only"
    UNDO_UNAVAILABLE = "undo_unavailable"
    ELEVATION_REQUIRED = "elevation_required"
    WRITE_REJECTED = "write_rejected"
    WRITE_PARTIAL = "write_partial"
    NATIVE_FAILED = "native_failed"


@dataclass(frozen=True)
class SelectionCaptureResult:
    success: bool
    status: SelectionTransactionStatus
    target: Optional[TargetSnapshot] = None
    transport: str = "none"


@dataclass(frozen=True)
class SelectionReplaceResult:
    success: bool
    status: SelectionTransactionStatus
    transport: str = "none"
    partial_possible: bool = False
    undo_available: bool = False
    undo_token: Optional[Any] = None
