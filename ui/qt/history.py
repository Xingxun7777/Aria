# history.py
# History popup window for Aria (tray single-click)
# Shows the most recent ASR transcriptions from the unified HistoryStore
# (data/history/*.jsonl) — the same data source as the full history browser.
# DebugLog session files are debug-only and are no longer read by any UI.

import html
import sys
from datetime import datetime
from typing import List, Optional

from PySide6.QtCore import Qt, Signal, QTimer


def _hlog(msg: str):
    """History debug logging (pythonw.exe safe)."""
    if sys.stdout is not None:
        print(msg)


from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QFrame,
    QApplication,
    QGraphicsDropShadowEffect,
)
from PySide6.QtGui import QKeySequence, QShortcut

from . import styles


def record_display_fields(input_text: str, output_text: str) -> tuple:
    """
    Map a HistoryStore ASR record to popup display fields.

    Records written with a known raw transcript store it in input_text and
    the final text in output_text; records without a distinct raw transcript
    store the final text in input_text and leave output_text empty.

    Returns:
        (display_text, raw_text) where display_text is the final committed
        text and raw_text is the distinct raw ASR transcript ("" if none).
    """
    input_text = (input_text or "").strip()
    output_text = (output_text or "").strip()
    if output_text:
        raw = input_text if input_text != output_text else ""
        return output_text, raw
    return input_text, ""


