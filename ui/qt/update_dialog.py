"""Update dialog for Aria auto-update (v1.0.5 spec).

Non-modal, presents 3 choices:
    - 立即重启并更新 → triggers backend.apply_staged_update()
    - 下次启动时更新 → do nothing (state stays 'ready', next boot will re-surface)
    - 跳过此版本 → adds to update_prefs.skipped_versions
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QTextBrowser,
    QWidget,
)


class UpdateDialog(QDialog):
    """Frameless dark dialog shown when an update is staged and ready."""

    def __init__(
        self,
        local_version: str,
        remote_version: str,
        notes_summary: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._result_choice = "dismissed"
        self._local = local_version
        self._remote = remote_version

        self.setWindowTitle("Aria 有新版本")
        self.setModal(False)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(420)

        # Apply dark theme to the dialog itself (avoid white flash)
        self.setStyleSheet(
            """
            QDialog { background-color: #1e1e1e; color: #e0e0e0; }
            QLabel { color: #e0e0e0; }
            QTextBrowser {
                background-color: #2a2a2a;
                color: #cccccc;
                border: 1px solid #3a3a3a;
                border-radius: 6px;
                padding: 8px;
            }
            QPushButton {
                background-color: #3a3a3a;
                color: #e0e0e0;
                border: 1px solid #4a4a4a;
                border-radius: 6px;
                padding: 6px 14px;
                min-width: 90px;
            }
            QPushButton:hover { background-color: #4a4a4a; }
            QPushButton#primary {
                background-color: #3a6ea5;
                border-color: #4a7eb5;
            }
            QPushButton#primary:hover { background-color: #4a7eb5; }
            """
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel(f"v{local_version}  →  v{remote_version}")
        font = QFont()
        font.setPointSize(12)
        font.setBold(True)
        title.setFont(font)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        hint = QLabel("新版本已下载并校验。请选择应用时机：")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        if notes_summary:
            browser = QTextBrowser()
            browser.setPlainText(notes_summary)
            browser.setMaximumHeight(140)
            browser.setReadOnly(True)
            layout.addWidget(browser)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._btn_now = QPushButton("立即重启并更新")
        self._btn_now.setObjectName("primary")
        self._btn_now.clicked.connect(self._on_apply_now)

        self._btn_later = QPushButton("下次启动时更新")
        self._btn_later.clicked.connect(self._on_later)

        self._btn_skip = QPushButton("跳过此版本")
        self._btn_skip.clicked.connect(self._on_skip)

        btn_row.addWidget(self._btn_now)
        btn_row.addStretch(1)
        btn_row.addWidget(self._btn_later)
        btn_row.addWidget(self._btn_skip)

        layout.addLayout(btn_row)

    @property
    def choice(self) -> str:
        """One of: 'apply_now' | 'later' | 'skip' | 'dismissed'."""
        return self._result_choice

    def _on_apply_now(self):
        self._result_choice = "apply_now"
        self.accept()

    def _on_later(self):
        self._result_choice = "later"
        self.accept()

    def _on_skip(self):
        self._result_choice = "skip"
        self.accept()


def show_update_dialog(
    local_version: str,
    remote_version: str,
    notes_summary: str,
    on_apply_now: Callable[[], None],
    on_skip: Callable[[str], None],
    parent: QWidget | None = None,
) -> UpdateDialog:
    """Show the dialog non-modally. Handlers fire on button click."""
    dlg = UpdateDialog(local_version, remote_version, notes_summary, parent)
    result = dlg.exec()  # modal for now; keeps focus but non-blocking is fine too
    if dlg.choice == "apply_now":
        on_apply_now()
    elif dlg.choice == "skip":
        on_skip(remote_version)
    return dlg
