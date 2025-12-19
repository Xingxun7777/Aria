# overlay.py
# Recording overlay window with Win32 non-activating behavior
# Based on F3 spec section 4.2

import sys
import ctypes
from PySide6.QtCore import Qt, Slot, QTimer
from PySide6.QtWidgets import QWidget, QLabel, QHBoxLayout, QApplication
from . import styles


class RecordingOverlay(QWidget):
    """
    Floating capsule overlay that shows recording status.
    Uses Win32 API to ensure it never steals focus.
    """

    def __init__(self):
        super().__init__()

        # Window flags for non-activating overlay
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        self._init_ui()

        self.current_text = ""
        self.is_final = False

    def _init_ui(self):
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setAlignment(Qt.AlignCenter)

        # Capsule container
        self.capsule = QWidget()
        self.capsule.setObjectName("capsule")
        self.capsule.setStyleSheet(styles.STYLESHEET_OVERLAY)
        self.capsule.setFixedWidth(400)
        self.capsule.setFixedHeight(50)

        # Inner layout
        capsule_layout = QHBoxLayout(self.capsule)
        capsule_layout.setContentsMargins(20, 5, 20, 5)

        # Status icon
        self.status_label = QLabel("🔴")
        self.status_label.setObjectName("statusIcon")

        # Text display
        self.text_label = QLabel("正在聆听...")
        self.text_label.setObjectName("transcript")
        self.text_label.setAlignment(Qt.AlignCenter)

        capsule_layout.addWidget(self.status_label)
        capsule_layout.addWidget(self.text_label)
        capsule_layout.addStretch()

        self.layout.addWidget(self.capsule)

        # Position at bottom center
        self.resize(800, 100)
        self._move_to_bottom_center()

    def _move_to_bottom_center(self):
        screen = self.screen()
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return  # No screen available
        geometry = screen.geometry()
        x = (geometry.width() - self.width()) // 2
        y = geometry.height() - 150
        self.move(x, y)

    def showEvent(self, event):
        """Apply Win32 extended styles for non-activation."""
        super().showEvent(event)

        if sys.platform != "win32":
            return

        try:
            hwnd = int(self.winId())

            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_TOPMOST = 0x00000008
            WS_EX_TRANSPARENT = 0x00000020

            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)

            user32.SetWindowLongW(
                hwnd,
                GWL_EXSTYLE,
                style
                | WS_EX_NOACTIVATE
                | WS_EX_TOOLWINDOW
                | WS_EX_TOPMOST
                | WS_EX_TRANSPARENT,
            )

            # Force topmost
            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)

        except Exception as e:
            print(f"Win32 API Error: {e}")

    @Slot(str)
    def on_state_changed(self, state: str):
        """Handle state changes from backend."""
        if state == "IDLE":
            self.hide()
        elif state == "RECORDING":
            self.status_label.setText("🔴")
            self.text_label.setText("Listening...")
            self.capsule.setStyleSheet(
                styles.STYLESHEET_OVERLAY
                + "QWidget#capsule { border: 1px solid #EF4444; }"
            )
            self._move_to_bottom_center()
            self.show()
        elif state == "TRANSCRIBING":
            self.status_label.setText("✨")
            self.text_label.setText("Polishing...")
            self.capsule.setStyleSheet(
                styles.STYLESHEET_OVERLAY
                + "QWidget#capsule { border: 1px solid #2563EB; }"
            )
            self.show()

    @Slot(str, bool)
    def on_text_updated(self, text: str, is_final: bool):
        """Handle text updates from backend (supports streaming interim results)."""
        self.text_label.setText(text)
        if is_final:
            # Final result: white bold text
            self.text_label.setStyleSheet("color: white; font-weight: bold;")
            self.status_label.setText("✨")  # Ready to insert
        else:
            # Interim result: gray italic text with streaming indicator
            self.text_label.setStyleSheet("color: #9CA3AF; font-style: italic;")
            self.status_label.setText("💭")  # Streaming/thinking

    @Slot(float)
    def on_level_changed(self, level: float):
        """Handle audio level changes for visual feedback."""
        # Could animate the capsule border or add a level indicator
        pass

    @Slot()
    def on_insert_complete(self):
        """Handle text insertion complete - hide overlay with brief delay."""
        # Show success briefly, then hide
        self.status_label.setText("✓")
        self.capsule.setStyleSheet(
            styles.STYLESHEET_OVERLAY + "QWidget#capsule { border: 1px solid #10B981; }"
        )
        # Hide after 500ms
        QTimer.singleShot(500, self.hide)
