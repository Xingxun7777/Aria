"""
Elevation Warning Dialog
========================
Non-focus-stealing popup to display permission elevation warning.
Shows when Aria cannot inject text into elevated (admin) windows.

UI Design follows translation_popup.py style:
- Dark theme (#1E1E1E background)
- Non-focus-stealing (WS_EX_NOACTIVATE)
- Orange warning icon
- Two action buttons: Close Aria / Restart as Admin
"""

import sys
import ctypes
from typing import Optional
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QBrush, QPen, QFont
from PySide6.QtWidgets import (
    QWidget,
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QCheckBox,
    QGraphicsOpacityEffect,
)

# Debug logging
_DEBUG_LOG = Path(__file__).parent.parent.parent / "DebugLog" / "wakeword_debug.log"


def _log(msg: str):
    """Write debug message (pythonw.exe safe)."""
    import datetime

    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [ELEVATION] {msg}\n"
    if sys.stdout is not None:
        print(line.strip())
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


class ElevationWarningDialog(QWidget):
    """
    Non-focus-stealing popup for displaying elevation warning.

    Signals:
        closeRequested: User wants to close Aria
        restartAsAdminRequested: User wants to restart with admin privileges
    """

    closeRequested = Signal()
    restartAsAdminRequested = Signal()
    disableRequested = Signal()  # User wants to temporarily disable (not quit)

    # Style constants (matching translation_popup.py)
    POPUP_WIDTH = 380
    CORNER_RADIUS = 12
    BG_COLOR = QColor(30, 30, 30, 242)  # #1E1E1E with 95% opacity
    BORDER_COLOR = QColor(64, 64, 64)
    TEXT_COLOR = QColor(229, 229, 229)  # #E5E5E5
    SECONDARY_COLOR = QColor(156, 163, 175)  # #9CA3AF gray
    WARNING_COLOR = QColor(249, 115, 22)  # #F97316 orange
    PRIMARY_BTN_COLOR = QColor(37, 99, 235)  # #2563EB blue

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        # Window flags for non-activating popup
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        # State
        self._target_title: str = ""
        self._dont_remind_again: bool = False  # "下次不再提醒" checkbox state

        self._init_ui()
        self._init_animations()

    def _init_ui(self):
        """Initialize UI components."""
        self.setFixedWidth(self.POPUP_WIDTH)
        self.setMinimumHeight(160)

        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # Header row: warning icon + title + close button
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        # Warning icon (orange triangle)
        self._icon_label = QLabel("\u26A0\uFE0F")  # Warning emoji
        self._icon_label.setStyleSheet(
            f"""
            QLabel {{
                color: {self.WARNING_COLOR.name()};
                font-size: 18px;
                padding: 0;
                background: transparent;
            }}
        """
        )
        header_layout.addWidget(self._icon_label)

        # Title
        self._title_label = QLabel("\u6743\u9650\u4E0D\u8DB3")  # "权限不足"
        self._title_label.setStyleSheet(
            f"""
            QLabel {{
                color: {self.WARNING_COLOR.name()};
                font-size: 14px;
                font-weight: bold;
                padding: 0;
                background: transparent;
            }}
        """
        )
        header_layout.addWidget(self._title_label)
        header_layout.addStretch()

        # Close button (X)
        self._close_btn = QPushButton("\u2715")  # ✕
        self._close_btn.setFixedSize(24, 24)
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setStyleSheet(
            """
            QPushButton {
                background: transparent;
                border: none;
                color: #9CA3AF;
                font-size: 14px;
                font-weight: bold;
                padding: 0;
            }
            QPushButton:hover {
                color: #E5E5E5;
                background: rgba(255, 255, 255, 0.1);
                border-radius: 4px;
            }
        """
        )
        self._close_btn.clicked.connect(self._on_dismiss)
        header_layout.addWidget(self._close_btn)

        layout.addLayout(header_layout)

        # Main message
        self._message_label = QLabel(
            "\u65E0\u6CD5\u5411\u9AD8\u6743\u9650\u7A97\u53E3\u8F93\u5165\u6587\u5B57"
        )  # "无法向高权限窗口输入文字"
        self._message_label.setWordWrap(True)
        self._message_label.setStyleSheet(
            f"""
            QLabel {{
                color: {self.TEXT_COLOR.name()};
                font-size: 14px;
                padding: 0;
                background: transparent;
                line-height: 1.4;
            }}
        """
        )
        layout.addWidget(self._message_label)

        # Target info (gray, smaller)
        self._target_label = QLabel()
        self._target_label.setWordWrap(True)
        self._target_label.setStyleSheet(
            f"""
            QLabel {{
                color: {self.SECONDARY_COLOR.name()};
                font-size: 12px;
                padding: 0;
                background: transparent;
            }}
        """
        )
        layout.addWidget(self._target_label)

        # Spacer
        layout.addSpacing(8)

        # "Don't remind again" checkbox
        self._dont_remind_checkbox = QCheckBox(
            "\u4E0B\u6B21\u4E0D\u518D\u63D0\u9192"
        )  # "下次不再提醒"
        self._dont_remind_checkbox.setStyleSheet(
            f"""
            QCheckBox {{
                color: {self.SECONDARY_COLOR.name()};
                font-size: 12px;
                background: transparent;
                spacing: 6px;
            }}
            QCheckBox::indicator {{
                width: 14px;
                height: 14px;
                border: 1px solid #6B7280;
                border-radius: 3px;
                background: transparent;
            }}
            QCheckBox::indicator:checked {{
                background: {self.PRIMARY_BTN_COLOR.name()};
                border-color: {self.PRIMARY_BTN_COLOR.name()};
            }}
            QCheckBox::indicator:hover {{
                border-color: #9CA3AF;
            }}
        """
        )
        self._dont_remind_checkbox.stateChanged.connect(self._on_dont_remind_changed)
        layout.addWidget(self._dont_remind_checkbox)

        layout.addSpacing(4)

        # Button row
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(12)

        # Disable button (secondary - gray border) - temporarily disable, not quit
        self._close_aria_btn = QPushButton("\u6682\u65F6\u7981\u7528")  # "暂时禁用"
        self._close_aria_btn.setFixedHeight(36)
        self._close_aria_btn.setCursor(Qt.PointingHandCursor)
        self._close_aria_btn.setStyleSheet(
            """
            QPushButton {
                background: transparent;
                border: 1px solid #6B7280;
                border-radius: 6px;
                color: #E5E5E5;
                font-size: 13px;
                padding: 0 16px;
            }
            QPushButton:hover {
                background: rgba(255, 255, 255, 0.1);
                border-color: #9CA3AF;
            }
            QPushButton:pressed {
                background: rgba(255, 255, 255, 0.05);
            }
        """
        )
        self._close_aria_btn.clicked.connect(self._on_close_aria)
        btn_layout.addWidget(self._close_aria_btn)

        # Restart as Admin button (primary - blue)
        self._admin_btn = QPushButton(
            "\u4EE5\u7BA1\u7406\u5458\u8EAB\u4EFD\u91CD\u542F"
        )  # "以管理员身份重启"
        self._admin_btn.setFixedHeight(36)
        self._admin_btn.setCursor(Qt.PointingHandCursor)
        self._admin_btn.setStyleSheet(
            f"""
            QPushButton {{
                background: {self.PRIMARY_BTN_COLOR.name()};
                border: none;
                border-radius: 6px;
                color: white;
                font-size: 13px;
                font-weight: 500;
                padding: 0 16px;
            }}
            QPushButton:hover {{
                background: #3B82F6;
            }}
            QPushButton:pressed {{
                background: #1D4ED8;
            }}
        """
        )
        self._admin_btn.clicked.connect(self._on_restart_admin)
        btn_layout.addWidget(self._admin_btn)

        layout.addLayout(btn_layout)

        # Shortcut hints
        hint_layout = QHBoxLayout()
        hint_layout.setContentsMargins(0, 4, 0, 0)
        hint_layout.setSpacing(0)

        self._hint_close = QLabel("(Esc)")
        self._hint_close.setAlignment(Qt.AlignCenter)
        self._hint_close.setStyleSheet(
            f"""
            QLabel {{
                color: {self.SECONDARY_COLOR.name()};
                font-size: 10px;
                background: transparent;
            }}
        """
        )
        hint_layout.addWidget(self._hint_close)

        self._hint_admin = QLabel("(Enter)")
        self._hint_admin.setAlignment(Qt.AlignCenter)
        self._hint_admin.setStyleSheet(
            f"""
            QLabel {{
                color: {self.SECONDARY_COLOR.name()};
                font-size: 10px;
                background: transparent;
            }}
        """
        )
        hint_layout.addWidget(self._hint_admin)

        layout.addLayout(hint_layout)

    def _init_animations(self):
        """Initialize show/hide animations."""
        # Opacity effect
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity_effect)

        # Fade in animation (150ms)
        self._fade_in = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_in.setDuration(150)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.OutCubic)

        # Fade out animation (100ms)
        self._fade_out = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_out.setDuration(100)
        self._fade_out.setStartValue(1.0)
        self._fade_out.setEndValue(0.0)
        self._fade_out.setEasingCurve(QEasingCurve.InCubic)
        self._fade_out.finished.connect(self._on_fade_out_finished)

    def paintEvent(self, event):
        """Custom paint for rounded rectangle background."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Draw rounded rectangle background
        painter.setBrush(QBrush(self.BG_COLOR))
        painter.setPen(QPen(self.BORDER_COLOR, 1))
        painter.drawRoundedRect(
            self.rect().adjusted(1, 1, -1, -1),
            self.CORNER_RADIUS,
            self.CORNER_RADIUS,
        )

    def showEvent(self, event):
        """Apply Win32 extended styles and start animation."""
        super().showEvent(event)
        self._apply_win32_styles()
        self._fade_in.start()
        _log("Elevation dialog shown")

    def _apply_win32_styles(self):
        """Apply Win32 extended window styles for non-activation."""
        if sys.platform != "win32":
            return

        try:
            hwnd = int(self.winId())

            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_TOPMOST = 0x00000008

            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd,
                GWL_EXSTYLE,
                style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
            )

            # Force topmost
            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            )
        except Exception as e:
            _log(f"Failed to apply Win32 styles: {e}")

    def show_warning(self, message: str = "", target_title: str = ""):
        """
        Show the elevation warning dialog.

        Args:
            message: Custom warning message (optional)
            target_title: Title of the target elevated window (optional)
        """
        _log(f"show_warning called: msg={message[:50] if message else 'default'}...")

        # Check if user has opted out of reminders
        if self._load_dont_remind_preference():
            _log("User opted out of elevation reminders, auto-disabling")
            # Directly emit disable signal without showing dialog
            QTimer.singleShot(10, lambda: self.disableRequested.emit())
            return

        self._target_title = target_title

        # Reset checkbox state (don't carry over from previous show)
        self._dont_remind_checkbox.setChecked(False)

        # Update target info if provided
        if target_title:
            self._target_label.setText(f"\u76EE\u6807: {target_title}")  # "目标: ..."
            self._target_label.show()
        else:
            self._target_label.hide()

        # Update message if custom provided
        if message:
            self._message_label.setText(message)
        else:
            self._message_label.setText(
                "\u65E0\u6CD5\u5411\u9AD8\u6743\u9650\u7A97\u53E3\u8F93\u5165\u6587\u5B57"
            )  # "无法向高权限窗口输入文字"

        # Stop any running fade-out animation
        self._fade_out.stop()
        self._opacity_effect.setOpacity(1.0)

        # Position in center of primary screen
        self._position_center()

        # Show and animate
        self.show()
        self.raise_()

    def _position_center(self):
        """Position dialog in center of primary screen."""
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return

        screen_geo = screen.availableGeometry()
        self.adjustSize()

        x = screen_geo.center().x() - self.width() // 2
        y = screen_geo.center().y() - self.height() // 2

        self.move(x, y)

    def keyPressEvent(self, event):
        """Handle keyboard shortcuts: Esc = close, Enter = admin restart."""
        if event.key() == Qt.Key_Escape:
            self._on_close_aria()
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._on_restart_admin()
        else:
            super().keyPressEvent(event)

    def _on_dismiss(self):
        """Just hide the dialog without action."""
        _log("Dismiss button clicked")
        self._start_fade_out()

    def _on_close_aria(self):
        """User clicked 'Disable' button - temporarily disable hotkey listening."""
        _log("Disable button clicked (temporarily disable)")
        self._start_fade_out()
        # Emit disableRequested signal (not closeRequested - we don't want to quit)
        QTimer.singleShot(50, lambda: self.disableRequested.emit())

    def _on_restart_admin(self):
        """User clicked 'Restart as Admin' button."""
        _log("Restart as Admin button clicked")
        self._start_fade_out()
        # Emit signal after a small delay
        QTimer.singleShot(50, lambda: self.restartAsAdminRequested.emit())

    def _start_fade_out(self):
        """Start fade out animation."""
        self._fade_out.start()

    def _on_fade_out_finished(self):
        """Hide window after fade out."""
        self.hide()
        self._opacity_effect.setOpacity(1.0)  # Reset for next show

    def _on_dont_remind_changed(self, state: int):
        """Handle checkbox state change."""
        self._dont_remind_again = state == 2  # Qt.Checked = 2
        _log(f"Don't remind again: {self._dont_remind_again}")
        # Save preference to config
        self._save_dont_remind_preference()

    def _save_dont_remind_preference(self):
        """Save 'don't remind again' preference to config."""
        try:
            import json
            from aria.core.utils import get_config_path

            config_path = get_config_path("hotwords.json")
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            if "elevation_dialog" not in config:
                config["elevation_dialog"] = {}
            config["elevation_dialog"]["dont_remind"] = self._dont_remind_again

            import os

            tmp_path = str(config_path) + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, config_path)

            _log(f"Saved dont_remind preference: {self._dont_remind_again}")
        except Exception as e:
            _log(f"Failed to save dont_remind preference: {e}")

    def _load_dont_remind_preference(self) -> bool:
        """Load 'don't remind again' preference from config."""
        try:
            import json
            from aria.core.utils import get_config_path

            config_path = get_config_path("hotwords.json")
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            return config.get("elevation_dialog", {}).get("dont_remind", False)
        except Exception as e:
            _log(f"Failed to load dont_remind preference: {e}")
            return False
