# popup_menu.py
# Styled popup menu for VoiceType floating ball
# Left-click menu with enable toggle, polish modes, and settings

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


class ToggleSwitch(QWidget):
    """iOS-style toggle switch."""

    toggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
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

    def setChecked(self, checked):
        if self._checked != checked:
            self._checked = checked
            self._animation.setStartValue(self._circle_pos)
            self._animation.setEndValue(23 if checked else 3)
            self._animation.start()
            self.toggled.emit(checked)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setChecked(not self._checked)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Background
        if self._checked:
            bg_color = QColor(76, 175, 80)  # Green
        else:
            bg_color = QColor(180, 180, 180)  # Gray

        painter.setPen(Qt.NoPen)
        painter.setBrush(bg_color)
        painter.drawRoundedRect(0, 0, 44, 24, 12, 12)

        # Circle
        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(self._circle_pos, 3, 18, 18)


class ModeButton(QPushButton):
    """Styled mode selection button."""

    def __init__(self, text, icon_char="", parent=None):
        super().__init__(parent)
        self.setText(text)
        self._icon_char = icon_char
        self._selected = False
        self.setCheckable(True)
        self.setFixedHeight(36)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(self._get_style())

    def _get_style(self):
        return """
            ModeButton {
                background-color: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 8px;
                color: rgba(255, 255, 255, 0.8);
                font-size: 13px;
                padding: 4px 12px;
            }
            ModeButton:hover {
                background-color: rgba(255, 255, 255, 0.15);
                border-color: rgba(255, 255, 255, 0.25);
            }
            ModeButton:checked {
                background-color: rgba(100, 180, 255, 0.3);
                border-color: rgba(100, 180, 255, 0.6);
                color: white;
            }
        """


