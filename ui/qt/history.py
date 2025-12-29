# history.py
# History popup window for Aria
# Shows recent transcriptions with copy functionality

import sys
import json
from pathlib import Path
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
from PySide6.QtGui import QColor, QKeySequence, QCursor, QShortcut


class HistoryItem(QFrame):
    """Single history item - click anywhere to copy."""

    copyClicked = Signal(str)
    deleteClicked = Signal(int)  # Emits index for deletion

    def __init__(
        self, text: str, timestamp: str, index: int, filename: str = "", parent=None
    ):
        super().__init__(parent)
        self.text = text
        self.index = index
        self.filename = filename  # Store filename for deletion
        self._delete_pending = False  # Flag to prevent copy when delete is clicked

        self.setStyleSheet(
            """
            HistoryItem {
                background-color: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 8px;
                padding: 8px;
            }
            HistoryItem:hover {
                background-color: rgba(255, 255, 255, 0.1);
                border-color: rgba(100, 180, 255, 0.3);
            }
        """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        # Header row: timestamp + copy hint + delete button
        header = QHBoxLayout()

        time_label = QLabel(timestamp)
        time_label.setStyleSheet(
            """
            QLabel {
                color: rgba(255, 255, 255, 0.5);
                font-size: 11px;
            }
        """
        )
        time_label.setAttribute(Qt.WA_TransparentForMouseEvents)  # Pass clicks through
        header.addWidget(time_label)

        header.addStretch()

        # Copy hint (shows on hover via CSS)
        self._copy_hint = QLabel("点击复制")
        self._copy_hint.setStyleSheet(
            """
            QLabel {
                color: rgba(100, 180, 255, 0.6);
                font-size: 10px;
            }
        """
        )
        self._copy_hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        header.addWidget(self._copy_hint)

        # Delete button
        delete_btn = QPushButton("×")
        delete_btn.setFixedSize(20, 20)
        delete_btn.setCursor(Qt.PointingHandCursor)
        delete_btn.setStyleSheet(
            """
            QPushButton {
                color: rgba(255, 255, 255, 0.4);
                background: transparent;
                border: none;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                color: rgba(255, 100, 100, 0.9);
                background: rgba(255, 100, 100, 0.15);
                border-radius: 4px;
            }
        """
        )
        delete_btn.clicked.connect(self._on_delete_clicked)
        header.addWidget(delete_btn)

        layout.addLayout(header)

        # Text content - NO text selection, entire area is clickable
        self._text_label = QLabel(text)
        self._text_label.setWordWrap(True)
        self._text_label.setStyleSheet(
            """
            QLabel {
                color: rgba(255, 255, 255, 0.9);
                font-size: 13px;
                line-height: 1.4;
            }
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
    - Shows last 10 transcriptions
    - Click to copy
    - Keyboard shortcuts Ctrl+1 through Ctrl+9 for quick copy
    - Auto-closes after copy
    """

    closed = Signal()

    def __init__(self, debug_log_dir: Optional[Path] = None, parent=None):
        super().__init__(parent)

        self.debug_log_dir = (
            debug_log_dir or Path(__file__).parent.parent.parent / "DebugLog"
        )
        self.history_items: List[dict] = []

        self._init_window()
        self._init_ui()
        self._init_shortcuts()
        self._apply_shadow()

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
            """
            QFrame {
                background-color: rgba(35, 35, 40, 0.95);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }
        """
        )
        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(12, 12, 12, 12)
        container_layout.setSpacing(8)

        # Title row with clear button
        title_row = QHBoxLayout()

        title = QLabel("历史记录 (原始ASR)")
        title.setStyleSheet(
            """
            QLabel {
                color: white;
                font-size: 14px;
                font-weight: bold;
            }
        """
        )
        title_row.addWidget(title)
        title_row.addStretch()

        # Clear all button
        self._clear_btn = QPushButton("清空")
        self._clear_btn.setCursor(Qt.PointingHandCursor)
        self._clear_btn.setStyleSheet(
            """
            QPushButton {
                color: rgba(255, 150, 150, 0.8);
                background: transparent;
                border: 1px solid rgba(255, 150, 150, 0.3);
                border-radius: 4px;
                padding: 3px 10px;
                font-size: 11px;
            }
            QPushButton:hover {
                color: rgba(255, 100, 100, 1.0);
                background: rgba(255, 100, 100, 0.15);
                border-color: rgba(255, 100, 100, 0.5);
            }
        """
        )
        self._clear_btn.clicked.connect(self._clear_all)
        title_row.addWidget(self._clear_btn)

        container_layout.addLayout(title_row)

        # Hint
        hint = QLabel("显示未润色文本，点击复制")
        hint.setStyleSheet(
            """
            QLabel {
                color: rgba(255, 255, 255, 0.5);
                font-size: 11px;
                padding-bottom: 8px;
            }
        """
        )
        container_layout.addWidget(hint)

        # Scroll area for history items
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            """
            QScrollArea {
                border: none;
                background: transparent;
            }
            QScrollBar:vertical {
                background: rgba(255, 255, 255, 0.05);
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 0.2);
                border-radius: 4px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(255, 255, 255, 0.3);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
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
            """
            QLabel {
                color: rgba(255, 255, 255, 0.4);
                font-size: 12px;
                padding: 20px;
            }
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
        shadow.setColor(QColor(0, 0, 0, 100))
        shadow.setOffset(0, 4)
        self.container.setGraphicsEffect(shadow)

    def load_history(self, max_items: int = 10):
        """Load recent transcriptions from debug log files."""
        self.history_items.clear()

        # Clear existing items
        while self.items_layout.count() > 1:  # Keep the stretch
            item = self.items_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self.debug_log_dir.exists():
            self.empty_label.show()
            return

        # Find all session files and sort by modification time (newest first)
        session_files = list(self.debug_log_dir.glob("session_*.json"))
        session_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        # Load recent sessions
        loaded = 0
        for session_file in session_files:
            if loaded >= max_items:
                break

            try:
                with open(session_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # Get raw ASR text (before polish) for display
                # This helps user compare raw vs polished output
                asr_data = data.get("asr", {})
                raw_text = asr_data.get("raw_text", "").strip()
                final_text = data.get("final_text", "").strip()

                # Use raw ASR text for display, but keep final for reference
                display_text = raw_text if raw_text else final_text
                if not display_text:
                    continue

                # Parse timestamp
                start_time = data.get("start_time", "")
                try:
                    dt = datetime.fromisoformat(start_time)
                    timestamp = dt.strftime("%H:%M:%S")
                except Exception:
                    timestamp = ""

                self.history_items.append(
                    {
                        "text": display_text,  # Raw ASR text for copy
                        "polished": final_text,  # Keep polished for reference
                        "timestamp": timestamp,
                        "file": session_file.name,
                    }
                )

                # Create UI item showing raw ASR text
                item = HistoryItem(
                    display_text, timestamp, loaded, filename=session_file.name
                )
                item.copyClicked.connect(self._on_copy)
                item.deleteClicked.connect(self._delete_item)
                self.items_layout.insertWidget(loaded, item)

                loaded += 1

            except Exception as e:
                _hlog(f"[History] Failed to load {session_file}: {e}")
                continue

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

    def _delete_item(self, index: int):
        """Delete a single history item by index."""
        if 0 <= index < len(self.history_items):
            item_data = self.history_items[index]
            filename = item_data.get("file", "")

            # Delete the session file with path validation
            if filename:
                file_path = (self.debug_log_dir / filename).resolve()
                # Security: ensure file is within expected directory
                if file_path.parent == self.debug_log_dir.resolve():
                    try:
                        if file_path.exists():
                            file_path.unlink()
                    except Exception as e:
                        _hlog(f"[History] Failed to delete {filename}: {e}")

            # Reload the history to refresh UI
            self.load_history()

    def _clear_all(self):
        """Clear all history items."""
        if not self.history_items:
            return

        # Delete all session files with path validation
        for item_data in self.history_items:
            filename = item_data.get("file", "")
            if filename:
                file_path = (self.debug_log_dir / filename).resolve()
                # Security: ensure file is within expected directory
                if file_path.parent == self.debug_log_dir.resolve():
                    try:
                        if file_path.exists():
                            file_path.unlink()
                    except Exception as e:
                        _hlog(f"[History] Failed to delete {filename}: {e}")

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
