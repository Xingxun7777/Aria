"""
Reminder Dialog
===============
Non-focus-stealing popup for reminder confirmation (undo model) and notification.

Two modes:
1. Confirmation: "已设置提醒: 开会 03-21 20:00 (3小时后) [撤销]"
2. Notification: "提醒时间到: 开会 [知道了]"

Follows ElevationWarningDialog pattern for window flags and styling.
"""

import sys
import ctypes
from typing import Optional

from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QApplication,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QBrush, QPen, QFont


def _dlog(msg: str):
    if sys.stdout is not None:
        print(f"[REMINDER_DLG] {msg}")


class ReminderDialog(QWidget):
    """Non-focus-stealing reminder popup (undo model confirmation + notification)."""

    undoClicked = Signal(str)  # reminder_id — user wants to cancel
    dismissClicked = Signal(str)  # reminder_id — user acknowledged

    POPUP_WIDTH = 360
    CORNER_RADIUS = 12
    BG_COLOR = QColor(30, 30, 30, 242)
    BORDER_COLOR = QColor(64, 64, 64)
    TEXT_COLOR = QColor(229, 229, 229)
    SECONDARY_COLOR = QColor(156, 163, 175)
    ACCENT_BLUE = QColor(59, 130, 246)  # #3B82F6
    ACCENT_AMBER = QColor(245, 158, 11)  # #F59E0B

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self._reminder_id = ""
        self._mode = "confirm"  # "confirm" or "notify"
        self._auto_dismiss_timer = QTimer(self)
        self._auto_dismiss_timer.setSingleShot(True)
        self._auto_dismiss_timer.timeout.connect(self._on_auto_dismiss)

        self._init_ui()

    def _apply_win32_noactivate(self):
        """Apply WS_EX_NOACTIVATE on Windows to prevent focus steal."""
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
        except Exception as e:
            _dlog(f"Win32 style failed: {e}")

    def _init_ui(self):
        self.setFixedWidth(self.POPUP_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        # Header: icon + title
        header = QHBoxLayout()
        header.setSpacing(8)
        self._icon = QLabel()
        self._icon.setStyleSheet("font-size: 18px; background: transparent;")
        header.addWidget(self._icon)

        self._title = QLabel()
        self._title.setStyleSheet(
            f"color: {self.TEXT_COLOR.name()}; font-size: 14px; "
            f"font-weight: bold; background: transparent;"
        )
        header.addWidget(self._title)
        header.addStretch()
        layout.addLayout(header)

        # Content
        self._content_label = QLabel()
        self._content_label.setWordWrap(True)
        self._content_label.setStyleSheet(
            f"color: {self.TEXT_COLOR.name()}; font-size: 15px; "
            f"background: transparent; padding: 4px 0;"
        )
        layout.addWidget(self._content_label)

        # Time display (for confirm mode)
        self._time_label = QLabel()
        self._time_label.setStyleSheet(
            f"color: {self.SECONDARY_COLOR.name()}; font-size: 12px; "
            f"background: transparent;"
        )
        layout.addWidget(self._time_label)

        # Button row
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        btn_layout.addStretch()

        self._undo_btn = QPushButton("撤销")
        self._undo_btn.setCursor(Qt.PointingHandCursor)
        self._undo_btn.setStyleSheet(
            f"""
            QPushButton {{
                background: rgba(239, 68, 68, 200);
                color: white;
                border: none;
                border-radius: 6px;
                padding: 6px 18px;
                font-size: 13px;
            }}
            QPushButton:hover {{ background: rgba(239, 68, 68, 255); }}
            """
        )
        self._undo_btn.clicked.connect(self._on_undo)
        btn_layout.addWidget(self._undo_btn)

        self._dismiss_btn = QPushButton("知道了")
        self._dismiss_btn.setCursor(Qt.PointingHandCursor)
        self._dismiss_btn.setStyleSheet(
            f"""
            QPushButton {{
                background: rgba(59, 130, 246, 200);
                color: white;
                border: none;
                border-radius: 6px;
                padding: 6px 18px;
                font-size: 13px;
            }}
            QPushButton:hover {{ background: rgba(59, 130, 246, 255); }}
            """
        )
        self._dismiss_btn.clicked.connect(self._on_dismiss)
        btn_layout.addWidget(self._dismiss_btn)

        layout.addLayout(btn_layout)

    def paintEvent(self, event):
        """Draw rounded rectangle background with border."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        path = QPainterPath()
        path.addRoundedRect(
            0.5,
            0.5,
            self.width() - 1,
            self.height() - 1,
            self.CORNER_RADIUS,
            self.CORNER_RADIUS,
        )

        painter.fillPath(path, QBrush(self.BG_COLOR))
        painter.setPen(QPen(self.BORDER_COLOR, 1))
        painter.drawPath(path)

    def show_confirm(
        self, reminder_id: str, content: str, trigger_display: str, anchor_pos=None
    ):
        """Show confirmation with confirm + cancel buttons near the floating ball."""
        self._mode = "confirm"
        self._reminder_id = reminder_id
        self._anchor_pos = anchor_pos

        self._icon.setText("\u23F0")  # Alarm clock emoji
        self._title.setText("设置提醒")
        self._title.setStyleSheet(
            f"color: {self.ACCENT_BLUE.name()}; font-size: 14px; "
            f"font-weight: bold; background: transparent;"
        )
        self._content_label.setText(content)
        self._time_label.setText(trigger_display)
        self._time_label.show()
        self._undo_btn.setText("取消")
        self._undo_btn.show()
        self._dismiss_btn.setText("确认")
        self._dismiss_btn.show()

        self._show_popup(auto_dismiss_ms=0)  # No auto-dismiss, wait for user
        _dlog(f"Confirm: id={reminder_id}, content='{content}', time={trigger_display}")

    def show_notify(
        self, reminder_id: str, content: str, batch_count: int = 0, anchor_pos=None
    ):
        """Show notification: reminder time has arrived."""
        self._mode = "notify"
        self._reminder_id = reminder_id
        self._anchor_pos = anchor_pos

        if batch_count > 1:
            self._icon.setText("\U0001F514")  # Bell emoji
            self._title.setText(f"有 {batch_count} 个提醒")
        else:
            self._icon.setText("\U0001F514")
            self._title.setText("提醒时间到")

        self._title.setStyleSheet(
            f"color: {self.ACCENT_AMBER.name()}; font-size: 14px; "
            f"font-weight: bold; background: transparent;"
        )
        self._content_label.setText(content)
        self._time_label.hide()
        self._undo_btn.hide()
        self._dismiss_btn.setText("知道了")
        self._dismiss_btn.show()

        self._show_popup(auto_dismiss_ms=30000)
        _dlog(f"Notify: id={reminder_id}, content='{content}', batch={batch_count}")

    def _show_popup(self, auto_dismiss_ms: int = 30000):
        """Position and show the popup. If already visible, hide first to reset."""
        # If already showing, stop existing timers/animations before reuse
        if self.isVisible():
            self._auto_dismiss_timer.stop()
            self.hide()

        self.adjustSize()

        # Position: above the floating ball (anchor), or fallback to screen center
        anchor = getattr(self, "_anchor_pos", None)
        if anchor:
            x = anchor.x() - self.width() // 2
            y = anchor.y() - self.height() - 10
            # Keep on screen
            screen = QApplication.primaryScreen()
            if screen:
                geo = screen.availableGeometry()
                x = max(geo.left() + 5, min(x, geo.right() - self.width() - 5))
                y = max(geo.top() + 5, y)
            self.move(x, y)
        else:
            screen = QApplication.primaryScreen()
            if screen:
                geo = screen.availableGeometry()
                x = geo.center().x() - self.width() // 2
                y = geo.center().y() - self.height() // 2
                self.move(x, y)

        self.setWindowOpacity(0.0)
        self.show()
        self._apply_win32_noactivate()

        # Fade in
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setDuration(200)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._fade_anim.start()

        # Auto-dismiss timer (0 = no auto-dismiss, user must click)
        if auto_dismiss_ms > 0:
            self._auto_dismiss_timer.start(auto_dismiss_ms)
        else:
            self._auto_dismiss_timer.stop()

    def _fade_out_and_close(self):
        """Fade out then hide."""
        self._auto_dismiss_timer.stop()
        anim = QPropertyAnimation(self, b"windowOpacity")
        anim.setDuration(150)
        anim.setStartValue(self.windowOpacity())
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.InCubic)
        anim.finished.connect(self.hide)
        anim.start()
        # Keep reference to prevent GC
        self._close_anim = anim

    def _on_undo(self):
        """User clicked undo — cancel the reminder."""
        _dlog(f"Undo clicked: {self._reminder_id}")
        self.undoClicked.emit(self._reminder_id)
        self._fade_out_and_close()

    def _on_dismiss(self):
        """User acknowledged the notification."""
        _dlog(f"Dismiss clicked: {self._reminder_id}")
        self.dismissClicked.emit(self._reminder_id)
        self._fade_out_and_close()

    def _on_auto_dismiss(self):
        """Auto-dismiss after timeout."""
        _dlog(f"Auto-dismiss: mode={self._mode}, id={self._reminder_id}")
        self._fade_out_and_close()

    def keyPressEvent(self, event):
        """Handle keyboard shortcuts."""
        if event.key() == Qt.Key_Escape:
            if self._mode == "confirm":
                self._on_undo()
            else:
                self._on_dismiss()
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self._mode == "notify":
                self._on_dismiss()
        else:
            super().keyPressEvent(event)