class PopupMenu(QWidget):
    """
    Styled popup menu for floating ball.

    Features:
    - Enable/disable toggle
    - Polish mode selection (High Quality / Fast / Custom)
    - Advanced settings button
    """

    # Signals
    enableToggled = Signal(bool)
    modeChanged = Signal(str)  # "quality" or "fast"
    settingsRequested = Signal()
    lockToggled = Signal(bool)  # Lock position toggle
    sleepToggled = Signal(bool)  # Sleep/wake toggle
    streamingToggled = Signal(bool)  # Streaming display toggle
    translateModeChanged = Signal(str)  # "popup" or "clipboard"
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._enabled = True
        self._current_mode = "fast"  # Default matches config
        self._is_locked = False
        self._is_sleeping = False
        self._init_window()
        self._init_ui()
        self._apply_shadow()

    def _init_window(self):
        """Setup window flags for popup."""
        self.setWindowFlags(
            Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(200)

    def _init_ui(self):
        """Build the UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Container with background
        self.container = QFrame()
        self.container.setStyleSheet(
            """
            QFrame {
                background-color: rgba(35, 35, 40, 0.95);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }
        """
        )
        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(12, 12, 12, 12)
        container_layout.setSpacing(12)

        # --- Enable Toggle Row ---
        enable_row = QHBoxLayout()
        enable_label = QLabel("VoiceType-Dev")
        enable_label.setStyleSheet(
            """
            QLabel {
                color: white;
                font-size: 14px;
                font-weight: bold;
            }
        """
        )
        self.toggle = ToggleSwitch()
        self.toggle.setChecked(True)
        self.toggle.toggled.connect(self._on_enable_toggled)

        enable_row.addWidget(enable_label)
        enable_row.addStretch()
        enable_row.addWidget(self.toggle)
        container_layout.addLayout(enable_row)

        # --- Separator ---
        separator = QFrame()
        separator.setFixedHeight(1)
        separator.setStyleSheet("background-color: rgba(255, 255, 255, 0.1);")
        container_layout.addWidget(separator)

        # --- Polish Mode Label ---
        mode_label = QLabel("润色模式")
        mode_label.setStyleSheet(
            """
            QLabel {
                color: rgba(255, 255, 255, 0.6);
                font-size: 11px;
            }
        """
        )
        container_layout.addWidget(mode_label)

        # --- Mode Buttons ---
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)

        modes = [
            ("quality", "高质量", "Gemini API, ~1.7s"),
            ("fast", "快速", "本地 Qwen, ~155ms"),
        ]

        for mode_id, mode_name, tooltip in modes:
            btn = ModeButton(mode_name)
            btn.setToolTip(tooltip)
            btn.setProperty("mode_id", mode_id)
            self.mode_group.addButton(btn)
            container_layout.addWidget(btn)

            if mode_id == self._current_mode:
                btn.setChecked(True)

        self.mode_group.buttonClicked.connect(self._on_mode_clicked)

        # --- Separator ---
        separator2 = QFrame()
        separator2.setFixedHeight(1)
        separator2.setStyleSheet("background-color: rgba(255, 255, 255, 0.1);")
        container_layout.addWidget(separator2)

        # --- Translation Output Mode Section ---
        translate_mode_label = QLabel("翻译输出模式")
        translate_mode_label.setStyleSheet(
            """
            QLabel {
                color: rgba(255, 255, 255, 0.6);
                font-size: 11px;
            }
        """
        )
        container_layout.addWidget(translate_mode_label)

        # Translation mode buttons row
        translate_mode_row = QHBoxLayout()
        translate_mode_row.setSpacing(8)

        self.translate_mode_group = QButtonGroup(self)
        self.translate_mode_group.setExclusive(True)

        mode_btn_style = """
            QPushButton {
                background-color: rgba(60, 60, 70, 0.8);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 6px;
                color: white;
                font-size: 12px;
                padding: 6px 12px;
            }
            QPushButton:hover {
                background-color: rgba(80, 80, 95, 0.9);
                border: 1px solid rgba(255, 255, 255, 0.2);
            }
            QPushButton:checked {
                background-color: rgba(76, 175, 80, 0.8);
                border: 1px solid rgba(76, 175, 80, 0.5);
            }
        """

        self.translate_popup_btn = QPushButton("弹窗")
        self.translate_popup_btn.setCursor(Qt.PointingHandCursor)
        self.translate_popup_btn.setCheckable(True)
        self.translate_popup_btn.setStyleSheet(mode_btn_style)
        self.translate_popup_btn.setProperty("mode_id", "popup")

        self.translate_clipboard_btn = QPushButton("剪贴板")
        self.translate_clipboard_btn.setCursor(Qt.PointingHandCursor)
        self.translate_clipboard_btn.setCheckable(True)
        self.translate_clipboard_btn.setStyleSheet(mode_btn_style)
        self.translate_clipboard_btn.setProperty("mode_id", "clipboard")

        self.translate_mode_group.addButton(self.translate_popup_btn)
        self.translate_mode_group.addButton(self.translate_clipboard_btn)

        # Default to popup mode
        self.translate_popup_btn.setChecked(True)

        self.translate_mode_group.buttonClicked.connect(self._on_translate_mode_clicked)

        translate_mode_row.addWidget(self.translate_popup_btn)
        translate_mode_row.addWidget(self.translate_clipboard_btn)
        container_layout.addLayout(translate_mode_row)

        # --- Separator ---
        separator3 = QFrame()
        separator3.setFixedHeight(1)
        separator3.setStyleSheet("background-color: rgba(255, 255, 255, 0.1);")
        container_layout.addWidget(separator3)

        # --- Settings Button ---
        self.settings_btn = QPushButton("高级设置")
        self.settings_btn.setCursor(Qt.PointingHandCursor)
        self.settings_btn.setStyleSheet(
            """
            QPushButton {
                background-color: transparent;
                border: none;
                color: rgba(100, 180, 255, 0.9);
                font-size: 13px;
                padding: 6px;
                text-align: center;
            }
            QPushButton:hover {
                color: rgba(130, 200, 255, 1.0);
                text-decoration: underline;
            }
        """
        )
        self.settings_btn.clicked.connect(self._on_settings_clicked)
        container_layout.addWidget(self.settings_btn)

        # --- Separator ---
        separator3 = QFrame()
        separator3.setFixedHeight(1)
        separator3.setStyleSheet("background-color: rgba(255, 255, 255, 0.1);")
        container_layout.addWidget(separator3)

        # --- Lock Position Row ---
        lock_row = QHBoxLayout()
        lock_label = QLabel("🔒 锁定位置")
        lock_label.setStyleSheet(
            """
            QLabel {
                color: rgba(255, 255, 255, 0.8);
                font-size: 13px;
            }
        """
        )
        self.lock_toggle = ToggleSwitch()
        self.lock_toggle.setChecked(False)
        self.lock_toggle.toggled.connect(self._on_lock_toggled)

        lock_row.addWidget(lock_label)
        lock_row.addStretch()
        lock_row.addWidget(self.lock_toggle)
        container_layout.addLayout(lock_row)

        # --- Sleep Toggle Row ---
        sleep_row = QHBoxLayout()
        sleep_label = QLabel("😴 休眠模式")
        sleep_label.setStyleSheet(
            """
            QLabel {
                color: rgba(255, 255, 255, 0.8);
                font-size: 13px;
            }
        """
        )
        self.sleep_toggle = ToggleSwitch()
        self.sleep_toggle.setChecked(False)
        self.sleep_toggle.toggled.connect(self._on_sleep_toggled)

        sleep_row.addWidget(sleep_label)
        sleep_row.addStretch()
        sleep_row.addWidget(self.sleep_toggle)
        container_layout.addLayout(sleep_row)

        # --- Streaming Display Toggle Row ---
        streaming_row = QHBoxLayout()
        streaming_label = QLabel("💬 实时字幕")
        streaming_label.setStyleSheet(
            """
            QLabel {
                color: rgba(255, 255, 255, 0.8);
                font-size: 13px;
            }
        """
        )
        self.streaming_toggle = ToggleSwitch()
        self.streaming_toggle.setChecked(True)  # Default on
        self.streaming_toggle.toggled.connect(self._on_streaming_toggled)

        streaming_row.addWidget(streaming_label)
        streaming_row.addStretch()
        streaming_row.addWidget(self.streaming_toggle)
        container_layout.addLayout(streaming_row)

        layout.addWidget(self.container)

    def _apply_shadow(self):
        """Apply drop shadow effect."""
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 100))
        shadow.setOffset(0, 4)
        self.container.setGraphicsEffect(shadow)

    def _on_enable_toggled(self, enabled):
        """Handle enable toggle."""
        self._enabled = enabled
        self.enableToggled.emit(enabled)

    def _on_mode_clicked(self, button):
        """Handle mode button click."""
        mode_id = button.property("mode_id")
        if mode_id and mode_id != self._current_mode:
            self._current_mode = mode_id
            self.modeChanged.emit(mode_id)

    def _on_settings_clicked(self):
        """Handle settings button click."""
        self.close()
        self.settingsRequested.emit()

    def _on_translate_mode_clicked(self, button):
        """Handle translate output mode button click."""
        mode_id = button.property("mode_id")
        if mode_id:
            self.translateModeChanged.emit(mode_id)

    def _on_lock_toggled(self, locked):
        """Handle lock toggle."""
        self._is_locked = locked
        self.lockToggled.emit(locked)

    def _on_sleep_toggled(self, sleeping):
        """Handle sleep toggle."""
        self._is_sleeping = sleeping
        self.sleepToggled.emit(sleeping)

    def _on_streaming_toggled(self, enabled):
        """Handle streaming display toggle."""
        self.streamingToggled.emit(enabled)

    def setEnabled(self, enabled):
        """Set the enable state."""
        self._enabled = enabled
        self.toggle.setChecked(enabled)

    def setMode(self, mode):
        """Set the current mode."""
        self._current_mode = mode
        for btn in self.mode_group.buttons():
            if btn.property("mode_id") == mode:
                btn.setChecked(True)
                break

    def setLocked(self, locked):
        """Set the lock state."""
        self._is_locked = locked
        self.lock_toggle.setChecked(locked)

    def setSleeping(self, sleeping):
        """Set the sleeping state."""
        self._is_sleeping = sleeping
        self.sleep_toggle.setChecked(sleeping)

    # Alias for compatibility with floating_ball.py
    def set_sleeping_state(self, is_sleeping):
        """Alias for setSleeping - compatibility with snake_case naming."""
        self.setSleeping(is_sleeping)

    def setStreaming(self, enabled):
        """Set the streaming display state."""
        self.streaming_toggle.setChecked(enabled)

    def setTranslateMode(self, mode):
        """Set the translate output mode."""
        if mode == "clipboard":
            self.translate_clipboard_btn.setChecked(True)
        else:
            self.translate_popup_btn.setChecked(True)

    def showAt(self, global_pos: QPoint):
        """Show popup at specified position."""
        # Adjust position to be above and to the left of the ball
        self.adjustSize()
        x = global_pos.x() - self.width() // 2
        y = global_pos.y() - self.height() - 10

        # Ensure on screen
        from PySide6.QtWidgets import QApplication

        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            if x < geom.left():
                x = geom.left() + 5
            if x + self.width() > geom.right():
                x = geom.right() - self.width() - 5
            if y < geom.top():
                # Show below instead
                y = global_pos.y() + 30

        self.move(x, y)
        self.show()

    def closeEvent(self, event):
        """Handle close."""
        self.closed.emit()
        super().closeEvent(event)
