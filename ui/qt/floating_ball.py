# floating_ball.py
# Floating ball widget - main UI for Aria
# Left-click: toggle ASR, Right-click: show popup menu (settings, lock, mode)
# Middle-click: lock position (legacy shortcut)

import sys
import ctypes
import math
import json
from PySide6.QtCore import (
    Qt,
    Signal,
    QPoint,
    QPointF,
    QRectF,
    QTimer,
    Slot,
    QPropertyAnimation,
    QEasingCurve,
    QParallelAnimationGroup,
)
from PySide6.QtWidgets import QWidget, QApplication, QLabel, QGraphicsOpacityEffect
from PySide6.QtGui import (
    QPainter,
    QColor,
    QBrush,
    QPen,
    QPixmap,
    QRadialGradient,
    QConicalGradient,
    QLinearGradient,
)

from .popup_menu import PopupMenu


class _StreamingLabel(QLabel):
    """QLabel with a 'frosted glass lite' panel for elegant readability.

    Three painted layers create a premium feel without actual backdrop blur:
      1. Semi-transparent warm-dark panel  (readable on any desktop)
      2. Top-edge highlight gradient       (simulates glass catching light)
      3. Hair-thin luminous border         (defines edges subtly)

    WA_TranslucentBackground on Windows breaks stylesheet background-color,
    so all visual layers are drawn by QPainter in paintEvent.
    """

    _BG = QColor(28, 25, 38, 130)  # warm purple-dark, ~51 % opacity
    _HL_TOP = QColor(255, 255, 255, 18)  # glass highlight start
    _HL_END = QColor(255, 255, 255, 0)  # glass highlight fade
    _BORDER = QColor(255, 255, 255, 20)  # barely-visible edge
    _R = 10.0  # corner radius

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(0, 0, -1, -1)

        # Layer 1 — warm semi-transparent panel
        p.setBrush(self._BG)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(r, self._R, self._R)

        # Layer 2 — top-edge highlight (glass catching overhead light)
        grad = QLinearGradient(0, 0, 0, min(r.height(), 22))
        grad.setColorAt(0.0, self._HL_TOP)
        grad.setColorAt(1.0, self._HL_END)
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(r, self._R, self._R)

        # Layer 3 — hair-thin luminous border
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(self._BORDER, 0.5))
        p.drawRoundedRect(r, self._R, self._R)

        p.end()
        # Text rendered by QLabel (stylesheet color)
        super().paintEvent(event)


class _NoticeLabel(QLabel):
    """Small quiet notice near the floating ball.

    This is intentionally not a system notification: no focus, no tray balloon,
    and no Windows notification sound.
    """

    _BG = QColor(28, 25, 38, 190)
    _BORDER = QColor(255, 255, 255, 24)
    _R = 10.0

    _ACCENTS = {
        "info": QColor(255, 170, 51, 210),
        "warning": QColor(255, 170, 51, 230),
        "success": QColor(76, 175, 80, 220),
        "error": QColor(255, 120, 90, 220),
    }

    def __init__(self):
        super().__init__()
        self._accent = self._ACCENTS["info"]
        self.setStyleSheet(
            """
            QLabel {
                background: transparent;
                color: rgba(255, 255, 255, 235);
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI";
                font-size: 12px;
                font-weight: 500;
                padding: 8px 13px 8px 15px;
            }
            """
        )
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setMaximumWidth(300)
        self.setWordWrap(True)

    def set_level(self, level: str):
        self._accent = self._ACCENTS.get(str(level or "info"), self._ACCENTS["info"])
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(0, 0, -1, -1)

        p.setPen(QPen(self._BORDER, 0.7))
        p.setBrush(self._BG)
        p.drawRoundedRect(r, self._R, self._R)

        # Small warm status dot, quieter than an icon and easy to ignore.
        p.setPen(Qt.NoPen)
        p.setBrush(self._accent)
        p.drawEllipse(QRectF(7, r.height() / 2 - 3, 6, 6))

        p.end()
        super().paintEvent(event)


def _load_local_sticker_config() -> dict | None:
    """Optional machine-local sticker easter-egg config.

    `config/sticker.local.json` is intentionally untracked — installs without
    the file simply have no sticker behavior.  Expected shape:
    `{"wakewords": ["..."], "sticker": "assets/example.png"}` (sticker path
    relative to the app root, or absolute).
    """
    import json
    from pathlib import Path

    path = Path(__file__).parent.parent.parent / "config" / "sticker.local.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    words = data.get("wakewords")
    if not isinstance(words, list) or not words:
        return None
    return data


class _StickerPopup(QWidget):
    """Transparent popup for showing a sticker image above the floating ball.

    Shown only when a machine-local config opts in — a cute sticker after
    successful command execution as easter egg feedback.
    """

    def __init__(self):
        super().__init__()
        from PySide6.QtWidgets import QVBoxLayout, QLabel

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self._pixmap = None
        self._load_sticker()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._img_label = QLabel()
        self._img_label.setStyleSheet("background: transparent;")
        self._img_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._img_label)

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self._fade_out)

    def _load_sticker(self):
        from pathlib import Path

        cfg = _load_local_sticker_config()
        if not cfg:
            return
        raw = str(cfg.get("sticker") or "")
        if not raw:
            return
        sticker_path = Path(raw)
        if not sticker_path.is_absolute():
            sticker_path = Path(__file__).parent.parent.parent / sticker_path
        if sticker_path.exists():
            self._pixmap = QPixmap(str(sticker_path))

    def show_sticker(self, anchor_pos):
        """Show sticker above the given anchor position (ball center)."""
        if not self._pixmap or self._pixmap.isNull():
            return

        size = 80
        scaled = self._pixmap.scaled(
            size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self._img_label.setPixmap(scaled)
        self.setFixedSize(scaled.width() + 4, scaled.height() + 4)

        # Center horizontally on anchor, above it
        x = anchor_pos.x() - self.width() // 2
        y = anchor_pos.y() - self.height() - 5
        self.move(x, y)

        # Show at zero opacity below final position, then animate in
        # (avoids 1-frame flash at full opacity on Windows)
        self._final_y = y
        self.setWindowOpacity(0.0)
        self.move(x, y + 15)  # Start 15px below final position
        self.show()
        self.raise_()

        def _start_fade():
            self._fade_in_group = QParallelAnimationGroup(self)

            # Opacity: 0 → 1
            opacity_anim = QPropertyAnimation(self, b"windowOpacity")
            opacity_anim.setDuration(350)
            opacity_anim.setStartValue(0.0)
            opacity_anim.setEndValue(1.0)
            opacity_anim.setEasingCurve(QEasingCurve.OutCubic)
            self._fade_in_group.addAnimation(opacity_anim)

            # Slide up: y+15 → y
            pos_anim = QPropertyAnimation(self, b"pos")
            pos_anim.setDuration(350)
            pos_anim.setStartValue(self.pos())
            pos_anim.setEndValue(QPoint(x, self._final_y))
            pos_anim.setEasingCurve(QEasingCurve.OutBack)
            self._fade_in_group.addAnimation(pos_anim)

            self._fade_in_group.start()

        QTimer.singleShot(10, _start_fade)
        self._dismiss_timer.start(1800)

    def _fade_out(self):
        anim = QPropertyAnimation(self, b"windowOpacity")
        anim.setDuration(300)
        anim.setStartValue(self.windowOpacity())
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.InCubic)
        anim.finished.connect(self.hide)
        anim.start()
        self._fade_out_anim = anim


