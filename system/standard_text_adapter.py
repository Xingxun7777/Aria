"""Native exact replacement for addressable Win32 Edit/RichEdit controls."""

import ctypes
import hashlib
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from .target_surface import (
    STANDARD_TEXT_CONTROL_CLASSES,
    SurfaceKind,
    TargetSnapshot,
)


WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
EM_GETSEL = 0x00B0
EM_SETSEL = 0x00B1
EM_REPLACESEL = 0x00C2
GWL_STYLE = -16
ES_PASSWORD = 0x0020
ES_MULTILINE = 0x0004
ES_READONLY = 0x0800
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002


class StandardTextEditStatus(str, Enum):
    CONFIRMED = "confirmed"
    TARGET_UNAVAILABLE = "target_unavailable"
    UNSUPPORTED_SURFACE = "unsupported_surface"
    UNSUPPORTED_CONTROL = "unsupported_control"
    PROTECTED_CONTROL = "protected_control"
    READ_ONLY = "read_only"
    UNDO_UNAVAILABLE = "undo_unavailable"
    ELEVATION_REQUIRED = "elevation_required"
    INVALID_ARGUMENT = "invalid_argument"
    TEXT_UNAVAILABLE = "text_unavailable"
    TEXT_TOO_LONG = "text_too_long"
    SOURCE_NOT_FOUND = "source_not_found"
    AMBIGUOUS_MATCH = "ambiguous_match"
    TOO_MANY_MATCHES = "too_many_matches"
    OVERLAPPING_MATCHES = "overlapping_matches"
    BATCH_UNSUPPORTED = "batch_unsupported"
    SELECTION_UNAVAILABLE = "selection_unavailable"
    SELECTION_CHANGED = "selection_changed"
    SELECTION_FAILED = "selection_failed"
    TARGET_CHANGED = "target_changed"
    CONTENT_CHANGED = "content_changed"
    WRITE_REJECTED = "write_rejected"
    WRITE_PARTIAL = "write_partial"


@dataclass(frozen=True)
class StandardTextCandidateToken:
    """Content-free identity for one ambiguous exact-match snapshot."""

    hwnd: int
    content_sha256: str
    ranges: tuple[tuple[int, int], ...]  # native UTF-16 offsets, document order


@dataclass(frozen=True)
class StandardTextUndoToken:
    """Content-free proof for compensating one confirmed Aria transaction."""

    hwnd: int
    before_sha256: str
    after_sha256: str
    start: int
    end: int  # native UTF-16 range containing the replacement after the edit
    ranges: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class StandardTextEditResult:
    success: bool
    status: StandardTextEditStatus
    match_count: int = 0
    partial_possible: bool = False
    undo_available: bool = False
    candidate_token: Optional[StandardTextCandidateToken] = None
    undo_token: Optional[StandardTextUndoToken] = None


@dataclass(frozen=True)
class StandardTextSelectionBookmark:
    """Content-free native selection identity for one guarded replacement."""

    hwnd: int
    start: int
    end: int
    # Optional full-control hash captured after an Aria insertion.  It lets a
    # later edit distinguish a still-valid numeric offset from an unrelated
    # external edit that shifted/reused that offset without storing text.
    content_sha256: str = ""


@dataclass(frozen=True)
class StandardTextSelectionCapture:
    success: bool
    status: StandardTextEditStatus
    bookmark: Optional[StandardTextSelectionBookmark] = None


@dataclass(frozen=True)
class TextReadResult:
    success: bool
    text: str = ""
    status: StandardTextEditStatus = StandardTextEditStatus.TEXT_UNAVAILABLE


