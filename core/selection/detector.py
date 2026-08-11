"""
Selection Detector
==================
Detect and capture selected text via clipboard.
"""

import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from aria.system.output import OutputInjector


@dataclass
class SelectionResult:
    """Result of selection detection."""

    has_selection: bool
    selected_text: Optional[str] = None
    original_clipboard: Optional[str] = None
    # Full-format snapshot of the pre-probe clipboard (text, images, files),
    # so callers can put back non-text content too. original_clipboard keeps
    # the legacy text-only view (None when the clipboard held e.g. an image).
    original_clipboard_formats: Optional[Dict[int, bytes]] = None


class SelectionDetector:
    """
    Detect selected text by sending Ctrl+C and checking clipboard change.

    Usage:
        detector = SelectionDetector(output_injector)
        result = detector.detect()
        if result.has_selection:
            process(result.selected_text)
    """

    def __init__(self, output_injector: "OutputInjector"):
        """
        Initialize detector.

        Args:
            output_injector: OutputInjector instance for clipboard operations
        """
        self.output_injector = output_injector

    def detect(
        self,
        copy_delay_ms: int = 50,
        expected_target=None,
    ) -> SelectionResult:
        """
        Detect if text is currently selected.

        Flow:
        1. Read current clipboard (backup)
        2. Put a unique sentinel into the clipboard
        3. Send Ctrl+C to copy selection
        4. Wait for clipboard update
        5. If the sentinel was replaced by non-empty text, return it as the
           current selection

        Args:
            copy_delay_ms: Delay after Ctrl+C to wait for clipboard update
            expected_target: Optional content-free target snapshot.  When
                supplied, the focus identity is rechecked at the Ctrl+C
                SendInput edge instead of trusting a prior Ctrl+A/selection.

        Returns:
            SelectionResult with detection status and text
        """
        # 1. Backup current clipboard.
        # Keep the legacy text backup for callers that restore after they have
        # processed the selection.  If available, also keep a full-format
        # backup so a failed no-selection probe can restore images/files/etc.
        original_clipboard = self.output_injector._get_clipboard_text()
        original_clipboard_all_formats = None
        if hasattr(self.output_injector, "_backup_clipboard_all_formats"):
            try:
                original_clipboard_all_formats = (
                    self.output_injector._backup_clipboard_all_formats()
                )
            except Exception:
                original_clipboard_all_formats = None

        sentinel = f"__ARIA_SELECTION_SENTINEL_{uuid.uuid4().hex}__"
        sentinel_set = False
        if hasattr(self.output_injector, "_set_clipboard_text"):
            try:
                # mark_as_injection: a leftover sentinel (probe + restore both
                # failed) is garbage — the marker lets the self-echo guard
                # discard it instead of restoring it after the next paste.
                set_result = self.output_injector._set_clipboard_text(
                    sentinel, mark_as_injection=True
                )
                if isinstance(set_result, tuple):
                    sentinel_set = bool(set_result[0])
                else:
                    sentinel_set = bool(set_result)
            except Exception:
                sentinel_set = False

        # 2. Send Ctrl+C to copy selection
        if expected_target is None:
            success = self.output_injector.send_key("c", modifiers=["ctrl"])
        else:
            success = self.output_injector.send_key(
                "c",
                modifiers=["ctrl"],
                expected_target=expected_target,
            )
        if not success:
            if sentinel_set:
                self._restore_probe_clipboard(
                    original_clipboard, original_clipboard_all_formats
                )
            return SelectionResult(
                has_selection=False,
                original_clipboard=original_clipboard,
                original_clipboard_formats=original_clipboard_all_formats,
            )

        # 3. Wait for clipboard update
        time.sleep(copy_delay_ms / 1000.0)

        # 4. Read new clipboard content
        new_clipboard = self.output_injector._get_clipboard_text()

        # 5. Check if selection exists.
        # With the sentinel path, selected text may legitimately equal the
        # user's previous clipboard.  The old "clipboard changed" heuristic
        # incorrectly treated that as no selection.
        if sentinel_set:
            has_selection = self._is_valid_selection_after_sentinel(
                sentinel, new_clipboard
            )
        else:
            # Fallback for clipboard implementations that cannot write a
            # sentinel; preserves the legacy behavior.
            has_selection = self._is_valid_selection(original_clipboard, new_clipboard)

        if has_selection:
            return SelectionResult(
                has_selection=True,
                selected_text=new_clipboard,
                original_clipboard=original_clipboard,
                original_clipboard_formats=original_clipboard_all_formats,
            )
        else:
            if sentinel_set:
                self._restore_probe_clipboard(
                    original_clipboard, original_clipboard_all_formats
                )
            return SelectionResult(
                has_selection=False,
                original_clipboard=original_clipboard,
                original_clipboard_formats=original_clipboard_all_formats,
            )

    def _restore_probe_clipboard(
        self,
        original_text: Optional[str],
        original_all_formats,
    ) -> None:
        """Restore clipboard after a failed sentinel probe.

        An EMPTY formats dict must fall through (not short-circuit on the
        trivially-true empty restore): the pre-probe clipboard was empty, so
        the sentinel still needs to be cleared by the text path below.

        The full-format restore gets one retry: a single transient failure
        here (the probed app may still hold the clipboard right after
        Ctrl+C) would otherwise permanently destroy a copied image/file
        that only exists in this snapshot.
        """
        if original_all_formats and hasattr(
            self.output_injector, "_restore_clipboard_all_formats"
        ):
            for attempt in range(2):
                try:
                    if self.output_injector._restore_clipboard_all_formats(
                        original_all_formats
                    ):
                        return
                except Exception:
                    pass
                if attempt == 0:
                    time.sleep(0.1)

        if original_text is not None:
            self.output_injector._set_clipboard_text(original_text)
        else:
            # Last-resort cleanup: do not leave the private sentinel visible in
            # the user's clipboard when the pre-probe state had no text.
            self.output_injector._set_clipboard_text("")

    def _is_valid_selection_after_sentinel(
        self, sentinel: str, new: Optional[str]
    ) -> bool:
        """Check selection after the clipboard was primed with a sentinel."""
        if not new:
            return False

        if new == sentinel:
            return False

        if not new.strip():
            return False

        return True

    def _is_valid_selection(self, original: Optional[str], new: Optional[str]) -> bool:
        """
        Check if the clipboard change indicates a valid text selection.

        Args:
            original: Clipboard content before Ctrl+C
            new: Clipboard content after Ctrl+C

        Returns:
            True if valid selection detected
        """
        # No new content
        if not new:
            return False

        # Clipboard didn't change (no selection or same content selected)
        if new == original:
            return False

        # Only whitespace selected (likely accidental)
        if not new.strip():
            return False

        return True

    def restore_clipboard(
        self,
        original_content: Optional[str],
        original_formats: Optional[Dict[int, bytes]] = None,
    ) -> None:
        """
        Restore clipboard to original content.

        Prefers the full-format snapshot when available, so non-text content
        (images, file lists) survives a selection command too. Never stamps
        the injection marker — this puts USER content back, and marking it
        as ours would make the self-echo guard drop it after the next paste.

        The full-format restore gets one retry: a transient failure would
        permanently destroy image/file content that exists only in the
        snapshot.

        Args:
            original_content: Text content to restore (legacy fallback).
            original_formats: Full-format snapshot from detect().
        """
        if original_formats and hasattr(
            self.output_injector, "_restore_clipboard_all_formats"
        ):
            for attempt in range(2):
                try:
                    if self.output_injector._restore_clipboard_all_formats(
                        original_formats
                    ):
                        return
                except Exception:
                    pass
                if attempt == 0:
                    time.sleep(0.1)
        if original_content is not None:
            self.output_injector._set_clipboard_text(original_content)

    def restore_clipboard_from(self, result: SelectionResult) -> None:
        """Restore the pre-detection clipboard captured in `result`.

        Convenience wrapper for the post-command restore in wakeword
        executors: handles the image-only clipboard case (original_clipboard
        is None but the format snapshot exists) that the old
        `if original_clipboard is not None` call sites silently skipped —
        which destroyed copied images.
        """
        formats = getattr(result, "original_clipboard_formats", None)
        if result.original_clipboard is None and not formats:
            return
        self.restore_clipboard(result.original_clipboard, formats)
