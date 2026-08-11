"""Guarded recent-text rewriting for common custom input fields.

Qt and Chromium applications often expose a perfectly usable text composer
without exposing a native HWND text range or a UI Automation TextPattern.
That makes an application-name allowlist both incomplete and unsafe.  This
adapter instead proves a small capability contract on the focused field:

* Ctrl+A/C can read the field while the same target remains focused;
* the field still has the exact content-free fingerprint captured after Aria's
  insertion;
* each Ctrl+Z moves through the exact expected post-insertion states;
* the replacement is read back exactly after paste.

Only text held by the caller/backend during the transaction is inspected.  A
bookmark stores lengths and SHA-256 fingerprints, never the copied content.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Optional, Protocol

from .target_surface import SurfaceKind, TargetSnapshot

if TYPE_CHECKING:
    from .output import OutputInjector


MAX_GENERIC_FIELD_CHARS = 32_768
GENERIC_TEXT_SURFACES = frozenset({SurfaceKind.CUSTOM, SurfaceKind.ELECTRON})


def canonicalize_field_text(value: str) -> str:
    """Normalize clipboard line endings without otherwise changing content."""

    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class GenericTextFingerprint:
    chars: int
    sha256: str


def fingerprint_field_text(value: str) -> GenericTextFingerprint:
    text = canonicalize_field_text(value)
    return GenericTextFingerprint(
        chars=len(text),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


@dataclass(frozen=True)
class GenericRecentTextBookmark:
    """Content-free undo/readback receipt for one or more tail insertions."""

    hwnd: int
    pid: int
    focused_hwnd: int
    start: int
    end: int
    # First state is the field before the first tracked insert.  Every later
    # state is the exact full-field fingerprint after one insertion.  The
    # recent-group combiner joins compatible two-state bookmarks into a chain.
    state_chain: tuple[GenericTextFingerprint, ...]

    @property
    def undo_steps(self) -> int:
        return max(0, len(self.state_chain) - 1)


class GenericTextEditStatus(str, Enum):
    CONFIRMED = "confirmed"
    INVALID_ARGUMENT = "invalid_argument"
    TARGET_UNAVAILABLE = "target_unavailable"
    TARGET_CHANGED = "target_changed"
    UNSUPPORTED_SURFACE = "unsupported_surface"
    SELECTION_UNAVAILABLE = "selection_unavailable"
    CONTENT_CHANGED = "content_changed"
    UNDO_UNAVAILABLE = "undo_unavailable"
    WRITE_REJECTED = "write_rejected"
    WRITE_PARTIAL = "write_partial"


@dataclass(frozen=True)
class GenericFieldReadResult:
    success: bool
    status: GenericTextEditStatus
    text: str = ""
    # Ctrl+A/C cannot distinguish an empty field from a field that refuses to
    # copy.  The adapter accepts this only after an exact pre-read followed by
    # a successful Ctrl+Z whose expected state is empty.
    empty_ambiguous: bool = False


@dataclass(frozen=True)
class GenericPasteResult:
    accepted: bool
    partial_possible: bool = False


@dataclass(frozen=True)
class GenericTextCaptureResult:
    success: bool
    status: GenericTextEditStatus
    bookmark: Optional[GenericRecentTextBookmark] = None


@dataclass(frozen=True)
class GenericTextEditResult:
    success: bool
    status: GenericTextEditStatus
    partial_possible: bool = False
    replacement_bookmark: Optional[GenericRecentTextBookmark] = None


class GenericTextBackend(Protocol):
    def target_is_current(self, target: TargetSnapshot) -> bool: ...

    def read_all(self, target: TargetSnapshot) -> GenericFieldReadResult: ...

    def undo(self, target: TargetSnapshot) -> bool: ...

    def redo(self, target: TargetSnapshot) -> bool: ...

    def paste(self, text: str, target: TargetSnapshot) -> GenericPasteResult: ...


class WindowsGenericTextBackend:
    """Windows keyboard/clipboard backend with guards at every key edge."""

    def __init__(self, injector: "OutputInjector") -> None:
        self.injector = injector

    def target_is_current(self, target: TargetSnapshot) -> bool:
        return bool(self.injector.is_target_snapshot_current(target))

    def read_all(self, target: TargetSnapshot) -> GenericFieldReadResult:
        from ..core.selection.detector import SelectionDetector

        settle = getattr(
            self.injector, "_settle_clipboard_before_generic_probe", None
        )
        if callable(settle) and not bool(settle()):
            return GenericFieldReadResult(
                False, GenericTextEditStatus.SELECTION_UNAVAILABLE
            )
        if not self.target_is_current(target):
            return GenericFieldReadResult(
                False, GenericTextEditStatus.TARGET_CHANGED
            )
        if not self.injector.send_key(
            "a", modifiers=["ctrl"], expected_target=target
        ):
            return GenericFieldReadResult(
                False,
                (
                    GenericTextEditStatus.TARGET_CHANGED
                    if not self.target_is_current(target)
                    else GenericTextEditStatus.SELECTION_UNAVAILABLE
                ),
            )

        detector = SelectionDetector(self.injector)
        result = detector.detect(copy_delay_ms=100, expected_target=target)
        copied = (
            canonicalize_field_text(result.selected_text or "")
            if result.has_selection
            else ""
        )
        collapse_ok = self.injector.send_key(
            "end", expected_target=target
        )
        # SelectionDetector restores failed probes itself.  Successful probes
        # intentionally leave the copied text in the clipboard for the caller,
        # so restore the complete pre-probe clipboard only after collapsing the
        # selection back to the input tail.
        if result.has_selection:
            detector.restore_clipboard_from(result)
        if not collapse_ok or not self.target_is_current(target):
            return GenericFieldReadResult(
                False, GenericTextEditStatus.TARGET_CHANGED
            )
        if result.has_selection:
            return GenericFieldReadResult(
                True, GenericTextEditStatus.CONFIRMED, text=copied
            )
        return GenericFieldReadResult(
            True,
            GenericTextEditStatus.CONFIRMED,
            text="",
            empty_ambiguous=True,
        )

    def undo(self, target: TargetSnapshot) -> bool:
        return bool(
            self.injector.send_key(
                "z", modifiers=["ctrl"], expected_target=target
            )
        )

    def redo(self, target: TargetSnapshot) -> bool:
        return bool(
            self.injector.send_key(
                "y", modifiers=["ctrl"], expected_target=target
            )
        )

    def paste(self, text: str, target: TargetSnapshot) -> GenericPasteResult:
        accepted = bool(
            self.injector._insert_text_clipboard(
                text,
                expected_target=target,
                paste_chord="ctrl_v",
                allow_typewriter_fallback=False,
            )
        )
        delivery = dict(
            self.injector._last_delivery_metadata.get("delivery", {})
        )
        return GenericPasteResult(
            accepted=accepted,
            partial_possible=bool(delivery.get("partial_possible")),
        )


def _bookmark_matches_target(
    bookmark: GenericRecentTextBookmark, target: TargetSnapshot
) -> bool:
    return (
        int(bookmark.hwnd) == int(target.hwnd)
        and int(bookmark.pid) == int(target.pid)
        and int(bookmark.focused_hwnd) == int(target.focused_hwnd)
    )


def _read_matches(
    result: GenericFieldReadResult,
    expected: GenericTextFingerprint,
    *,
    allow_ambiguous_empty: bool = False,
) -> bool:
    if not result.success:
        return False
    if (
        allow_ambiguous_empty
        and expected.chars == 0
        and result.empty_ambiguous
        and result.text == ""
    ):
        return True
    if result.empty_ambiguous:
        return False
    return fingerprint_field_text(result.text) == expected


class GenericTextFieldAdapter:
    """Capability-gated recent rewrite transaction for custom text fields."""

    def __init__(
        self,
        backend: GenericTextBackend,
        *,
        max_field_chars: int = MAX_GENERIC_FIELD_CHARS,
    ) -> None:
        self.backend = backend
        self.max_field_chars = max(1, int(max_field_chars))

    @staticmethod
    def supports_target(target: TargetSnapshot) -> bool:
        return bool(
            target
            and target.is_valid
            and target.profile.kind in GENERIC_TEXT_SURFACES
        )

    def capture_recent_insert(
        self, target: TargetSnapshot, inserted_text: str
    ) -> GenericTextCaptureResult:
        return self.capture_recent_segments(target, (inserted_text,))

    def capture_recent_segments(
        self, target: TargetSnapshot, inserted_segments
    ) -> GenericTextCaptureResult:
        segments = tuple(
            canonicalize_field_text(item)
            for item in tuple(inserted_segments or ())
        )
        if (
            not segments
            or any(not item for item in segments)
            or target is None
            or not target.is_valid
        ):
            return GenericTextCaptureResult(
                False, GenericTextEditStatus.INVALID_ARGUMENT
            )
        if not self.supports_target(target):
            return GenericTextCaptureResult(
                False, GenericTextEditStatus.UNSUPPORTED_SURFACE
            )
        if not self.backend.target_is_current(target):
            return GenericTextCaptureResult(
                False, GenericTextEditStatus.TARGET_CHANGED
            )

        read = self.backend.read_all(target)
        if not read.success:
            return GenericTextCaptureResult(False, read.status)
        if read.empty_ambiguous:
            return GenericTextCaptureResult(
                False, GenericTextEditStatus.SELECTION_UNAVAILABLE
            )
        full_text = canonicalize_field_text(read.text)
        inserted = "".join(segments)
        if len(full_text) > self.max_field_chars:
            return GenericTextCaptureResult(
                False, GenericTextEditStatus.UNSUPPORTED_SURFACE
            )
        if not full_text.endswith(inserted):
            return GenericTextCaptureResult(
                False, GenericTextEditStatus.CONTENT_CHANGED
            )
        prefix = full_text[: len(full_text) - len(inserted)]
        state_chain = [fingerprint_field_text(prefix)]
        running = prefix
        for segment in segments:
            running += segment
            state_chain.append(fingerprint_field_text(running))
        bookmark = GenericRecentTextBookmark(
            hwnd=int(target.hwnd),
            pid=int(target.pid),
            focused_hwnd=int(target.focused_hwnd),
            start=len(prefix),
            end=len(full_text),
            state_chain=tuple(state_chain),
        )
        return GenericTextCaptureResult(
            True, GenericTextEditStatus.CONFIRMED, bookmark
        )

    def _restore_by_redo(
        self,
        target: TargetSnapshot,
        count: int,
        expected: GenericTextFingerprint,
    ) -> bool:
        for _ in range(max(0, int(count))):
            if not self.backend.redo(target):
                return False
        restored = self.backend.read_all(target)
        return _read_matches(restored, expected)

    def _restore_original_by_paste(
        self,
        target: TargetSnapshot,
        original_text: str,
        expected: GenericTextFingerprint,
    ) -> bool:
        attempt = self.backend.paste(original_text, target)
        if not attempt.accepted and attempt.partial_possible:
            return False
        restored = self.backend.read_all(target)
        return _read_matches(restored, expected)

    def replace_bookmarked_range(
        self,
        target: TargetSnapshot,
        bookmark: GenericRecentTextBookmark,
        original_text: str,
        replacement: str,
    ) -> GenericTextEditResult:
        original = canonicalize_field_text(original_text)
        revised = canonicalize_field_text(replacement)
        if not original or not revised or target is None or not target.is_valid:
            return GenericTextEditResult(
                False, GenericTextEditStatus.INVALID_ARGUMENT
            )
        if not self.supports_target(target):
            return GenericTextEditResult(
                False, GenericTextEditStatus.UNSUPPORTED_SURFACE
            )
        if not isinstance(bookmark, GenericRecentTextBookmark):
            return GenericTextEditResult(
                False, GenericTextEditStatus.SELECTION_UNAVAILABLE
            )
        if not _bookmark_matches_target(bookmark, target):
            return GenericTextEditResult(
                False, GenericTextEditStatus.TARGET_CHANGED
            )
        if (
            bookmark.undo_steps <= 0
            or bookmark.start < 0
            or bookmark.end < bookmark.start
            or len(bookmark.state_chain) < 2
        ):
            return GenericTextEditResult(
                False, GenericTextEditStatus.SELECTION_UNAVAILABLE
            )
        if not self.backend.target_is_current(target):
            return GenericTextEditResult(
                False, GenericTextEditStatus.TARGET_CHANGED
            )

        before = self.backend.read_all(target)
        if not before.success:
            return GenericTextEditResult(False, before.status)
        if before.empty_ambiguous:
            return GenericTextEditResult(
                False, GenericTextEditStatus.SELECTION_UNAVAILABLE
            )
        full_text = canonicalize_field_text(before.text)
        original_full_fingerprint = bookmark.state_chain[-1]
        if (
            len(full_text) > self.max_field_chars
            or fingerprint_field_text(full_text) != original_full_fingerprint
            or bookmark.end != len(full_text)
            or full_text[bookmark.start : bookmark.end] != original
        ):
            return GenericTextEditResult(
                False, GenericTextEditStatus.CONTENT_CHANGED
            )

        undone = 0
        current_text = full_text
        for state_index in range(len(bookmark.state_chain) - 2, -1, -1):
            state_before_undo = bookmark.state_chain[state_index + 1]
            expected_after_undo = bookmark.state_chain[state_index]
            if not self.backend.undo(target):
                restored = self._restore_by_redo(
                    target, undone, original_full_fingerprint
                )
                return GenericTextEditResult(
                    False,
                    (
                        GenericTextEditStatus.UNDO_UNAVAILABLE
                        if restored
                        else GenericTextEditStatus.WRITE_PARTIAL
                    ),
                    partial_possible=not restored,
                )
            undone += 1
            after_undo = self.backend.read_all(target)
            if not after_undo.success:
                # The key was accepted but its effect can no longer be read.
                # Do not guess with Ctrl+Y or paste over an unknown state.
                return GenericTextEditResult(
                    False,
                    GenericTextEditStatus.WRITE_PARTIAL,
                    partial_possible=True,
                )
            if _read_matches(
                after_undo,
                expected_after_undo,
                allow_ambiguous_empty=True,
            ):
                current_text = canonicalize_field_text(after_undo.text)
                continue

            # If Ctrl+Z was accepted but the exact field did not change, it is
            # a harmless no-op.  Do not send Ctrl+Y: that could redo an older,
            # unrelated user action.
            if _read_matches(after_undo, state_before_undo):
                restored = self._restore_by_redo(
                    target, undone - 1, original_full_fingerprint
                )
                return GenericTextEditResult(
                    False,
                    (
                        GenericTextEditStatus.UNDO_UNAVAILABLE
                        if restored
                        else GenericTextEditStatus.WRITE_PARTIAL
                    ),
                    partial_possible=not restored,
                )

            restored = self._restore_by_redo(
                target, undone, original_full_fingerprint
            )
            return GenericTextEditResult(
                False,
                (
                    GenericTextEditStatus.CONTENT_CHANGED
                    if restored
                    else GenericTextEditStatus.WRITE_PARTIAL
                ),
                partial_possible=not restored,
            )

        prefix = current_text
        if fingerprint_field_text(prefix) != bookmark.state_chain[0]:
            restored = self._restore_by_redo(
                target, undone, original_full_fingerprint
            )
            return GenericTextEditResult(
                False,
                (
                    GenericTextEditStatus.CONTENT_CHANGED
                    if restored
                    else GenericTextEditStatus.WRITE_PARTIAL
                ),
                partial_possible=not restored,
            )

        paste = self.backend.paste(replacement, target)
        after_paste = self.backend.read_all(target)
        expected_text = prefix + revised
        expected_fingerprint = fingerprint_field_text(expected_text)
        if _read_matches(after_paste, expected_fingerprint):
            replacement_bookmark = GenericRecentTextBookmark(
                hwnd=int(target.hwnd),
                pid=int(target.pid),
                focused_hwnd=int(target.focused_hwnd),
                start=len(prefix),
                end=len(expected_text),
                state_chain=(
                    fingerprint_field_text(prefix),
                    expected_fingerprint,
                ),
            )
            return GenericTextEditResult(
                True,
                GenericTextEditStatus.CONFIRMED,
                replacement_bookmark=replacement_bookmark,
            )

        # A rejected/no-op paste leaves the exactly verified prefix intact.
        # Restore only Aria's original range; never overwrite an unknown state.
        if _read_matches(
            after_paste,
            bookmark.state_chain[0],
            allow_ambiguous_empty=True,
        ):
            restored = self._restore_original_by_paste(
                target, original_text, original_full_fingerprint
            )
            return GenericTextEditResult(
                False,
                (
                    GenericTextEditStatus.WRITE_REJECTED
                    if restored
                    else GenericTextEditStatus.WRITE_PARTIAL
                ),
                partial_possible=not restored,
            )

        # The write may have landed partially or focus may have changed after
        # SendInput.  Do not paste again over an unverified field.
        return GenericTextEditResult(
            False,
            GenericTextEditStatus.WRITE_PARTIAL,
            partial_possible=True,
        )


__all__ = [
    "GENERIC_TEXT_SURFACES",
    "MAX_GENERIC_FIELD_CHARS",
    "GenericFieldReadResult",
    "GenericPasteResult",
    "GenericRecentTextBookmark",
    "GenericTextCaptureResult",
    "GenericTextEditResult",
    "GenericTextEditStatus",
    "GenericTextFieldAdapter",
    "GenericTextFingerprint",
    "WindowsGenericTextBackend",
    "canonicalize_field_text",
    "fingerprint_field_text",
]
