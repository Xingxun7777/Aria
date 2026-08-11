# popup_menu.py
# Styled popup menu for Aria floating ball
# Left-click menu with enable toggle, polish modes, and settings

import json
from dataclasses import replace

from PySide6.QtCore import (
    Qt,
    Signal,
    QTimer,
    QPropertyAnimation,
    QEasingCurve,
    Property,
    QPoint,
    QRectF,
)
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QFrame,
    QGraphicsDropShadowEffect,
    QButtonGroup,
    QSlider,
)
from PySide6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QFont,
    QFontMetrics,
)

from . import styles


POPUP_MENU_THEME = replace(
    styles.DARK_THEME,
    panel_bg="rgba(26, 26, 30, 0.97)",
    button_bg="rgba(255, 255, 255, 0.05)",
    button_hover_bg="rgba(255, 255, 255, 0.10)",
    border="rgba(255, 255, 255, 0.08)",
    border_strong="rgba(255, 255, 255, 0.22)",
    text_primary="#E5E7EB",
    text_secondary="#9CA3AF",
    text_muted="#6B7280",
    accent="#FF8C00",
    accent_hover="#FFAA33",
    accent_soft="rgba(255, 140, 0, 0.18)",
    accent_border="rgba(255, 140, 0, 0.38)",
    separator="rgba(255, 255, 255, 0.06)",
    popup_shadow="rgba(0, 0, 0, 0.48)",
    success="#4CAF50",
)


class ToggleSwitch(QWidget):
    """iOS-style toggle switch."""

    toggled = Signal(bool)

    def __init__(self, theme: styles.ThemePalette, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._checked = False
        self._circle_pos = 3
        self.setFixedSize(44, 24)
        self.setCursor(Qt.PointingHandCursor)

        # Animation
        self._animation = QPropertyAnimation(self, b"circle_pos")
        self._animation.setDuration(150)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)

    def get_circle_pos(self):
        return self._circle_pos

    def set_circle_pos(self, pos):
        self._circle_pos = pos
        self.update()

    circle_pos = Property(int, get_circle_pos, set_circle_pos)

    def isChecked(self):
        return self._checked

    def setChecked(self, checked, emit=True):
        if self._checked != checked:
            self._checked = checked
            self._animation.setStartValue(self._circle_pos)
            self._animation.setEndValue(23 if checked else 3)
            self._animation.start()
            if emit:
                self.toggled.emit(checked)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setChecked(not self._checked)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Background
        if self._checked:
            bg_color = QColor(self._theme.success)
        else:
            bg_color = QColor(self._theme.border_strong)

        painter.setPen(Qt.NoPen)
        painter.setBrush(bg_color)
        painter.drawRoundedRect(0, 0, 44, 24, 12, 12)

        # Circle
        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(self._circle_pos, 3, 18, 18)


class ModeButton(QPushButton):
    """Styled mode selection button."""

    def __init__(self, text, theme: styles.ThemePalette, icon_char="", parent=None):
        super().__init__(parent)
        self._theme = theme
        self.setText(text)
        self._icon_char = icon_char
        self._selected = False
        self.setCheckable(True)
        self.setFixedHeight(32)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(self._get_style())

    def _get_style(self):
        return f"""
            ModeButton {{
                background-color: {self._theme.button_bg};
                border: 1px solid {self._theme.border};
                border-radius: 6px;
                color: {self._theme.text_primary};
                font-size: 12px;
                padding: 4px 8px;
            }}
            ModeButton:hover {{
                background-color: {self._theme.button_hover_bg};
                border-color: {self._theme.border_strong};
            }}
            ModeButton:checked {{
                background-color: {self._theme.accent_soft};
                border-color: {self._theme.accent_border};
                color: {self._theme.accent_hover};
            }}
            ModeButton:disabled {{
                background-color: rgba(255, 255, 255, 0.03);
                border-color: {self._theme.border};
                color: {self._theme.text_muted};
            }}
        """


