# tray.py
# System tray icon and menu
# Based on F3 spec section 4.4

from PySide6.QtWidgets import QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QAction, QPixmap, QPainter, QColor, QPen, QBrush
from PySide6.QtCore import Signal, Qt


def create_aria_icon(size: int = 64, recording: bool = False) -> QIcon:
    """Create a professional microphone + waveform Aria icon."""
    from PySide6.QtGui import QRadialGradient, QPainterPath
    from PySide6.QtCore import QPointF, QRectF

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)

    center = size // 2
    radius = size // 2 - 2

    # Gradient background (matching floating ball style)
    gradient = QRadialGradient(center, center, radius)
    gradient.setColorAt(0, QColor(50, 50, 55, 245))
    gradient.setColorAt(0.7, QColor(35, 35, 40, 245))
    gradient.setColorAt(1.0, QColor(25, 25, 30, 245))

    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(gradient))
    p.drawEllipse(center - radius, center - radius, radius * 2, radius * 2)

    # Subtle border
    border_color = QColor("#ff8c00") if not recording else QColor("#ff4500")
    p.setPen(QPen(border_color, 1.5))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(
        center - radius + 1, center - radius + 1, (radius - 1) * 2, (radius - 1) * 2
    )

    # === Microphone (left side) ===
    mic_color = QColor("#ffffff") if not recording else border_color
    p.setPen(Qt.NoPen)
    p.setBrush(mic_color)

    # Microphone head (rounded rectangle)
    mic_w = size * 0.18
    mic_h = size * 0.28
    mic_x = center - size * 0.22
    mic_y = center - mic_h * 0.6
    p.drawRoundedRect(QRectF(mic_x, mic_y, mic_w, mic_h), mic_w * 0.4, mic_w * 0.4)

    # Microphone stand (arc + line)
    p.setPen(QPen(mic_color, size * 0.04))
    p.setBrush(Qt.NoBrush)

    # Arc under mic head
    arc_w = mic_w * 1.4
    arc_h = mic_h * 0.5
    arc_x = mic_x - (arc_w - mic_w) / 2
    arc_y = mic_y + mic_h - arc_h * 0.3
    p.drawArc(QRectF(arc_x, arc_y, arc_w, arc_h), 0, -180 * 16)

    # Stand line
    stand_x = mic_x + mic_w / 2
    stand_top = arc_y + arc_h / 2
    stand_bottom = center + size * 0.25
    p.drawLine(QPointF(stand_x, stand_top), QPointF(stand_x, stand_bottom))

    # Stand base
    base_w = size * 0.15
    p.drawLine(
        QPointF(stand_x - base_w / 2, stand_bottom),
        QPointF(stand_x + base_w / 2, stand_bottom),
    )

    # === Sound waves (right side) ===
    wave_color = border_color if recording else QColor("#ff8c00")
    wave_alpha = 255 if recording else 200

    wave_x = center + size * 0.05
    wave_y = center

    # 3 curved wave arcs
    for i, (wave_r, alpha_mult) in enumerate([(0.12, 1.0), (0.22, 0.7), (0.32, 0.4)]):
        wave_radius = size * wave_r
        wave_pen = QPen(
            QColor(
                wave_color.red(),
                wave_color.green(),
                wave_color.blue(),
                int(wave_alpha * alpha_mult),
            ),
            size * 0.035,
        )
        wave_pen.setCapStyle(Qt.RoundCap)
        p.setPen(wave_pen)
        p.setBrush(Qt.NoBrush)

        # Draw arc (right-facing sound wave)
        arc_rect = QRectF(
            wave_x - wave_radius, wave_y - wave_radius, wave_radius * 2, wave_radius * 2
        )
        p.drawArc(arc_rect, -60 * 16, 120 * 16)  # 120 degree arc facing right

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
        self._icon_idle = create_aria_icon(64, recording=False)
        self._icon_recording = create_aria_icon(64, recording=True)
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
        tooltip = "Aria-Dev - 录音中..." if is_recording else "Aria-Dev"
        self.setToolTip(tooltip)
