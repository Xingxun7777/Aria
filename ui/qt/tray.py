# tray.py
# System tray icon and menu
# Based on F3 spec section 4.4

from PySide6.QtWidgets import QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QAction, QPixmap, QPainter, QColor, QPen, QBrush
from PySide6.QtCore import Signal, Qt


def create_voicetype_icon(size: int = 64, recording: bool = False) -> QIcon:
    """Create a black-orange VoiceType icon programmatically."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)

    center = size // 2
    radius = size // 2 - 4

    # Dark circle background
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(30, 30, 35, 240))
    p.drawEllipse(center - radius, center - radius, radius * 2, radius * 2)

    # Orange border
    orange = QColor("#ff8c00") if not recording else QColor("#ff5500")
    p.setPen(QPen(orange, 3))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(
        center - radius + 2, center - radius + 2, (radius - 2) * 2, (radius - 2) * 2
    )

    # Sound wave bars (3 bars)
    bar_color = orange
    p.setPen(Qt.NoPen)
    p.setBrush(bar_color)

    bar_w = size // 10
    bar_gap = size // 12
    total_w = bar_w * 3 + bar_gap * 2
    start_x = center - total_w // 2

    heights = (
        [size // 4, size // 2.5, size // 4]
        if not recording
        else [size // 3, size // 2, size // 3]
    )
    for i, h in enumerate(heights):
        x = start_x + i * (bar_w + bar_gap)
        y = center - h // 2
        p.drawRoundedRect(int(x), int(y), bar_w, int(h), 2, 2)

    p.end()
    return QIcon(pixmap)


class SystemTray(QSystemTrayIcon):
    """System tray icon with context menu."""

    toggleRequested = Signal()
    settingsRequested = Signal()
    quitRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        # Custom black-orange icon
        self._icon_idle = create_voicetype_icon(64, recording=False)
        self._icon_recording = create_voicetype_icon(64, recording=True)
        self.setIcon(self._icon_idle)
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
        self.setIcon(self._icon_recording if is_recording else self._icon_idle)
        tooltip = "VoiceType-Dev - 录音中..." if is_recording else "VoiceType-Dev"
        self.setToolTip(tooltip)