class SoftSlider(QSlider):
    """Compact custom-painted slider for the dark floating popup."""

    def __init__(self, theme: styles.ThemePalette, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._theme = theme
        self.setFixedHeight(26)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)

    def enterEvent(self, event):
        self.update()
        return super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        return super().leaveEvent(event)

    def _track_rect(self) -> QRectF:
        margin = 11.0
        y = self.height() / 2.0 - 3.0
        return QRectF(margin, y, max(1.0, self.width() - margin * 2.0), 6.0)

    def _ratio(self) -> float:
        span = max(1, self.maximum() - self.minimum())
        return max(0.0, min(1.0, (self.value() - self.minimum()) / span))

    def _value_from_x(self, x: float) -> int:
        rect = self._track_rect()
        ratio = max(0.0, min(1.0, (float(x) - rect.left()) / max(1.0, rect.width())))
        return int(round(self.minimum() + ratio * (self.maximum() - self.minimum())))

    def _event_x(self, event) -> float:
        if hasattr(event, "position"):
            return float(event.position().x())
        return float(event.pos().x())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            self.setSliderDown(True)
            self.setValue(self._value_from_x(self._event_x(event)))
            self.update()
            event.accept()
            return
        return super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown() and self.isEnabled():
            self.setValue(self._value_from_x(self._event_x(event)))
            self.update()
            event.accept()
            return
        return super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.isSliderDown():
            self.setValue(self._value_from_x(self._event_x(event)))
            self.setSliderDown(False)
            self.sliderReleased.emit()
            self.update()
            event.accept()
            return
        return super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        track = self._track_rect()
        center_y = track.center().y()
        ratio = self._ratio()
        handle_x = track.left() + track.width() * ratio
        enabled = self.isEnabled()
        active = (self.underMouse() or self.isSliderDown()) and enabled

        # Quiet recessed rail: less "system default", more like the existing cards.
        rail_bg = QColor(255, 255, 255, 34 if enabled else 18)
        rail_border = QColor(255, 255, 255, 28 if enabled else 14)
        painter.setPen(QPen(rail_border, 1))
        painter.setBrush(rail_bg)
        painter.drawRoundedRect(track, 3.0, 3.0)

        # Subtle baseline inside the rail to avoid a flat plastic look.
        inner = QRectF(track.left() + 1, track.top() + 1, track.width() - 2, 2)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 16 if enabled else 8))
        painter.drawRoundedRect(inner, 2.0, 2.0)

        fill_width = max(0.0, handle_x - track.left())
        if fill_width > 0.5:
            fill = QRectF(track.left(), track.top(), fill_width, track.height())
            fill_grad = QLinearGradient(fill.left(), 0, fill.right(), 0)
            if enabled:
                fill_grad.setColorAt(0, QColor(255, 140, 0))
                fill_grad.setColorAt(1, QColor(255, 179, 71))
            else:
                fill_grad.setColorAt(0, QColor(120, 120, 120))
                fill_grad.setColorAt(1, QColor(150, 150, 150))
            painter.setBrush(fill_grad)
            painter.drawRoundedRect(fill, 3.0, 3.0)

        # Soft halo + warm knob, without the harsh white ring from the old style.
        halo_radius = 10.0 if active else 8.5
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 140, 0, 58 if active else 34))
        painter.drawEllipse(
            QRectF(
                handle_x - halo_radius,
                center_y - halo_radius,
                halo_radius * 2,
                halo_radius * 2,
            )
        )

        knob_radius = 7.6 if active else 7.0
        knob_rect = QRectF(
            handle_x - knob_radius,
            center_y - knob_radius,
            knob_radius * 2,
            knob_radius * 2,
        )
        knob_grad = QLinearGradient(0, knob_rect.top(), 0, knob_rect.bottom())
        if enabled:
            knob_grad.setColorAt(0, QColor(255, 226, 166))
            knob_grad.setColorAt(0.58, QColor(255, 175, 63))
            knob_grad.setColorAt(1, QColor(255, 142, 18))
            border = QColor(255, 193, 102, 230)
        else:
            knob_grad.setColorAt(0, QColor(160, 160, 160))
            knob_grad.setColorAt(1, QColor(95, 95, 95))
            border = QColor(130, 130, 130, 170)
        painter.setBrush(knob_grad)
        painter.setPen(QPen(border, 1.2))
        painter.drawEllipse(knob_rect)

        # Tiny top highlight keeps the knob tactile but not "icon-like".
        if enabled:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 72))
            painter.drawEllipse(QRectF(handle_x - 3.0, center_y - 5.0, 5.0, 3.0))


