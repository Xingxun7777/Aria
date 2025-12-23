"""
Translation Popup Widget
========================
Non-focus-stealing popup to display translation results.
Designed for VoiceType v1.1 action-driven architecture.

UI Spec (from Gemini consultation):
- Size: 360px fixed width, height adaptive (max 300px)
- Position: Cursor right-bottom (offset 10, 20), auto-avoid screen edges
- Background: #1E1E1E 95% opacity, rounded 8px
- Animation: Appear 150ms (Opacity + Scale), disappear 100ms
- Interaction: Click anywhere to copy and close; Esc to close
- Auto-dismiss: 1.5s after mouse leaves
"""

import sys
import ctypes
from typing import Optional
from pathlib import Path

# Debug logging for pythonw.exe compatibility
_DEBUG_LOG = Path(__file__).parent.parent.parent / "DebugLog" / "wakeword_debug.log"


def _tlog(msg: str):
    """Write translation popup debug message (pythonw.exe safe)."""
    # RAW DEBUG: Write immediately on function entry
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[RAW] _tlog entered with: {msg[:50]}...\n")
    except Exception:
        pass

    import datetime

    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [TPOPUP] {msg}\n"
    if sys.stdout is not None:
        print(line.strip())
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


from PySide6.QtCore import (
    Qt,
    Signal,
    QTimer,
    QPropertyAnimation,
    QEasingCurve,
    Property,
    QPoint,
)
from PySide6.QtGui import QCursor, QGuiApplication, QColor, QPainter, QBrush, QPen
from PySide6.QtWidgets import (
    QWidget,
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QApplication,
    QGraphicsOpacityEffect,
    QGraphicsDropShadowEffect,
    QScrollArea,
    QFrame,
)


