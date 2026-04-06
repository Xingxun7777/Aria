# splash.py
# Mini glass widget splash with segmented progress
# Design: 220×110, frosted glass, 4-segment progress bar
# Optimized: Fixed memory leaks, thread safety, performance
# Fix: Removed QGraphicsDropShadowEffect (causes black window on Windows)

import sys
import math
from typing import Tuple, Optional
from PySide6.QtCore import (
    Qt,
    QTimer,
    Signal,
    QRectF,
    QPropertyAnimation,
    QEasingCurve,
    QPoint,
)
from PySide6.QtGui import (
    QPainter,
    QColor,
    QLinearGradient,
    QPen,
    QFont,
    QPainterPath,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QWidget


class Config:
    # Window size includes shadow padding
    SHADOW_PADDING = 20  # Extra space for manual shadow
    CONTENT_WIDTH = 220
    CONTENT_HEIGHT = 110
    WIDTH = CONTENT_WIDTH + SHADOW_PADDING * 2
    HEIGHT = CONTENT_HEIGHT + SHADOW_PADDING + 10  # Less padding on top
    RADIUS = 16

    # Dark glass (black-orange theme)
    BG = QColor(25, 25, 30, 230)
    BORDER = QColor(60, 60, 65, 200)

    # Multi-color gradient: warm orange tones with subtle variety
    # Progress bar uses a richer gradient for visual interest
    ACCENT_1 = QColor("#ff6b00")  # Deep orange (start)
    ACCENT_2 = QColor("#ff8c00")  # Bright orange (mid)
    ACCENT_3 = QColor("#ffaa33")  # Golden orange (end)

    TEXT_LIGHT = QColor("#f8f8f8")
    TEXT_MUTED = QColor("#a0a0a5")

    # 4 stages
    STAGES = ["python_env", "asr_model", "qt_ui", "audio_capture"]
    STAGE_RANGES = [(0, 5), (5, 50), (50, 80), (80, 100)]


class SplashWindow(QWidget):
    closed = Signal()

    def __init__(self, version: str = ""):
        super().__init__()
        self._version = version
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)  # Better transparency on Windows
        self.setFixedSize(Config.WIDTH, Config.HEIGHT)

        # Center on screen (with null check) - center the content area
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.geometry()
            # Adjust for shadow padding when centering
            self.move(
                (geo.width() - Config.WIDTH) // 2, (geo.height() - Config.HEIGHT) // 2
            )

        # State
        self._progress = 0.0
        self._display_progress = 0.0
        self._message = "Initializing..."
        self._closing = False
        self._phase = 0.0
        self._drag: Optional[QPoint] = None  # Fix: initialize drag position

        # Content offset (for manual shadow)
        self._content_x = Config.SHADOW_PADDING
        self._content_y = 10  # Less top padding

        # NOTE: Removed QGraphicsDropShadowEffect - causes black window on Windows
        # Shadow is now drawn manually in paintEvent

        # Cache gradients for performance
        # Wave bars: deep orange to bright orange
        self._wave_grad = QLinearGradient(0, 0, 0, 14)
        self._wave_grad.setColorAt(0, Config.ACCENT_2)
        self._wave_grad.setColorAt(1, Config.ACCENT_1)

        # Progress bar: rich multi-color gradient (deep orange → bright → golden)
        self._prog_grad = QLinearGradient(0, 0, Config.CONTENT_WIDTH, 0)
        self._prog_grad.setColorAt(0.0, Config.ACCENT_1)  # Deep orange
        self._prog_grad.setColorAt(0.5, Config.ACCENT_2)  # Bright orange
        self._prog_grad.setColorAt(1.0, Config.ACCENT_3)  # Golden orange

        # Animation timer - 30fps is sufficient for smooth UI
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)  # 30fps - balanced performance

        # Opacity animation with proper parent
        self._opacity_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._opacity_anim.setDuration(150)
        self._opacity_anim.setStartValue(1.0)
        self._opacity_anim.setEndValue(0.0)
        self._opacity_anim.setEasingCurve(QEasingCurve.OutQuad)
        self._opacity_anim.finished.connect(self._on_fade_finished)  # Connect once

    def _tick(self):
        """Animation tick - smooth progress interpolation."""
        diff = self._progress - self._display_progress
        if abs(diff) > 0.001:
            self._display_progress += diff * 0.15
        self._phase = (self._phase + 0.06) % (2 * math.pi)  # Keep bounded
        self.update()

    def _get_segment_fill(self, seg_idx: int) -> float:
        """Calculate fill percentage (0-1) for a segment."""
        if seg_idx < 0 or seg_idx >= len(Config.STAGE_RANGES):
            return 0.0
        start, end = Config.STAGE_RANGES[seg_idx]
        current = self._display_progress * 100

        if current <= start:
            return 0.0
        elif current >= end:
            return 1.0
        else:
            return (current - start) / (end - start)

    def paintEvent(self, event):
        """Draw splash screen with error handling."""
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            self._draw_background(p)
            self._draw_content(p)
        except Exception:
            # Fallback: simple white box
            p.fillRect(self.rect(), QColor(255, 255, 255, 220))
            p.setPen(Config.TEXT_LIGHT)
            p.drawText(self.rect(), Qt.AlignCenter, "Loading...")
        finally:
            p.end()

    def _draw_background(self, p: QPainter):
        """Draw frosted glass background with manual shadow."""
        cx, cy = self._content_x, self._content_y
        cw, ch = Config.CONTENT_WIDTH, Config.CONTENT_HEIGHT
        r = Config.RADIUS

        # Draw soft shadow manually (multiple semi-transparent ellipses)
        shadow_layers = [
            (8, QColor(0, 0, 0, 15)),
            (12, QColor(0, 0, 0, 10)),
            (16, QColor(0, 0, 0, 5)),
        ]
        for offset, color in shadow_layers:
            shadow_rect = QRectF(
                cx - offset / 2, cy + offset / 2, cw + offset, ch + offset / 2
            )
            shadow_path = QPainterPath()
            shadow_path.addRoundedRect(shadow_rect, r + offset / 4, r + offset / 4)
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawPath(shadow_path)

        # Content area shape
        content_rect = QRectF(cx, cy, cw, ch)
        shape = QPainterPath()
        shape.addRoundedRect(content_rect, r, r)

        # Glass background
        p.setPen(Qt.NoPen)
        p.setBrush(Config.BG)
        p.drawPath(shape)

        # Top highlight (subtle for dark theme)
        p.setClipPath(shape)
        highlight = QPainterPath()
        highlight.addRoundedRect(cx, cy, cw, ch * 0.5, r, r)
        p.setBrush(QColor(255, 255, 255, 15))
        p.drawPath(highlight)
        p.setClipping(False)

        # Border
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(Config.BORDER, 1))
        p.drawPath(shape)

    def _draw_content(self, p: QPainter):
        """Draw logo, status, and progress bar."""
        # Use content offset for all drawing
        cx, cy = self._content_x, self._content_y
        cw, ch = Config.CONTENT_WIDTH, Config.CONTENT_HEIGHT
        margin_x = 16
        margin_top = 20

        # Sound wave bars (offset by content position)
        wave_x, wave_y = cx + margin_x, cy + margin_top + 4
        bar_w, bar_gap = 3, 4
        bar_heights = [
            6 + 8 * abs(math.sin(self._phase)),
            6 + 8 * abs(math.sin(self._phase + 1.2)),
            6 + 8 * abs(math.sin(self._phase + 0.6)),
        ]

        # Update gradient position
        self._wave_grad.setStart(0, wave_y + 14)
        self._wave_grad.setFinalStop(0, wave_y)

        p.setPen(Qt.NoPen)
        for i, bh in enumerate(bar_heights):
            bx = wave_x + i * (bar_w + bar_gap)
            by = wave_y + (14 - bh) / 2
            bar_path = QPainterPath()
            bar_path.addRoundedRect(QRectF(bx, by, bar_w, bh), 1.5, 1.5)
            p.setBrush(self._wave_grad)
            p.drawPath(bar_path)

        # Logo + version
        logo_x = wave_x + 3 * (bar_w + bar_gap) + 6
        font = QFont("Segoe UI", 12, QFont.Bold)
        font.setLetterSpacing(QFont.AbsoluteSpacing, -0.3)
        p.setFont(font)
        p.setPen(Config.TEXT_LIGHT)
        p.drawText(int(logo_x), int(cy + margin_top + 13), "Aria")
        # Measure "Aria" width with the LOGO font before switching
        aria_width = p.fontMetrics().horizontalAdvance("Aria")

        if self._version:
            ver_font = QFont("Segoe UI", 7)
            p.setFont(ver_font)
            p.setPen(Config.TEXT_MUTED)
            ver_x = logo_x + aria_width + 5
            p.drawText(int(ver_x), int(cy + margin_top + 13), f"v{self._version}")

        # Status text (centered in content area)
        small_font = QFont("Segoe UI", 8)
        p.setFont(small_font)
        p.setPen(Config.TEXT_MUTED)
        fm = p.fontMetrics()
        text_w = fm.horizontalAdvance(self._message)
        p.drawText(
            int(cx + (cw - text_w) / 2), int(cy + margin_top + 38), self._message
        )

        # Segmented progress bar
        bar_y = cy + ch - margin_x - 3
        bar_h = 3
        total_w = cw - margin_x * 2
        seg_gap, seg_count = 4, 4
        seg_w = (total_w - seg_gap * (seg_count - 1)) / seg_count

        self._prog_grad.setStart(cx + margin_x, 0)
        self._prog_grad.setFinalStop(cx + margin_x + total_w, 0)

        for i in range(seg_count):
            seg_x = cx + margin_x + i * (seg_w + seg_gap)

            # Track (lighter for dark theme)
            track = QPainterPath()
            track.addRoundedRect(seg_x, bar_y, seg_w, bar_h, 1.5, 1.5)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 25))
            p.drawPath(track)

            # Fill
            fill_pct = self._get_segment_fill(i)
            if fill_pct > 0:
                fill_w = seg_w * fill_pct
                fill = QPainterPath()
                fill.addRoundedRect(seg_x, bar_y, fill_w, bar_h, 1.5, 1.5)
                p.setBrush(self._prog_grad)
                p.drawPath(fill)

    # --- Public API ---

    def set_progress(self, percent: int, message: Optional[str] = None):
        """
        Update progress (thread-safe via Qt event loop).

        Args:
            percent: Progress percentage (0-100)
            message: Optional status message
        """
        self._progress = max(0, min(100, percent)) / 100.0
        if message:
            self._message = message
        # Removed processEvents() - timer handles redraws automatically

    def fade_out_and_close(self, duration: int = 150):
        """Fade out and close window."""
        if self._closing:
            return
        self._closing = True
        self._timer.stop()

        if duration != self._opacity_anim.duration():
            self._opacity_anim.setDuration(duration)
        self._opacity_anim.start()

    def _on_fade_finished(self):
        """Handle fade animation completion."""
        self.closed.emit()
        self.close()

    def closeEvent(self, event):
        """Ensure timer is stopped on close."""
        self._timer.stop()
        super().closeEvent(event)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.LeftButton and self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)