class PopupMenu(QWidget):
    """
    Styled popup menu for floating ball.

    Features:
    - Enable/disable toggle
    - Polish mode selection (Off / Local / Quality)
    - Quick toggles (lock, sleep, streaming, translate mode)
    - Settings & history links
    """

    # Signals
    enableToggled = Signal(bool)
    modeChanged = Signal(str)  # "off", "quality", or "fast"
    ocrModeChanged = Signal(str)  # "off", "auto", or "full" (screen OCR tier)
    captureModeChanged = Signal(str)  # "standard", "noisy", or "whisper" (mic DSP)
    settingsRequested = Signal()
    historyRequested = Signal()
    correctionRulesRequested = Signal()
    pasteLastRequested = Signal()
    copyLastRequested = Signal()
    draftBoxRequested = Signal()
    lockToggled = Signal(bool)
    deepSleepToggled = Signal(bool)
    streamingToggled = Signal(bool)
    translateModeChanged = Signal(str)  # "popup" or "clipboard"
    outputModeChanged = Signal(str)  # text output: "clipboard" or "typewriter"
    apiSwitchBackRequested = Signal()
    deepseekSetupRequested = Signal()
    asrDeviceModeChanged = Signal(str)  # "gpu" or "cpu"
    asrEngineModeChanged = Signal(str)  # cross-engine switch target, e.g. "qwen3_sherpa"

    # 识别方式 card button skins. The same two physical buttons render either
    # the legacy torch/funasr GPU-CPU device switch ("device") or the
    # sherpa/llamacpp cross-engine switch ("engine"), keyed by the button's
    # asr_device_mode property (gpu = left, cpu = right).
    _ASR_DEVICE_BUTTONS = {
        "gpu": ("显卡加速", "识别更快；如果显卡忙，会自动避开"),
        "cpu": ("CPU加速", "不占用显卡，绘图时更稳"),
    }
    _ASR_ENGINE_BUTTONS = {
        "gpu": (
            "qwen3_llamacpp",
            "GPU 加速",
            "切换到 GPU 加速引擎（llama.cpp），识别更快",
        ),
        "cpu": (
            "qwen3_sherpa",
            "CPU 轻量",
            "切换到轻量引擎（sherpa），不占用显卡",
        ),
    }
    micInputGainChanged = Signal(float)  # software mic gain, 1.0 = 100%
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme = POPUP_MENU_THEME
        self._enabled = True
        self._current_mode = "quality"
        self._current_ocr_mode = "auto"
        self._current_capture_mode = "standard"
        self._is_locked = False
        self._is_deep_sleeping = False
        self._is_loading = False
        self._engine_info = "FunASR"
        self._asr_status = {}
        self._current_asr_device_mode = "gpu"
        # "device" (torch/funasr GPU-CPU switch), "engine" (sherpa/llamacpp
        # cross-engine switch) or "none" (degraded, card disabled).
        self._asr_switch_kind = "device"
        self._mic_input_gain = 1.0
        self._current_output_mode = "clipboard"
        self._translate_mode = "popup"
        self._api_status = {}
        self._mic_gain_emit_timer: QTimer | None = None
        self._init_window()
        self._init_ui()
        self._apply_shadow()

    def _init_window(self):
        """Setup window flags for popup."""
        self.setWindowFlags(
            Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(292)

    def _init_ui(self):
        """Build the UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(0)

        # Container with background
        self.container = QFrame()
        self.container.setStyleSheet(self._container_style())
        cl = QVBoxLayout(self.container)
        cl.setContentsMargins(14, 14, 14, 12)
        cl.setSpacing(0)

        # ── Header: Aria + engine info + toggle ──
        header = QHBoxLayout()
        header.setSpacing(6)

        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        title_label = QLabel("Aria")
        title_label.setStyleSheet(
            f"color: {self._theme.text_primary}; font-size: 15px; font-weight: bold;"
        )
        title_col.addWidget(title_label)

        self.engine_label = QLabel(self._engine_info)
        self.engine_label.setStyleSheet(
            f"color: {self._theme.text_muted}; font-size: 10px;"
        )
        title_col.addWidget(self.engine_label)
        header.addLayout(title_col)

        header.addStretch()
        self.toggle = ToggleSwitch(self._theme)
        self.toggle.setChecked(True)
        self.toggle.toggled.connect(self._on_enable_toggled)
        header.addWidget(self.toggle)
        cl.addLayout(header)

        cl.addSpacing(10)
        cl.addWidget(self._make_separator())
        cl.addSpacing(8)

        # ── ASR runtime status + quick device switch ──
        self.asr_card = QFrame()
        self.asr_card.setObjectName("asrCard")
        self.asr_card.setStyleSheet(
            f"QFrame#asrCard {{ background-color: {self._theme.button_bg};"
            f" border: 1px solid {self._theme.border}; border-radius: 9px; }}"
        )
        asr_l = QVBoxLayout(self.asr_card)
        asr_l.setContentsMargins(12, 10, 12, 10)
        asr_l.setSpacing(6)

        asr_title_row = QHBoxLayout()
        asr_title_row.setContentsMargins(0, 0, 0, 0)
        asr_title_row.setSpacing(8)
        self.asr_dot = QLabel()
        self.asr_dot.setFixedSize(8, 8)
        self.asr_dot.setStyleSheet(
            f"background-color: {self._theme.text_muted}; border-radius: 4px;"
        )
        self.asr_title = QLabel("识别方式")
        self.asr_title.setStyleSheet(
            f"color: {self._theme.text_primary}; font-size: 12px; font-weight: 600;"
        )
        asr_title_row.addWidget(self.asr_dot)
        asr_title_row.addWidget(self.asr_title)
        asr_title_row.addStretch(1)
        asr_l.addLayout(asr_title_row)

        self.asr_detail = QLabel("等待状态…")
        self.asr_detail.setStyleSheet(
            f"color: {self._theme.text_secondary}; font-size: 11px; padding-left: 16px;"
        )
        self.asr_detail.setWordWrap(True)
        asr_l.addWidget(self.asr_detail)

        self.asr_device_group = QButtonGroup(self)
        self.asr_device_group.setExclusive(True)
        self._asr_engine_hints = {}
        asr_mode_row = QHBoxLayout()
        asr_mode_row.setSpacing(6)
        for mode_id in ("gpu", "cpu"):
            mode_name, mode_tip = self._ASR_DEVICE_BUTTONS[mode_id]
            btn = ModeButton(mode_name, self._theme)
            btn.setProperty("asr_device_mode", mode_id)
            btn.setToolTip(mode_tip)
            self.asr_device_group.addButton(btn)
            mode_col = QVBoxLayout()
            mode_col.setContentsMargins(0, 0, 0, 0)
            mode_col.setSpacing(2)
            mode_col.addWidget(btn)
            hint = QLabel("")
            hint.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
            hint.setStyleSheet(
                f"color: {self._theme.text_muted}; font-size: 9px;"
            )
            hint.hide()
            self._asr_engine_hints[mode_id] = hint
            mode_col.addWidget(hint)
            asr_mode_row.addLayout(mode_col, 1)
            if mode_id == self._current_asr_device_mode:
                btn.setChecked(True)
        self.asr_device_group.buttonClicked.connect(self._on_asr_device_clicked)
        asr_l.addLayout(asr_mode_row)

        cl.addWidget(self.asr_card)

        cl.addSpacing(10)
        cl.addWidget(self._make_separator())
        cl.addSpacing(8)

        # ── Polish Mode ──
        section = QLabel("润色模式")
        section.setStyleSheet(self._section_style())
        cl.addWidget(section)
        cl.addSpacing(6)

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)

        modes = [
            ("off", "关闭"),
            ("fast", "本地润色"),
            ("quality", "高质量"),
        ]

        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        for mode_id, mode_name in modes:
            btn = ModeButton(mode_name, self._theme)
            btn.setProperty("mode_id", mode_id)
            self.mode_group.addButton(btn)
            mode_row.addWidget(btn)
            if mode_id == self._current_mode:
                btn.setChecked(True)
        self.mode_group.buttonClicked.connect(self._on_mode_clicked)
        cl.addLayout(mode_row)

        cl.addSpacing(10)
        cl.addWidget(self._make_separator())
        cl.addSpacing(8)

        # ── High-quality polish status card ──
        self.api_card = QFrame()
        self.api_card.setObjectName("apiCard")
        self.api_card.setStyleSheet(
            f"QFrame#apiCard {{ background-color: {self._theme.button_bg};"
            f" border: 1px solid {self._theme.border}; border-radius: 8px; }}"
        )
        card_l = QVBoxLayout(self.api_card)
        card_l.setContentsMargins(12, 10, 12, 10)
        card_l.setSpacing(5)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        self.api_dot = QLabel()
        self.api_dot.setFixedSize(8, 8)
        self.api_dot.setStyleSheet(
            f"background-color: {self._theme.text_muted}; border-radius: 4px;"
        )
        self.api_title = QLabel("高质量润色")
        self.api_title.setStyleSheet(
            f"color: {self._theme.text_primary}; font-size: 12px; font-weight: 600;"
        )
        title_row.addWidget(self.api_dot)
        title_row.addWidget(self.api_title)
        title_row.addStretch(1)
        card_l.addLayout(title_row)

        self.api_status_detail = QLabel("未开启")
        self.api_status_detail.setStyleSheet(
            f"color: {self._theme.text_secondary}; font-size: 11px; padding-left: 16px;"
        )
        card_l.addWidget(self.api_status_detail)

        self.api_switch_btn = self._make_action_btn("切回常用线路")
        self.api_switch_btn.clicked.connect(self._on_api_switch_clicked)
        self.api_switch_btn.hide()
        self.api_setup_btn = self._make_action_btn("配置 DeepSeek API")
        self.api_setup_btn.setToolTip("输入 API Key，自动配置并验证 DeepSeek Flash")
        self.api_setup_btn.clicked.connect(self._on_deepseek_setup_clicked)
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(16, 2, 0, 0)
        btn_row.addWidget(self.api_setup_btn)
        btn_row.addWidget(self.api_switch_btn)
        btn_row.addStretch(1)
        card_l.addLayout(btn_row)

        cl.addWidget(self.api_card)

        cl.addSpacing(10)
        cl.addWidget(self._make_separator())
        cl.addSpacing(8)

        # ── Screen OCR Mode (three-tier cache-aware) ──
        # off  = no screen reading at all (lowest cost / best privacy)
        # auto = read screen for auto-hotword learning only; not injected
        #        into Polish prompt → DeepSeek prefix-cache stays warm
        # full = legacy: every utterance feeds fresh screen text into Polish
        ocr_section = QLabel("屏幕识别")
        ocr_section.setStyleSheet(self._section_style())
        cl.addWidget(ocr_section)
        cl.addSpacing(6)

        self.ocr_mode_group = QButtonGroup(self)
        self.ocr_mode_group.setExclusive(True)

        ocr_modes = [
            ("off", "关闭"),
            ("auto", "仅自动学习"),
            ("full", "开启OCR"),
        ]

        ocr_row = QHBoxLayout()
        ocr_row.setSpacing(6)
        for mode_id, mode_name in ocr_modes:
            btn = ModeButton(mode_name, self._theme)
            btn.setProperty("ocr_mode_id", mode_id)
            self.ocr_mode_group.addButton(btn)
            ocr_row.addWidget(btn)
            if mode_id == self._current_ocr_mode:
                btn.setChecked(True)
        self.ocr_mode_group.buttonClicked.connect(self._on_ocr_mode_clicked)
        cl.addLayout(ocr_row)

        cl.addSpacing(10)
        cl.addWidget(self._make_separator())
        cl.addSpacing(8)

        # ── Capture Mode (mic DSP: HPF→Gate→AGC→Limiter) ──
        # standard = daily default, temperate AGC + moderate gate
        # noisy    = strong-environment-noise mode, aggressive gate to reject
        #            steady noise (HVAC/fan/typing)
        # whisper  = quiet-environment + soft-voice rescue, aggressive AGC
        capture_section = QLabel("收音模式")
        capture_section.setStyleSheet(self._section_style())
        cl.addWidget(capture_section)
        cl.addSpacing(6)

        self.capture_mode_group = QButtonGroup(self)
        self.capture_mode_group.setExclusive(True)

        capture_modes = [
            ("standard", "正常"),
            ("noisy", "嘈杂"),
            ("whisper", "轻语"),
        ]

        capture_row = QHBoxLayout()
        capture_row.setSpacing(6)
        for mode_id, mode_name in capture_modes:
            btn = ModeButton(mode_name, self._theme)
            btn.setProperty("capture_mode_id", mode_id)
            self.capture_mode_group.addButton(btn)
            capture_row.addWidget(btn)
            if mode_id == self._current_capture_mode:
                btn.setChecked(True)
        self.capture_mode_group.buttonClicked.connect(self._on_capture_mode_clicked)
        cl.addLayout(capture_row)

        cl.addSpacing(8)
        cl.addWidget(self._make_mic_gain_panel())
        cl.addSpacing(10)
        cl.addWidget(self._make_separator())
        cl.addSpacing(8)

        # ── Output Mode (how recognized text lands in the target app) ──
        # clipboard  = Ctrl+V paste — fast, default
        # typewriter = per-character SendInput — keeps the clipboard
        #              untouched; for remote desktop / apps where paste fails
        # Pure config flip (output.typewriter_mode), no assets and no async
        # reload — so unlike the ASR card these buttons are never disabled.
        output_section = QLabel("输出方式")
        output_section.setStyleSheet(self._section_style())
        cl.addWidget(output_section)
        cl.addSpacing(6)

        self.output_mode_group = QButtonGroup(self)
        self.output_mode_group.setExclusive(True)

        output_modes = [
            (
                "clipboard",
                "剪贴板",
                "剪贴板模式（Ctrl+V 粘贴）：速度快，适合日常使用",
            ),
            (
                "typewriter",
                "打字机",
                "打字机模式（逐字输入）：不占用剪贴板，适合远程桌面等粘贴不生效的场景",
            ),
        ]

        output_row = QHBoxLayout()
        output_row.setSpacing(6)
        for mode_id, mode_name, mode_tip in output_modes:
            btn = ModeButton(mode_name, self._theme)
            btn.setProperty("output_mode_id", mode_id)
            btn.setToolTip(mode_tip)
            self.output_mode_group.addButton(btn)
            output_row.addWidget(btn)
            if mode_id == self._current_output_mode:
                btn.setChecked(True)
        self.output_mode_group.buttonClicked.connect(self._on_output_mode_clicked)
        cl.addLayout(output_row)

        cl.addSpacing(10)
        cl.addWidget(self._make_separator())
        cl.addSpacing(6)

        # ── Quick Actions ──
        # Keep this strip deliberately minimal. Recovery remains available
        # through the last-transcript flow; Draft Box is currently disabled,
        # and correction rules stay in their dedicated settings flow.
        action_row = QHBoxLayout()
        action_row.setSpacing(6)

        self.settings_btn = self._make_action_btn("设置")
        self.settings_btn.clicked.connect(self._on_settings_clicked)
        action_row.addWidget(self.settings_btn)

        self.history_btn = self._make_action_btn("历史")
        self.history_btn.clicked.connect(self._on_history_clicked)
        action_row.addWidget(self.history_btn)

        cl.addLayout(action_row)

        cl.addSpacing(8)
        cl.addWidget(self._make_separator())
        cl.addSpacing(6)

        # ── Toggle Rows ──
        self._add_toggle_row(cl, "翻译弹窗", self._on_translate_toggled, "translate")
        self.translate_toggle.setChecked(True, emit=False)  # Default: popup mode ON
        cl.addSpacing(4)
        self._add_toggle_row(cl, "实时字幕", self._on_streaming_toggled, "streaming")
        cl.addSpacing(4)
        self._add_toggle_row(cl, "锁定位置", self._on_lock_toggled, "lock")
        cl.addSpacing(8)

        # ── Deep Sleep Button ──
        self.deep_sleep_btn = QPushButton("深度休眠")
        self.deep_sleep_btn.setCursor(Qt.PointingHandCursor)
        self.deep_sleep_btn.setFixedHeight(30)
        self._update_deep_sleep_btn_style()
        self.deep_sleep_btn.clicked.connect(self._on_deep_sleep_clicked)
        cl.addWidget(self.deep_sleep_btn)

        layout.addWidget(self.container)

    def _make_separator(self) -> QFrame:
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color: {self._theme.separator};")
        return sep

    def _make_action_btn(self, text: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(30)
        btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {self._theme.button_bg};
                border: 1px solid {self._theme.border};
                border-radius: 6px;
                color: {self._theme.text_secondary};
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {self._theme.button_hover_bg};
                border-color: {self._theme.border_strong};
                color: {self._theme.text_primary};
            }}
        """
        )
        return btn

    def _make_mic_gain_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("micGainPanel")
        panel.setStyleSheet(
            f"""
            QFrame#micGainPanel {{
                background-color: {self._theme.button_bg};
                border: 1px solid {self._theme.border};
                border-radius: 8px;
            }}
        """
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        title = QLabel("麦克风音量")
        title.setStyleSheet(f"color: {self._theme.text_primary}; font-size: 12px;")
        self.mic_gain_value = QLabel("100%")
        self.mic_gain_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.mic_gain_value.setStyleSheet(
            f"color: {self._theme.accent_hover}; font-size: 12px; font-weight: 600;"
        )
        row.addWidget(title)
        row.addStretch(1)
        row.addWidget(self.mic_gain_value)
        layout.addLayout(row)

        self.mic_gain_slider = SoftSlider(self._theme)
        self.mic_gain_slider.setRange(50, 200)
        self.mic_gain_slider.setSingleStep(5)
        self.mic_gain_slider.setPageStep(10)
        self.mic_gain_slider.setValue(100)
        self.mic_gain_slider.setToolTip("调高更容易收小声；环境吵时建议保持 100% 左右")
        self.mic_gain_slider.valueChanged.connect(self._on_mic_gain_slider_changed)
        self.mic_gain_slider.sliderReleased.connect(self._emit_mic_gain_now)
        layout.addWidget(self.mic_gain_slider)

        self._mic_gain_emit_timer = QTimer(self)
        self._mic_gain_emit_timer.setSingleShot(True)
        self._mic_gain_emit_timer.setInterval(160)
        self._mic_gain_emit_timer.timeout.connect(self._emit_mic_gain_now)
        return panel

    def _add_toggle_row(self, parent_layout, label_text, callback, attr_name):
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        label = QLabel(label_text)
        label.setStyleSheet(f"color: {self._theme.text_primary}; font-size: 12px;")
        toggle = ToggleSwitch(self._theme)
        toggle.setChecked(False)
        toggle.toggled.connect(callback)
        setattr(self, f"{attr_name}_toggle", toggle)

        row.addWidget(label)
        row.addStretch()
        row.addWidget(toggle)
        parent_layout.addLayout(row)

    def _section_style(self) -> str:
        return (
            f"color: {self._theme.text_muted}; font-size: 10px;"
            f" font-weight: 600; letter-spacing: 0.5px;"
        )

    def _apply_shadow(self):
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(32)
        shadow.setColor(styles.qcolor(self._theme.popup_shadow))
        shadow.setOffset(0, 6)
        self.container.setGraphicsEffect(shadow)

    def _container_style(self) -> str:
        return f"""
            QFrame {{
                background-color: {self._theme.panel_bg};
                border-radius: 14px;
                border: 1px solid {self._theme.border};
            }}
        """

    # ── Callbacks ──

    def _on_enable_toggled(self, enabled):
        self._enabled = enabled
        self.enableToggled.emit(enabled)

    def _on_mode_clicked(self, button):
        mode_id = button.property("mode_id")
        if mode_id and mode_id != self._current_mode:
            self._current_mode = mode_id
            self.modeChanged.emit(mode_id)

    def _on_ocr_mode_clicked(self, button):
        mode_id = button.property("ocr_mode_id")
        if mode_id and mode_id != self._current_ocr_mode:
            self._current_ocr_mode = mode_id
            self.ocrModeChanged.emit(mode_id)

    def _on_capture_mode_clicked(self, button):
        mode_id = button.property("capture_mode_id")
        if mode_id and mode_id != self._current_capture_mode:
            self._current_capture_mode = mode_id
            self.captureModeChanged.emit(mode_id)

    def _on_output_mode_clicked(self, button):
        mode_id = button.property("output_mode_id")
        if mode_id and mode_id != self._current_output_mode:
            self._current_output_mode = mode_id
            self.outputModeChanged.emit(mode_id)

    def _on_mic_gain_slider_changed(self, value: int):
        self._mic_input_gain = max(0.5, min(2.0, float(value) / 100.0))
        self.mic_gain_value.setText(f"{int(round(self._mic_input_gain * 100))}%")
        if self._mic_gain_emit_timer is not None:
            self._mic_gain_emit_timer.start()

    def _emit_mic_gain_now(self):
        if self._mic_gain_emit_timer is not None and self._mic_gain_emit_timer.isActive():
            self._mic_gain_emit_timer.stop()
        self.micInputGainChanged.emit(float(self._mic_input_gain))

    def _on_asr_device_clicked(self, button):
        if self._asr_switch_kind == "engine":
            # Cross-engine mode: the button carries the target engine id and
            # clicking the already-active engine is a no-op.
            target = button.property("asr_engine_target")
            current = str((self._asr_status or {}).get("engine") or "").lower()
            if target and str(target) != current:
                self.asrEngineModeChanged.emit(str(target))
            return
        mode_id = button.property("asr_device_mode")
        if mode_id and mode_id != self._current_asr_device_mode:
            self._current_asr_device_mode = mode_id
            self.asrDeviceModeChanged.emit(mode_id)

    def _on_settings_clicked(self):
        self.close()
        self.settingsRequested.emit()

    def _on_history_clicked(self):
        self.close()
        self.historyRequested.emit()

    def _on_correction_rules_clicked(self):
        self.close()
        self.correctionRulesRequested.emit()

    def _on_paste_last_clicked(self):
        self.close()
        self.pasteLastRequested.emit()

    def _on_copy_last_clicked(self):
        self.close()
        self.copyLastRequested.emit()

    def _on_draft_box_clicked(self):
        self.close()
        self.draftBoxRequested.emit()

    def _on_translate_toggled(self, checked):
        mode = "popup" if checked else "clipboard"
        self._translate_mode = mode
        self.translateModeChanged.emit(mode)

    def _on_lock_toggled(self, locked):
        self._is_locked = locked
        self.lockToggled.emit(locked)

    def _on_deep_sleep_clicked(self):
        # Don't toggle state here — wait for backend confirmation via setDeepSleeping()
        # This prevents desync if backend fails to enter/exit deep sleep
        want_deep = not self._is_deep_sleeping
        self.deepSleepToggled.emit(want_deep)

    def _on_streaming_toggled(self, enabled):
        self.streamingToggled.emit(enabled)

    def _on_api_switch_clicked(self):
        self.apiSwitchBackRequested.emit()

    def _on_deepseek_setup_clicked(self):
        # Close the popup before opening the password dialog; otherwise a
        # Qt.Popup can steal focus from the modal input box on Windows.
        self.close()
        self.deepseekSetupRequested.emit()

    # ── Public API ──

    def setAppEnabled(self, enabled):
        self._enabled = enabled
        self.toggle.setChecked(enabled, emit=False)

    def setMode(self, mode):
        self._current_mode = mode
        for btn in self.mode_group.buttons():
            if btn.property("mode_id") == mode:
                btn.setChecked(True)
                break

    def setOcrMode(self, mode):
        if mode not in ("off", "auto", "full"):
            return
        self._current_ocr_mode = mode
        for btn in self.ocr_mode_group.buttons():
            if btn.property("ocr_mode_id") == mode:
                btn.setChecked(True)
                break

    def setCaptureMode(self, mode):
        if mode not in ("standard", "noisy", "whisper"):
            return
        self._current_capture_mode = mode
        for btn in self.capture_mode_group.buttons():
            if btn.property("capture_mode_id") == mode:
                btn.setChecked(True)
                break

    def setOutputMode(self, mode):
        if mode not in ("clipboard", "typewriter"):
            return
        self._current_output_mode = mode
        for btn in self.output_mode_group.buttons():
            if btn.property("output_mode_id") == mode:
                btn.setChecked(True)
                break

    def setMicInputGain(self, gain):
        try:
            value = float(gain)
        except (TypeError, ValueError):
            value = 1.0
        value = max(0.5, min(2.0, value))
        self._mic_input_gain = value
        slider_value = int(round(value * 100))
        self.mic_gain_slider.blockSignals(True)
        try:
            self.mic_gain_slider.setValue(slider_value)
            self.mic_gain_value.setText(f"{slider_value}%")
        finally:
            self.mic_gain_slider.blockSignals(False)

    def setAsrStatus(self, status_payload):
        """Update ASR runtime status card and CPU/GPU quick switch."""
        try:
            if isinstance(status_payload, str):
                status = json.loads(status_payload) if status_payload else {}
            else:
                status = dict(status_payload or {})
        except Exception:
            status = {}
        self._asr_status = status

        requested = str(status.get("requested_mode") or "gpu").lower()
        if requested not in ("gpu", "cpu"):
            requested = "gpu"
        active = str(status.get("active_mode") or "").lower()
        gpu_install_progress = max(
            0, min(100, int(status.get("gpu_install_progress", 0) or 0))
        )
        fallback = bool(status.get("fallback_active"))
        loading = bool(status.get("hot_reloading")) or active == "loading"
        can_switch = bool(status.get("can_request_gpu", True))
        switch_kind = str(status.get("switch_kind") or "device").lower()
        if switch_kind not in ("device", "engine", "none"):
            switch_kind = "device"
        self._asr_switch_kind = switch_kind
        self._current_asr_device_mode = requested

        if switch_kind == "engine":
            # sherpa/llamacpp: the card is a cross-engine switcher. Highlight
            # follows the (target) engine reported by the backend, so a
            # hot-reload rollback snaps the highlight back automatically.
            current_engine = str(status.get("engine") or "").lower()
            raw_targets = status.get("engine_targets")
            targets = {}
            if isinstance(raw_targets, list):
                for entry in raw_targets:
                    if isinstance(entry, dict):
                        targets[str(entry.get("engine") or "").lower()] = entry
            for btn in self.asr_device_group.buttons():
                mode = btn.property("asr_device_mode")
                engine_id, default_label, ok_tip = self._ASR_ENGINE_BUTTONS[mode]
                entry = targets.get(engine_id, {})
                btn.setProperty("asr_engine_target", engine_id)
                btn.setText(str(entry.get("label") or default_label))
                is_current = engine_id == current_engine
                available = bool(entry.get("available", is_current))
                installable = bool(entry.get("installable", False))
                installing = bool(entry.get("installing", False))
                btn.setChecked(is_current)
                btn.setEnabled((available or installable) and not loading and not installing)
                hint = self._asr_engine_hints.get(mode)
                if hint is not None:
                    if installing:
                        hint.setText(f"安装中 {gpu_install_progress}%")
                        hint.show()
                    elif not available and installable:
                        hint.setText("未安装")
                        hint.show()
                    elif not available:
                        hint.setText("不可用")
                        hint.show()
                    else:
                        hint.clear()
                        hint.hide()
                if loading:
                    btn.setToolTip("正在切换识别引擎…")
                elif installing:
                    install_message = str(
                        status.get("gpu_install_message")
                        or "正在后台下载并验证 GPU 加速组件"
                    )
                    btn.setToolTip(install_message)
                elif not available and installable:
                    btn.setToolTip("点击安装 GPU 加速（约 3 GB），完成后自动切换")
                elif not available:
                    reason = str(entry.get("reason") or "").strip()
                    btn.setToolTip(f"暂不可用：{reason or '缺少所需文件'}")
                else:
                    btn.setToolTip(ok_tip)
        else:
            # torch/funasr device switch (legacy semantics) or the degraded
            # "none" state (everything grayed out).
            for btn in self.asr_device_group.buttons():
                mode = btn.property("asr_device_mode")
                label, tip = self._ASR_DEVICE_BUTTONS[mode]
                hint = self._asr_engine_hints.get(mode)
                if hint is not None:
                    hint.clear()
                    hint.hide()
                btn.setProperty("asr_engine_target", None)
                btn.setText(label)
                btn.setChecked(mode == requested)
                btn.setEnabled(can_switch and not loading and switch_kind != "none")
                if not can_switch or switch_kind == "none":
                    btn.setToolTip("当前引擎不支持 GPU/CPU 快速切换")
                else:
                    btn.setToolTip(tip)

        title = str(status.get("status_message") or "识别方式")
        detail = str(status.get("detail") or "")

        t = self._theme
        if loading:
            dot = t.accent
        elif active == "gpu":
            dot = t.success
        elif fallback:
            dot = t.accent
        elif active == "cpu":
            dot = t.success
        else:
            dot = t.text_muted

        self.asr_dot.setStyleSheet(
            f"background-color: {dot}; border-radius: 4px;"
        )
        self.asr_title.setText(title)
        self.asr_title.setStyleSheet(
            f"color: {t.text_primary}; font-size: 12px; font-weight: 600;"
        )
        self.asr_detail.setText(detail or "这里显示当前识别方式")
        self.asr_detail.setStyleSheet(
            f"color: {t.text_secondary}; font-size: 11px; padding-left: 16px;"
        )

    def setApiStatus(self, status_payload):
        """Update the high-quality polish status card shown in the popup."""
        try:
            if isinstance(status_payload, str):
                status = json.loads(status_payload) if status_payload else {}
            else:
                status = dict(status_payload or {})
        except Exception:
            status = {}
        self._api_status = status

        enabled = bool(status.get("enabled"))
        configured = bool(status.get("configured"))
        setup_in_progress = bool(status.get("setup_in_progress"))
        setup_error = str(status.get("setup_error") or "")
        using_backup = bool(status.get("using_backup"))
        is_slow = bool(status.get("last_was_slow"))
        msg = str(status.get("status_message") or "")
        is_error = any(k in msg for k in ("出错", "错误", "失败", "异常"))

        t = self._theme
        if setup_in_progress:
            dot, title_color, detail = t.accent, t.text_primary, "正在验证 DeepSeek API…"
        elif not configured:
            detail = "配置失败，请重试" if setup_error else "尚未配置 DeepSeek API"
            dot = "#EF5350" if setup_error else t.text_muted
            title_color = t.text_primary if setup_error else t.text_muted
        elif not enabled:
            dot, title_color, detail = t.text_muted, t.text_muted, "未开启"
        elif using_backup:
            dot, title_color, detail = t.accent, t.text_primary, "已切换备用线路"
        elif is_error:
            dot, title_color, detail = "#EF5350", t.text_primary, "连接异常，正在重试"
        elif is_slow:
            dot, title_color, detail = t.accent, t.text_primary, "当前网络较慢"
        else:
            dot, title_color, detail = t.success, t.text_primary, "运行流畅"

        self.api_dot.setStyleSheet(
            f"background-color: {dot}; border-radius: 4px;"
        )
        self.api_title.setStyleSheet(
            f"color: {title_color}; font-size: 12px; font-weight: 600;"
        )
        self.api_status_detail.setText(detail)
        self.api_status_detail.setStyleSheet(
            f"color: {t.text_secondary}; font-size: 11px; padding-left: 16px;"
        )

        can_switch = bool(status.get("can_switch_primary"))
        self.api_switch_btn.setVisible(can_switch)
        self.api_switch_btn.setEnabled(can_switch)
        self.api_setup_btn.setVisible(not configured)
        self.api_setup_btn.setEnabled(not setup_in_progress)
        self.api_setup_btn.setText(
            "正在验证…" if setup_in_progress else "配置 DeepSeek API"
        )

    def setLocked(self, locked):
        self._is_locked = locked
        self.lock_toggle.setChecked(locked, emit=False)

    def setDeepSleeping(self, active):
        self._is_deep_sleeping = active
        self._is_loading = False
        self._update_deep_sleep_btn_style()

    def setLoading(self, loading):
        self._is_loading = loading
        self._update_deep_sleep_btn_style()

    def _update_deep_sleep_btn_style(self):
        t = self._theme
        if self._is_loading:
            self.deep_sleep_btn.setText("加载中...")
            self.deep_sleep_btn.setEnabled(False)
            self.deep_sleep_btn.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: {t.button_bg};
                    border: 1px solid {t.border};
                    border-radius: 6px;
                    color: {t.text_secondary};
                    font-size: 12px;
                }}
                """
            )
        elif self._is_deep_sleeping:
            self.deep_sleep_btn.setText("唤醒引擎")
            self.deep_sleep_btn.setEnabled(True)
            self.deep_sleep_btn.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: rgba(220, 80, 60, 0.15);
                    border: 1px solid rgba(220, 80, 60, 0.4);
                    border-radius: 6px;
                    color: #e05545;
                    font-size: 12px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background-color: rgba(220, 80, 60, 0.25);
                    border-color: rgba(220, 80, 60, 0.6);
                }}
                """
            )
        else:
            self.deep_sleep_btn.setText("深度休眠")
            self.deep_sleep_btn.setEnabled(True)
            self.deep_sleep_btn.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: {t.button_bg};
                    border: 1px solid {t.border};
                    border-radius: 6px;
                    color: {t.text_secondary};
                    font-size: 12px;
                }}
                QPushButton:hover {{
                    background-color: {t.button_hover_bg};
                    border-color: {t.border_strong};
                    color: {t.text_primary};
                }}
                """
            )

    def setStreaming(self, enabled):
        self.streaming_toggle.setChecked(enabled, emit=False)

    def setTranslateMode(self, mode):
        self._translate_mode = mode
        # translate_toggle: checked = popup, unchecked = clipboard
        self.translate_toggle.setChecked(mode == "popup", emit=False)

    def setEngineInfo(self, engine_name: str):
        self._engine_info = engine_name
        self.engine_label.setText(engine_name)

    def showAt(self, global_pos: QPoint):
        self.adjustSize()
        x = global_pos.x() - self.width() // 2
        y = global_pos.y() - self.height() - 10

        from PySide6.QtWidgets import QApplication

        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            if x < geom.left():
                x = geom.left() + 5
            if x + self.width() > geom.right():
                x = geom.right() - self.width() - 5
            if y < geom.top():
                y = global_pos.y() + 30

        self.move(x, y)
        self.show()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)