class TranslationPopup(QWidget):
    """
    Non-focus-stealing popup for displaying translation results.

    Signals:
        copyRequested: Emitted when user clicks to copy translation
        closed: Emitted when popup is closed
    """

    copyRequested = Signal(str)  # Emits translated text for copying
    closed = Signal()

    # Style constants
    POPUP_WIDTH = 420
    MAX_HEIGHT = 520
    CORNER_RADIUS = 8
    BG_COLOR = QColor(30, 30, 30, 242)  # #1E1E1E with 95% opacity
    BORDER_COLOR = QColor(64, 64, 64)
    TEXT_COLOR = QColor(229, 229, 229)
    SOURCE_COLOR = QColor(156, 163, 175)  # Gray for source text
    LOADING_COLOR = QColor(147, 197, 253)  # Light blue for loading

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

        self._init_ui()
        self._init_animations()

        # State
        self._current_request_id: Optional[str] = None
        self._translated_text: str = ""
        self._is_loading: bool = False

        # Auto-dismiss timer (1.5s after mouse leaves)
        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self._on_dismiss_timeout)

    def _init_ui(self):
        """Initialize UI components."""
        self.setFixedWidth(self.POPUP_WIDTH)
        self.setMinimumHeight(60)
        self.setMaximumHeight(self.MAX_HEIGHT)

        # Main layout with padding
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # Header row: title + close button
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        # Title label
        self._title_label = QLabel("译文")
        self._title_label.setStyleSheet(
            f"""
            QLabel {{
                color: {self.LOADING_COLOR.name()};
                font-size: 11px;
                font-weight: bold;
                padding: 0;
                background: transparent;
            }}
        """
        )
        header_layout.addWidget(self._title_label)
        header_layout.addStretch()

        # Close button
        self._close_btn = QPushButton("✕")
        self._close_btn.setFixedSize(20, 20)
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
        self._close_btn.clicked.connect(self._on_close_clicked)
        header_layout.addWidget(self._close_btn)

        layout.addLayout(header_layout)

        # Source text label (smaller, gray)
        self._source_label = QLabel()
        self._source_label.setWordWrap(True)
        self._source_label.setStyleSheet(
            f"""
            QLabel {{
                color: {self.SOURCE_COLOR.name()};
                font-size: 12px;
                padding: 0;
                background: transparent;
            }}
        """
        )
        self._source_label.setMaximumHeight(40)
        layout.addWidget(self._source_label)

        # Divider line
        self._divider = QWidget()
        self._divider.setFixedHeight(1)
        self._divider.setStyleSheet(f"background: {self.BORDER_COLOR.name()};")
        layout.addWidget(self._divider)

        # Scroll area for long translation results
        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QFrame.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll_area.setStyleSheet(
            """
            QScrollArea {
                background: transparent;
                border: none;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 6px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 0.3);
                border-radius: 3px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(255, 255, 255, 0.5);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
        """
        )
        self._scroll_area.setMinimumHeight(150)  # Ensure readable area
        self._scroll_area.setMaximumHeight(380)  # Leave room for other elements

        # Translation result label (inside scroll area)
        self._result_label = QLabel()
        self._result_label.setWordWrap(True)
        self._result_label.setStyleSheet(
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
        # Note: Don't use TextSelectableByMouse - conflicts with WindowDoesNotAcceptFocus
        # The popup is designed for "click to copy all and close"
        self._result_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._scroll_area.setWidget(self._result_label)
        layout.addWidget(self._scroll_area)

        # Hint label (bottom)
        self._hint_label = QLabel("点击复制")
        self._hint_label.setAlignment(Qt.AlignRight)
        self._hint_label.setStyleSheet(
            f"""
            QLabel {{
                color: {self.SOURCE_COLOR.name()};
                font-size: 10px;
                padding: 0;
                background: transparent;
            }}
        """
        )
        layout.addWidget(self._hint_label)

        # Add shadow effect
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(20)
        self._shadow.setColor(QColor(0, 0, 0, 80))
        self._shadow.setOffset(0, 4)
        # Note: Shadow applied to content widget would interfere with opacity effect
        # So we draw the shadow in paintEvent instead

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
        _tlog("showEvent: ENTERED")
        super().showEvent(event)
        _tlog("showEvent: super().showEvent done")
        self._apply_win32_styles()
        _tlog("showEvent: _apply_win32_styles done")
        self._fade_in.start()
        _tlog("showEvent: _fade_in.start done")

    def _apply_win32_styles(self):
        """Apply Win32 extended window styles for non-activation."""
        _tlog("_apply_win32_styles: ENTERED")
        if sys.platform != "win32":
            _tlog("_apply_win32_styles: not win32, returning")
            return

        try:
            _tlog("_apply_win32_styles: getting winId")
            hwnd = int(self.winId())
            _tlog(f"_apply_win32_styles: hwnd={hwnd}")

            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_TOPMOST = 0x00000008

            _tlog("_apply_win32_styles: getting user32")
            user32 = ctypes.windll.user32
            _tlog("_apply_win32_styles: calling GetWindowLongW")
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            _tlog(f"_apply_win32_styles: style={style}")

            _tlog("_apply_win32_styles: calling SetWindowLongW")
            user32.SetWindowLongW(
                hwnd,
                GWL_EXSTYLE,
                style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
            )
            _tlog("_apply_win32_styles: SetWindowLongW done")

            # Force topmost
            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            _tlog("_apply_win32_styles: calling SetWindowPos")
            user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            )
            _tlog("_apply_win32_styles: SetWindowPos done - COMPLETE")

        except Exception as e:
            _tlog(f"_apply_win32_styles: EXCEPTION: {e}")

    def show_loading(self, source_text: str, request_id: str):
        """
        Show popup in loading state.

        Args:
            source_text: Original text being translated
            request_id: Unique request ID to match with result
        """
        # RAW DEBUG: Write BEFORE anything else
        try:
            with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
                f.write(f"[RAW] show_loading ENTERED: req={request_id}\n")
        except Exception:
            pass

        _tlog(
            f"show_loading START: request_id={request_id}, text_len={len(source_text)}"
        )
        try:
            self._current_request_id = request_id
            self._is_loading = True
            self._translated_text = ""
            _tlog("show_loading: state vars set")

            # Update title with character count
            text_len = len(source_text)
            self._title_label.setText(f"翻译 ({text_len}字)")
            _tlog("show_loading: title set")

            # Truncate long source text for display
            display_source = (
                source_text.strip().replace(chr(10), " ").replace(chr(13), "")
            )
            if len(display_source) > 80:
                display_source = display_source[:77] + "..."
            self._source_label.setText(f"原文: {display_source}")
            _tlog("show_loading: source label set")

            # Show loading state
            self._result_label.setText("正在翻译...")
            _tlog("show_loading: result label text set")
            self._result_label.setStyleSheet(
                f"""
                QLabel {{
                    color: {self.LOADING_COLOR.name()};
                    font-size: 14px;
                    font-style: italic;
                    padding: 0;
                    background: transparent;
                }}
            """
            )
            _tlog("show_loading: result label style set")
            self._hint_label.hide()
            _tlog("show_loading: hint label hidden")

            # Stop any running fade-out animation
            self._fade_out.stop()
            _tlog("show_loading: fade_out stopped")
            self._opacity_effect.setOpacity(1.0)
            _tlog("show_loading: opacity set to 1.0")

            # Position and show (adjustSize FIRST, then position)
            self.adjustSize()
            _tlog("show_loading: adjustSize done")
            self._position_near_cursor()
            _tlog("show_loading: position done")
            self.show()
            _tlog("show_loading: show() called - COMPLETE")
        except Exception as e:
            _tlog(f"show_loading ERROR: {e}")

    def show_result(self, translated_text: str, request_id: str):
        """
        Show translation result.

        Args:
            translated_text: Translated text
            request_id: Request ID (ignored if doesn't match current)
        """
        # Ignore stale responses
        if request_id != self._current_request_id:
            # Guard for pythonw.exe (sys.stdout is None)
            import sys

            if sys.stdout is not None:
                print(
                    f"[TranslationPopup] Ignoring stale response: {request_id} != {self._current_request_id}"
                )
            return

        self._is_loading = False
        self._translated_text = translated_text

        # Update title to show completion
        self._title_label.setText("译文")

        # Update display
        self._result_label.setText(translated_text)
        self._result_label.setStyleSheet(
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
        self._hint_label.show()

        # Reposition after content change
        self.adjustSize()
        self._position_near_cursor()

    def show_error(self, error_msg: str, request_id: str):
        """
        Show error state.

        Args:
            error_msg: Error message to display
            request_id: Request ID (ignored if doesn't match current)
        """
        if request_id != self._current_request_id:
            return

        self._is_loading = False
        self._translated_text = ""

        self._result_label.setText(f"翻译失败: {error_msg}")
        self._result_label.setStyleSheet(
            f"""
            QLabel {{
                color: #EF4444;
                font-size: 14px;
                padding: 0;
                background: transparent;
            }}
        """
        )
        self._hint_label.setText("点击关闭")
        self._hint_label.show()

        # Reposition after content change
        self.adjustSize()
        self._position_near_cursor()

    def _position_near_cursor(self):
        """Position popup near cursor with screen boundary awareness."""
        cursor_pos = QCursor.pos()
        screen = QGuiApplication.screenAt(cursor_pos)
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return

        screen_geo = screen.availableGeometry()

        # Default offset: right-bottom of cursor
        x = cursor_pos.x() + 10
        y = cursor_pos.y() + 20

        # Ensure popup stays within screen bounds
        popup_width = self.width()
        popup_height = self.height()

        # Adjust X if would go off right edge
        if x + popup_width > screen_geo.right():
            x = cursor_pos.x() - popup_width - 10

        # Adjust Y if would go off bottom edge
        if y + popup_height > screen_geo.bottom():
            y = cursor_pos.y() - popup_height - 10

        # Ensure not off left/top edges
        x = max(x, screen_geo.left())
        y = max(y, screen_geo.top())

        self.move(x, y)

    def mousePressEvent(self, event):
        """Handle click to copy and close."""
        if event.button() == Qt.LeftButton:
            _tlog(
                f"mousePressEvent: LeftButton clicked, has_text={bool(self._translated_text)}"
            )
            # Only copy if there's actual content (not just whitespace)
            if self._translated_text and self._translated_text.strip():
                # Copy directly here for reliability (in addition to signal)
                # This ensures copy works even if signal/slot has issues
                try:
                    clipboard = QApplication.clipboard()
                    clipboard.setText(self._translated_text)
                    _tlog(
                        f"mousePressEvent: Direct clipboard copy done, len={len(self._translated_text)}"
                    )
                except Exception as e:
                    _tlog(f"mousePressEvent: Clipboard error: {e}")
                    # Retry once
                    try:
                        QApplication.clipboard().setText(self._translated_text)
                        _tlog("mousePressEvent: Clipboard retry succeeded")
                    except Exception as retry_e:
                        _tlog(f"mousePressEvent: Clipboard retry failed: {retry_e}")

                # Also emit signal for any additional handlers
                self.copyRequested.emit(self._translated_text)
            self.dismiss()
            event.accept()
        else:
            super().mousePressEvent(event)

    def enterEvent(self, event):
        """Cancel auto-dismiss when mouse enters."""
        self._dismiss_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        """Start auto-dismiss timer when mouse leaves."""
        if not self._is_loading:
            self._dismiss_timer.start(1500)  # 1.5 seconds
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        """Handle Esc key to close."""
        if event.key() == Qt.Key_Escape:
            self.dismiss()
        else:
            super().keyPressEvent(event)

    def dismiss(self):
        """Start dismiss animation."""
        self._dismiss_timer.stop()
        self._fade_out.start()

    def _on_fade_out_finished(self):
        """Hide window after fade out."""
        self.hide()
        self._opacity_effect.setOpacity(1.0)  # Reset for next show
        self.closed.emit()

    def _on_close_clicked(self):
        """Handle close button click."""
        self.dismiss()

    def _on_dismiss_timeout(self):
        """Auto-dismiss after timeout."""
        self.dismiss()

    def reset(self):
        """Reset popup state for reuse."""
        self._dismiss_timer.stop()
        self._current_request_id = None
        self._translated_text = ""
        self._is_loading = False
        self._source_label.clear()
        self._result_label.clear()
        self._hint_label.setText("点击复制")