class FloatingBall(QWidget):
    """
    Floating ball widget that serves as the main Aria interface.

    Design:
    - Idle: Gray ball with subtle border
    - Recording: Gray ball with flowing rainbow border
    - Speaking: Rainbow border flows faster

    Interactions:
    - Left-click: Toggle ASR recording on/off
    - Right-click: Show popup menu (enable toggle, polish mode, settings, lock)
    - Middle-click: Lock position (legacy shortcut)
    - Drag: Move position (when unlocked)
    """

    # Signals
    toggleRequested = Signal()  # Left-click: toggle ASR
    detailsRequested = Signal()  # From popup menu: open settings
    historyRequested = Signal()  # From popup menu: open history browser
    correctionRulesRequested = Signal()  # Open local explicit-rule manager
    pasteLastRequested = Signal()  # Recover the newest ASR text into current target
    copyLastRequested = Signal()  # Copy the newest ASR text to clipboard
    draftBoxRequested = Signal()  # Open the editable local fallback
    lockToggled = Signal(bool)  # Middle-click: lock state changed
    enableToggled = Signal(bool)  # From popup menu: enable/disable
    modeChanged = Signal(str)  # From popup menu: polish mode changed
    ocrModeChanged = Signal(str)  # From popup menu: OCR tier "off"|"auto"|"full"
    captureModeChanged = Signal(
        str
    )  # From popup menu: mic DSP "standard"|"noisy"|"whisper"
    asrDeviceModeChanged = Signal(str)  # From popup menu: "gpu" or "cpu"
    asrEngineModeChanged = Signal(str)  # From popup menu: target ASR engine id
    micInputGainChanged = Signal(float)  # From popup menu: software mic gain
    deepSleepToggled = Signal(bool)  # From popup menu: deep sleep toggle
    translateModeChanged = Signal(str)  # From popup menu: "popup" or "clipboard"
    outputModeChanged = Signal(str)  # From popup menu: "clipboard" or "typewriter"
    apiSwitchBackRequested = Signal()  # From popup menu: switch Polish API to primary
    deepseekSetupRequested = Signal()  # Open the guided DeepSeek API setup dialog

    # Ball states
    STATE_IDLE = "idle"
    STATE_RECORDING = "recording"
    STATE_TRANSCRIBING = "transcribing"
    STATE_LOCKED = "locked"
    STATE_SELECTION_LISTENING = (
        "selection_listening"  # Purple: waiting for voice command
    )
    STATE_SELECTION_PROCESSING = "selection_processing"  # Blue: processing with LLM
    STATE_SLEEPING = "sleeping"  # Light sleep: dim, ignore all input
    STATE_DEEP_SLEEPING = "deep_sleeping"  # Deep sleep: engine off, very dim
    STATE_LOADING = "loading"  # Engine reloading: amber pulse

    def __init__(self, size: int = 48):
        super().__init__()

        self.ball_size = size
        self._state = self.STATE_IDLE
        self._is_locked = False
        self._drag_position = None
        self._click_pos = None  # For click vs drag detection
        self._asr_enabled = False
        self._is_speaking = False  # True when voice activity detected
        self._is_processing = False  # True when waiting for ASR/polish to complete

        # Update badge (v1.0.5 auto-update): red dot overlay indicating ready-to-apply
        self._update_badge_visible = False
        self._update_badge_tooltip = ""

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
        self._SCALE_IDLE = 0.85  # Constant for idle state (was 0.75, too small)
        self._SCALE_ACTIVE = 1.0  # Constant for active state

        # Audio level for waveform effect (0.0 - 1.0)
        self._audio_level = 0.0
        self._audio_level_smooth = 0.0  # Smoothed for display

        # Fallback shrink timer - ensures ball shrinks even if on_insert_complete is delayed
        self._shrink_fallback_timer = QTimer(self)
        self._shrink_fallback_timer.setSingleShot(True)
        self._shrink_fallback_timer.timeout.connect(self._force_shrink)

        # Command execution visual feedback
        self._command_flash_active = False
        self._command_flash_color = QColor(100, 180, 255, 200)  # Default: blue

        # Slow pipeline stage glow (appears only after 3s threshold)
        self._slow_glow_active = False
        self._slow_glow_color = None

        # Sticker easter egg state
        self._pending_sticker = False

        # Bounce animation for command execution (physical feedback)
        self._bounce_active = False
        self._bounce_phase = 0.0  # 0.0 -> 1.0 during animation
        self._bounce_amplitude = 8.0  # Max pixels to move
        self._bounce_duration_frames = 12  # ~400ms at 33ms/frame

        # Press physics animation (design: "Alive Micro-Interactions")
        self._press_scale = 1.0  # 1.0 = normal, 0.9 = pressed
        self._press_scale_target = 1.0
        self._PRESS_SCALE_DOWN = 0.9  # Scale when "pressed"
        self._PRESS_SCALE_NORMAL = 1.0  # Normal scale

        # Corner radius for squircle effect (lock mode visual distinction)
        self._corner_radius = 28  # Full round (ball_size/2 ≈ 24)
        self._corner_radius_target = 28
        self._CORNER_CIRCLE = 28  # Fully round
        self._CORNER_SQUIRCLE = 10  # Rounded square for locked state

        # Auto-send state indicator (persistent color tint)
        self._auto_send_enabled = False

        # Current ASR engine info for popup display
        self._engine_info = "语音输入"
        self._asr_status = {}

        # Debug log file for tracking state changes (controlled by config)
        self._debug_log_path = None
        self._debug_logging_enabled = False
        try:
            import json
            from pathlib import Path

            config_path = (
                Path(__file__).parent.parent.parent / "config" / "hotwords.json"
            )
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    self._debug_logging_enabled = config.get("general", {}).get(
                        "debug_logging", False
                    )

            if self._debug_logging_enabled:
                log_dir = Path(__file__).parent.parent.parent / "DebugLog"
                log_dir.mkdir(exist_ok=True)
                self._debug_log_path = log_dir / "floating_ball_debug.log"
                # Clear old log
                with open(self._debug_log_path, "w", encoding="utf-8") as f:
                    f.write(f"=== FloatingBall Debug Log ===\n")
        except Exception as e:
            # Guard for pythonw.exe (sys.stdout is None)
            if sys.stdout is not None:
                print(f"[FloatingBall] Failed to init debug log: {e}")

        # Topmost guard: Windows silently strips WS_EX_TOPMOST in several
        # situations (fullscreen apps, session lock/unlock, explorer.exe
        # restarts, other apps reshuffling the z-order). Qt applies
        # WindowStaysOnTopHint only at native window creation, so once the
        # flag is lost the ball stays buried until the next hide/show cycle.
        # Re-check every 2s and re-assert only when actually lost.
        self._topmost_guard_timer = QTimer(self)
        self._topmost_guard_timer.timeout.connect(self._ensure_topmost)
        if sys.platform == "win32":
            self._topmost_guard_timer.start(2000)

        # Start animation timer immediately (needed for smooth transitions)
        self._pulse_timer.start(50)
        self._log(
            f"Initialized with scale={self._window_scale}, target={self._window_scale_target}"
        )

    def _log(self, msg: str):
        """Write to debug log file (only if debug_logging enabled in config, pythonw.exe safe)."""
        if not self._debug_logging_enabled:
            return
        import datetime
        import sys

        timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{timestamp}] {msg}"
        # Guard for pythonw.exe (sys.stdout is None)
        if sys.stdout is not None:
            print(f"[FloatingBall] {msg}")
        if self._debug_log_path:
            try:
                with open(self._debug_log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    def _init_window(self):
        """Setup window flags for floating behavior."""
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        # Fixed size for the ball
        self.setFixedSize(self.ball_size + 10, self.ball_size + 10)

        # Initial position: bottom-right corner
        self._move_to_default_position()

    def _init_ui(self):
        """Initialize the ball appearance and streaming text label."""
        # Sticker popup (easter egg)
        self._sticker_popup = _StickerPopup()

        # Floating text label for streaming ASR results
        # Design: "Phantom HUD" - elegant, warm, premium feel
        self._streaming_label = _StreamingLabel()
        self._streaming_label.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self._streaming_label.setAttribute(Qt.WA_TranslucentBackground)
        self._streaming_label.setAttribute(Qt.WA_ShowWithoutActivating)
        # Panel painted by _StreamingLabel.paintEvent (frosted glass lite).
        self._streaming_label.setStyleSheet(
            """
            QLabel {
                background: transparent;
                color: rgba(255, 255, 255, 230);
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI";
                font-size: 13px;
                font-weight: 400;
                padding: 10px 16px;
            }
            """
        )
        self._streaming_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._streaming_label.setMaximumWidth(320)
        self._streaming_label.setMaximumHeight(100)
        self._streaming_label.setWordWrap(True)
        self._streaming_label.hide()

        # Fade animation via window opacity (NOT QGraphicsOpacityEffect —
        # that breaks stylesheet background rendering with WA_TranslucentBackground on Windows)
        self._streaming_label.setWindowOpacity(0.0)

        self._streaming_fade_anim = QPropertyAnimation(
            self._streaming_label, b"windowOpacity"
        )
        self._streaming_fade_anim.setEasingCurve(QEasingCurve.OutCubic)

        # Position animation (slide effect)
        self._streaming_pos_anim = QPropertyAnimation(self._streaming_label, b"pos")
        self._streaming_pos_anim.setEasingCurve(QEasingCurve.OutCubic)

        # Combined animation group
        self._streaming_anim_group = QParallelAnimationGroup()
        self._streaming_anim_group.addAnimation(self._streaming_fade_anim)
        self._streaming_anim_group.addAnimation(self._streaming_pos_anim)

        # Store base position for animations
        self._streaming_base_pos = QPoint(0, 0)

        # Timer to auto-hide streaming label after inactivity
        self._streaming_hide_timer = QTimer(self)
        self._streaming_hide_timer.setSingleShot(True)
        self._streaming_hide_timer.timeout.connect(self._fade_out_streaming_label)

        # Streaming display enabled flag
        self._streaming_display_enabled = True

        # Streaming label state machine (prevents animation race conditions)
        # States: "hidden", "fading_in", "visible", "fading_out"
        self._streaming_state = "hidden"

        # Quiet runtime notice for recoverable issues ("未识别到有效路径" etc.).
        # Uses a local no-activate window instead of QMessageBox/tray balloons,
        # so failures do not steal focus or play Windows notification sounds.
        self._notice_label = _NoticeLabel()
        self._notice_label.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self._notice_label.setAttribute(Qt.WA_TranslucentBackground)
        self._notice_label.setAttribute(Qt.WA_ShowWithoutActivating)
        self._notice_label.setWindowOpacity(0.0)
        self._notice_label.hide()
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._fade_out_notice)
        self._notice_fade_anim = QPropertyAnimation(
            self._notice_label, b"windowOpacity"
        )
        self._notice_fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._last_notice_key = ""
        self._last_notice_at = 0
        self._notice_hide_connected = False
        self._gpu_install_notice_active = False

    def _trim_notice_text(self, text: str) -> str:
        text = " ".join(str(text or "").strip().split())
        if not text:
            return "未识别到有效内容"
        if len(text) > 48:
            return text[:45] + "..."
        return text

    def _update_notice_position(self):
        """Position notice near the ball without covering the right-click menu."""
        self._notice_label.adjustSize()
        label_size = self._notice_label.sizeHint()
        ball_top_left = self.mapToGlobal(QPoint(0, 0))
        ball_center = self.mapToGlobal(self.rect().center())

        x = ball_center.x() - label_size.width() // 2
        y = ball_top_left.y() - label_size.height() - 8

        screen = QApplication.screenAt(ball_center) or QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = max(geo.left() + 10, min(x, geo.right() - label_size.width() - 10))
            if y < geo.top() + 10:
                y = ball_top_left.y() + self.height() + 8

        self._notice_label.move(x, y)

    def show_notice(self, message: str, level: str = "info", duration_ms: int = 1800):
        """Show a quiet local notice near the floating ball."""
        import time

        text = self._trim_notice_text(message)
        level = str(level or "info")
        now_ms = int(time.monotonic() * 1000)
        key = f"{level}:{text}"
        if key == self._last_notice_key and now_ms - self._last_notice_at < 1200:
            return
        self._last_notice_key = key
        self._last_notice_at = now_ms

        self._notice_timer.stop()
        self._notice_fade_anim.stop()
        if self._notice_hide_connected:
            try:
                self._notice_fade_anim.finished.disconnect(self._hide_notice)
            except (RuntimeError, TypeError):
                pass
            self._notice_hide_connected = False
        self._notice_label.set_level(level)
        self._notice_label.setText(text)
        self._update_notice_position()
        self._notice_label.setWindowOpacity(0.0)
        self._notice_label.show()
        self._notice_label.raise_()

        self._notice_fade_anim.setDuration(130)
        self._notice_fade_anim.setStartValue(0.0)
        self._notice_fade_anim.setEndValue(1.0)
        self._notice_fade_anim.start()
        if level in ("warning", "error"):
            duration_ms = max(int(duration_ms), 3200)
        self._notice_timer.start(max(900, int(duration_ms)))

    def _fade_out_notice(self):
        if not self._notice_label.isVisible():
            return
        self._notice_fade_anim.stop()
        if self._notice_hide_connected:
            try:
                self._notice_fade_anim.finished.disconnect(self._hide_notice)
            except (RuntimeError, TypeError):
                pass
            self._notice_hide_connected = False
        self._notice_fade_anim.setDuration(220)
        self._notice_fade_anim.setStartValue(self._notice_label.windowOpacity())
        self._notice_fade_anim.setEndValue(0.0)
        self._notice_fade_anim.finished.connect(self._hide_notice)
        self._notice_hide_connected = True
        self._notice_fade_anim.start()

    def _hide_notice(self):
        self._notice_label.hide()
        if self._notice_hide_connected:
            try:
                self._notice_fade_anim.finished.disconnect(self._hide_notice)
            except (RuntimeError, TypeError):
                pass
            self._notice_hide_connected = False

    def _show_gpu_install_progress(self, percent: int, message: str = "") -> None:
        """Keep one in-place progress notice visible beside the floating ball."""
        self._gpu_install_notice_active = True
        self._notice_timer.stop()
        self._notice_fade_anim.stop()
        if self._notice_hide_connected:
            try:
                self._notice_fade_anim.finished.disconnect(self._hide_notice)
            except (RuntimeError, TypeError):
                pass
            self._notice_hide_connected = False
        text = f"GPU 加速安装 {max(0, min(100, int(percent)))}%"
        if message and int(percent) >= 90:
            text += f" · {message}"
        self._notice_label.set_level("info")
        self._notice_label.setText(self._trim_notice_text(text))
        self._update_notice_position()
        if not self._notice_label.isVisible():
            self._notice_label.setWindowOpacity(1.0)
            self._notice_label.show()
        else:
            self._notice_label.setWindowOpacity(1.0)
        self._notice_label.raise_()

    def _fade_out_streaming_label(self):
        """Fade out and hide the streaming label with slide-down animation."""
        # State machine guard: only fade out if visible or fading in
        if self._streaming_state not in ("visible", "fading_in"):
            return

        self._streaming_state = "fading_out"

        # Disconnect any existing finished signal to prevent race conditions
        try:
            self._streaming_anim_group.finished.disconnect(self._on_fade_out_finished)
        except (RuntimeError, TypeError):
            pass  # Not connected
        try:
            self._streaming_anim_group.finished.disconnect(self._on_fade_in_finished)
        except (RuntimeError, TypeError):
            pass  # Not connected

        # Stop any running animation
        self._streaming_anim_group.stop()

        # Current position
        current_pos = self._streaming_label.pos()
        end_pos = QPoint(current_pos.x(), current_pos.y() + 12)  # Slide down 12px

        # Configure fade out (opacity)
        self._streaming_fade_anim.setDuration(280)
        self._streaming_fade_anim.setStartValue(self._streaming_label.windowOpacity())
        self._streaming_fade_anim.setEndValue(0.0)
        self._streaming_fade_anim.setEasingCurve(QEasingCurve.InCubic)

        # Configure slide down (position)
        self._streaming_pos_anim.setDuration(280)
        self._streaming_pos_anim.setStartValue(current_pos)
        self._streaming_pos_anim.setEndValue(end_pos)
        self._streaming_pos_anim.setEasingCurve(QEasingCurve.InCubic)

        # Hide widget when animation finishes
        self._streaming_anim_group.finished.connect(self._on_fade_out_finished)
        self._streaming_anim_group.start()

    def _on_fade_out_finished(self):
        """Called when fade-out animation completes."""
        # State machine guard: only hide if we're actually fading out
        # (prevents race condition where fade-in interrupted fade-out)
        if self._streaming_state != "fading_out":
            return

        self._streaming_label.hide()
        self._streaming_state = "hidden"

        # Disconnect to avoid multiple connections
        try:
            self._streaming_anim_group.finished.disconnect(self._on_fade_out_finished)
        except (RuntimeError, TypeError):
            pass  # Already disconnected

    def _fade_in_streaming_label(self):
        """Fade in the streaming label with slide-up animation."""
        # State machine: if already visible and not fading out, just update position
        if self._streaming_state == "visible":
            self._update_streaming_label_position()
            return

        # Mark state transition
        was_fading_out = self._streaming_state == "fading_out"
        self._streaming_state = "fading_in"

        # Disconnect any existing finished signals to prevent race conditions
        try:
            self._streaming_anim_group.finished.disconnect(self._on_fade_out_finished)
        except (RuntimeError, TypeError):
            pass  # Not connected
        try:
            self._streaming_anim_group.finished.disconnect(self._on_fade_in_finished)
        except (RuntimeError, TypeError):
            pass  # Not connected

        # Stop any running animation
        self._streaming_anim_group.stop()

        # Calculate positions
        self._update_streaming_label_position()
        target_pos = self._streaming_label.pos()

        # Get current opacity for smooth transition (especially if interrupting fade-out)
        current_opacity = self._streaming_label.windowOpacity()

        # If interrupting a fade-out, start from current state
        if was_fading_out and self._streaming_label.isVisible():
            start_pos = self._streaming_label.pos()
            start_opacity = current_opacity
        else:
            # Fresh fade-in from below
            start_pos = QPoint(target_pos.x(), target_pos.y() + 10)  # Start 10px below
            start_opacity = 0.0
            self._streaming_label.setWindowOpacity(0.0)
            self._streaming_label.move(start_pos)

        # Ensure visible before animation
        if not self._streaming_label.isVisible():
            self._streaming_label.show()

        # Configure fade in (opacity) - shorter duration if already partially visible
        duration = 100 if was_fading_out else 180
        self._streaming_fade_anim.setDuration(duration)
        self._streaming_fade_anim.setStartValue(start_opacity)
        self._streaming_fade_anim.setEndValue(1.0)
        self._streaming_fade_anim.setEasingCurve(QEasingCurve.OutCubic)

        # Configure slide up (position)
        self._streaming_pos_anim.setDuration(duration)
        self._streaming_pos_anim.setStartValue(start_pos)
        self._streaming_pos_anim.setEndValue(target_pos)
        self._streaming_pos_anim.setEasingCurve(QEasingCurve.OutCubic)

        # Store base position
        self._streaming_base_pos = target_pos

        # Connect finished signal for state update
        self._streaming_anim_group.finished.connect(self._on_fade_in_finished)
        self._streaming_anim_group.start()

    def _on_fade_in_finished(self):
        """Called when fade-in animation completes."""
        # State machine guard
        if self._streaming_state != "fading_in":
            return

        self._streaming_state = "visible"

        # Disconnect to avoid multiple connections
        try:
            self._streaming_anim_group.finished.disconnect(self._on_fade_in_finished)
        except (RuntimeError, TypeError):
            pass  # Already disconnected

    def _update_streaming_label_position(self):
        """Position the streaming label above the ball."""
        ball_pos = self.pos()
        ball_size = self.size()
        label_size = self._streaming_label.sizeHint()

        # Position above the ball, centered
        x = ball_pos.x() + (ball_size.width() - label_size.width()) // 2
        y = ball_pos.y() - label_size.height() - 10

        # Keep on screen
        screen = self.screen()
        if screen:
            screen_geo = screen.geometry()
            x = max(10, min(x, screen_geo.width() - label_size.width() - 10))
            y = max(10, y)

        self._streaming_label.move(x, y)

    def _init_popup_menu(self):
        """Initialize the popup menu."""
        self._popup_menu = PopupMenu()
        self._popup_menu.enableToggled.connect(self._on_menu_enable_toggled)
        self._popup_menu.modeChanged.connect(self._on_menu_mode_changed)
        self._popup_menu.ocrModeChanged.connect(self._on_menu_ocr_mode_changed)
        self._popup_menu.captureModeChanged.connect(self._on_menu_capture_mode_changed)
        self._popup_menu.asrDeviceModeChanged.connect(
            self._on_menu_asr_device_mode_changed
        )
        self._popup_menu.asrEngineModeChanged.connect(
            self._on_menu_asr_engine_mode_changed
        )
        self._popup_menu.outputModeChanged.connect(
            self._on_menu_output_mode_changed
        )
        self._popup_menu.micInputGainChanged.connect(
            self._on_menu_mic_input_gain_changed
        )
        self._popup_menu.settingsRequested.connect(self._on_menu_settings)
        self._popup_menu.historyRequested.connect(self._on_menu_history)
        self._popup_menu.correctionRulesRequested.connect(
            self._on_menu_correction_rules
        )
        self._popup_menu.pasteLastRequested.connect(self._on_menu_paste_last)
        self._popup_menu.copyLastRequested.connect(self._on_menu_copy_last)
        self._popup_menu.draftBoxRequested.connect(self._on_menu_draft_box)
        self._popup_menu.lockToggled.connect(self._on_menu_lock_toggled)
        self._popup_menu.streamingToggled.connect(self._on_menu_streaming_toggled)
        self._popup_menu.deepSleepToggled.connect(self._on_menu_deep_sleep_toggled)
        self._popup_menu.translateModeChanged.connect(
            self._on_menu_translate_mode_changed
        )
        self._popup_menu.apiSwitchBackRequested.connect(
            self._on_menu_api_switch_back_requested
        )
        self._popup_menu.deepseekSetupRequested.connect(
            self._on_menu_deepseek_setup_requested
        )

    def _on_menu_enable_toggled(self, enabled):
        """Handle enable toggle from popup menu."""
        self.enableToggled.emit(enabled)

    def _on_menu_mode_changed(self, mode):
        """Handle mode change from popup menu."""
        self.modeChanged.emit(mode)

    def _on_menu_ocr_mode_changed(self, mode):
        """Handle OCR-tier change from popup menu."""
        self.ocrModeChanged.emit(mode)

    def _on_menu_capture_mode_changed(self, mode):
        """Handle capture-mode change from popup menu."""
        self.captureModeChanged.emit(mode)

    def _on_menu_asr_device_mode_changed(self, mode):
        """Handle ASR CPU/GPU mode change from popup menu."""
        self.asrDeviceModeChanged.emit(mode)

    def _on_menu_asr_engine_mode_changed(self, engine):
        """Handle cross-engine (llamacpp/sherpa) switch from popup menu."""
        self.asrEngineModeChanged.emit(engine)

    def _on_menu_output_mode_changed(self, mode):
        """Handle text output-mode (clipboard/typewriter) change from popup menu."""
        self.outputModeChanged.emit(mode)

    def _on_menu_mic_input_gain_changed(self, gain):
        """Handle microphone input gain change from popup menu."""
        self.micInputGainChanged.emit(float(gain))

    def _on_menu_settings(self):
        """Handle settings request from popup menu."""
        self.detailsRequested.emit()

    def _on_menu_history(self):
        """Handle history request from popup menu."""
        self.historyRequested.emit()

    def _on_menu_correction_rules(self):
        """Handle explicit-correction manager request from popup menu."""
        self.correctionRulesRequested.emit()

    def _on_menu_paste_last(self):
        """Handle last-transcript paste request from popup menu."""
        self.pasteLastRequested.emit()

    def _on_menu_copy_last(self):
        """Handle last-transcript copy request from popup menu."""
        self.copyLastRequested.emit()

    def _on_menu_draft_box(self):
        """Handle editable Draft Box request from popup menu."""
        self.draftBoxRequested.emit()

    def _on_menu_streaming_toggled(self, enabled):
        """Handle streaming display toggle from popup menu."""
        self.set_streaming_display(enabled)

    def _on_menu_deep_sleep_toggled(self, deep):
        """Handle deep sleep toggle from popup menu."""
        self.deepSleepToggled.emit(deep)

    def _on_menu_translate_mode_changed(self, mode):
        """Handle translate mode change from popup menu."""
        self.translateModeChanged.emit(mode)

    def _on_menu_api_switch_back_requested(self):
        """Handle primary API switch request from popup menu."""
        self.apiSwitchBackRequested.emit()

    def _on_menu_deepseek_setup_requested(self):
        """Forward the guided DeepSeek setup request to the Qt entry point."""
        self.deepseekSetupRequested.emit()

    def _on_menu_lock_toggled(self, locked):
        """Handle lock toggle from popup menu."""
        self._is_locked = locked

        # Update visual state
        if locked:
            self._window_scale_target = self._SCALE_IDLE
            self._icon_scale_target = 0.0
            # Squircle animation: circle -> rounded square
            self._corner_radius_target = self._CORNER_SQUIRCLE
        else:
            # Restore based on current state
            if (
                self._state == self.STATE_RECORDING
                or self._state == self.STATE_TRANSCRIBING
                or self._is_processing
            ):
                self._window_scale_target = self._SCALE_ACTIVE
                self._icon_scale_target = 1.0
            else:
                self._window_scale_target = self._SCALE_IDLE
                self._icon_scale_target = 0.0
            # Squircle animation: rounded square -> circle
            self._corner_radius_target = self._CORNER_CIRCLE

        self.lockToggled.emit(locked)
        self.update()

    def show_popup_menu(self):
        """Show the popup menu at the ball's position."""
        if self._popup_menu:
            # Sync current state before showing
            self._popup_menu.setLocked(self._is_locked)
            self._popup_menu.setStreaming(self._streaming_display_enabled)
            # Sync deep sleep / loading state based on current ball state
            if self._state == self.STATE_LOADING:
                self._popup_menu.setDeepSleeping(True)
                self._popup_menu.setLoading(True)
            else:
                is_deep = self._state == self.STATE_DEEP_SLEEPING
                self._popup_menu.setDeepSleeping(is_deep)
            # Sync engine info
            self._popup_menu.setEngineInfo(self._engine_info)
            self._popup_menu.setAsrStatus(self._asr_status)
            # Note: enable state is NOT synced here — it's managed by
            # set_enabled_state() calls from main.py and persists in popup_menu._enabled.
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

        # Combined scale: window_scale * press_scale (for press physics effect)
        combined_scale = self._window_scale * self._press_scale
        painter.scale(combined_scale, combined_scale)

        # Apply bounce offset (command execution feedback)
        if self._bounce_active or self._bounce_phase > 0:
            # Sine wave bounce: up -> down -> up (nod effect)
            bounce_y = (
                math.sin(self._bounce_phase * math.pi * 2) * self._bounce_amplitude
            )
            painter.translate(0, -bounce_y)

        painter.translate(-center)

        radius = self.ball_size // 2

        # Use corner_radius to determine shape (circle vs squircle)
        use_squircle = self._corner_radius < self._CORNER_CIRCLE - 2

        # Debug: Log state during paint (to diagnose gray ball issue)
        if (
            hasattr(self, "_last_logged_state")
            and self._last_logged_state != self._state
        ):
            print(
                f"[PAINT] State changed: {self._last_logged_state} -> {self._state}, flash_active={self._command_flash_active}"
            )
        self._last_logged_state = self._state

        # Glass-morphism ball body
        is_sleeping_visual = self._state in (
            self.STATE_SLEEPING,
            self.STATE_DEEP_SLEEPING,
        )
        is_loading_visual = self._state == self.STATE_LOADING
        if self._state == self.STATE_DEEP_SLEEPING:
            # Deep sleeping: extremely dim
            base_alpha = 20
            highlight_alpha = 4
        elif is_sleeping_visual:
            # Light sleeping: very dim, muted appearance
            base_alpha = 40
            highlight_alpha = 8
        elif is_loading_visual:
            # Loading: moderate dim with pulse
            base_alpha = 60
            highlight_alpha = 12
        elif self._is_locked:
            # Locked: almost invisible, minimal alpha to keep hit test working
            base_alpha = 8
            highlight_alpha = 0
        elif self._state == self.STATE_TRANSCRIBING:
            base_alpha = 200
            highlight_alpha = 50
        else:
            base_alpha = 180
            highlight_alpha = 45

        # Main ball gradient - use purple/blue tint for selection modes
        gradient = QRadialGradient(center, radius)
        if self._state == self.STATE_SELECTION_LISTENING:
            # Purple tinted background for selection listening mode
            gradient.setColorAt(0, QColor(70, 45, 90, base_alpha))
            gradient.setColorAt(0.85, QColor(55, 35, 70, base_alpha))
            gradient.setColorAt(1.0, QColor(45, 30, 60, int(base_alpha * 0.7)))
        elif self._state == self.STATE_SELECTION_PROCESSING:
            # Blue tinted background for selection processing mode
            gradient.setColorAt(0, QColor(45, 55, 90, base_alpha))
            gradient.setColorAt(0.85, QColor(35, 45, 70, base_alpha))
            gradient.setColorAt(1.0, QColor(30, 40, 60, int(base_alpha * 0.7)))
        else:
            # Default dark glass
            gradient.setColorAt(0, QColor(45, 45, 50, base_alpha))
            gradient.setColorAt(0.85, QColor(35, 35, 40, base_alpha))
            gradient.setColorAt(1.0, QColor(30, 30, 35, int(base_alpha * 0.7)))

        # Draw ball body (circle or squircle based on lock state)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(gradient))
        if use_squircle:
            # Squircle: rounded rectangle for locked state
            rect = self.rect().adjusted(5, 5, -5, -5)
            painter.drawRoundedRect(rect, self._corner_radius, self._corner_radius)
        else:
            painter.drawEllipse(center, radius, radius)

        # Inner highlight (top-left, simulating light source)
        highlight_center = QPoint(center.x() - radius // 3, center.y() - radius // 3)
        highlight = QRadialGradient(highlight_center, radius * 0.6)
        highlight.setColorAt(0, QColor(255, 255, 255, highlight_alpha))
        highlight.setColorAt(0.5, QColor(255, 255, 255, highlight_alpha // 3))
        highlight.setColorAt(1, QColor(255, 255, 255, 0))
        painter.setBrush(QBrush(highlight))
        if use_squircle:
            rect = self.rect().adjusted(5, 5, -5, -5)
            painter.drawRoundedRect(rect, self._corner_radius, self._corner_radius)
        else:
            painter.drawEllipse(center, radius, radius)

        # Draw border - pulsing cyan when recording, subtle white otherwise
        if self._state == self.STATE_RECORDING:
            self._draw_rainbow_border(painter, center, radius)
        elif self._state == self.STATE_SELECTION_LISTENING:
            # Purple pulsing border for selection mode (listening for command)
            self._draw_purple_border(painter, center, radius)
        elif self._state == self.STATE_SELECTION_PROCESSING:
            # Blue spinning border for selection processing
            breath = 0.5 + 0.5 * math.sin(self._pulse_phase * math.pi * 2)
            alpha = int(150 + 70 * breath)
            painter.setPen(QPen(QColor(100, 120, 255, alpha), 2.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, radius - 1, radius - 1)
        elif self._state == self.STATE_TRANSCRIBING:
            # Subtle blue border when transcribing
            painter.setPen(QPen(QColor(100, 150, 255, 120), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, radius - 1, radius - 1)
        elif self._state == self.STATE_SLEEPING:
            # Light sleeping: very dim blue-ish border
            painter.setPen(QPen(QColor(150, 150, 200, 30), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, radius - 1, radius - 1)
        elif self._state == self.STATE_DEEP_SLEEPING:
            # Deep sleeping: dim gray border (engine off)
            painter.setPen(QPen(QColor(100, 100, 100, 25), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, radius - 1, radius - 1)
        elif self._state == self.STATE_LOADING:
            # Loading: pulsing amber border
            pulse = math.sin(self._pulse_phase * math.pi * 2)
            alpha = int(100 + 80 * pulse)
            painter.setPen(QPen(QColor(255, 180, 50, alpha), 2.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, radius - 1, radius - 1)
        elif self._is_locked:
            # Squircle border when locked (subtle, nearly invisible)
            painter.setPen(QPen(QColor(255, 255, 255, 25), 1.0))
            painter.setBrush(Qt.NoBrush)
            if use_squircle:
                rect = self.rect().adjusted(6, 6, -6, -6)
                painter.drawRoundedRect(
                    rect, self._corner_radius - 1, self._corner_radius - 1
                )
            else:
                painter.drawEllipse(center, radius - 1, radius - 1)
        else:
            # Subtle white border when idle
            painter.setPen(QPen(QColor(255, 255, 255, 40), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, radius - 1, radius - 1)

        # Command flash overlay (for wake-up animation and command feedback)
        if self._command_flash_active:
            print(f"[PAINT] Rendering flash! state={self._state}")
            flash_gradient = QRadialGradient(center, radius)
            flash_color = self._command_flash_color
            # Inner bright, outer fade
            flash_gradient.setColorAt(
                0,
                QColor(
                    flash_color.red(),
                    flash_color.green(),
                    flash_color.blue(),
                    flash_color.alpha(),
                ),
            )
            flash_gradient.setColorAt(
                0.6,
                QColor(
                    flash_color.red(),
                    flash_color.green(),
                    flash_color.blue(),
                    flash_color.alpha() // 2,
                ),
            )
            flash_gradient.setColorAt(
                1.0,
                QColor(flash_color.red(), flash_color.green(), flash_color.blue(), 0),
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(flash_gradient))
            painter.drawEllipse(center, radius, radius)
            # Also draw a brighter border ring
            painter.setPen(QPen(flash_color, 2.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, radius - 1, radius - 1)

        # Slow-stage glow indicator (subtle pulsing center dot)
        # Only visible when pipeline stage exceeds 3s threshold
        if getattr(self, "_slow_glow_active", False) and getattr(
            self, "_slow_glow_color", None
        ):
            glow_color = self._slow_glow_color
            # Pulsing alpha using rainbow_angle (already animating at 50ms)
            pulse = int(80 + 80 * math.sin(self._rainbow_angle * math.pi / 180))
            glow_r = radius * 0.35  # Small center glow
            glow_grad = QRadialGradient(QPointF(center), glow_r)
            glow_grad.setColorAt(
                0,
                QColor(glow_color.red(), glow_color.green(), glow_color.blue(), pulse),
            )
            glow_grad.setColorAt(
                1.0, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), 0)
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(glow_grad))
            painter.drawEllipse(QPointF(center), glow_r, glow_r)

        # Draw icon/indicator
        self._draw_indicator(painter, center, radius)

        # Draw update-ready badge (small red dot, top-right)
        if self._update_badge_visible:
            badge_r = max(4, self.ball_size // 10)
            # Position at top-right of the ball bounding circle
            offset_x = radius * 0.72
            offset_y = -radius * 0.72
            badge_center = QPointF(center.x() + offset_x, center.y() + offset_y)
            painter.setPen(QPen(QColor(255, 255, 255, 220), 1.2))
            painter.setBrush(QBrush(QColor(231, 76, 60, 240)))
            painter.drawEllipse(badge_center, badge_r, badge_r)

    def set_update_badge(self, visible: bool, tooltip: str = "") -> None:
        """Show/hide the red update-ready badge. Safe to call from any thread."""
        if (
            self._update_badge_visible == visible
            and self._update_badge_tooltip == tooltip
        ):
            return
        self._update_badge_visible = visible
        self._update_badge_tooltip = tooltip
        if tooltip:
            self.setToolTip(tooltip)
        self.update()

    def _draw_rainbow_border(self, painter: QPainter, center: QPoint, radius: int):
        """Draw rainbow border - 3-segment style, faster+brighter when speaking."""
        angle = self._rainbow_angle
        gradient = QConicalGradient(QPointF(center), angle)

        # Breathing effect for waiting state
        breath = 0.5 + 0.5 * math.sin(self._pulse_phase * math.pi * 2)

        # Segment parameters
        seg_len = 0.22  # Rainbow segment length (~22% each)
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
            gradient.setColorAt(
                seg_start + 0.04, QColor(c1[0], c1[1], c1[2], base_alpha // 2)
            )
            gradient.setColorAt(
                seg_start + 0.07, QColor(c1[0], c1[1], c1[2], base_alpha)
            )

            # Middle gradient between two colors
            gradient.setColorAt(
                seg_start + seg_len * 0.5, QColor(c2[0], c2[1], c2[2], base_alpha)
            )

            # Smooth fade out (longer gradient)
            gradient.setColorAt(
                seg_start + seg_len - 0.07, QColor(c2[0], c2[1], c2[2], base_alpha)
            )
            gradient.setColorAt(
                seg_start + seg_len - 0.04, QColor(c2[0], c2[1], c2[2], base_alpha // 2)
            )
            gradient.setColorAt(seg_start + seg_len, QColor(c2[0], c2[1], c2[2], 0))

        pen = QPen(QBrush(gradient), border_width)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(center, radius - 1, radius - 1)

    def _draw_purple_border(self, painter: QPainter, center: QPoint, radius: int):
        """Draw purple pulsing border for selection mode."""
        angle = self._rainbow_angle
        gradient = QConicalGradient(QPointF(center), angle)

        # Breathing effect
        breath = 0.5 + 0.5 * math.sin(self._pulse_phase * math.pi * 2)

        # Purple gradient colors
        purple_light = (180, 100, 255)  # Light purple
        purple_dark = (120, 60, 200)  # Dark purple

        if self._is_speaking:
            base_alpha = 220
            border_width = 3.5
        else:
            base_alpha = int(120 + 80 * breath)
            border_width = 2.5

        # Create 3-segment purple border (similar to rainbow but monochrome)
        seg_len = 0.22
        gap_len = 0.111

        for seg in range(3):
            seg_start = seg * (seg_len + gap_len)

            # Fade in
            gradient.setColorAt(
                seg_start, QColor(purple_light[0], purple_light[1], purple_light[2], 0)
            )
            gradient.setColorAt(
                seg_start + 0.04,
                QColor(
                    purple_light[0], purple_light[1], purple_light[2], base_alpha // 2
                ),
            )
            gradient.setColorAt(
                seg_start + 0.07,
                QColor(purple_light[0], purple_light[1], purple_light[2], base_alpha),
            )

            # Middle
            gradient.setColorAt(
                seg_start + seg_len * 0.5,
                QColor(purple_dark[0], purple_dark[1], purple_dark[2], base_alpha),
            )

            # Fade out
            gradient.setColorAt(
                seg_start + seg_len - 0.07,
                QColor(purple_dark[0], purple_dark[1], purple_dark[2], base_alpha),
            )
            gradient.setColorAt(
                seg_start + seg_len - 0.04,
                QColor(purple_dark[0], purple_dark[1], purple_dark[2], base_alpha // 2),
            )
            gradient.setColorAt(
                seg_start + seg_len,
                QColor(purple_dark[0], purple_dark[1], purple_dark[2], 0),
            )

        pen = QPen(QBrush(gradient), border_width)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(center, radius - 1, radius - 1)

    def _draw_indicator(self, painter: QPainter, center: QPoint, radius: int):
        """Draw status indicator with audio-reactive wavy ring effect."""
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
                # No significant audio - show dot or ring based on auto-send state
                if self._auto_send_enabled:
                    # Auto-send ON: hollow ring (smaller, thicker)
                    ring_radius = 5
                    painter.setPen(QPen(QColor(255, 255, 255, base_alpha), 3))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawEllipse(center, ring_radius, ring_radius)
                else:
                    # Auto-send OFF: solid dot (default)
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
                base_radius = (
                    dot_radius + 20 * expansion * scale
                )  # Much larger expansion
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
                    wave2 = (
                        math.sin(angle * (num_waves + 2) - phase) * wave_amplitude * 0.4
                    )
                    wave3 = (
                        math.sin(angle * (num_waves - 1) + phase * 0.8)
                        * wave_amplitude
                        * 0.2
                    )

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
            painter.drawArc(
                center.x() - arc_size // 2,
                center.y() - arc_size // 2,
                arc_size,
                arc_size,
                arc_angle,
                270 * 16,
            )

        elif self._state == self.STATE_SLEEPING:
            # Light sleeping: small dim ring
            ring_radius = 4
            painter.setPen(QPen(QColor(150, 150, 200, 50), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, ring_radius, ring_radius)

        elif self._state == self.STATE_DEEP_SLEEPING:
            # Deep sleeping: small gray dot (engine off indicator)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(100, 100, 100, 40))
            painter.drawEllipse(center, 3, 3)

        elif self._state == self.STATE_LOADING:
            # Loading: rotating amber arc
            arc_rect = QRectF(center.x() - 6, center.y() - 6, 12, 12)
            arc_angle = int(self._rainbow_angle * 16)
            painter.setPen(QPen(QColor(255, 180, 50, 180), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawArc(arc_rect, arc_angle, 270 * 16)

        elif self._is_locked:
            # Locked: very subtle lock icon (nearly invisible)
            painter.setPen(QPen(QColor(255, 255, 255, 35), 1.0))
            painter.setBrush(Qt.NoBrush)
            # Small lock icon
            painter.drawRect(int(center.x() - 4), int(center.y() - 1), 8, 6)
            painter.drawArc(int(center.x() - 3), int(center.y() - 6), 6, 8, 0, 180 * 16)

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
        elif self._state == self.STATE_LOADING:
            # Moderate rotation for loading indicator (~60 deg/sec = 2 deg/frame)
            self._rainbow_angle = (self._rainbow_angle + 2.0) % 360.0

        # Track if any animation is active
        animations_active = False

        # Smooth icon scale animation
        if self._icon_scale != self._icon_scale_target:
            diff = self._icon_scale_target - self._icon_scale
            self._icon_scale += diff * 0.1  # Smooth easing
            if abs(diff) < 0.01:
                self._icon_scale = self._icon_scale_target
            else:
                animations_active = True

        # Smooth window scale animation (silky smooth)
        if self._window_scale != self._window_scale_target:
            diff = self._window_scale_target - self._window_scale
            self._window_scale += diff * 0.12  # Gentle easing for smooth feel
            if abs(diff) < 0.005:
                self._window_scale = self._window_scale_target
            else:
                animations_active = True
            # Debug: log when scale is changing significantly
            if abs(diff) > 0.05:
                print(
                    f"[FloatingBall] Animating scale: {self._window_scale:.2f} -> {self._window_scale_target:.2f}"
                )

        # Smooth audio level (gentle attack, very smooth decay)
        if self._audio_level > self._audio_level_smooth:
            # Gentle attack - not too fast
            self._audio_level_smooth += (
                self._audio_level - self._audio_level_smooth
            ) * 0.15
            animations_active = True
        else:
            # Faster decay for responsive fade out (~0.5s response)
            self._audio_level_smooth += (
                self._audio_level - self._audio_level_smooth
            ) * 0.15
            if self._audio_level_smooth > 0.01:
                animations_active = True

        # Bounce animation update (for command execution feedback)
        if self._bounce_active:
            self._bounce_phase += 1.0 / self._bounce_duration_frames
            if self._bounce_phase >= 1.0:
                self._bounce_phase = 1.0
                self._bounce_active = False
            animations_active = True

        # Press scale animation (elastic spring back effect)
        if self._press_scale != self._press_scale_target:
            diff = self._press_scale_target - self._press_scale
            # Use different easing for press down vs spring back
            if self._press_scale_target < 1.0:
                # Press down: fast
                self._press_scale += diff * 0.3
            else:
                # Spring back: elastic overshoot using EaseOutBack-like curve
                self._press_scale += diff * 0.15
                # Add slight overshoot when approaching 1.0
                if self._press_scale > 1.0 and self._press_scale < 1.05:
                    self._press_scale = min(1.02, self._press_scale + 0.01)
            if abs(diff) < 0.005:
                self._press_scale = self._press_scale_target
            else:
                animations_active = True

        # Corner radius animation (circle <-> squircle morph)
        if self._corner_radius != self._corner_radius_target:
            diff = self._corner_radius_target - self._corner_radius
            self._corner_radius += diff * 0.15
            if abs(diff) < 0.5:
                self._corner_radius = self._corner_radius_target
            else:
                animations_active = True

        # Adaptive frame rate: slow down when truly idle to save CPU
        is_truly_idle = (
            self._state == self.STATE_IDLE
            and not animations_active
            and not self._is_speaking
            and not self._is_processing
        )

        current_interval = self._pulse_timer.interval()
        if is_truly_idle:
            # Idle: 5 FPS (200ms) - minimal CPU usage
            if current_interval < 200:
                self._pulse_timer.setInterval(200)
        elif self._is_speaking:
            # Speaking: 30 FPS (33ms) - smooth animation
            if current_interval != 33:
                self._pulse_timer.setInterval(33)
        else:
            # Recording/Transcribing: 20 FPS (50ms)
            if current_interval != 50:
                self._pulse_timer.setInterval(50)

        self.update()

    # === Mouse Events ===

    def mousePressEvent(self, event):
        """Handle mouse press for drag start."""
        if event.button() == Qt.LeftButton and not self._is_locked:
            self._drag_position = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            self._click_pos = (
                event.globalPosition().toPoint()
            )  # Record for click detection
        event.accept()

    def mouseMoveEvent(self, event):
        """Handle mouse move for dragging."""
        if (
            event.buttons() == Qt.LeftButton
            and self._drag_position
            and not self._is_locked
        ):
            self.move(event.globalPosition().toPoint() - self._drag_position)
        event.accept()

    def mouseReleaseEvent(self, event):
        """Handle mouse release."""
        if event.button() == Qt.LeftButton:
            if not self._is_locked and self._click_pos:
                # Check if it was a click (not a drag)
                moved = (
                    event.globalPosition().toPoint() - self._click_pos
                ).manhattanLength()
                if moved < 10:  # Small movement = click
                    # Left-click: Toggle ASR on/off
                    self.toggleRequested.emit()
            self._drag_position = None
            self._click_pos = None
        elif event.button() == Qt.MiddleButton:
            # Middle-click: Lock position
            self._toggle_lock()
        elif event.button() == Qt.RightButton:
            # Right-click: Show popup menu immediately (no delay)
            self.show_popup_menu()
        event.accept()

    def _toggle_lock(self):
        """Toggle lock state (visual dimming, ignores drag/left/middle clicks)."""
        self._is_locked = not self._is_locked

        # When locked, shrink to idle size; when unlocked, restore based on state
        if self._is_locked:
            self._window_scale_target = self._SCALE_IDLE  # Shrink to idle size
            self._icon_scale_target = 0.0  # Hide active icon
            # Squircle animation: circle -> rounded square
            self._corner_radius_target = self._CORNER_SQUIRCLE
            # Hide streaming text label immediately
            if self._streaming_state in ("visible", "fading_in"):
                self._fade_out_streaming_label()
        else:
            # Restore based on current state and processing flag
            if (
                self._state == self.STATE_RECORDING
                or self._state == self.STATE_TRANSCRIBING
                or self._is_processing
            ):
                self._window_scale_target = self._SCALE_ACTIVE
                self._icon_scale_target = 1.0
            elif self._state == self.STATE_SLEEPING:
                # Respect SLEEPING state's smaller scale
                self._window_scale_target = self._SCALE_IDLE * 0.9
            elif self._state == self.STATE_DEEP_SLEEPING:
                self._window_scale_target = self._SCALE_IDLE * 0.82
            elif self._state == self.STATE_LOADING:
                self._window_scale_target = self._SCALE_IDLE * 0.95
                self._icon_scale_target = 0.0
            else:
                self._window_scale_target = self._SCALE_IDLE
                self._icon_scale_target = 0.0
            # Squircle animation: rounded square -> circle
            self._corner_radius_target = self._CORNER_CIRCLE

        self.lockToggled.emit(self._is_locked)
        self.update()

    def _update_click_through(self):
        """Update Win32 click-through style."""
        if sys.platform != "win32":
            return

        try:
            hwnd = int(self.winId())

            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080

            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)

            # Note: WS_EX_TOPMOST cannot be set via SetWindowLong (the window
            # manager owns that bit); topmost is asserted via SetWindowPos in
            # _ensure_topmost below.
            if self._is_locked:
                # Enable click-through
                new_style = (
                    style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
                )
            else:
                # Disable click-through
                new_style = (
                    (style & ~WS_EX_TRANSPARENT) | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
                )

            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)

            self._ensure_topmost(force=True)
        except Exception as e:
            print(f"Win32 API Error: {e}")

    def _ensure_topmost(self, force: bool = False):
        """Re-assert topmost if Windows silently dropped WS_EX_TOPMOST.

        Windows strips the topmost status in several situations (exclusive
        fullscreen apps, session lock/unlock, explorer.exe restarts, other
        processes reshuffling the z-order).  Called every 2s by the guard
        timer; SetWindowPos is only invoked when the flag is actually missing
        so we don't fight other topmost windows (IME candidate lists,
        screenshot tools) for the top of the band.
        """
        if sys.platform != "win32" or not self.isVisible():
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32

            GWL_EXSTYLE = -20
            WS_EX_TOPMOST = 0x00000008
            if not force:
                style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                if style & WS_EX_TOPMOST:
                    return  # Still topmost — don't touch the z-order
                self._log("Topmost flag lost — re-asserting HWND_TOPMOST")

            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        except Exception as e:
            self._log(f"_ensure_topmost failed: {e}")

    def showEvent(self, event):
        """Apply Win32 styles on show."""
        super().showEvent(event)

        if sys.platform != "win32":
            return

        try:
            hwnd = int(self.winId())

            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080

            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd,
                GWL_EXSTYLE,
                style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
            )

            self._ensure_topmost(force=True)
        except Exception as e:
            print(f"Win32 API Error: {e}")

    # === State Management ===

    @Slot(str)
    def on_state_changed(self, state: str):
        """Handle state changes from backend."""

        # Normalize state string to handle whitespace/case mismatches
        state = (state or "").strip().upper()

        old_state = self._state
        self._log(f"on_state_changed: '{old_state}' -> '{state}'")
        # Always print state changes to console for debugging
        print(
            f"[FloatingBall] STATE CHANGE: '{old_state}' -> '{state}' (STATE_SLEEPING='{self.STATE_SLEEPING}')"
        )
        self._log(
            f"STATE: {old_state} -> {state}, processing={self._is_processing}, scale={self._window_scale:.2f}, target={self._window_scale_target:.2f}"
        )

        if state == "IDLE":
            was_sleeping = old_state in (
                self.STATE_SLEEPING,
                self.STATE_DEEP_SLEEPING,
                self.STATE_LOADING,
            )
            # Also check if we were in a dim/sleeping-like visual state
            # (scale target is smaller than idle, indicating sleeping appearance)
            was_visually_sleeping = self._window_scale_target < self._SCALE_IDLE
            self._log(
                f"IDLE check: was_sleeping={was_sleeping}, was_visually={was_visually_sleeping}, old='{old_state}', target={self._window_scale_target:.2f}"
            )
            print(
                f"[FloatingBall] IDLE: was_sleeping={was_sleeping}, was_visually_sleeping={was_visually_sleeping}, old_state='{old_state}', STATE_SLEEPING='{self.STATE_SLEEPING}'"
            )
            self._state = self.STATE_IDLE
            self._is_speaking = False

            # Force restore from SLEEPING - always reset visual state
            # Use OR condition to catch cases where old_state tracking failed
            if was_sleeping or was_visually_sleeping:
                self._is_processing = False  # Ensure clean state
                self._icon_scale_target = 0.0
                self._window_scale_target = self._SCALE_IDLE
                # Force scale to a visible minimum immediately (don't wait for animation)
                if self._window_scale < 0.5:
                    self._window_scale = 0.7  # Jump to visible range, animate the rest
                self._log(f"WAKE UP! Restoring scale to {self._SCALE_IDLE}")
                print(
                    f"[FloatingBall] WAKE UP! old={old_state}, new={self._state}, "
                    f"scale={self._window_scale:.3f}→{self._window_scale_target}, "
                    f"was_visually_sleeping={was_visually_sleeping}"
                )

                # Trigger wake-up animation: bounce only (flash disabled per user feedback)
                self._bounce_active = True
                self._bounce_phase = 0.0

                # Ensure smooth animation by setting timer to high FPS
                self._pulse_timer.setInterval(33)  # 30 FPS for smooth wake animation

                # Force window visibility (in case it got lost during deep sleep)
                self.show()
                self.raise_()

                # Force immediate repaint
                self.update()
            # Only shrink if NOT processing (backend finished)
            elif not self._is_processing:
                self._icon_scale_target = 0.0
                self._window_scale_target = self._SCALE_IDLE
                self._log(f"IDLE (not processing): SHRINK to {self._SCALE_IDLE}")
            else:
                # Still processing - start short fallback timer (3 seconds)
                self._shrink_fallback_timer.start(3000)
                self._log(
                    f"IDLE (processing): waiting for on_insert_complete, fallback 3s"
                )
        elif state == "RECORDING":
            self._state = self.STATE_RECORDING
            self._is_processing = False
            self._shrink_fallback_timer.stop()
            # User is about to speak — recover topmost immediately if lost
            # (don't wait for the 2s guard timer tick)
            self._ensure_topmost()
            # Recording indicator on, but window stays small until voice detected
            self._icon_scale_target = 1.0
            self._window_scale_target = self._SCALE_IDLE
            # Press physics: brief "press down" effect then spring back
            self._press_scale_target = self._PRESS_SCALE_DOWN
            # Schedule spring back after brief press
            QTimer.singleShot(80, self._spring_back_press)
            self._log(f"RECORDING: scale={self._SCALE_IDLE}, press animation")
        elif state == "TRANSCRIBING":
            self._state = self.STATE_TRANSCRIBING
            self._is_speaking = False
            self._is_processing = True  # Backend is processing!
            # Ball stays BIG while backend is processing
            # Don't change scale here - keep current size
            self._log(
                f"TRANSCRIBING: processing=True, scale stays {self._window_scale_target}"
            )
        elif state == "SELECTION_LISTENING":
            self._state = self.STATE_SELECTION_LISTENING
            self._is_processing = False
            self._shrink_fallback_timer.stop()
            # Purple border, grow the ball
            self._icon_scale_target = 1.0
            self._window_scale_target = self._SCALE_ACTIVE
            self._log(f"SELECTION_LISTENING: purple mode, scale={self._SCALE_ACTIVE}")
        elif state == "SELECTION_PROCESSING":
            self._state = self.STATE_SELECTION_PROCESSING
            self._is_speaking = False
            self._is_processing = True
            # Blue border, keep ball big
            self._window_scale_target = self._SCALE_ACTIVE
            self._log(
                f"SELECTION_PROCESSING: processing=True, scale={self._SCALE_ACTIVE}"
            )
        elif state == "SLEEPING":
            print(
                f"[FloatingBall] SLEEPING received! Setting state to '{self.STATE_SLEEPING}'"
            )
            self._state = self.STATE_SLEEPING
            self._is_speaking = False
            self._is_processing = False
            # Light sleeping: shrink slightly, dim appearance
            self._window_scale_target = self._SCALE_IDLE * 0.9
            self._icon_scale_target = 0.0
            self._log(f"SLEEPING: dim mode, scale={self._SCALE_IDLE * 0.9}")
        elif state == "DEEP_SLEEPING":
            self._state = self.STATE_DEEP_SLEEPING
            self._is_speaking = False
            self._is_processing = False
            # Deep sleeping: very dim, smaller than light sleep
            self._window_scale_target = self._SCALE_IDLE * 0.82
            self._icon_scale_target = 0.0
            self._log(f"DEEP_SLEEPING: engine off, scale={self._SCALE_IDLE * 0.82}")
        elif state == "LOADING":
            self._state = self.STATE_LOADING
            self._is_speaking = False
            self._is_processing = True  # Reuse for pulsing animation
            # Loading: grow slightly, amber pulse
            self._window_scale_target = self._SCALE_IDLE * 0.95
            self._icon_scale_target = 0.0
            self._pulse_timer.setInterval(33)  # 30 FPS for smooth animation
            # Sync popup menu button to "加载中..."
            if self._popup_menu:
                self._popup_menu.setLoading(True)
            self._log(f"LOADING: engine reloading, scale={self._SCALE_IDLE * 0.95}")

        if old_state != self._state:
            self.update()

    @Slot(bool)
    def on_voice_activity(self, is_speaking: bool):
        """Handle voice activity detection updates."""
        self._log(
            f"VOICE: {is_speaking}, state={self._state}, scale={self._window_scale:.2f}"
        )
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
                    # Stopped speaking but still recording: shrink to idle
                    self._window_scale_target = self._SCALE_IDLE
                    self._pulse_timer.start(50)  # 20 FPS
                    self._log(f"Voice stopped, shrink to {self._SCALE_IDLE}")

            self.update()

    @Slot(float)
    def on_level_changed(self, level: float):
        """Handle audio level updates for waveform visualization."""
        self._audio_level = min(
            1.0, level * 6.0
        )  # High sensitivity for better visual response

    @Slot(str, bool)
    def on_text_updated(self, text: str, is_final: bool):
        """Handle text updates - show streaming results in floating label with fade animation."""
        if self._state in (
            self.STATE_SLEEPING,
            self.STATE_DEEP_SLEEPING,
            self.STATE_LOADING,
        ):
            return
        if is_final:
            # Final result - fade out streaming label
            self._streaming_hide_timer.stop()
            self._fade_out_streaming_label()
            if text:
                self.setToolTip(f"Last: {text[:50]}...")
        elif text and self._streaming_display_enabled and not self._is_locked:
            # Interim result - show in floating label with fade-in
            # Only show the last ~60 chars for readability
            display_text = text if len(text) <= 60 else "..." + text[-57:]
            self._streaming_label.setText(display_text)
            self._streaming_label.adjustSize()

            # State machine: fade in if hidden or fading out, update position if visible
            if self._streaming_state in ("hidden", "fading_out"):
                self._fade_in_streaming_label()
            elif self._streaming_state in ("visible", "fading_in"):
                # Already visible/appearing - just update position
                self._update_streaming_label_position()

            # Auto-hide after 2 seconds of no updates
            self._streaming_hide_timer.stop()
            self._streaming_hide_timer.start(2000)

            self.setToolTip(f"Listening: {text[:30]}...")

    @Slot()
    def on_insert_complete(self):
        """Handle text insertion complete - shrink ball now that processing is done."""
        self._log(
            f"INSERT_COMPLETE: state={self._state}, processing={self._is_processing}"
        )

        # Cancel fallback timer
        self._shrink_fallback_timer.stop()
        self._is_processing = False
        self._clear_slow_glow()  # Pipeline done, clear any slow indicator

        # Sticker easter egg: show after pipeline completes (not on command dispatch)
        if getattr(self, "_pending_sticker", False):
            self._pending_sticker = False
            self._try_show_sticker()

        # FIX: If still recording, DON'T shrink — user is speaking in continuous mode.
        # The ball should stay active-sized until recording actually stops.
        if self._state in (self.STATE_RECORDING, self.STATE_SELECTION_LISTENING):
            self._log(
                f"INSERT_COMPLETE: still {self._state}, keeping ball active (no shrink)"
            )
            # Keep recording indicator on
            self._icon_scale_target = 1.0
            self.update()
            return

        # Not recording: shrink as usual
        self._is_speaking = False
        self._icon_scale_target = 0.0

        # Respect sleeping states' smaller scale (bug fix: don't override sleeping shrink)
        if self._state == self.STATE_SLEEPING:
            self._window_scale_target = self._SCALE_IDLE * 0.9
            self._log(f">>> SHRINK to {self._SCALE_IDLE * 0.9} (sleeping)")
        elif self._state == self.STATE_DEEP_SLEEPING:
            self._window_scale_target = self._SCALE_IDLE * 0.82
            self._log(f">>> SHRINK to {self._SCALE_IDLE * 0.82} (deep sleeping)")
        elif self._state == self.STATE_LOADING:
            self._window_scale_target = self._SCALE_IDLE * 0.95
            self._log(f">>> SHRINK to {self._SCALE_IDLE * 0.95} (loading)")
        else:
            self._window_scale_target = self._SCALE_IDLE
            self._log(f">>> SHRINK to {self._SCALE_IDLE}")

        self.update()

    @Slot(str, bool)
    def on_command_executed(self, command_id: str, success: bool):
        """Animate successful commands; failed commands explain themselves upstream.

        The backend already emits the concrete refusal reason when one is
        useful. Showing a second generic ``指令未执行`` warning here both hid
        that reason and made harmless non-command speech look like an error.
        """
        self._log(f"COMMAND_EXECUTED: {command_id}, success={success}")

        if success:
            self._bounce_active = True
            self._bounce_phase = 0.0
            self.update()

            # Mark that a wakeword command succeeded (for sticker on completion)
            if command_id.startswith("小助手:"):
                self._pending_sticker = True

    @Slot(str, list)
    def on_highlight_saved(self, text_preview: str, tags: list):
        """Handle highlight saved - gold flash + bounce animation."""
        self._log(f"HIGHLIGHT_SAVED: '{text_preview}', tags={tags}")

        # Gold flash for highlight save success
        self._command_flash_color = QColor(255, 200, 50, 220)  # Gold color
        self._command_flash_active = True
        self._bounce_active = True
        self._bounce_phase = 0.0
        self.update()

        # Clear flash after 400ms
        from PySide6.QtCore import QTimer

        QTimer.singleShot(400, self._clear_command_flash)

    @Slot(str)
    def on_asr_failure(self, phase: str):
        """Handle ASR final-segment failure - short red error flash.

        Reuses the command-flash overlay; auto-recovers after 2s so the ball
        never sticks in an error look.  A generation token keeps a rapid
        second failure's flash alive when the first failure's timer fires.
        """
        self._log(f"ASR_FAILURE: phase={phase}, red error flash")
        self._command_flash_color = QColor(239, 68, 68, 220)  # Red #EF4444
        self._command_flash_active = True
        self._asr_failure_flash_gen = getattr(self, "_asr_failure_flash_gen", 0) + 1
        gen = self._asr_failure_flash_gen
        self.update()

        from PySide6.QtCore import QTimer

        QTimer.singleShot(2000, lambda: self._clear_asr_failure_flash(gen))

    def _clear_asr_failure_flash(self, gen: int):
        """Clear the error flash only if no newer failure re-armed it."""
        if gen != getattr(self, "_asr_failure_flash_gen", 0):
            return
        self._clear_command_flash()

    def on_reminder_fired(self):
        """Handle reminder notification - blue flash + bounce animation."""
        self._log("REMINDER_FIRED: blue flash + bounce")
        self._command_flash_color = QColor(59, 130, 246, 220)  # Blue #3B82F6
        self._command_flash_active = True
        self._bounce_active = True
        self._bounce_phase = 0.0
        self.update()

        from PySide6.QtCore import QTimer

        QTimer.singleShot(600, self._clear_command_flash)

    def _try_show_sticker(self):
        """Show the opt-in easter-egg sticker. Fades out streaming label first."""
        try:
            from pathlib import Path
            import json

            cfg = _load_local_sticker_config()
            if not cfg:
                return
            allowed = {str(w) for w in cfg.get("wakewords", []) if w}
            if not allowed:
                return

            wakeword_path = (
                Path(__file__).parent.parent.parent / "config" / "wakeword.json"
            )
            if not wakeword_path.exists():
                return
            with open(wakeword_path, "r", encoding="utf-8") as f:
                wc = json.load(f)
            if wc.get("wakeword", "") not in allowed:
                return

            # Fade out streaming label first, then show sticker after fade
            if self._streaming_label.isVisible():
                self._fade_out_streaming_label()
                QTimer.singleShot(200, self._show_sticker_at_ball)
            else:
                self._show_sticker_at_ball()
        except Exception:
            pass

    def _show_sticker_at_ball(self):
        """Position sticker centered above the ball and show it."""
        ball_global = self.mapToGlobal(self.rect().center())
        # Center horizontally above ball, offset upward
        anchor = QPoint(ball_global.x(), ball_global.y() - self.ball_size // 2)
        self._sticker_popup.show_sticker(anchor)

    @Slot(str)
    def on_slow_stage(self, stage: str):
        """Show subtle center glow when pipeline is slow.

        stage: 'gpu' (ASR slow, orange) or 'api' (polish slow, blue)
        Only appears after 3s threshold — invisible during normal operation.
        Auto-clears when next state change occurs (IDLE/insert_complete).
        """
        if stage == "gpu":
            self._slow_glow_color = QColor(245, 158, 11, 160)  # Amber
        elif stage == "api":
            self._slow_glow_color = QColor(96, 165, 250, 160)  # Light blue
        else:
            self._slow_glow_color = None
        self._slow_glow_active = True
        self.update()

    def _clear_slow_glow(self):
        """Clear the slow-stage glow indicator."""
        if hasattr(self, "_slow_glow_active") and self._slow_glow_active:
            self._slow_glow_active = False
            self._slow_glow_color = None
            self.update()

    def _clear_command_flash(self):
        """Clear command execution flash."""
        self._command_flash_active = False
        self.update()

    def _spring_back_press(self):
        """Spring back from press-down to normal scale (elastic effect)."""
        self._press_scale_target = self._PRESS_SCALE_NORMAL

    def _force_shrink(self):
        """Fallback: Force shrink if on_insert_complete didn't arrive in time."""
        self._log(
            f">>> FALLBACK_SHRINK triggered, processing={self._is_processing}, state={self._state}"
        )
        # Don't force shrink during active recording — voice activity drives the scale
        if self._state in (self.STATE_RECORDING, self.STATE_SELECTION_LISTENING):
            self._log(">>> FALLBACK_SHRINK: still recording, skipped")
            self._is_processing = False
            return
        if self._is_processing:
            # on_insert_complete didn't arrive, force shrink anyway
            self._is_processing = False
            self._is_speaking = False
            self._icon_scale_target = 0.0

            # Respect sleeping states' smaller scale
            if self._state == self.STATE_SLEEPING:
                self._window_scale_target = self._SCALE_IDLE * 0.9
                self._log(
                    f">>> SHRINK (fallback) to {self._SCALE_IDLE * 0.9} (sleeping)"
                )
            elif self._state == self.STATE_DEEP_SLEEPING:
                self._window_scale_target = self._SCALE_IDLE * 0.82
                self._log(
                    f">>> SHRINK (fallback) to {self._SCALE_IDLE * 0.82} (deep sleeping)"
                )
            elif self._state == self.STATE_LOADING:
                self._window_scale_target = self._SCALE_IDLE * 0.95
                self._log(
                    f">>> SHRINK (fallback) to {self._SCALE_IDLE * 0.95} (loading)"
                )
            else:
                self._window_scale_target = self._SCALE_IDLE
                self._log(f">>> SHRINK (fallback) to {self._SCALE_IDLE}")

            self.update()

    def unlock(self):
        """Unlock the ball (called externally to restore interactivity)."""
        if self._is_locked:
            self._is_locked = False
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

    def set_ocr_mode(self, mode: str) -> None:
        """
        Set screen-OCR tier on popup menu (for syncing with backend / settings).

        Args:
            mode: "off", "auto", or "full"
        """
        if self._popup_menu:
            self._popup_menu.setOcrMode(mode)

    def set_capture_mode(self, mode: str) -> None:
        """
        Set mic capture-mode tier on popup menu (for syncing with backend).

        Args:
            mode: "standard", "noisy", or "whisper"
        """
        if self._popup_menu:
            self._popup_menu.setCaptureMode(mode)

    def set_mic_input_gain(self, gain: float) -> None:
        """Set microphone receive-volume slider on popup menu."""
        if self._popup_menu:
            self._popup_menu.setMicInputGain(gain)

    def set_output_mode(self, mode: str) -> None:
        """
        Set text output mode on popup menu (no signal emission).

        Args:
            mode: "clipboard" or "typewriter"
        """
        if self._popup_menu:
            self._popup_menu.setOutputMode(mode)

    def set_translate_mode(self, mode: str) -> None:
        """
        Set translate output mode on popup menu (for syncing with other UI components).

        Args:
            mode: "popup" or "clipboard"
        """
        if self._popup_menu:
            self._popup_menu.setTranslateMode(mode)

    def set_api_status(self, status_payload) -> None:
        """Set API failover status on popup menu."""
        if self._popup_menu:
            self._popup_menu.setApiStatus(status_payload)

    def set_asr_status(self, status_payload) -> None:
        """Store ASR status and keep GPU install progress visible by the ball."""
        try:
            if isinstance(status_payload, str):
                status = json.loads(status_payload) if status_payload else {}
            else:
                status = dict(status_payload or {})
        except Exception:
            status = {}
        self._asr_status = status

        installing = bool(status.get("gpu_installing"))
        if installing:
            self._show_gpu_install_progress(
                int(status.get("gpu_install_progress", 0) or 0),
                str(status.get("gpu_install_message") or ""),
            )
        elif self._gpu_install_notice_active:
            self._gpu_install_notice_active = False
            # The worker always emits a concrete completion/failure notice.
            # Do not start another timer here: it could shorten that notice.

        if self._popup_menu:
            self._popup_menu.setAsrStatus(status)
        self._log(f"ASR_STATUS: {status}")

    def set_enabled_state(self, enabled: bool) -> None:
        """
        Set enable state on popup menu (for syncing with backend state).

        Args:
            enabled: True if ASR is enabled
        """
        if self._popup_menu:
            self._popup_menu.setAppEnabled(enabled)

    def set_deep_sleeping_state(self, is_deep_sleeping: bool) -> None:
        """
        Set deep sleeping state for popup menu button sync.

        Args:
            is_deep_sleeping: True if currently in deep sleep mode
        """
        if self._popup_menu:
            self._popup_menu.setDeepSleeping(is_deep_sleeping)

    def set_auto_send(self, enabled: bool) -> None:
        """
        Set auto-send state indicator (changes ball interior color).

        Args:
            enabled: True if auto-send is enabled
        """
        print(
            f"[FloatingBall] set_auto_send called: {enabled} (was: {self._auto_send_enabled})"
        )
        if self._auto_send_enabled != enabled:
            self._auto_send_enabled = enabled
            self._log(f"AUTO_SEND state changed: {enabled}")
            print(
                f"[FloatingBall] AUTO_SEND state changed to: {enabled}, calling update()"
            )
            self.update()

    def set_streaming_display(self, enabled: bool) -> None:
        """
        Enable or disable streaming text display.

        Args:
            enabled: True to show streaming text, False to hide
        """
        self._streaming_display_enabled = enabled
        if not enabled:
            # Hide immediately if disabling - force state to allow fade out
            self._streaming_hide_timer.stop()
            if self._streaming_state != "hidden":
                # Force state to allow fade-out even if fading_in
                if self._streaming_state == "fading_in":
                    self._streaming_state = "visible"
                self._fade_out_streaming_label()
        self._log(f"STREAMING_DISPLAY: {enabled}")

    def is_streaming_display_enabled(self) -> bool:
        """Return whether streaming display is enabled."""
        return self._streaming_display_enabled

    def set_engine_info(self, engine_name: str) -> None:
        """
        Set the current ASR engine name for popup display.

        Args:
            engine_name: Engine name, e.g., "FunASR", "Qwen3-ASR (1.7B)"
        """
        self._engine_info = engine_name
        if self._popup_menu:
            self._popup_menu.setEngineInfo(engine_name)
        self._log(f"ENGINE_INFO: {engine_name}")