class HistoryItem(QFrame):
    """Single history item - click anywhere to copy."""

    copyClicked = Signal(str)
    deleteClicked = Signal(int)  # Emits index for deletion

    def __init__(
        self,
        text: str,
        timestamp: str,
        index: int,
        raw_text: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._theme = styles.get_theme_palette()
        self.text = text
        self.index = index
        self.raw_text = raw_text
        self._delete_pending = False  # Flag to prevent copy when delete is clicked

        self.setStyleSheet(
            f"""
            HistoryItem {{
                background-color: {self._theme.button_bg};
                border: 1px solid {self._theme.border};
                border-radius: 8px;
                padding: 8px;
            }}
            HistoryItem:hover {{
                background-color: {self._theme.button_hover_bg};
                border-color: {self._theme.accent_border};
            }}
        """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        # Header row: timestamp + raw badge + copy hint + delete button
        header = QHBoxLayout()

        time_label = QLabel(timestamp)
        time_label.setStyleSheet(
            f"""
            QLabel {{
                color: {self._theme.text_muted};
                font-size: 11px;
            }}
        """
        )
        time_label.setAttribute(Qt.WA_TransparentForMouseEvents)  # Pass clicks through
        header.addWidget(time_label)

        # Raw-transcript badge: only when the record has a distinct raw ASR
        # text. The full raw text is shown via the item tooltip on hover.
        if raw_text:
            raw_badge = QLabel("原")
            raw_badge.setStyleSheet(
                f"""
                QLabel {{
                    color: {self._theme.text_muted};
                    border: 1px solid {self._theme.border};
                    border-radius: 4px;
                    padding: 0px 4px;
                    font-size: 10px;
                }}
            """
            )
            raw_badge.setAttribute(Qt.WA_TransparentForMouseEvents)
            header.addWidget(raw_badge)
            _raw_preview = (
                raw_text if len(raw_text) <= 600 else raw_text[:600] + "…"
            )
            self.setToolTip(
                f"<b>原始转写</b><br/>{html.escape(_raw_preview)}"
            )

        header.addStretch()

        # Copy hint (shows on hover via CSS)
        self._copy_hint = QLabel("点击复制")
        self._copy_hint.setStyleSheet(
            f"""
            QLabel {{
                color: {self._theme.accent};
                font-size: 10px;
            }}
        """
        )
        self._copy_hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        header.addWidget(self._copy_hint)

        # Delete button
        delete_btn = QPushButton("×")
        delete_btn.setFixedSize(20, 20)
        delete_btn.setCursor(Qt.PointingHandCursor)
        delete_btn.setStyleSheet(
            f"""
            QPushButton {{
                color: {self._theme.text_muted};
                background: transparent;
                border: none;
                font-size: 14px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                color: {self._theme.danger};
                background: {self._theme.danger_soft};
                border-radius: 4px;
            }}
        """
        )
        delete_btn.clicked.connect(self._on_delete_clicked)
        header.addWidget(delete_btn)

        layout.addLayout(header)

        # Text content - NO text selection, entire area is clickable
        self._text_label = QLabel(text)
        self._text_label.setWordWrap(True)
        self._text_label.setStyleSheet(
            f"""
            QLabel {{
                color: {self._theme.text_primary};
                font-size: 13px;
                line-height: 1.4;
            }}
        """
        )
        # Make text label pass mouse events to parent for click-to-copy
        self._text_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(self._text_label)

        # Entire item is clickable
        self.setCursor(Qt.PointingHandCursor)

    def _on_delete_clicked(self):
        """Handle delete button click - stop propagation."""
        self._delete_pending = True  # Flag to prevent copy on this click
        self.deleteClicked.emit(self.index)

    def mousePressEvent(self, event):
        """Click anywhere to copy."""
        # Skip if delete was just clicked (button click propagates to parent)
        if self._delete_pending:
            self._delete_pending = False
            event.accept()  # Mark event as handled
            return

        if event.button() == Qt.LeftButton:
            self.copyClicked.emit(self.text)
            event.accept()
        else:
            super().mousePressEvent(event)


class HistoryWindow(QWidget):
    """
    History popup window showing recent transcriptions.

    Features:
    - Shows the last 10 ASR records from the unified HistoryStore
    - Final committed text by default; raw ASR transcript on hover
    - Click to copy
    - Keyboard shortcuts Ctrl+1 through Ctrl+9 for quick copy
    - Auto-closes after copy
    - Delete removes the HistoryStore record (never touches DebugLog files)
    """

    closed = Signal()

    def __init__(self, history_store=None, parent=None):
        super().__init__(parent)
        self._theme = styles.get_theme_palette()

        self._history_store = history_store
        self.history_items: List[dict] = []

        self._init_window()
        self._init_ui()
        self._init_shortcuts()
        self._apply_shadow()

    def set_history_store(self, store):
        """Set the history store (backend is created after the UI)."""
        self._history_store = store

    def _init_window(self):
        """Setup window flags for popup."""
        self.setWindowFlags(
            Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(400)
        self.setMaximumHeight(500)

    def _init_ui(self):
        """Build the UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(0)

        # Container with background
        self.container = QFrame()
        self.container.setStyleSheet(
            f"""
            QFrame {{
                background-color: {self._theme.panel_bg};
                border-radius: 12px;
                border: 1px solid {self._theme.border};
            }}
        """
        )
        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(12, 12, 12, 12)
        container_layout.setSpacing(8)

        # Title row with clear button
        title_row = QHBoxLayout()

        title = QLabel("历史记录")
        title.setStyleSheet(
            f"""
            QLabel {{
                color: {self._theme.text_primary};
                font-size: 14px;
                font-weight: bold;
            }}
        """
        )
        title_row.addWidget(title)
        title_row.addStretch()

        # Clear all button
        self._clear_btn = QPushButton("清空")
        self._clear_btn.setCursor(Qt.PointingHandCursor)
        self._clear_btn.setStyleSheet(
            f"""
            QPushButton {{
                color: {self._theme.danger};
                background: transparent;
                border: 1px solid {self._theme.danger_border};
                border-radius: 4px;
                padding: 3px 10px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                color: {self._theme.danger};
                background: {self._theme.danger_soft};
                border-color: {self._theme.danger_border};
            }}
        """
        )
        self._clear_btn.clicked.connect(self._clear_all)
        title_row.addWidget(self._clear_btn)

        container_layout.addLayout(title_row)

        # Hint
        hint = QLabel("点击复制 · Ctrl+1-9 快捷复制 · 悬浮查看原始转写")
        hint.setStyleSheet(
            f"""
            QLabel {{
                color: {self._theme.text_muted};
                font-size: 11px;
                padding-bottom: 8px;
            }}
        """
        )
        container_layout.addWidget(hint)

        # Scroll area for history items
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            f"""
            QScrollArea {{
                border: none;
                background: transparent;
            }}
            QScrollBar:vertical {{
                background: {self._theme.scrollbar_track};
                width: 8px;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: {self._theme.scrollbar_handle};
                border-radius: 4px;
                min-height: 20px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {self._theme.border_strong};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
        """
        )

        self.items_widget = QWidget()
        self.items_layout = QVBoxLayout(self.items_widget)
        self.items_layout.setContentsMargins(0, 0, 0, 0)
        self.items_layout.setSpacing(6)
        self.items_layout.addStretch()

        scroll.setWidget(self.items_widget)
        container_layout.addWidget(scroll)

        # Empty state label
        self.empty_label = QLabel("暂无历史记录")
        self.empty_label.setStyleSheet(
            f"""
            QLabel {{
                color: {self._theme.text_muted};
                font-size: 12px;
                padding: 20px;
            }}
        """
        )
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.hide()
        container_layout.addWidget(self.empty_label)

        layout.addWidget(self.container)

    def _init_shortcuts(self):
        """Setup keyboard shortcuts for quick copy."""
        for i in range(9):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self)
            shortcut.activated.connect(lambda idx=i: self._copy_by_index(idx))

    def _apply_shadow(self):
        """Apply drop shadow effect."""
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(20)
        shadow.setColor(styles.qcolor(self._theme.popup_shadow))
        shadow.setOffset(0, 4)
        self.container.setGraphicsEffect(shadow)

    def load_history(self, max_items: int = 10):
        """Load recent ASR records from the unified history store."""
        self.history_items.clear()

        # Clear existing items
        while self.items_layout.count() > 1:  # Keep the stretch
            item = self.items_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        records = []
        if self._history_store is not None:
            try:
                from aria.core.history.models import RecordType

                records = self._history_store.recent(
                    record_type=RecordType.ASR, limit=max_items
                )
            except Exception as e:
                _hlog(f"[History] Failed to load history store records: {e}")
                records = []

        loaded = 0
        for record in records:
            display_text, raw_text = record_display_fields(
                record.input_text, record.output_text
            )
            if not display_text:
                continue

            try:
                dt = datetime.fromisoformat(record.timestamp)
                timestamp = dt.strftime("%H:%M:%S")
                record_date = dt.strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                timestamp = ""
                record_date = ""

            self.history_items.append(
                {
                    "text": display_text,  # Final committed text (copy target)
                    "raw": raw_text,  # Distinct raw ASR transcript ("" if none)
                    "timestamp": timestamp,
                    "id": record.id,
                    "date": record_date,
                }
            )

            item = HistoryItem(
                display_text, timestamp, loaded, raw_text=raw_text
            )
            item.copyClicked.connect(self._on_copy)
            item.deleteClicked.connect(self._delete_item)
            self.items_layout.insertWidget(loaded, item)

            loaded += 1

        if loaded == 0:
            self.empty_label.show()
        else:
            self.empty_label.hide()

    def _copy_by_index(self, index: int):
        """Copy history item by index (from keyboard shortcut)."""
        if 0 <= index < len(self.history_items):
            text = self.history_items[index]["text"]
            self._on_copy(text)

    def _on_copy(self, text: str):
        """Handle copy action."""
        # Guard against None or empty text
        if not text:
            _hlog("[History] No text to copy, closing")
            self.close()
            return

        try:
            clipboard = QApplication.clipboard()
            clipboard.setText(text)
            _hlog(f"[History] Copied to clipboard: {text[:50]}...")
        except Exception as e:
            _hlog(f"[History] Clipboard error: {e}")
            # Retry once
            try:
                QApplication.clipboard().setText(text)
                _hlog("[History] Clipboard retry succeeded")
            except Exception as retry_e:
                _hlog(f"[History] Clipboard retry also failed: {retry_e}")

        # Brief visual feedback then close
        QTimer.singleShot(100, self.close)

    def _delete_record(self, item_data: dict) -> None:
        """Delete one HistoryStore record described by a history_items entry."""
        if self._history_store is None:
            return
        record_id = item_data.get("id", "")
        record_date = item_data.get("date", "")
        if not record_id or not record_date:
            return
        try:
            self._history_store.delete(record_date, record_id)
        except Exception as e:
            _hlog(f"[History] Failed to delete record {record_id}: {e}")

    def _delete_item(self, index: int):
        """Delete a single history item by index."""
        if 0 <= index < len(self.history_items):
            self._delete_record(self.history_items[index])
            # Reload the history to refresh UI
            self.load_history()

    def _clear_all(self):
        """Delete the records currently shown in the popup (max 10)."""
        if not self.history_items:
            return

        for item_data in self.history_items:
            self._delete_record(item_data)

        # Reload (will show empty state)
        self.load_history()

    def showAt(self, global_pos):
        """Show popup at specified position."""
        self.load_history()
        self.adjustSize()

        # Position near the tray icon
        x = global_pos.x() - self.width() // 2
        y = global_pos.y() - self.height() - 10

        # Ensure on screen
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            if x < geom.left():
                x = geom.left() + 5
            if x + self.width() > geom.right():
                x = geom.right() - self.width() - 5
            if y < geom.top():
                # Show below instead
                y = global_pos.y() + 10

        self.move(x, y)
        self.show()
        self.activateWindow()

    def closeEvent(self, event):
        """Handle close."""
        self.closed.emit()
        super().closeEvent(event)
