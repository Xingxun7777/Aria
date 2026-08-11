"""Fail-closed native Microsoft Word text delivery.

The adapter attaches only to an already-running Word instance. It never uses
``Dispatch``/``EnsureDispatch`` and therefore cannot launch Word or create a
document as a side effect. All results are content-free so callers can expose
recovery state without logging document names, paths, selection text, or the
dictated text.
"""

from dataclasses import dataclass
from enum import Enum
import ctypes
from ctypes import wintypes
import hashlib
from typing import Any, Callable, Optional

from .target_surface import TargetSnapshot, supports_word_compatible_ranges


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND


class WordInsertStatus(str, Enum):
    INSERTED = "inserted"
    COM_UNAVAILABLE = "com_unavailable"
    BOOKMARK_UNAVAILABLE = "bookmark_unavailable"
    TARGET_CHANGED = "target_changed"
    WINDOW_MISMATCH = "window_mismatch"
    PROTECTED_VIEW = "protected_view"
    DOCUMENT_READ_ONLY = "document_read_only"
    DOCUMENT_PROTECTED = "document_protected"
    NON_MAIN_STORY = "non_main_story"
    UNSAFE_SELECTION = "unsafe_selection"
    SELECTION_CHANGED = "selection_changed"
    CONTENT_CONTROL_LOCKED = "content_control_locked"
    UNDO_UNAVAILABLE = "undo_unavailable"
    UNDO_BUSY = "undo_busy"
    UNDO_START_FAILED = "undo_start_failed"
    INSERT_FAILED = "insert_failed"
    VERIFY_FAILED = "verify_failed"


@dataclass(frozen=True)
class WordInsertResult:
    success: bool
    status: WordInsertStatus
    safe_to_fallback: bool
    partial_possible: bool = False
    verified: bool = False
    track_revisions: bool = False


@dataclass(frozen=True)
class WordBookmark:
    """Content-free selection identity captured at the ASR commit boundary."""

    hwnd: int
    start: int
    end: int
    story_type: int
    # Hash of a bounded local context around a post-insert range.  No document
    # text, title or path is retained.  Pre-insert selection bookmarks leave it
    # empty because their identity is checked immediately by insert_text().
    context_sha256: str = ""


def normalize_word_text(text: str) -> str:
    """Convert platform line endings to Word paragraph marks."""
    value = str(text or "")
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r")