def run_splash(address: Tuple[str, int]):
    """Entry point for splash subprocess with IPC."""
    from aria.progress_ipc import ProgressListener

    app = QApplication(sys.argv)
    splash = SplashWindow()
    splash.show()

    listener = ProgressListener(address)
    poll_timer = QTimer(splash)  # Parent ensures cleanup
    ready = False

    def poll():
        nonlocal ready
        if not ready:
            ready = listener.start(timeout=0.1)
            return
        ev = listener.poll(timeout=0.05)
        if ev:
            splash.set_progress(ev.percent, ev.message)
            if ev.stage in ("done", "error"):
                poll_timer.stop()
                listener.close()
                QTimer.singleShot(250, splash.fade_out_and_close)

    poll_timer.timeout.connect(poll)
    poll_timer.start(50)

    # Timeout fallback - closes if main process never signals done
    def timeout_handler():
        if not splash._closing:
            splash.fade_out_and_close()

    QTimer.singleShot(60000, timeout_handler)
    splash.closed.connect(app.quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    app = QApplication(sys.argv)
    splash = SplashWindow()
    splash.show()

    # Simulate 4 stages
    stages = [
        (600, 5, "Checking environment..."),
        (3000, 50, "Loading AI models..."),
        (800, 80, "Connecting engine..."),
        (500, 100, "Starting UI..."),
    ]
    stage_idx = [0]
    start_pct = [0]

    def animate_stage():
        idx = stage_idx[0]
        if idx >= len(stages):
            splash.set_progress(100, "Ready")
            QTimer.singleShot(400, splash.fade_out_and_close)
            return

        duration, end_pct, msg = stages[idx]
        splash._message = msg
        begin = start_pct[0]
        elapsed = [0]

        def step():
            elapsed[0] += 33
            t = min(elapsed[0] / duration, 1.0)
            current = begin + (end_pct - begin) * t
            splash.set_progress(int(current))

            if t < 1.0:
                QTimer.singleShot(33, step)
            else:
                start_pct[0] = end_pct
                stage_idx[0] += 1
                QTimer.singleShot(100, animate_stage)

        step()

    QTimer.singleShot(300, animate_stage)
    splash.closed.connect(app.quit)
    sys.exit(app.exec())
