# floating_ball.py
# Floating ball widget - main UI for VoiceType
# Left-click: show details, Middle-click: toggle ASR, Right-click: lock + transparency

import sys
import ctypes
import math
import time
from PySide6.QtCore import Qt, Signal, QPoint, QTimer, Slot, QSize, QPointF
from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout, QApplication, QGraphicsDropShadowEffect
from PySide6.QtGui import QPainter, QColor, QBrush, QPen, QRadialGradient, QConicalGradient, QCursor

from .popup_menu import PopupMenu


class FloatingBall(QWidget):
    """
    Floating ball widget that serves as the main VoiceType interface.

    Design:
    - Idle: Gray ball with subtle border
    - Recording: Gray ball with flowing rainbow border
    - Speaking: Rainbow border flows faster

    Interactions:
    - Left-click: Show details panel
    - Middle-click: Toggle ASR recording
    - Right-click: Lock position + enable click-through transparency
    - Drag: Move position (when unlocked)
    """

    # Signals
    toggleRequested = Signal()      # Middle-click: toggle ASR
    detailsRequested = Signal()     # Double-click: show settings
    menuRequested = Signal()        # Left-click: show popup menu
    lockToggled = Signal(bool)      # Right-click: lock state changed
    enableToggled = Signal(bool)    # From popup menu: enable/disable
    modeChanged = Signal(str)       # From popup menu: polish mode changed

    # Ball states
    STATE_IDLE = "idle"
    STATE_RECORDING = "recording"
    STATE_TRANSCRIBING = "transcribing"
    STATE_LOCKED = "locked"

    def __init__(self, size: int = 48):
        super().__init__()

        self.ball_size = size
        self._state = self.STATE_IDLE
        self._is_locked = False
        self._is_transparent = False
        self._drag_position = None
        self._click_pos = None  # For click vs drag detection
        self._asr_enabled = False
        self._is_speaking = False  # True when voice activity detected
        self._is_processing = False  # True when waiting for ASR/polish to complete

        # Double-click detection
        self._last_click_time = 0
        self._double_click_interval = 0.3  # 300ms for double-click

        # Popup menu
        self._popup_menu = None

        self._init_window()
        self._init_ui()
        self._init_popup_menu()

        # Animation timer for rainbow border
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._update_pulse)
        self._pulse_phase = 0.0
        self._rainbow_angle = 0.0  # For rainbow rotation

        # Icon scale animation (0.0 = small/idle, 1.0 = full/recording)
        self._icon_scale = 0.0
        self._icon_scale_target = 0.0

        # Window scale animation (0.85 = idle, 1.0 = active full)
        # 0.85 makes the ball slightly smaller when idle but clearly visible
        self._window_scale = 1.0  # Start at full size for visibility on launch
        self._window_scale_target = 0.85  # Will shrink to idle after a moment
        self._SCALE_IDLE = 0.85   # Constant for idle state (was 0.75, too small)
        self._SCALE_ACTIVE = 1.0  # Constant for active state

        # Audio level for waveform effect (0.0 - 1.0)
        self._audio_level = 0.0
        self._audio_level_smooth = 0.0  # Smoothed for display

        # Fallback shrink timer - ensures ball shrinks even if on_insert_complete is delayed
        self._shrink_fallback_timer = QTimer(self)
        self._shrink_fallback_timer.setSingleShot(True)
        self._shrink_fallback_timer.timeout.connect(self._force_shrink)

        # Debug log file for tracking state changes
        self._debug_log_path = None
        try:
            from pathlib import Path
            log_dir = Path(__file__).parent.parent.parent / "DebugLog"
            log_dir.mkdir(exist_ok=True)
            self._debug_log_path = log_dir / "floating_ball_debug.log"
            # Clear old log
            with open(self._debug_log_path, 'w', encoding='utf-8') as f:
                f.write(f"=== FloatingBall Debug Log ===\n")
        except Exception as e:
            print(f"[FloatingBall] Failed to create debug log: {e}")

        # Start animation timer immediately (needed for smooth transitions)
        self._pulse_timer.start(50)
        self._log(f"Initialized with scale={self._window_scale}, target={self._window_scale_target}")

    def _log(self, msg: str):
        """Write to debug log file."""
        import datetime
        timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{timestamp}] {msg}"
        print(f"[FloatingBall] {msg}")
        if self._debug_log_path:
            try:
                with open(self._debug_log_path, 'a', encoding='utf-8') as f:
                    f.write(line + "\n")
            except Exception:
                pass

    def _init_window(self):
        """Setup window flags for floating behavior."""
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.Tool |
            Qt.WindowStaysOnTopHint |
            Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        # Fixed size for the ball
        self.setFixedSize(self.ball_size + 10, self.ball_size + 10)

        # Initial position: bottom-right corner
        self._move_to_default_position()

    def _init_ui(self):
        """Initialize the ball appearance."""
        # Note: QGraphicsDropShadowEffect causes caching issues with dynamic repaints
        # The ball doesn't update visually when state changes because the effect caches the pixmap
        # Disabled for now to ensure state changes are visible
        pass

    def _init_popup_menu(self):
        """Initialize the popup menu."""
        self._popup_menu = PopupMenu()
        self._popup_menu.enableToggled.connect(self._on_menu_enable_toggled)
        self._popup_menu.modeChanged.connect(self._on_menu_mode_changed)
        self._popup_menu.settingsRequested.connect(self._on_menu_settings)

    def _on_menu_enable_toggled(self, enabled):
        """Handle enable toggle from popup menu."""
        self.enableToggled.emit(enabled)

    def _on_menu_mode_changed(self, mode):
        """Handle mode change from popup menu."""
        self.modeChanged.emit(mode)

    def _on_menu_settings(self):
        """Handle settings request from popup menu."""
        self.detailsRequested.emit()

    def show_popup_menu(self):
        """Show the popup menu at the ball's position."""
        if self._popup_menu:
            # Position above the ball
            ball_center = self.mapToGlobal(self.rect().center())
            self._popup_menu.showAt(ball_center)

    def _move_to_default_position(self):
        """Move ball to bottom-right corner."""
        screen = self.screen()
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        x = geometry.right() - self.width() - 50
        y = geometry.bottom() - self.height() - 100
        self.move(x, y)

    def paintEvent(self, event):
        """Draw the floating ball with rainbow border when recording."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Apply window scale transform (centered)
        center = self.rect().center()
        painter.translate(center)
        painter.scale(self._window_scale, self._window_scale)
        painter.translate(-center)

        radius = self.ball_size // 2

        # Ball body is always dark gray (glass effect)
        if self._is_locked:
            # Locked: lighter and more transparent
            base_color = QColor(60, 60, 65, 100)  # Semi-transparent gray
            glow_intensity = 40
        elif self._state == self.STATE_TRANSCRIBING:
            base_color = QColor(50, 50, 60)  # Slightly blue tint
            glow_intensity = 90
        else:
            base_color = QColor(40, 40, 45)  # Dark glass
            glow_intensity = 80

        # Draw glow gradient for ball body
        gradient = QRadialGradient(center, radius * 1.2)
        gradient.setColorAt(0, base_color)
        gradient.setColorAt(0.7, base_color)
        gradient.setColorAt(1, QColor(0, 0, 0, 0))

        # Draw ball body
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawEllipse(center, radius, radius)

        # Draw border - rainbow when recording, subtle white otherwise
        if self._state == self.STATE_RECORDING:
            self._draw_rainbow_border(painter, center, radius)
        elif self._state == self.STATE_TRANSCRIBING:
            # Subtle blue border when transcribing
            painter.setPen(QPen(QColor(100, 150, 255, 120), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, radius - 1, radius - 1)
        elif self._is_locked:
            # Very subtle border when locked (more transparent)
            painter.setPen(QPen(QColor(255, 255, 255, 25), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, radius - 1, radius - 1)
        else:
            # Subtle white border when idle
            painter.setPen(QPen(QColor(255, 255, 255, 40), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, radius - 1, radius - 1)

        # Draw icon/indicator
        self._draw_indicator(painter, center, radius)

    def _draw_rainbow_border(self, painter: QPainter, center: QPoint, radius: int):
        """Draw rainbow border - 3-segment style, faster+brighter when speaking."""
        import math

        angle = self._rainbow_angle
        gradient = QConicalGradient(QPointF(center), angle)

        # Breathing effect for waiting state
        breath = 0.5 + 0.5 * math.sin(self._pulse_phase * math.pi * 2)

        # Segment parameters
        seg_len = 0.22   # Rainbow segment length (~22% each)
        gap_len = 0.111  # Gap length (~11% each), total = 3*(22+11) = 99%

        # Rainbow colors - 2 colors per segment for smooth gradient
        rainbow = [
            (255, 120, 120),  # Soft red
            (255, 200, 100),  # Orange-yellow
            (180, 255, 150),  # Yellow-green
            (100, 220, 200),  # Green-cyan
            (120, 150, 255),  # Blue
            (200, 130, 255),  # Purple-pink
        ]

        if self._is_speaking:
            # Speaking: same 3-segment but more opaque and vivid
            base_alpha = 220
            border_width = 3.5
        else:
            # Waiting/Recording: clearly visible rainbow to show active state
            base_alpha = int(100 + 60 * breath)  # 100-160, clearly visible
            border_width = 2.5

        # Draw 3 segments with smooth gradient fade in/out
        for seg in range(3):
            seg_start = seg * (seg_len + gap_len)
            c1 = rainbow[seg * 2]
            c2 = rainbow[seg * 2 + 1]

            # Smooth fade in (longer gradient)
            gradient.setColorAt(seg_start, QColor(c1[0], c1[1], c1[2], 0))
            gradient.setColorAt(seg_start + 0.04, QColor(c1[0], c1[1], c1[2], base_alpha // 2))
            gradient.setColorAt(seg_start + 0.07, QColor(c1[0], c1[1], c1[2], base_alpha))

            # Middle gradient between two colors
            gradient.setColorAt(seg_start + seg_len * 0.5, QColor(c2[0], c2[1], c2[2], base_alpha))

            # Smooth fade out (longer gradient)
            gradient.setColorAt(seg_start + seg_len - 0.07, QColor(c2[0], c2[1], c2[2], base_alpha))
            gradient.setColorAt(seg_start + seg_len - 0.04, QColor(c2[0], c2[1], c2[2], base_alpha // 2))
            gradient.setColorAt(seg_start + seg_len, QColor(c2[0], c2[1], c2[2], 0))

        pen = QPen(QBrush(gradient), border_width)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(center, radius - 1, radius - 1)

    def _draw_indicator(self, painter: QPainter, center: QPoint, radius: int):
        """Draw status indicator with audio-reactive wavy ring effect."""
        import math
        from PySide6.QtGui import QPainterPath

        # Use _icon_scale for smooth transitions (not state directly)
        # _icon_scale animates: 0.0 (idle) <-> 1.0 (recording)
        icon_progress = self._icon_scale
        level = self._audio_level_smooth

        # Show recording indicator while animating (icon_progress > 0.05)
        if self._state == self.STATE_RECORDING or icon_progress > 0.05:
            # Core concept: dot expands into wavy ring
            # - Low/no audio: solid dot
            # - Audio detected: dot transforms into expanding wavy ring

            dot_radius = 2
            # Alpha fades with icon_progress for smooth transition
            base_alpha = int(180 * max(0.3, icon_progress))

            if level < 0.08:
                # No significant audio - just show solid dot
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(255, 255, 255, base_alpha))
                painter.drawEllipse(center, dot_radius, dot_radius)
            else:
                # Audio detected - draw wavy ring (dot has "expanded" into this)
                # Ring starts from dot size and expands with level
                # Map level 0.08-1.0 to expansion 0-1
                expansion = (level - 0.08) / 0.92

                # Ring parameters - starts small, grows significantly with audio
                # Use icon_progress for scaling during transition
                scale = 0.5 + 0.5 * icon_progress
                base_radius = dot_radius + 20 * expansion * scale  # Much larger expansion
                wave_amplitude = 3 * expansion * scale + 1  # More visible waves
                num_waves = 6  # Fewer waves = smoother look

                # Animation phase - slower rotation for smoother feel
                phase = self._pulse_phase * 2 * math.pi

                # Draw wavy ring using path
                path = QPainterPath()
                points = 72  # More points = smoother curve

                for i in range(points + 1):
                    angle = (i / points) * 2 * math.pi

                    # Gentler wave frequencies for organic but smooth look
                    wave1 = math.sin(angle * num_waves + phase * 1.5) * wave_amplitude
                    wave2 = math.sin(angle * (num_waves + 2) - phase) * wave_amplitude * 0.4
                    wave3 = math.sin(angle * (num_waves - 1) + phase * 0.8) * wave_amplitude * 0.2

                    # Total radius at this angle
                    r = base_radius + wave1 + wave2 + wave3

                    x = center.x() + r * math.cos(angle)
                    y = center.y() + r * math.sin(angle)

                    if i == 0:
                        path.moveTo(x, y)
                    else:
                        path.lineTo(x, y)

                path.closeSubpath()

                # Draw the wavy ring - alpha based on expansion and icon_progress
                ring_alpha = int((80 + 140 * expansion) * max(0.2, icon_progress))
                line_width = 1.0 + 1.0 * expansion
                painter.setPen(QPen(QColor(255, 255, 255, ring_alpha), line_width))
                painter.setBrush(Qt.NoBrush)
                painter.drawPath(path)

                # Optional: second outer ring for stronger signals
                if expansion > 0.5:
                    outer_path = QPainterPath()
                    outer_base = base_radius + 3
                    outer_amp = wave_amplitude * 0.4

                    for i in range(points + 1):
                        angle = (i / points) * 2 * math.pi
                        wave = math.sin(angle * num_waves - phase * 2) * outer_amp
                        r = outer_base + wave

                        x = center.x() + r * math.cos(angle)
                        y = center.y() + r * math.sin(angle)

                        if i == 0:
                            outer_path.moveTo(x, y)
                        else:
                            outer_path.lineTo(x, y)

                    outer_path.closeSubpath()
                    outer_factor = (expansion - 0.5) / 0.5
                    outer_alpha = int(80 * outer_factor)
                    painter.setPen(QPen(QColor(255, 255, 255, outer_alpha), 0.8))
                    painter.drawPath(outer_path)

        elif self._state == self.STATE_TRANSCRIBING:
            # Transcribing: rotating arc (processing)
            scale = 0.5 + 0.5 * icon_progress
            arc_size = int(12 * scale)
            painter.setPen(QPen(QColor(255, 255, 255, 200), 2 * scale))
            painter.setBrush(Qt.NoBrush)
            arc_angle = int(self._rainbow_angle * 16)
            painter.drawArc(center.x() - arc_size//2, center.y() - arc_size//2,
                          arc_size, arc_size, arc_angle, 270 * 16)

        elif self._is_locked:
            # Locked: subtle lock icon (more transparent)
            painter.setPen(QPen(QColor(255, 255, 255, 100), 1.5))
            painter.setBrush(Qt.NoBrush)
            # Larger lock icon
            painter.drawRect(int(center.x() - 5), int(center.y() - 1), 10, 8)
            painter.drawArc(int(center.x() - 4), int(center.y() - 8), 8, 10, 0, 180 * 16)

        else:
            # Idle: small subtle dot
            dot_r = 2
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 60))
            painter.drawEllipse(center, dot_r, dot_r)

    def _update_pulse(self):
        """Update animation - smooth rotation, faster when speaking."""
        self._pulse_phase = (self._pulse_phase + 0.03) % 1.0  # Slower breathing

        # Rainbow rotation - always smooth, speed varies
        if self._is_speaking:
            # Fast rotation when speaking (~120 deg/sec at 30fps = 4 deg/frame)
            self._rainbow_angle = (self._rainbow_angle + 4.0) % 360.0
        elif self._state == self.STATE_RECORDING:
            # Gentle rotation when waiting (~45 deg/sec at 30fps = 1.5 deg/frame)
            self._rainbow_angle = (self._rainbow_angle + 1.5) % 360.0

        # Smooth icon scale animation
        if self._icon_scale != self._icon_scale_target:
            diff = self._icon_scale_target - self._icon_scale
            self._icon_scale += diff * 0.1  # Smooth easing
            if abs(diff) < 0.01:
                self._icon_scale = self._icon_scale_target

        # Smooth window scale animation (silky smooth)
        if self._window_scale != self._window_scale_target:
            diff = self._window_scale_target - self._window_scale
            self._window_scale += diff * 0.12  # Gentle easing for smooth feel
            if abs(diff) < 0.005:
                self._window_scale = self._window_scale_target
            # Debug: log when scale is changing significantly
            if abs(diff) > 0.05:
                print(f"[FloatingBall] Animating scale: {self._window_scale:.2f} -> {self._window_scale_target:.2f}")

        # Smooth audio level (gentle attack, very smooth decay)
        if self._audio_level > self._audio_level_smooth:
            # Gentle attack - not too fast
            self._audio_level_smooth += (self._audio_level - self._audio_level_smooth) * 0.15
        else:
            # Very smooth decay for natural fade out
            self._audio_level_smooth += (self._audio_level - self._audio_level_smooth) * 0.06

        self.update()

    # === Mouse Events ===

    def mousePressEvent(self, event):
        """Handle mouse press for drag start."""
        if event.button() == Qt.LeftButton and not self._is_locked:
            self._drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._click_pos = event.globalPosition().toPoint()  # Record for click detection
        event.accept()

    def mouseMoveEvent(self, event):
        """Handle mouse move for dragging."""
        if event.buttons() == Qt.LeftButton and self._drag_position and not self._is_locked:
            self.move(event.globalPosition().toPoint() - self._drag_position)
        event.accept()

    def mouseReleaseEvent(self, event):
        """Handle mouse release."""
        if event.button() == Qt.LeftButton:
            if not self._is_locked and self._click_pos:
                # Check if it was a click (not a drag)
                moved = (event.globalPosition().toPoint() - self._click_pos).manhattanLength()
                if moved < 10:  # Small movement = click
                    current_time = time.time()
                    time_since_last = current_time - self._last_click_time

                    if time_since_last < self._double_click_interval:
                        # Double-click: open settings directly
                        self._last_click_time = 0  # Reset to prevent triple-click
                        self.detailsRequested.emit()
                    else:
                        # Single-click: show popup menu
                        self._last_click_time = current_time
                        self.show_popup_menu()
            self._drag_position = None
            self._click_pos = None
        elif event.button() == Qt.MiddleButton:
            if not self._is_locked:
                self.toggleRequested.emit()
        elif event.button() == Qt.RightButton:
            self._toggle_lock()
        event.accept()

    def _toggle_lock(self):
        """Toggle lock state and visual transparency (no click-through)."""
        self._is_locked = not self._is_locked
        self._is_transparent = self._is_locked

        # Note: We do NOT enable WS_EX_TRANSPARENT anymore
        # This allows right-click to still work for unlocking
        # The ball just becomes visually dimmed and ignores drag/left/middle clicks

        # When locked, shrink to idle size; when unlocked, restore based on state
        if self._is_locked:
            self._window_scale_target = self._SCALE_IDLE  # Shrink to idle size
            self._icon_scale_target = 0.0    # Hide active icon
        else:
            # Restore based on current state and processing flag
            if self._state == self.STATE_RECORDING or self._state == self.STATE_TRANSCRIBING or self._is_processing:
                self._window_scale_target = self._SCALE_ACTIVE
                self._icon_scale_target = 1.0
            else:
                self._window_scale_target = self._SCALE_IDLE
                self._icon_scale_target = 0.0

        self.lockToggled.emit(self._is_locked)
        self.update()

    def _update_click_through(self):
        """Update Win32 click-through style."""
        if sys.platform != 'win32':
            return

        try:
            hwnd = int(self.winId())

            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_TOPMOST = 0x00000008

            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)

            if self._is_transparent:
                # Enable click-through
                new_style = style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
            else:
                # Disable click-through
                new_style = (style & ~WS_EX_TRANSPARENT) | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST

            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)

            # Force topmost
            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                              SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception as e:
            print(f"Win32 API Error: {e}")

    def showEvent(self, event):
        """Apply Win32 styles on show."""
        super().showEvent(event)

        if sys.platform != 'win32':
            return

        try:
            hwnd = int(self.winId())

            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_TOPMOST = 0x00000008

            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                 style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)

            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
        except Exception as e:
            print(f"Win32 API Error: {e}")

    # === State Management ===

    @Slot(str)
    def on_state_changed(self, state: str):
        """Handle state changes from backend."""
        old_state = self._state
        self._log(f"STATE: {old_state} -> {state}, processing={self._is_processing}, scale={self._window_scale:.2f}, target={self._window_scale_target:.2f}")

        if state == "IDLE":
            self._state = self.STATE_IDLE
            self._is_speaking = False
            # Only shrink if NOT processing (backend finished)
            if not self._is_processing:
                self._icon_scale_target = 0.0
                self._window_scale_target = self._SCALE_IDLE
                self._log(f"IDLE (not processing): SHRINK to {self._SCALE_IDLE}")
            else:
                # Still processing - start short fallback timer (3 seconds)
                self._shrink_fallback_timer.start(3000)
                self._log(f"IDLE (processing): waiting for on_insert_complete, fallback 3s")
        elif state == "RECORDING":
            self._state = self.STATE_RECORDING
            self._is_processing = False
            self._shrink_fallback_timer.stop()
            # Recording indicator on, but window stays small until voice detected
            self._icon_scale_target = 1.0
            self._window_scale_target = self._SCALE_IDLE
            self._log(f"RECORDING: scale={self._SCALE_IDLE}")
        elif state == "TRANSCRIBING":
            self._state = self.STATE_TRANSCRIBING
            self._is_speaking = False
            self._is_processing = True  # Backend is processing!
            # Ball stays BIG while backend is processing
            # Don't change scale here - keep current size
            self._log(f"TRANSCRIBING: processing=True, scale stays {self._window_scale_target}")

        if old_state != self._state:
            self.update()

    @Slot(bool)
    def on_voice_activity(self, is_speaking: bool):
        """Handle voice activity detection updates."""
        self._log(f"VOICE: {is_speaking}, state={self._state}, scale={self._window_scale:.2f}")
        if self._is_speaking != is_speaking:
            self._is_speaking = is_speaking

            # Adjust timer speed and size based on voice activity
            if self._state == self.STATE_RECORDING:
                if is_speaking:
                    # Speaking: GROW the ball (backend has work to do)
                    self._window_scale_target = self._SCALE_ACTIVE
                    self._pulse_timer.start(33)  # 30 FPS
                    self._log(f">>> GROW ball to {self._SCALE_ACTIVE}")
                else:
                    # Stopped speaking but still recording: keep big
                    self._pulse_timer.start(50)  # 20 FPS
                    self._log(f"Voice stopped, scale stays {self._window_scale_target}")

            self.update()

    @Slot(float)
    def on_level_changed(self, level: float):
        """Handle audio level updates for waveform visualization."""
        self._audio_level = min(1.0, level * 6.0)  # High sensitivity for better visual response

    @Slot(str, bool)
    def on_text_updated(self, text: str, is_final: bool):
        """Handle text updates (for tooltip or status)."""
        if is_final and text:
            self.setToolTip(f"Last: {text[:50]}...")
        elif text:
            self.setToolTip(f"Listening: {text[:30]}...")

    @Slot()
    def on_insert_complete(self):
        """Handle text insertion complete - shrink ball now that processing is done."""
        self._log(f"INSERT_COMPLETE: state={self._state}, processing={self._is_processing}")

        # Cancel fallback timer
        self._shrink_fallback_timer.stop()
        self._is_processing = False

        # Always shrink when processing is complete
        # This is the correct behavior - ball should be small when backend is idle
        self._is_speaking = False
        self._icon_scale_target = 0.0
        self._window_scale_target = self._SCALE_IDLE
        self._log(f">>> SHRINK to {self._SCALE_IDLE}")

        self.update()

    def _force_shrink(self):
        """Fallback: Force shrink if on_insert_complete didn't arrive in time."""
        self._log(f">>> FALLBACK_SHRINK triggered, processing={self._is_processing}")
        if self._is_processing:
            # on_insert_complete didn't arrive, force shrink anyway
            self._is_processing = False
            self._is_speaking = False
            self._icon_scale_target = 0.0
            self._window_scale_target = self._SCALE_IDLE
            self._log(f">>> SHRINK (fallback) to {self._SCALE_IDLE}")
            self.update()

    def unlock(self):
        """Unlock the ball (called externally to restore interactivity)."""
        if self._is_locked:
            self._is_locked = False
            self._is_transparent = False
            self._update_click_through()
            self.lockToggled.emit(False)
            self.update()

    def set_polish_mode(self, mode: str) -> None:
        """
        Set polish mode on popup menu (for syncing with other UI components).

        Args:
            mode: "fast" or "quality"
        """
        if self._popup_menu:
            self._popup_menu.setMode(mode)