class WordAdapter:
    """Insert into the active main-document selection as one undo record."""

    WD_NO_PROTECTION = -1
    WD_MAIN_TEXT_STORY = 1

    def __init__(
        self,
        *,
        get_active_object: Optional[Callable[[], Any]] = None,
        get_root_window: Optional[Callable[[int], int]] = None,
        co_initialize: Optional[Callable[[], None]] = None,
        co_uninitialize: Optional[Callable[[], None]] = None,
    ) -> None:
        self._get_active_object = get_active_object
        self._get_root_window = get_root_window or self._default_get_root_window
        self._co_initialize = co_initialize or self._default_co_initialize
        self._co_uninitialize = co_uninitialize or self._default_co_uninitialize

    def _active_object_for_target(self, expected_target: TargetSnapshot):
        """Attach to the matching Word-compatible app without launching it.

        WPS normally registers both ``KWPS.Application`` and a Word-compatible
        alias. Prefer its own ProgID so a simultaneously running Microsoft Word
        instance cannot be mistaken for the WPS document selected by the user.
        Injected factories retain the original zero-argument test contract.
        """

        if expected_target is None or not supports_word_compatible_ranges(
            expected_target.profile.process_name
        ):
            raise RuntimeError("Unsupported Word-compatible target")
        if self._get_active_object is not None:
            return self._get_active_object()
        from win32com.client import GetActiveObject

        process = str(expected_target.profile.process_name or "").strip().lower()
        progids = (
            ("KWPS.Application", "Word.Application")
            if process == "wps.exe"
            else ("Word.Application",)
        )
        last_error = None
        for progid in progids:
            try:
                return GetActiveObject(progid)
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("No Word-compatible application ProgID is available")

    @staticmethod
    def _default_get_root_window(hwnd: int) -> int:
        try:
            # GA_ROOT = 2. WPS exposes an inner Word-compatible HWND whose root
            # is the actual Qt foreground window captured by Aria.
            return int(_user32.GetAncestor(int(hwnd), 2) or 0)
        except Exception:
            return 0

    @staticmethod
    def _default_co_initialize() -> None:
        import pythoncom

        pythoncom.CoInitialize()

    @staticmethod
    def _default_co_uninitialize() -> None:
        import pythoncom

        pythoncom.CoUninitialize()

    @staticmethod
    def _normalize_hwnd(value: Any) -> int:
        hwnd = int(value or 0)
        return hwnd + (1 << 32) if hwnd < 0 else hwnd

    @staticmethod
    def _same_com_object(left: Any, right: Any) -> bool:
        if left is right:
            return True
        left_dispatch = getattr(left, "_oleobj_", None)
        right_dispatch = getattr(right, "_oleobj_", None)
        return (
            left_dispatch is not None
            and right_dispatch is not None
            and left_dispatch == right_dispatch
        )

    def _window_matches_target(
        self, active_window: Any, expected_target: TargetSnapshot
    ) -> bool:
        """Bind a COM window to the exact foreground carrier snapshot."""

        if active_window is None or expected_target is None:
            return False
        try:
            active_hwnd = self._normalize_hwnd(active_window.Hwnd)
        except Exception:
            return False
        expected_hwnd = self._normalize_hwnd(expected_target.hwnd)
        if active_hwnd == expected_hwnd:
            return True
        if str(expected_target.profile.process_name or "").lower() != "wps.exe":
            return False
        try:
            root_hwnd = self._normalize_hwnd(self._get_root_window(active_hwnd))
        except Exception:
            return False
        return root_hwnd == expected_hwnd

    @staticmethod
    def _range_identity(word_range: Any) -> tuple[int, int, int]:
        return (
            int(word_range.Start),
            int(word_range.End),
            int(word_range.StoryType),
        )

    @staticmethod
    def _result(
        status: WordInsertStatus,
        *,
        success: bool = False,
        safe_to_fallback: bool = False,
        partial_possible: bool = False,
        verified: bool = False,
        track_revisions: bool = False,
    ) -> WordInsertResult:
        return WordInsertResult(
            success=success,
            status=status,
            safe_to_fallback=safe_to_fallback,
            partial_possible=partial_possible,
            verified=verified,
            track_revisions=track_revisions,
        )

    def _active_protected_view_matches(
        self, app: Any, expected_target: TargetSnapshot
    ) -> bool:
        try:
            protected = app.ActiveProtectedViewWindow
        except Exception:
            return False
        if protected is None:
            return False
        try:
            protected_window = protected.Window
            if self._normalize_hwnd(protected_window.Hwnd) == 0:
                return True
        except Exception:
            # An active protected-view object that cannot expose its owning
            # window is not permission to write through ActiveDocument.
            return True
        return self._window_matches_target(protected_window, expected_target)

    @staticmethod
    def _selection_has_locked_content_control(selection: Any) -> bool:
        try:
            control = selection.ParentContentControl
        except Exception:
            return False
        if control is None:
            return False
        try:
            return bool(control.LockContents) or bool(control.LockContentControl)
        except Exception:
            return True

    def capture_bookmark(
        self, expected_target: TargetSnapshot
    ) -> Optional[WordBookmark]:
        """Capture the active Word selection without retaining a COM object."""
        if expected_target is None or not expected_target.is_valid:
            return None
        expected_hwnd = self._normalize_hwnd(expected_target.hwnd)
        com_initialized = False
        try:
            self._co_initialize()
            com_initialized = True
            app = self._active_object_for_target(expected_target)
            if self._active_protected_view_matches(app, expected_target):
                return None
            active_window = app.ActiveWindow
            selection = app.Selection
            if (
                active_window is None
                or selection is None
                or not self._window_matches_target(active_window, expected_target)
            ):
                return None
            selection_range = selection.Range.Duplicate
            start, end, story = self._range_identity(selection_range)
            return WordBookmark(expected_hwnd, start, end, story)
        except Exception:
            return None
        finally:
            if com_initialized:
                try:
                    self._co_uninitialize()
                except Exception:
                    pass

    @staticmethod
    def _configure_exact_find(word_range: Any, text: str) -> Any:
        finder = word_range.Find
        try:
            finder.ClearFormatting()
        except Exception:
            pass
        finder.Text = text
        finder.Forward = True
        finder.Wrap = 0  # wdFindStop
        finder.Format = False
        finder.MatchCase = True
        finder.MatchWholeWord = False
        finder.MatchWildcards = False
        return finder

    def _find_unique_range_near(
        self,
        document: Any,
        expected_text: str,
        *,
        hint_start: int,
        hint_end: int,
        required_end: Optional[int] = None,
    ) -> Optional[Any]:
        """Find one exact match in a bounded local Word range.

        This permits unrelated edits before the dictated passage to shift its
        offsets without reading or uploading the whole document.  More than
        one candidate is deliberately ambiguous.
        """

        try:
            content_end = int(document.Content.End)
            window_start = max(0, int(hint_start) - 4096)
            window_end = min(content_end, max(int(hint_end), int(hint_start)) + 4096)
        except Exception:
            return None
        if window_end <= window_start:
            return None

        matches = []
        cursor = window_start
        while cursor < window_end and len(matches) < 2:
            try:
                probe = document.Range(cursor, window_end)
                finder = self._configure_exact_find(probe, expected_text)
                if not bool(finder.Execute()):
                    break
                found_start = int(probe.Start)
                found_end = int(probe.End)
                if (
                    str(probe.Text or "") == expected_text
                    and (required_end is None or found_end == int(required_end))
                ):
                    matches.append(probe.Duplicate)
                next_cursor = max(found_end, found_start + 1)
                if next_cursor <= cursor:
                    break
                cursor = next_cursor
            except Exception:
                return None
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _range_context_sha256(document: Any, start: int, end: int) -> str:
        try:
            content_end = int(document.Content.End)
            context_start = max(0, int(start) - 64)
            context_end = min(content_end, int(end) + 64)
            context = str(document.Range(context_start, context_end).Text or "")
        except Exception:
            return ""
        payload = (
            f"{int(start) - context_start}:{int(end) - context_start}:" + context
        ).encode("utf-8", errors="surrogatepass")
        return hashlib.sha256(payload).hexdigest()

    def capture_recent_insert(
        self,
        text: str,
        expected_target: TargetSnapshot,
        *,
        target_is_current: Callable[[TargetSnapshot], bool],
    ) -> Optional[WordBookmark]:
        """Capture the verified Word range ending at the post-insert caret."""

        if not text or expected_target is None or not expected_target.is_valid:
            return None
        expected_hwnd = self._normalize_hwnd(expected_target.hwnd)
        prior = expected_target.adapter_token
        if not isinstance(prior, WordBookmark) or prior.hwnd != expected_hwnd:
            return None
        word_text = normalize_word_text(text)
        com_initialized = False
        try:
            self._co_initialize()
            com_initialized = True
            app = self._active_object_for_target(expected_target)
            active_window = app.ActiveWindow
            document = app.ActiveDocument
            selection = app.Selection
            if (
                active_window is None
                or document is None
                or selection is None
                or not self._window_matches_target(active_window, expected_target)
                or not bool(target_is_current(expected_target))
            ):
                return None
            caret = selection.Range.Duplicate
            caret_start, caret_end, story = self._range_identity(caret)
            if caret_start != caret_end or story != self.WD_MAIN_TEXT_STORY:
                return None

            candidate = None
            if int(prior.start) <= caret_end:
                direct = document.Range(int(prior.start), caret_end)
                if (
                    int(direct.StoryType) == self.WD_MAIN_TEXT_STORY
                    and str(direct.Text or "") == word_text
                ):
                    candidate = direct
            if candidate is None:
                candidate = self._find_unique_range_near(
                    document,
                    word_text,
                    hint_start=int(prior.start),
                    hint_end=caret_end,
                    required_end=caret_end,
                )
            if candidate is None or not bool(target_is_current(expected_target)):
                return None
            start, end, story = self._range_identity(candidate)
            if story != self.WD_MAIN_TEXT_STORY or str(candidate.Text or "") != word_text:
                return None
            return WordBookmark(
                expected_hwnd,
                start,
                end,
                story,
                context_sha256=self._range_context_sha256(
                    document, start, end
                ),
            )
        except Exception:
            return None
        finally:
            if com_initialized:
                try:
                    self._co_uninitialize()
                except Exception:
                    pass

    def replace_bookmarked_range(
        self,
        replacement: str,
        expected_text: str,
        expected_target: TargetSnapshot,
        *,
        target_is_current: Callable[[TargetSnapshot], bool],
    ) -> WordInsertResult:
        """Replace one verified recent Word range as a custom undo record."""

        if (
            not replacement
            or not expected_text
            or replacement == expected_text
            or "\x00" in (replacement + expected_text)
            or expected_target is None
            or not expected_target.is_valid
        ):
            return self._result(WordInsertStatus.COM_UNAVAILABLE)
        expected_hwnd = self._normalize_hwnd(expected_target.hwnd)
        bookmark = expected_target.adapter_token
        if not isinstance(bookmark, WordBookmark) or bookmark.hwnd != expected_hwnd:
            return self._result(WordInsertStatus.BOOKMARK_UNAVAILABLE)
        word_source = normalize_word_text(expected_text)
        word_replacement = normalize_word_text(replacement)
        com_initialized = False
        undo_started = False
        write_started = False
        track_revisions = False

        try:
            self._co_initialize()
            com_initialized = True
            app = self._active_object_for_target(expected_target)
            if self._active_protected_view_matches(app, expected_target):
                return self._result(WordInsertStatus.PROTECTED_VIEW)
            active_window = app.ActiveWindow
            document = app.ActiveDocument
            selection = app.Selection
            if (
                active_window is None
                or document is None
                or selection is None
                or not self._window_matches_target(active_window, expected_target)
            ):
                return self._result(WordInsertStatus.WINDOW_MISMATCH)
            if bool(document.ReadOnly):
                return self._result(WordInsertStatus.DOCUMENT_READ_ONLY)
            if int(document.ProtectionType) != self.WD_NO_PROTECTION:
                return self._result(WordInsertStatus.DOCUMENT_PROTECTED)
            track_revisions = bool(document.TrackRevisions)
            if int(bookmark.story_type) != self.WD_MAIN_TEXT_STORY:
                return self._result(WordInsertStatus.NON_MAIN_STORY)

            source_range = document.Range(int(bookmark.start), int(bookmark.end))
            context_matches = (
                not bookmark.context_sha256
                or bookmark.context_sha256
                == self._range_context_sha256(
                    document, int(source_range.Start), int(source_range.End)
                )
            )
            if not context_matches or str(source_range.Text or "") != word_source:
                source_range = self._find_unique_range_near(
                    document,
                    word_source,
                    hint_start=int(bookmark.start),
                    hint_end=int(bookmark.end),
                )
            if source_range is None or str(source_range.Text or "") != word_source:
                return self._result(WordInsertStatus.SELECTION_CHANGED)
            if "\x07" in word_source:
                return self._result(WordInsertStatus.UNSAFE_SELECTION)
            source_identity = self._range_identity(source_range)

            try:
                undo_record = app.UndoRecord
                if undo_record is None:
                    raise AttributeError("UndoRecord unavailable")
                if bool(undo_record.IsRecordingCustomRecord):
                    return self._result(WordInsertStatus.UNDO_BUSY)
            except Exception:
                return self._result(WordInsertStatus.UNDO_UNAVAILABLE)
            if not bool(target_is_current(expected_target)):
                return self._result(WordInsertStatus.TARGET_CHANGED)
            if (
                app.ActiveWindow is None
                or not self._window_matches_target(
                    app.ActiveWindow, expected_target
                )
                or not self._same_com_object(document, app.ActiveDocument)
            ):
                return self._result(WordInsertStatus.TARGET_CHANGED)

            commit_range = document.Range(source_identity[0], source_identity[1])
            if (
                self._range_identity(commit_range) != source_identity
                or str(commit_range.Text or "") != word_source
            ):
                return self._result(WordInsertStatus.SELECTION_CHANGED)
            selection.SetRange(source_identity[0], source_identity[1])
            if self._selection_has_locked_content_control(selection):
                return self._result(WordInsertStatus.CONTENT_CONTROL_LOCKED)
            if self._range_identity(selection.Range.Duplicate) != source_identity:
                return self._result(WordInsertStatus.TARGET_CHANGED)
            if not bool(target_is_current(expected_target)):
                return self._result(WordInsertStatus.TARGET_CHANGED)

            undo_record.StartCustomRecord("Aria Rewrite")
            undo_started = True
            write_range = selection.Range.Duplicate
            anchor = int(write_range.Start)
            write_started = True
            write_range.Text = word_replacement
            new_end = int(write_range.End)
            selection.SetRange(new_end, new_end)
            verify_range = document.Range(anchor, new_end)
            if str(verify_range.Text or "") != word_replacement:
                return self._result(
                    WordInsertStatus.VERIFY_FAILED,
                    partial_possible=True,
                    track_revisions=track_revisions,
                )
            undo_record.EndCustomRecord()
            undo_started = False
            return self._result(
                WordInsertStatus.INSERTED,
                success=True,
                verified=True,
                track_revisions=track_revisions,
            )
        except Exception:
            if write_started:
                return self._result(
                    WordInsertStatus.INSERT_FAILED,
                    partial_possible=True,
                    track_revisions=track_revisions,
                )
            return self._result(WordInsertStatus.COM_UNAVAILABLE)
        finally:
            if undo_started:
                try:
                    undo_record.EndCustomRecord()
                except Exception:
                    pass
            if com_initialized:
                try:
                    self._co_uninitialize()
                except Exception:
                    pass

    def insert_text(
        self,
        text: str,
        expected_target: TargetSnapshot,
        *,
        target_is_current: Callable[[TargetSnapshot], bool],
        expected_selection_text: Optional[str] = None,
    ) -> WordInsertResult:
        """Perform a single-stage native insert or return a safe refusal.

        ``safe_to_fallback`` is true only while no Word range write has been
        attempted and the foreground identity has not changed. Once the setter
        is invoked, any failure is treated as possibly partial and the caller
        must surface Draft Box recovery instead of blindly pasting again.
        """
        if not text or expected_target is None or not expected_target.is_valid:
            return self._result(
                WordInsertStatus.COM_UNAVAILABLE,
                safe_to_fallback=True,
            )

        expected_hwnd = self._normalize_hwnd(expected_target.hwnd)
        bookmark = expected_target.adapter_token
        if not isinstance(bookmark, WordBookmark) or bookmark.hwnd != expected_hwnd:
            return self._result(
                WordInsertStatus.BOOKMARK_UNAVAILABLE,
                safe_to_fallback=True,
            )
        word_text = normalize_word_text(text)
        com_initialized = False
        undo_started = False
        write_started = False
        track_revisions = False

        try:
            self._co_initialize()
            com_initialized = True
            app = self._active_object_for_target(expected_target)

            if self._active_protected_view_matches(app, expected_target):
                return self._result(WordInsertStatus.PROTECTED_VIEW)

            active_window = app.ActiveWindow
            if (
                active_window is None
                or not self._window_matches_target(active_window, expected_target)
            ):
                return self._result(
                    WordInsertStatus.WINDOW_MISMATCH,
                    safe_to_fallback=True,
                )

            document = app.ActiveDocument
            selection = app.Selection
            if document is None or selection is None:
                return self._result(
                    WordInsertStatus.COM_UNAVAILABLE,
                    safe_to_fallback=True,
                )
            if bool(document.ReadOnly):
                return self._result(WordInsertStatus.DOCUMENT_READ_ONLY)
            if int(document.ProtectionType) != self.WD_NO_PROTECTION:
                return self._result(WordInsertStatus.DOCUMENT_PROTECTED)
            if self._selection_has_locked_content_control(selection):
                return self._result(WordInsertStatus.CONTENT_CONTROL_LOCKED)

            track_revisions = bool(document.TrackRevisions)
            initial_range = selection.Range.Duplicate
            initial_identity = self._range_identity(initial_range)
            bookmark_identity = (
                int(bookmark.start),
                int(bookmark.end),
                int(bookmark.story_type),
            )
            if initial_identity != bookmark_identity:
                return self._result(WordInsertStatus.TARGET_CHANGED)
            if initial_identity[2] != self.WD_MAIN_TEXT_STORY:
                return self._result(
                    WordInsertStatus.NON_MAIN_STORY,
                    safe_to_fallback=True,
                    track_revisions=track_revisions,
                )
            if initial_identity[0] != initial_identity[1] and "\x07" in str(
                initial_range.Text or ""
            ):
                return self._result(
                    WordInsertStatus.UNSAFE_SELECTION,
                    track_revisions=track_revisions,
                )
            normalized_expected_selection = None
            if expected_selection_text is not None:
                normalized_expected_selection = normalize_word_text(
                    expected_selection_text
                )
                if str(initial_range.Text or "") != normalized_expected_selection:
                    return self._result(
                        WordInsertStatus.SELECTION_CHANGED,
                        track_revisions=track_revisions,
                    )

            try:
                undo_record = app.UndoRecord
                if undo_record is None:
                    raise AttributeError("UndoRecord unavailable")
                undo_busy = bool(undo_record.IsRecordingCustomRecord)
            except Exception:
                return self._result(
                    WordInsertStatus.UNDO_UNAVAILABLE,
                    safe_to_fallback=True,
                    track_revisions=track_revisions,
                )
            if undo_busy:
                # Clipboard/typewriter edits would also be captured by the
                # foreign custom record, so do not silently fall back here.
                return self._result(
                    WordInsertStatus.UNDO_BUSY,
                    track_revisions=track_revisions,
                )

            # Commit-edge checks: Windows identity, active Word document/window,
            # and selection bookmark must all still match immediately before
            # starting the custom undo record.
            try:
                target_current = bool(target_is_current(expected_target))
            except Exception:
                target_current = False
            if not target_current:
                return self._result(WordInsertStatus.TARGET_CHANGED)
            current_window = app.ActiveWindow
            current_document = app.ActiveDocument
            current_range = app.Selection.Range.Duplicate
            if (
                current_window is None
                or not self._window_matches_target(current_window, expected_target)
                or not self._same_com_object(document, current_document)
                or self._range_identity(current_range) != initial_identity
            ):
                return self._result(WordInsertStatus.TARGET_CHANGED)
            if (
                normalized_expected_selection is not None
                and str(current_range.Text or "") != normalized_expected_selection
            ):
                return self._result(
                    WordInsertStatus.SELECTION_CHANGED,
                    track_revisions=track_revisions,
                )

            try:
                undo_record.StartCustomRecord("Aria Dictation")
                undo_started = True
            except Exception:
                return self._result(
                    WordInsertStatus.UNDO_START_FAILED,
                    track_revisions=track_revisions,
                )

            anchor = int(current_range.Start)
            write_range = current_range.Duplicate
            write_started = True
            write_range.Text = word_text
            # Word Range offsets follow Word's own character model (important
            # for emoji/surrogate pairs). Use the post-write COM range end,
            # never Python's code-point length, to position and verify.
            new_end = int(write_range.End)
            app.Selection.SetRange(new_end, new_end)

            verify_range = document.Range(anchor, new_end)
            if str(verify_range.Text or "") != word_text:
                return self._result(
                    WordInsertStatus.VERIFY_FAILED,
                    partial_possible=True,
                    track_revisions=track_revisions,
                )

            try:
                undo_record.EndCustomRecord()
                undo_started = False
            except Exception:
                return self._result(
                    WordInsertStatus.INSERT_FAILED,
                    partial_possible=True,
                    track_revisions=track_revisions,
                )

            return self._result(
                WordInsertStatus.INSERTED,
                success=True,
                verified=True,
                track_revisions=track_revisions,
            )
        except Exception:
            if write_started:
                return self._result(
                    WordInsertStatus.INSERT_FAILED,
                    partial_possible=True,
                    track_revisions=track_revisions,
                )
            return self._result(
                WordInsertStatus.COM_UNAVAILABLE,
                safe_to_fallback=True,
                track_revisions=track_revisions,
            )
        finally:
            if undo_started:
                try:
                    undo_record.EndCustomRecord()
                except Exception:
                    # Any earlier failure already returns a partial/failed result.
                    pass
            if com_initialized:
                try:
                    self._co_uninitialize()
                except Exception:
                    pass