class Win32StandardTextBackend:
    """Bounded Win32 messaging; no UI Automation or clipboard side effects."""

    MAX_TEXT_UNITS = 60000
    MESSAGE_TIMEOUT_MS = 250

    def __init__(self, api) -> None:
        self.api = api

    def _send(self, hwnd: int, message: int, wparam: int, lparam: int):
        result = ctypes.c_size_t(0)
        try:
            ok = self.api.SendMessageTimeoutW(
                hwnd,
                message,
                wparam,
                lparam,
                SMTO_BLOCK | SMTO_ABORTIFHUNG,
                self.MESSAGE_TIMEOUT_MS,
                ctypes.byref(result),
            )
        except Exception:
            return False, 0
        return bool(ok), int(result.value)

    def get_class_name(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        try:
            copied = int(self.api.GetClassNameW(hwnd, buffer, 256) or 0)
        except Exception:
            return ""
        return buffer.value.lower() if copied > 0 else ""

    def get_style(self, hwnd: int) -> Optional[int]:
        try:
            return int(self.api.GetWindowLongW(hwnd, GWL_STYLE))
        except Exception:
            return None

    def read_text(self, hwnd: int) -> TextReadResult:
        ok, length = self._send(hwnd, WM_GETTEXTLENGTH, 0, 0)
        if not ok or length < 0:
            return TextReadResult(False)
        if length > self.MAX_TEXT_UNITS:
            return TextReadResult(
                False, status=StandardTextEditStatus.TEXT_TOO_LONG
            )

        buffer = ctypes.create_unicode_buffer(length + 1)
        ok, copied = self._send(
            hwnd,
            WM_GETTEXT,
            length + 1,
            ctypes.addressof(buffer),
        )
        if not ok or copied < 0 or copied > length:
            return TextReadResult(False)
        return TextReadResult(
            True,
            text=buffer.value,
            status=StandardTextEditStatus.CONFIRMED,
        )

    def get_selection(self, hwnd: int) -> Optional[tuple[int, int]]:
        start = wintypes.DWORD(0)
        end = wintypes.DWORD(0)
        ok, _ = self._send(
            hwnd,
            EM_GETSEL,
            ctypes.addressof(start),
            ctypes.addressof(end),
        )
        if not ok:
            return None
        return int(start.value), int(end.value)

    def set_selection(self, hwnd: int, start: int, end: int) -> bool:
        ok, _ = self._send(hwnd, EM_SETSEL, int(start), int(end))
        return ok

    def replace_selection(self, hwnd: int, replacement: str) -> bool:
        buffer = ctypes.create_unicode_buffer(replacement)
        ok, _ = self._send(
            hwnd,
            EM_REPLACESEL,
            1,
            ctypes.addressof(buffer),
        )
        return ok


def _find_all(text: str, source: str) -> list[int]:
    positions = []
    start = 0
    while True:
        index = text.find(source, start)
        if index < 0:
            return positions
        positions.append(index)
        start = index + 1


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _content_sha256(text: str) -> str:
    # Hash the native UTF-16 representation so even an unusual control value
    # cannot drift through a UTF-8 normalization/encoding path.
    return hashlib.sha256(
        text.encode("utf-16-le", errors="surrogatepass")
    ).hexdigest()


def _build_undo_token(
    hwnd: int,
    before_text: str,
    after_text: str,
    start: int,
    replacement: str,
) -> StandardTextUndoToken:
    end = int(start) + _utf16_units(replacement)
    return StandardTextUndoToken(
        hwnd=int(hwnd),
        before_sha256=_content_sha256(before_text),
        after_sha256=_content_sha256(after_text),
        start=int(start),
        end=end,
        ranges=((int(start), end),),
    )


def _build_replacement_plan(
    text: str,
    source: str,
    replacement: str,
    matches: list[int],
) -> tuple[str, str, tuple[int, int], tuple[tuple[int, int], ...]]:
    """Build one non-overlapping exact-replacement transaction."""

    parts: list[str] = []
    ranges: list[tuple[int, int]] = []
    cursor = 0
    native_cursor = 0
    replacement_units = _utf16_units(replacement)
    for position in matches:
        unchanged = text[cursor:position]
        parts.append(unchanged)
        native_cursor += _utf16_units(unchanged)
        start = native_cursor
        parts.append(replacement)
        native_cursor += replacement_units
        ranges.append((start, native_cursor))
        cursor = position + len(source)
    parts.append(text[cursor:])
    expected_text = "".join(parts)

    before_start = _utf16_units(text[: matches[0]])
    before_end = _utf16_units(text[: matches[-1] + len(source)])
    after_span = _split_utf16_range(
        expected_text, ranges[0][0], ranges[-1][1]
    )
    if after_span is None:
        raise ValueError("replacement plan produced an invalid UTF-16 span")
    return expected_text, after_span[1], (before_start, before_end), tuple(ranges)


def _restore_batch_span(
    edited_text: str,
    ranges: tuple[tuple[int, int], ...],
    source: str,
) -> Optional[str]:
    """Reconstruct the original transaction span from exact edited ranges."""

    if not ranges:
        return None
    encoded = edited_text.encode("utf-16-le")
    cursor = ranges[0][0]
    end = ranges[-1][1]
    parts: list[bytes] = []
    source_bytes = source.encode("utf-16-le")
    for start, range_end in ranges:
        if start < cursor or range_end < start or range_end * 2 > len(encoded):
            return None
        parts.append(encoded[cursor * 2 : start * 2])
        parts.append(source_bytes)
        cursor = range_end
    parts.append(encoded[cursor * 2 : end * 2])
    try:
        return b"".join(parts).decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def _split_utf16_range(
    text: str, start: int, end: int
) -> Optional[tuple[str, str, str]]:
    """Split a Python string at native UTF-16 offsets without surrogate drift."""
    if start < 0 or end < start:
        return None
    encoded = text.encode("utf-16-le")
    if end * 2 > len(encoded):
        return None
    try:
        return (
            encoded[: start * 2].decode("utf-16-le"),
            encoded[start * 2 : end * 2].decode("utf-16-le"),
            encoded[end * 2 :].decode("utf-16-le"),
        )
    except UnicodeDecodeError:
        # A range boundary in the middle of a surrogate pair is not a valid
        # addressable selection and must never be rounded or guessed.
        return None


class StandardTextAdapter:
    """Apply exact, addressable native edits with commit-edge verification."""

    MAX_NUMBERED_CANDIDATES = 20
    MAX_BATCH_MATCHES = 20

    def __init__(
        self,
        backend: Win32StandardTextBackend,
        *,
        target_is_current: Callable[[TargetSnapshot], bool],
    ) -> None:
        self.backend = backend
        self.target_is_current = target_is_current

    def _target_current(self, expected_target: TargetSnapshot) -> bool:
        try:
            return bool(self.target_is_current(expected_target))
        except Exception:
            return False

    def _restore_selection(
        self,
        expected_target: TargetSnapshot,
        control_hwnd: int,
        selection: tuple[int, int],
    ) -> None:
        if not self._target_current(expected_target):
            return
        try:
            self.backend.set_selection(control_hwnd, *selection)
        except Exception:
            pass

    def _editable_control_status(
        self, expected_target: TargetSnapshot
    ) -> tuple[Optional[int], Optional[StandardTextEditStatus]]:
        if (
            expected_target is None
            or not expected_target.is_valid
            or expected_target.focused_hwnd <= 0
        ):
            return None, StandardTextEditStatus.TARGET_UNAVAILABLE
        if (
            expected_target.profile.kind != SurfaceKind.STANDARD_TEXT
            or not expected_target.profile.addressable_text
        ):
            return None, StandardTextEditStatus.UNSUPPORTED_SURFACE
        if not self._target_current(expected_target):
            return None, StandardTextEditStatus.TARGET_CHANGED

        control_hwnd = int(expected_target.focused_hwnd)
        control_class = self.backend.get_class_name(control_hwnd)
        if control_class not in STANDARD_TEXT_CONTROL_CLASSES:
            return None, StandardTextEditStatus.UNSUPPORTED_CONTROL
        style = self.backend.get_style(control_hwnd)
        if style is None:
            return None, StandardTextEditStatus.UNSUPPORTED_CONTROL
        if style & ES_PASSWORD:
            return None, StandardTextEditStatus.PROTECTED_CONTROL
        if style & ES_READONLY:
            return None, StandardTextEditStatus.READ_ONLY
        if control_class == "edit" and style & ES_MULTILINE:
            # The legacy multiline Edit control records EM_REPLACESEL as an
            # insertion-only undo: one Ctrl+Z removes the replacement but does
            # not restore the selected source. RichEdit and single-line Edit
            # provide the complete replacement undo contract we require.
            return None, StandardTextEditStatus.UNDO_UNAVAILABLE
        return control_hwnd, None

    def capture_selection(
        self,
        expected_target: TargetSnapshot,
        expected_text: str,
    ) -> StandardTextSelectionCapture:
        """Capture and verify the exact native range copied by Ctrl+C."""
        if not expected_text or "\x00" in expected_text:
            return StandardTextSelectionCapture(
                False, StandardTextEditStatus.INVALID_ARGUMENT
            )
        control_hwnd, failure = self._editable_control_status(expected_target)
        if failure is not None:
            return StandardTextSelectionCapture(False, failure)
        assert control_hwnd is not None

        original_read = self.backend.read_text(control_hwnd)
        if not original_read.success:
            return StandardTextSelectionCapture(False, original_read.status)
        selection = self.backend.get_selection(control_hwnd)
        if selection is None or selection[0] == selection[1]:
            return StandardTextSelectionCapture(
                False, StandardTextEditStatus.SELECTION_UNAVAILABLE
            )
        split = _split_utf16_range(original_read.text, *selection)
        if split is None or split[1] != expected_text:
            return StandardTextSelectionCapture(
                False, StandardTextEditStatus.SELECTION_CHANGED
            )
        if not self._target_current(expected_target):
            return StandardTextSelectionCapture(
                False, StandardTextEditStatus.TARGET_CHANGED
            )
        return StandardTextSelectionCapture(
            True,
            StandardTextEditStatus.CONFIRMED,
            StandardTextSelectionBookmark(
                hwnd=control_hwnd,
                start=int(selection[0]),
                end=int(selection[1]),
            ),
        )

    def capture_recent_insert(
        self,
        expected_target: TargetSnapshot,
        inserted_text: str,
    ) -> StandardTextSelectionCapture:
        """Capture the exact range immediately before the post-insert caret."""

        if not inserted_text or "\x00" in inserted_text:
            return StandardTextSelectionCapture(
                False, StandardTextEditStatus.INVALID_ARGUMENT
            )
        control_hwnd, failure = self._editable_control_status(expected_target)
        if failure is not None:
            return StandardTextSelectionCapture(False, failure)
        assert control_hwnd is not None

        current = self.backend.read_text(control_hwnd)
        if not current.success:
            return StandardTextSelectionCapture(False, current.status)
        selection = self.backend.get_selection(control_hwnd)
        if selection is None or selection[0] != selection[1]:
            return StandardTextSelectionCapture(
                False, StandardTextEditStatus.SELECTION_UNAVAILABLE
            )
        end = int(selection[1])
        start = end - _utf16_units(inserted_text)
        split = _split_utf16_range(current.text, start, end)
        if start < 0 or split is None or split[1] != inserted_text:
            return StandardTextSelectionCapture(
                False, StandardTextEditStatus.CONTENT_CHANGED
            )
        if not self._target_current(expected_target):
            return StandardTextSelectionCapture(
                False, StandardTextEditStatus.TARGET_CHANGED
            )
        return StandardTextSelectionCapture(
            True,
            StandardTextEditStatus.CONFIRMED,
            StandardTextSelectionBookmark(
                control_hwnd,
                start,
                end,
                content_sha256=_content_sha256(current.text),
            ),
        )

    def replace_bookmarked_range(
        self,
        expected_target: TargetSnapshot,
        bookmark: StandardTextSelectionBookmark,
        expected_text: str,
        replacement: str,
    ) -> StandardTextEditResult:
        """Replace a still-identical recent range even if the caret has moved.

        If edits elsewhere shifted the native offsets, one unique exact match
        may relocate the bookmark.  Ambiguous matches remain fail-closed.
        """

        if (
            not isinstance(bookmark, StandardTextSelectionBookmark)
            or not expected_text
            or not replacement
            or expected_text == replacement
            or "\x00" in (expected_text + replacement)
        ):
            return StandardTextEditResult(
                False, StandardTextEditStatus.INVALID_ARGUMENT
            )
        control_hwnd, failure = self._editable_control_status(expected_target)
        if failure is not None:
            return StandardTextEditResult(False, failure)
        assert control_hwnd is not None
        if int(bookmark.hwnd) != control_hwnd:
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )

        original_read = self.backend.read_text(control_hwnd)
        if not original_read.success:
            return StandardTextEditResult(False, original_read.status)
        source_range = (int(bookmark.start), int(bookmark.end))
        split = _split_utf16_range(original_read.text, *source_range)
        offset_is_trusted = (
            not bookmark.content_sha256
            or bookmark.content_sha256 == _content_sha256(original_read.text)
        )
        if not offset_is_trusted or split is None or split[1] != expected_text:
            matches = _find_all(original_read.text, expected_text)
            if len(matches) != 1:
                return StandardTextEditResult(
                    False,
                    StandardTextEditStatus.CONTENT_CHANGED,
                    match_count=len(matches),
                )
            position = matches[0]
            source_range = (
                _utf16_units(original_read.text[:position]),
                _utf16_units(original_read.text[: position + len(expected_text)]),
            )
            split = _split_utf16_range(original_read.text, *source_range)
        if split is None or split[1] != expected_text:
            return StandardTextEditResult(
                False, StandardTextEditStatus.CONTENT_CHANGED
            )
        if not self._target_current(expected_target):
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )

        if not self.backend.set_selection(control_hwnd, *source_range):
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_FAILED
            )
        if self.backend.get_selection(control_hwnd) != source_range:
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_FAILED
            )
        commit_read = self.backend.read_text(control_hwnd)
        if not commit_read.success or commit_read.text != original_read.text:
            return StandardTextEditResult(
                False, StandardTextEditStatus.CONTENT_CHANGED
            )
        commit_split = _split_utf16_range(commit_read.text, *source_range)
        if commit_split is None or commit_split[1] != expected_text:
            return StandardTextEditResult(
                False, StandardTextEditStatus.CONTENT_CHANGED
            )
        if not self._target_current(expected_target):
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )
        if self.backend.get_selection(control_hwnd) != source_range:
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_CHANGED
            )

        if not self.backend.replace_selection(control_hwnd, replacement):
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.WRITE_PARTIAL,
                partial_possible=True,
                undo_available=True,
            )
        expected_full_text = split[0] + replacement + split[2]
        post_read = self.backend.read_text(control_hwnd)
        if post_read.success and post_read.text == expected_full_text:
            return StandardTextEditResult(
                True,
                StandardTextEditStatus.CONFIRMED,
                match_count=1,
                undo_available=True,
                undo_token=_build_undo_token(
                    control_hwnd,
                    original_read.text,
                    expected_full_text,
                    source_range[0],
                    replacement,
                ),
            )
        if post_read.success and post_read.text == original_read.text:
            return StandardTextEditResult(
                False, StandardTextEditStatus.WRITE_REJECTED
            )
        return StandardTextEditResult(
            False,
            StandardTextEditStatus.WRITE_PARTIAL,
            partial_possible=True,
            undo_available=True,
        )

    def replace_captured_selection(
        self,
        expected_target: TargetSnapshot,
        bookmark: StandardTextSelectionBookmark,
        expected_text: str,
        replacement: str,
    ) -> StandardTextEditResult:
        """Replace only the still-current, still-identical captured selection."""
        if (
            not isinstance(bookmark, StandardTextSelectionBookmark)
            or not expected_text
            or not replacement
            or expected_text == replacement
            or "\x00" in (expected_text + replacement)
        ):
            return StandardTextEditResult(
                False, StandardTextEditStatus.INVALID_ARGUMENT
            )
        control_hwnd, failure = self._editable_control_status(expected_target)
        if failure is not None:
            return StandardTextEditResult(False, failure)
        assert control_hwnd is not None
        if bookmark.hwnd != control_hwnd:
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )

        original_read = self.backend.read_text(control_hwnd)
        if not original_read.success:
            return StandardTextEditResult(False, original_read.status)
        expected_selection = (int(bookmark.start), int(bookmark.end))
        if self.backend.get_selection(control_hwnd) != expected_selection:
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_CHANGED
            )
        split = _split_utf16_range(original_read.text, *expected_selection)
        if split is None or split[1] != expected_text:
            return StandardTextEditResult(
                False, StandardTextEditStatus.CONTENT_CHANGED
            )
        if not self._target_current(expected_target):
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )

        # Reassert and verify the bookmark at the commit edge. This is cheap
        # for a native Edit/RichEdit control and closes the long LLM wait.
        if not self.backend.set_selection(control_hwnd, *expected_selection):
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_FAILED
            )
        if self.backend.get_selection(control_hwnd) != expected_selection:
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_FAILED
            )
        commit_read = self.backend.read_text(control_hwnd)
        if not commit_read.success or commit_read.text != original_read.text:
            return StandardTextEditResult(
                False, StandardTextEditStatus.CONTENT_CHANGED
            )
        if not self._target_current(expected_target):
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )
        if self.backend.get_selection(control_hwnd) != expected_selection:
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_CHANGED
            )

        if not self.backend.replace_selection(control_hwnd, replacement):
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.WRITE_PARTIAL,
                partial_possible=True,
                undo_available=True,
            )
        expected_full_text = split[0] + replacement + split[2]
        post_read = self.backend.read_text(control_hwnd)
        if post_read.success and post_read.text == expected_full_text:
            return StandardTextEditResult(
                True,
                StandardTextEditStatus.CONFIRMED,
                undo_available=True,
                undo_token=_build_undo_token(
                    control_hwnd,
                    original_read.text,
                    expected_full_text,
                    expected_selection[0],
                    replacement,
                ),
            )
        if post_read.success and post_read.text == original_read.text:
            return StandardTextEditResult(
                False, StandardTextEditStatus.WRITE_REJECTED
            )
        return StandardTextEditResult(
            False,
            StandardTextEditStatus.WRITE_PARTIAL,
            partial_possible=True,
            undo_available=True,
        )

    def replace_all(
        self,
        expected_target: TargetSnapshot,
        source: str,
        replacement: str,
    ) -> StandardTextEditResult:
        """Replace every non-overlapping exact match in one native mutation."""

        if (
            not source
            or not replacement
            or source == replacement
            or "\x00" in (source + replacement)
        ):
            return StandardTextEditResult(
                False, StandardTextEditStatus.INVALID_ARGUMENT
            )
        control_hwnd, failure = self._editable_control_status(expected_target)
        if failure is not None:
            return StandardTextEditResult(False, failure)
        assert control_hwnd is not None

        original_read = self.backend.read_text(control_hwnd)
        if not original_read.success:
            return StandardTextEditResult(False, original_read.status)
        original_text = original_read.text
        matches = _find_all(original_text, source)
        match_count = len(matches)
        if not matches:
            return StandardTextEditResult(
                False, StandardTextEditStatus.SOURCE_NOT_FOUND
            )
        if match_count > self.MAX_BATCH_MATCHES:
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.TOO_MANY_MATCHES,
                match_count=match_count,
            )
        if any(
            current < previous + len(source)
            for previous, current in zip(matches, matches[1:])
        ):
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.OVERLAPPING_MATCHES,
                match_count=match_count,
            )
        control_class = self.backend.get_class_name(control_hwnd)
        if match_count > 1 and control_class != "edit":
            # Replacing a span across several RichEdit runs can flatten the
            # formatting between matches. Keep the first vertical slice plain
            # Edit-only until a native TOM undo group is available and tested.
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.BATCH_UNSUPPORTED,
                match_count=match_count,
            )

        expected_text, replacement_span, before_span, after_ranges = (
            _build_replacement_plan(
                original_text, source, replacement, matches
            )
        )
        original_selection = self.backend.get_selection(control_hwnd)
        if original_selection is None:
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.SELECTION_UNAVAILABLE,
                match_count=match_count,
            )
        if not self._target_current(expected_target):
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.TARGET_CHANGED,
                match_count=match_count,
            )
        if not self.backend.set_selection(control_hwnd, *before_span):
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.SELECTION_FAILED,
                match_count=match_count,
            )
        if self.backend.get_selection(control_hwnd) != before_span:
            self._restore_selection(
                expected_target, control_hwnd, original_selection
            )
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.SELECTION_FAILED,
                match_count=match_count,
            )

        commit_read = self.backend.read_text(control_hwnd)
        if not commit_read.success or commit_read.text != original_text:
            self._restore_selection(
                expected_target, control_hwnd, original_selection
            )
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.CONTENT_CHANGED,
                match_count=match_count,
            )
        if not self._target_current(expected_target):
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.TARGET_CHANGED,
                match_count=match_count,
            )
        if self.backend.get_selection(control_hwnd) != before_span:
            self._restore_selection(
                expected_target, control_hwnd, original_selection
            )
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.SELECTION_CHANGED,
                match_count=match_count,
            )

        if not self.backend.replace_selection(control_hwnd, replacement_span):
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.WRITE_PARTIAL,
                match_count=match_count,
                partial_possible=True,
                undo_available=True,
            )
        post_read = self.backend.read_text(control_hwnd)
        if post_read.success and post_read.text == expected_text:
            return StandardTextEditResult(
                True,
                StandardTextEditStatus.CONFIRMED,
                match_count=match_count,
                undo_available=True,
                undo_token=StandardTextUndoToken(
                    hwnd=control_hwnd,
                    before_sha256=_content_sha256(original_text),
                    after_sha256=_content_sha256(expected_text),
                    start=after_ranges[0][0],
                    end=after_ranges[-1][1],
                    ranges=after_ranges,
                ),
            )
        if post_read.success and post_read.text == original_text:
            self._restore_selection(
                expected_target, control_hwnd, original_selection
            )
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.WRITE_REJECTED,
                match_count=match_count,
            )
        return StandardTextEditResult(
            False,
            StandardTextEditStatus.WRITE_PARTIAL,
            match_count=match_count,
            partial_possible=True,
            undo_available=True,
        )

    def replace_unique(
        self,
        expected_target: TargetSnapshot,
        source: str,
        replacement: str,
    ) -> StandardTextEditResult:
        if (
            expected_target is None
            or not expected_target.is_valid
            or expected_target.focused_hwnd <= 0
        ):
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_UNAVAILABLE
            )
        if (
            expected_target.profile.kind != SurfaceKind.STANDARD_TEXT
            or not expected_target.profile.addressable_text
        ):
            return StandardTextEditResult(
                False, StandardTextEditStatus.UNSUPPORTED_SURFACE
            )
        if not source or not replacement or source == replacement or "\x00" in (
            source + replacement
        ):
            return StandardTextEditResult(
                False, StandardTextEditStatus.INVALID_ARGUMENT
            )
        if not self._target_current(expected_target):
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )

        control_hwnd = int(expected_target.focused_hwnd)
        control_class = self.backend.get_class_name(control_hwnd)
        if control_class not in STANDARD_TEXT_CONTROL_CLASSES:
            return StandardTextEditResult(
                False, StandardTextEditStatus.UNSUPPORTED_CONTROL
            )
        style = self.backend.get_style(control_hwnd)
        if style is None:
            return StandardTextEditResult(
                False, StandardTextEditStatus.UNSUPPORTED_CONTROL
            )
        if style & ES_PASSWORD:
            return StandardTextEditResult(
                False, StandardTextEditStatus.PROTECTED_CONTROL
            )
        if style & ES_READONLY:
            return StandardTextEditResult(False, StandardTextEditStatus.READ_ONLY)
        if control_class == "edit" and style & ES_MULTILINE:
            return StandardTextEditResult(
                False, StandardTextEditStatus.UNDO_UNAVAILABLE
            )

        original_read = self.backend.read_text(control_hwnd)
        if not original_read.success:
            return StandardTextEditResult(False, original_read.status)
        original_text = original_read.text
        matches = _find_all(original_text, source)
        if not matches:
            return StandardTextEditResult(
                False, StandardTextEditStatus.SOURCE_NOT_FOUND
            )
        if len(matches) != 1:
            token = None
            if len(matches) <= self.MAX_NUMBERED_CANDIDATES:
                ranges = tuple(
                    (
                        _utf16_units(original_text[:position]),
                        _utf16_units(original_text[: position + len(source)]),
                    )
                    for position in matches
                )
                token = StandardTextCandidateToken(
                    hwnd=control_hwnd,
                    content_sha256=_content_sha256(original_text),
                    ranges=ranges,
                )
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.AMBIGUOUS_MATCH,
                match_count=len(matches),
                candidate_token=token,
            )

        original_selection = self.backend.get_selection(control_hwnd)
        if original_selection is None:
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.SELECTION_UNAVAILABLE,
                match_count=1,
            )

        python_start = matches[0]
        python_end = python_start + len(source)
        selection_start = _utf16_units(original_text[:python_start])
        selection_end = _utf16_units(original_text[:python_end])
        if not self._target_current(expected_target):
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED, match_count=1
            )
        if not self.backend.set_selection(
            control_hwnd, selection_start, selection_end
        ):
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_FAILED, match_count=1
            )
        if self.backend.get_selection(control_hwnd) != (
            selection_start,
            selection_end,
        ):
            self._restore_selection(
                expected_target, control_hwnd, original_selection
            )
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_FAILED, match_count=1
            )

        # Re-read after changing the selection. This catches edits made by the
        # target application while Aria was calculating UTF-16 positions.
        current_read = self.backend.read_text(control_hwnd)
        if not current_read.success or current_read.text != original_text:
            return StandardTextEditResult(
                False, StandardTextEditStatus.CONTENT_CHANGED, match_count=1
            )
        if not self._target_current(expected_target):
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED, match_count=1
            )

        if not self.backend.replace_selection(control_hwnd, replacement):
            # The timeout/API failed at the mutation edge. The control may have
            # handled the message before the caller learned it timed out.
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.WRITE_PARTIAL,
                match_count=1,
                partial_possible=True,
                undo_available=True,
            )

        expected_text = (
            original_text[:python_start]
            + replacement
            + original_text[python_end:]
        )
        post_read = self.backend.read_text(control_hwnd)
        if post_read.success and post_read.text == expected_text:
            return StandardTextEditResult(
                True,
                StandardTextEditStatus.CONFIRMED,
                match_count=1,
                undo_available=True,
                undo_token=_build_undo_token(
                    control_hwnd,
                    original_text,
                    expected_text,
                    selection_start,
                    replacement,
                ),
            )
        if post_read.success and post_read.text == original_text:
            self._restore_selection(
                expected_target, control_hwnd, original_selection
            )
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.WRITE_REJECTED,
                match_count=1,
            )
        return StandardTextEditResult(
            False,
            StandardTextEditStatus.WRITE_PARTIAL,
            match_count=1,
            partial_possible=True,
            undo_available=True,
        )

    def replace_candidate(
        self,
        expected_target: TargetSnapshot,
        token: StandardTextCandidateToken,
        occurrence: int,
        source: str,
        replacement: str,
    ) -> StandardTextEditResult:
        """Replace one numbered match from a still-identical earlier snapshot."""

        if (
            not isinstance(token, StandardTextCandidateToken)
            or not source
            or not replacement
            or source == replacement
            or "\x00" in (source + replacement)
            or not isinstance(occurrence, int)
            or occurrence < 1
            or occurrence > len(token.ranges)
        ):
            return StandardTextEditResult(
                False, StandardTextEditStatus.INVALID_ARGUMENT
            )
        control_hwnd, failure = self._editable_control_status(expected_target)
        if failure is not None:
            return StandardTextEditResult(False, failure)
        assert control_hwnd is not None
        if token.hwnd != control_hwnd:
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )

        original_read = self.backend.read_text(control_hwnd)
        if not original_read.success:
            return StandardTextEditResult(False, original_read.status)
        if _content_sha256(original_read.text) != token.content_sha256:
            return StandardTextEditResult(
                False, StandardTextEditStatus.CONTENT_CHANGED
            )
        native_range = token.ranges[occurrence - 1]
        split = _split_utf16_range(original_read.text, *native_range)
        if split is None or split[1] != source:
            return StandardTextEditResult(
                False, StandardTextEditStatus.CONTENT_CHANGED
            )
        original_selection = self.backend.get_selection(control_hwnd)
        if original_selection is None:
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_UNAVAILABLE
            )
        if not self._target_current(expected_target):
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )
        if not self.backend.set_selection(control_hwnd, *native_range):
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_FAILED
            )
        if self.backend.get_selection(control_hwnd) != native_range:
            self._restore_selection(expected_target, control_hwnd, original_selection)
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_FAILED
            )

        # Commit-edge proof: target, full content hash and native selection all
        # still identify the exact numbered occurrence from the first command.
        commit_read = self.backend.read_text(control_hwnd)
        if (
            not commit_read.success
            or _content_sha256(commit_read.text) != token.content_sha256
        ):
            self._restore_selection(expected_target, control_hwnd, original_selection)
            return StandardTextEditResult(
                False, StandardTextEditStatus.CONTENT_CHANGED
            )
        if not self._target_current(expected_target):
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )
        if self.backend.get_selection(control_hwnd) != native_range:
            self._restore_selection(expected_target, control_hwnd, original_selection)
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_CHANGED
            )

        if not self.backend.replace_selection(control_hwnd, replacement):
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.WRITE_PARTIAL,
                match_count=len(token.ranges),
                partial_possible=True,
                undo_available=True,
            )
        expected_text = split[0] + replacement + split[2]
        post_read = self.backend.read_text(control_hwnd)
        if post_read.success and post_read.text == expected_text:
            return StandardTextEditResult(
                True,
                StandardTextEditStatus.CONFIRMED,
                match_count=len(token.ranges),
                undo_available=True,
                undo_token=_build_undo_token(
                    control_hwnd,
                    original_read.text,
                    expected_text,
                    native_range[0],
                    replacement,
                ),
            )
        if post_read.success and post_read.text == original_read.text:
            self._restore_selection(expected_target, control_hwnd, original_selection)
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.WRITE_REJECTED,
                match_count=len(token.ranges),
            )
        return StandardTextEditResult(
            False,
            StandardTextEditStatus.WRITE_PARTIAL,
            match_count=len(token.ranges),
            partial_possible=True,
            undo_available=True,
        )

    def revert_edit(
        self,
        expected_target: TargetSnapshot,
        token: StandardTextUndoToken,
        source: str,
        replacement: str,
    ) -> StandardTextEditResult:
        """Compensate one confirmed edit only while its exact result remains."""

        if (
            not isinstance(token, StandardTextUndoToken)
            or not source
            or not replacement
            or source == replacement
            or "\x00" in (source + replacement)
            or token.start < 0
            or token.end < token.start
        ):
            return StandardTextEditResult(
                False, StandardTextEditStatus.INVALID_ARGUMENT
            )
        control_hwnd, failure = self._editable_control_status(expected_target)
        if failure is not None:
            return StandardTextEditResult(False, failure)
        assert control_hwnd is not None
        if token.hwnd != control_hwnd:
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )

        edited_read = self.backend.read_text(control_hwnd)
        if not edited_read.success:
            return StandardTextEditResult(False, edited_read.status)
        if _content_sha256(edited_read.text) != token.after_sha256:
            return StandardTextEditResult(
                False, StandardTextEditStatus.CONTENT_CHANGED
            )
        edited_ranges = token.ranges or ((token.start, token.end),)
        if (
            token.start != edited_ranges[0][0]
            or token.end != edited_ranges[-1][1]
            or any(
                start < token.start
                or end > token.end
                or end < start
                or (index > 0 and start < edited_ranges[index - 1][1])
                for index, (start, end) in enumerate(edited_ranges)
            )
        ):
            return StandardTextEditResult(
                False, StandardTextEditStatus.INVALID_ARGUMENT
            )
        for native_range in edited_ranges:
            range_split = _split_utf16_range(edited_read.text, *native_range)
            if range_split is None or range_split[1] != replacement:
                return StandardTextEditResult(
                    False, StandardTextEditStatus.CONTENT_CHANGED
                )
        source_span = _restore_batch_span(
            edited_read.text, edited_ranges, source
        )
        if source_span is None:
            return StandardTextEditResult(
                False, StandardTextEditStatus.INVALID_ARGUMENT
            )
        edited_range = (token.start, token.end)
        split = _split_utf16_range(edited_read.text, *edited_range)
        if split is None:
            return StandardTextEditResult(
                False, StandardTextEditStatus.CONTENT_CHANGED
            )
        original_selection = self.backend.get_selection(control_hwnd)
        if original_selection is None:
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_UNAVAILABLE
            )
        if not self._target_current(expected_target):
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )
        if not self.backend.set_selection(control_hwnd, *edited_range):
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_FAILED
            )
        if self.backend.get_selection(control_hwnd) != edited_range:
            self._restore_selection(expected_target, control_hwnd, original_selection)
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_FAILED
            )

        commit_read = self.backend.read_text(control_hwnd)
        if (
            not commit_read.success
            or _content_sha256(commit_read.text) != token.after_sha256
        ):
            self._restore_selection(expected_target, control_hwnd, original_selection)
            return StandardTextEditResult(
                False, StandardTextEditStatus.CONTENT_CHANGED
            )
        if not self._target_current(expected_target):
            return StandardTextEditResult(
                False, StandardTextEditStatus.TARGET_CHANGED
            )
        if self.backend.get_selection(control_hwnd) != edited_range:
            self._restore_selection(expected_target, control_hwnd, original_selection)
            return StandardTextEditResult(
                False, StandardTextEditStatus.SELECTION_CHANGED
            )

        if not self.backend.replace_selection(control_hwnd, source_span):
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.WRITE_PARTIAL,
                match_count=len(edited_ranges),
                partial_possible=True,
                undo_available=True,
            )
        expected_text = split[0] + source_span + split[2]
        post_read = self.backend.read_text(control_hwnd)
        if (
            post_read.success
            and post_read.text == expected_text
            and _content_sha256(post_read.text) == token.before_sha256
        ):
            return StandardTextEditResult(
                True,
                StandardTextEditStatus.CONFIRMED,
                match_count=len(edited_ranges),
                undo_available=True,
            )
        if post_read.success and post_read.text == edited_read.text:
            self._restore_selection(expected_target, control_hwnd, original_selection)
            return StandardTextEditResult(
                False,
                StandardTextEditStatus.WRITE_REJECTED,
                match_count=len(edited_ranges),
            )
        return StandardTextEditResult(
            False,
            StandardTextEditStatus.WRITE_PARTIAL,
            match_count=len(edited_ranges),
            partial_possible=True,
            undo_available=True,
        )
