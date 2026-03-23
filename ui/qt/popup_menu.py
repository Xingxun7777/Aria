# popup_menu.py
# Styled popup menu for Aria floating ball
# Left-click menu with enable toggle, polish modes, and settings

from dataclasses import replace

from PySide6.QtCore import (
    Qt,
    Signal,
    QTimer,
    QPropertyAnimation,
    QEasingCurve,
    Property,
    QPoint,
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
)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QFont

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
                padding: 4px 10px;
            }}
            ModeButton:hover {{
                background-color: {self._theme.button_hover_bg};
                border-color: {self._theme.border_strong};
            }}
            ModeButton:checked {{
                background-color: {self._theme.accent_soft};
                border-color: {self._theme.accent_border};
                color: {self._theme.text_inverse};
            }}
        """


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
    settingsRequested = Signal()
    historyRequested = Signal()
    lockToggled = Signal(bool)
    sleepToggled = Signal(bool)
    streamingToggled = Signal(bool)
    translateModeChanged = Signal(str)  # "popup" or "clipboard"
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme = POPUP_MENU_THEME
        self._enabled = True
        self._current_mode = "quality"
        self._is_locked = False
        self._is_sleeping = False
        self._engine_info = "FunASR"
        self._translate_mode = "popup"
        self._init_window()
        self._init_ui()
        self._apply_shadow()

    def _init_window(self):
        """Setup window flags for popup."""
        self.setWindowFlags(
            Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(210)

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
        cl.addSpacing(6)

        # ── Quick Actions ──
        action_row = QHBoxLayout()
        action_row.setSpacing(8)

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
        cl.addSpacing(4)
        self._add_toggle_row(cl, "实时字幕", self._on_streaming_toggled, "streaming")
        cl.addSpacing(4)
        self._add_toggle_row(cl, "锁定位置", self._on_lock_toggled, "lock")
        cl.addSpacing(4)
        self._add_toggle_row(cl, "休眠模式", self._on_sleep_toggled, "sleep")

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

    def _on_settings_clicked(self):
        self.close()
        self.settingsRequested.emit()

    def _on_history_clicked(self):
        self.close()
        self.historyRequested.emit()

    def _on_translate_toggled(self, checked):
        mode = "popup" if checked else "clipboard"
        self._translate_mode = mode
        self.translateModeChanged.emit(mode)

    def _on_lock_toggled(self, locked):
        self._is_locked = locked
        self.lockToggled.emit(locked)

    def _on_sleep_toggled(self, sleeping):
        self._is_sleeping = sleeping
        self.sleepToggled.emit(sleeping)

    def _on_streaming_toggled(self, enabled):
        self.streamingToggled.emit(enabled)

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

    def setLocked(self, locked):
        self._is_locked = locked
        self.lock_toggle.setChecked(locked, emit=False)

    def setSleeping(self, sleeping):
        self._is_sleeping = sleeping
        self.sleep_toggle.setChecked(sleeping, emit=False)

    # Alias for compatibility
    def set_sleeping_state(self, is_sleeping):
        self.setSleeping(is_sleeping)

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
