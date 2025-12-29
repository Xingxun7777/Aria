"""
Selection Detector
==================
Detect and capture selected text via clipboard.
"""

import time
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from aria.system.output import OutputInjector


@dataclass
class SelectionResult:
    """Result of selection detection."""

    has_selection: bool
    selected_text: Optional[str] = None
    original_clipboard: Optional[str] = None


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

    def detect(self, copy_delay_ms: int = 50) -> SelectionResult:
        """
        Detect if text is currently selected.

        Flow:
        1. Read current clipboard (backup)
        2. Send Ctrl+C to copy selection
        3. Wait for clipboard update
        4. Compare clipboard before/after
        5. Return result

        Args:
            copy_delay_ms: Delay after Ctrl+C to wait for clipboard update

        Returns:
            SelectionResult with detection status and text
        """
        # 1. Backup current clipboard
        original_clipboard = self.output_injector._get_clipboard_text()

        # 2. Send Ctrl+C to copy selection
        success = self.output_injector.send_key("c", modifiers=["ctrl"])
        if not success:
            return SelectionResult(
                has_selection=False, original_clipboard=original_clipboard
            )

        # 3. Wait for clipboard update
        time.sleep(copy_delay_ms / 1000.0)

        # 4. Read new clipboard content
        new_clipboard = self.output_injector._get_clipboard_text()

        # 5. Check if selection exists
        has_selection = self._is_valid_selection(original_clipboard, new_clipboard)

        if has_selection:
            return SelectionResult(
                has_selection=True,
                selected_text=new_clipboard,
                original_clipboard=original_clipboard,
            )
        else:
            return SelectionResult(
                has_selection=False, original_clipboard=original_clipboard
            )

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

    def restore_clipboard(self, original_content: Optional[str]) -> None:
        """
        Restore clipboard to original content.

        Args:
            original_content: Content to restore
        """
        if original_content is not None:
            self.output_injector._set_clipboard_text(original_content)
