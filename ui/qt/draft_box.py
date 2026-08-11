"""Editable local fallback for transcripts that could not be delivered."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import styles


class DraftBoxWindow(QWidget):
    """Local plaintext editor with explicit copy and guarded-send intents.

    The window never performs clipboard or Win32 injection itself. It keeps
    the draft in memory, emits user intent, and remains reusable after close.
    """

    sendRequested = Signal(str)
    copyRequested = Signal(str)
    loadLastRequested = Signal()

    _PARTIAL_STATUSES = {
        "target_changed_partial",
        "target_unavailable_partial",
        "word_com_partial",
        "terminal_chunk_partial",
        "game_text_partial",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme = styles.DARK_THEME
        self._reason = ""
        self._pending_drafts: list[tuple[str, str]] = []
        self._init_window()
        self._init_ui()

    def _init_window(self) -> None:
        self.setWindowTitle("Aria 草稿箱")
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.resize(580, 380)
        self.setMinimumSize(460, 300)

    def _init_ui(self) -> None:
        self.setStyleSheet(
            f"background-color: {self._theme.window_bg}; color: {self._theme.text_primary};"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        title = QLabel("草稿箱")
        title.setStyleSheet(
            f"color: {self._theme.text_primary}; font-size: 18px; font-weight: 700;"
        )
        root.addWidget(title)

        self.hint_label = QLabel(
            "在这里检查或修改转写。投递只写入目标，不会自动按 Enter。"
        )
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet(
            f"color: {self._theme.text_secondary}; font-size: 12px;"
        )
        root.addWidget(self.hint_label)

        self.status_frame = QFrame()
        self.status_frame.setObjectName("draftStatus")
        status_layout = QHBoxLayout(self.status_frame)
        status_layout.setContentsMargins(10, 7, 10, 7)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.status_label)
        self.status_frame.hide()
        root.addWidget(self.status_frame)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("输入或载入需要恢复的文字…")
        self.editor.setStyleSheet(
            f"""
            QPlainTextEdit {{
                background-color: {self._theme.panel_bg};
                color: {self._theme.text_primary};
                border: 1px solid {self._theme.border};
                border-radius: 8px;
                padding: 10px;
                selection-background-color: {self._theme.accent_soft};
                font-size: 14px;
            }}
            QPlainTextEdit:focus {{ border-color: {self._theme.accent_border}; }}
            """
        )
        root.addWidget(self.editor, 1)

        utility_row = QHBoxLayout()
        utility_row.setSpacing(8)

        self.load_last_btn = self._make_button("载入上次转写")
        self.load_last_btn.clicked.connect(self.loadLastRequested.emit)
        utility_row.addWidget(self.load_last_btn)

        self.next_pending_btn = self._make_button("待处理 0")
        self.next_pending_btn.setEnabled(False)
        self.next_pending_btn.clicked.connect(self._activate_next_pending)
        utility_row.addWidget(self.next_pending_btn)

        self.copy_btn = self._make_button("复制")
        self.copy_btn.clicked.connect(self._on_copy)
        utility_row.addWidget(self.copy_btn)

        utility_row.addStretch(1)
        root.addLayout(utility_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        action_row.addStretch(1)

        self.later_btn = self._make_button("稍后处理")
        self.later_btn.clicked.connect(self.hide)
        action_row.addWidget(self.later_btn)

        self.send_btn = self._make_button("投递到目标", primary=True)
        self.send_btn.setToolTip(
            "隐藏草稿箱后，把文字写入当时的前台目标；不会自动发送"
        )
        self.send_btn.clicked.connect(self._on_send)
        action_row.addWidget(self.send_btn)

        root.addLayout(action_row)

    def _make_button(self, text: str, *, primary: bool = False) -> QPushButton:
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(32)
        if primary:
            btn.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: {self._theme.accent};
                    color: #111111;
                    border: 1px solid {self._theme.accent_border};
                    border-radius: 6px;
                    padding: 0 14px;
                    font-weight: 700;
                }}
                QPushButton:hover {{ background-color: {self._theme.accent_hover}; }}
                """
            )
        else:
            btn.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: {self._theme.button_bg};
                    color: {self._theme.text_secondary};
                    border: 1px solid {self._theme.border};
                    border-radius: 6px;
                    padding: 0 12px;
                }}
                QPushButton:hover {{
                    background-color: {self._theme.button_hover_bg};
                    color: {self._theme.text_primary};
                    border-color: {self._theme.border_strong};
                }}
                """
            )
        return btn

    def current_text(self) -> str:
        """Return exact plaintext. Whitespace is preserved for document drafts."""
        return self.editor.toPlainText()

    def pending_count(self) -> int:
        return len(self._pending_drafts)

    def queue_draft(self, text: str, reason: str = "") -> int:
        """Retain an additional failed transcript without replacing the editor."""
        value = str(text or "")
        if not value.strip():
            return self.pending_count()
        self._pending_drafts.append((value, str(reason or "")))
        self._update_pending_button()
        partial_note = "，其中最新一条可能已部分上屏" if reason in self._PARTIAL_STATUSES else ""
        self.set_status(
            f"另有 {self.pending_count()} 条转写待处理{partial_note}；当前草稿没有被覆盖。",
            level="warning",
        )
        return self.pending_count()

    def set_draft(self, text: str, reason: str = "", *, replace: bool = True) -> None:
        """Load a draft and explain why it entered the fallback editor."""
        value = str(text or "")
        if not replace and self.current_text().strip():
            return
        self.editor.setPlainText(value)
        self._reason = str(reason or "")
        if self._reason in self._PARTIAL_STATUSES:
            self.set_status(
                "原目标可能已经收到部分文字。重新投递前请先检查，避免重复。",
                level="warning",
            )
        elif self._reason:
            self.set_status(
                "自动上屏未完成，完整转写已保留在草稿箱。",
                level="warning",
            )
        else:
            self.clear_status()

    def set_status(self, message: str, *, level: str = "info") -> None:
        palette = {
            "success": (self._theme.success, self._theme.accent_soft),
            "warning": (self._theme.accent_hover, self._theme.accent_soft),
            "error": (self._theme.danger, self._theme.danger_soft),
            "info": (self._theme.text_secondary, self._theme.button_bg),
        }
        color, background = palette.get(level, palette["info"])
        self.status_label.setText(str(message or ""))
        self.status_label.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.status_frame.setStyleSheet(
            f"QFrame#draftStatus {{ background: {background}; border: 1px solid {color}; border-radius: 6px; }}"
        )
        self.status_frame.setVisible(bool(message))

    def clear_status(self) -> None:
        self.status_label.clear()
        self.status_frame.hide()

    def _update_pending_button(self) -> None:
        count = self.pending_count()
        self.next_pending_btn.setText(f"待处理 {count}")
        self.next_pending_btn.setEnabled(count > 0)

    def _activate_next_pending(self) -> None:
        """Rotate drafts without deleting the currently edited text."""
        if not self._pending_drafts:
            self.set_status("没有其他待处理转写。", level="info")
            return

        current = self.current_text()
        current_reason = self._reason
        next_text, next_reason = self._pending_drafts.pop(0)
        if current.strip():
            self._pending_drafts.append((current, current_reason))
        self.set_draft(next_text, next_reason, replace=True)
        self._update_pending_button()

    def show_for_editing(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.editor.setFocus(Qt.OtherFocusReason)

    def _on_copy(self) -> None:
        text = self.current_text()
        if not text.strip():
            self.set_status("草稿为空，没有可复制的内容。", level="info")
            return
        self.copyRequested.emit(text)

    def _on_send(self) -> None:
        text = self.current_text()
        if not text.strip():
            self.set_status("草稿为空，没有可投递的内容。", level="info")
            return
        self.hide()
        self.sendRequested.emit(text)
