# tray.py
# System tray icon and menu
# Based on F3 spec section 4.4

from PySide6.QtWidgets import QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QAction
from PySide6.QtCore import Signal

class SystemTray(QSystemTrayIcon):
    """System tray icon with context menu."""

    toggleRequested = Signal()
    settingsRequested = Signal()
    quitRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        # Use system theme icon as fallback
        self.setIcon(QIcon.fromTheme("audio-input-microphone"))
        self.setVisible(True)

        self.menu = QMenu()
        self._init_menu()
        self.setContextMenu(self.menu)

        self.activated.connect(self.on_activated)

    def _init_menu(self):
        action_record = QAction("开始/停止录音", self)
        action_record.triggered.connect(self.toggleRequested.emit)
        self.menu.addAction(action_record)

        self.menu.addSeparator()

        action_settings = QAction("设置...", self)
        action_settings.triggered.connect(self.settingsRequested.emit)
        self.menu.addAction(action_settings)

        self.menu.addSeparator()

        action_quit = QAction("退出", self)
        action_quit.triggered.connect(self.quitRequested.emit)
        self.menu.addAction(action_quit)

    def on_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.toggleRequested.emit()

    def set_recording_state(self, is_recording: bool):
        """Update tray icon to reflect recording state."""
        # Could switch between mic_idle and mic_recording icons
        tooltip = "VoiceType - 录音中..." if is_recording else "VoiceType"
        self.setToolTip(tooltip)
