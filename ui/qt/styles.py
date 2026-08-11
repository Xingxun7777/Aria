# styles.py
# Unified theme tokens and QSS helpers for Aria

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication


@dataclass(frozen=True)
class ThemePalette:
    name: str
    window_bg: str
    panel_bg: str
    panel_alt_bg: str
    sidebar_bg: str
    sidebar_hover_bg: str
    input_bg: str
    input_focus_bg: str
    button_bg: str
    button_hover_bg: str
    border: str
    border_strong: str
    text_primary: str
    text_secondary: str
    text_muted: str
    text_inverse: str
    accent: str
    accent_hover: str
    accent_soft: str
    accent_border: str
    danger: str
    danger_soft: str
    danger_border: str
    success: str
    separator: str
    scrollbar_track: str
    scrollbar_handle: str
    popup_shadow: str
    quote_bg: str
    quote_border: str
    assistant_bubble_bg: str
    assistant_bubble_text: str
    user_bubble_bg: str
    user_bubble_text: str
    header_bg: str
    code_bg: str


DARK_THEME = ThemePalette(
    name="dark",
    window_bg="#161618",
    panel_bg="rgba(35, 35, 40, 0.95)",
    panel_alt_bg="rgba(22, 22, 24, 0.98)",
    sidebar_bg="rgba(0, 0, 0, 0.20)",
    sidebar_hover_bg="rgba(255, 255, 255, 0.08)",
    input_bg="rgba(0, 0, 0, 0.32)",
    input_focus_bg="rgba(0, 0, 0, 0.48)",
    button_bg="rgba(255, 255, 255, 0.08)",
    button_hover_bg="rgba(255, 255, 255, 0.16)",
    border="rgba(255, 255, 255, 0.20)",
    border_strong="rgba(255, 255, 255, 0.28)",
    text_primary="#E5E7EB",
    text_secondary="#9CA3AF",
    text_muted="#6B7280",
    text_inverse="#FFFFFF",
    accent="#FF8C00",
    accent_hover="#FFAA33",
    accent_soft="rgba(255, 140, 0, 0.20)",
    accent_border="rgba(255, 140, 0, 0.45)",
    danger="#EF4444",
    danger_soft="rgba(239, 68, 68, 0.16)",
    danger_border="rgba(239, 68, 68, 0.45)",
    success="#22C55E",
    separator="rgba(255, 255, 255, 0.10)",
    scrollbar_track="rgba(255, 255, 255, 0.05)",
    scrollbar_handle="rgba(255, 255, 255, 0.24)",
    popup_shadow="rgba(0, 0, 0, 0.35)",
    quote_bg="#1F2937",
    quote_border="#3B82F6",
    assistant_bubble_bg="#374151",
    assistant_bubble_text="#E5E7EB",
    user_bubble_bg="#FF8C00",
    user_bubble_text="#FFFFFF",
    header_bg="#232328",
    code_bg="#1F2937",
)

LIGHT_THEME = ThemePalette(
    name="light",
    window_bg="#F8FAFC",
    panel_bg="rgba(255, 255, 255, 0.96)",
    panel_alt_bg="rgba(248, 250, 252, 0.98)",
    sidebar_bg="rgba(15, 23, 42, 0.04)",
    sidebar_hover_bg="rgba(15, 23, 42, 0.06)",
    input_bg="#FFFFFF",
    input_focus_bg="#FFFFFF",
    button_bg="rgba(15, 23, 42, 0.04)",
    button_hover_bg="rgba(15, 23, 42, 0.08)",
    border="rgba(15, 23, 42, 0.12)",
    border_strong="rgba(15, 23, 42, 0.18)",
    text_primary="#111827",
    text_secondary="#4B5563",
    text_muted="#6B7280",
    text_inverse="#FFFFFF",
    accent="#FF8C00",
    accent_hover="#FFAA33",
    accent_soft="rgba(255, 140, 0, 0.12)",
    accent_border="rgba(255, 140, 0, 0.28)",
    danger="#DC2626",
    danger_soft="rgba(220, 38, 38, 0.10)",
    danger_border="rgba(220, 38, 38, 0.24)",
    success="#16A34A",
    separator="rgba(15, 23, 42, 0.08)",
    scrollbar_track="rgba(15, 23, 42, 0.04)",
    scrollbar_handle="rgba(15, 23, 42, 0.20)",
    popup_shadow="rgba(15, 23, 42, 0.16)",
    quote_bg="#EFF6FF",
    quote_border="#60A5FA",
    assistant_bubble_bg="#F3F4F6",
    assistant_bubble_text="#111827",
    user_bubble_bg="#FF8C00",
    user_bubble_text="#FFFFFF",
    header_bg="#F3F4F6",
    code_bg="#E5E7EB",
)

THEME = DARK_THEME


def get_theme_name() -> str:
    """Return the effective system theme name for the current Qt app."""
    app = QGuiApplication.instance()
    if app is None:
        return "dark"

    style_hints = app.styleHints()
    if not hasattr(style_hints, "colorScheme"):
        return "dark"

    try:
        scheme = style_hints.colorScheme()
    except Exception:
        return "dark"

    if scheme == Qt.ColorScheme.Light:
        return "light"
    return "dark"


def get_theme_palette(theme_name: str | None = None) -> ThemePalette:
    """Resolve a palette by explicit name or by current system theme."""
    theme_name = (theme_name or get_theme_name()).lower()
    return LIGHT_THEME if theme_name == "light" else DARK_THEME


def qcolor(value: str) -> QColor:
    """Build a QColor from a CSS-style color string."""
    return QColor(value)


def get_settings_stylesheet(theme_name: str | None = None) -> str:
    p = get_theme_palette(theme_name)
    return f"""
QMainWindow {{
    background-color: {p.window_bg};
    border: 1px solid {p.border};
    border-radius: 12px;
}}

/* Scroll areas (transparent viewport so dark window shows through) */
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollArea > QWidget#qt_scrollarea_viewport {{
    background: transparent;
}}

/* Group cards */
QGroupBox {{
    color: {p.text_primary};
    border: 1px solid {p.border};
    border-radius: 12px;
    margin-top: 22px;
    padding: 20px 18px 18px 18px;
    background-color: {p.panel_bg};
    font-size: 14px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 2px;
    padding: 0 2px 8px 2px;
    color: {p.text_primary};
    font-size: 14px;
    font-weight: 600;
}}

/* Sidebar */
QListWidget {{
    background-color: {p.sidebar_bg};
    border: none;
    border-right: 1px solid {p.border};
    outline: none;
    padding-top: 20px;
}}
QListWidget::item {{
    height: 48px;
    padding-left: 16px;
    color: {p.text_secondary};
    border-radius: 8px;
    margin: 4px 8px;
}}
QListWidget::item:selected {{
    background-color: {p.accent_soft};
    color: {p.text_primary};
    border: 1px solid {p.accent_border};
}}
QListWidget::item:hover {{
    background-color: {p.sidebar_hover_bg};
}}

/* Content area */
QWidget#contentArea {{
    background-color: transparent;
}}

QLabel {{
    color: {p.text_primary};
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
}}

/* Input fields */
QLineEdit, QTextEdit, QPlainTextEdit, QKeySequenceEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {p.input_bg};
    border: 1px solid {p.border};
    border-radius: 6px;
    color: {p.text_primary};
    padding: 8px;
    selection-background-color: {p.accent};
    selection-color: {p.text_inverse};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QKeySequenceEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {p.accent};
    background-color: {p.input_focus_bg};
}}

/* Buttons */
QPushButton {{
    background-color: {p.button_bg};
    border: 1px solid {p.border_strong};
    border-radius: 8px;
    color: {p.text_primary};
    padding: 8px 16px;
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: {p.button_hover_bg};
    border-color: {p.accent_border};
}}
QPushButton:pressed {{
    background-color: {p.panel_alt_bg};
}}
QPushButton:disabled {{
    color: {p.text_muted};
    border-color: {p.border};
    background-color: {p.panel_alt_bg};
}}
QPushButton#primaryBtn {{
    background-color: {p.accent};
    border: 1px solid {p.accent};
    color: {p.window_bg};
    font-weight: 600;
    padding: 10px 16px;
}}
QPushButton#primaryBtn:hover {{
    background-color: {p.accent_hover};
    border-color: {p.accent_hover};
}}
QPushButton#dangerBtn {{
    background-color: {p.danger_soft};
    border: 1px solid {p.danger_border};
    color: {p.danger};
}}
QPushButton#dangerBtn:hover {{
    background-color: {p.danger_soft};
    border-color: {p.danger};
}}

/* Tabs (segmented pills) */
QTabWidget::pane {{
    border: 1px solid {p.border};
    border-radius: 10px;
    background-color: {p.panel_alt_bg};
    top: -1px;
}}
QTabBar::tab {{
    background: {p.button_bg};
    color: {p.text_secondary};
    padding: 8px 18px;
    margin-right: 6px;
    border: 1px solid {p.border};
    border-radius: 8px;
}}
QTabBar::tab:selected {{
    background: {p.accent_soft};
    color: {p.text_primary};
    border: 1px solid {p.accent_border};
}}
QTabBar::tab:hover {{
    color: {p.text_primary};
}}

/* ComboBox */
QComboBox {{
    background-color: {p.input_bg};
    border: 1px solid {p.border};
    border-radius: 6px;
    color: {p.text_primary};
    min-height: 28px;
    padding: 4px 28px 4px 10px;
    selection-background-color: {p.accent};
    selection-color: {p.text_inverse};
}}
QComboBox:hover {{
    border: 1px solid {p.accent};
}}
QComboBox:focus, QComboBox:on {{
    border: 1px solid {p.accent};
    background-color: {p.input_focus_bg};
    color: {p.text_primary};
}}
QComboBox:disabled {{
    color: {p.text_muted};
    background-color: {p.panel_alt_bg};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background-color: {p.panel_bg};
    border: 1px solid {p.border};
    color: {p.text_primary};
    selection-background-color: {p.accent};
    selection-color: {p.text_inverse};
    outline: none;
}}
QComboBox QAbstractItemView::item {{
    min-height: 28px;
    padding: 6px 10px;
}}
QComboBox QAbstractItemView::item:selected {{
    background-color: {p.accent};
    color: {p.text_inverse};
}}

/* RadioButton */
QRadioButton {{
    color: {p.text_primary};
    spacing: 8px;
}}
QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border: 2px solid {p.border_strong};
    border-radius: 9px;
    background-color: transparent;
}}
QRadioButton::indicator:checked {{
    border: 2px solid {p.accent};
    background-color: {p.accent};
}}

/* CheckBox */
QCheckBox {{
    color: {p.text_primary};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 2px solid {p.border_strong};
    border-radius: 4px;
    background-color: transparent;
}}
QCheckBox::indicator:checked {{
    border: 2px solid {p.accent};
    background-color: {p.accent};
}}

/* TableWidget */
QTableWidget {{
    background-color: {p.panel_bg};
    border: 1px solid {p.border};
    border-radius: 6px;
    gridline-color: {p.border};
    color: {p.text_primary};
}}
QTableWidget::item {{
    padding: 8px;
}}
QTableWidget::item:selected {{
    background-color: {p.accent_soft};
    color: {p.text_primary};
}}
QTableWidget QLineEdit#tableCellEditor {{
    background-color: {p.input_focus_bg};
    border: 1px solid {p.accent};
    border-radius: 5px;
    color: {p.text_primary};
    min-height: 30px;
    padding: 4px 8px;
    selection-background-color: {p.accent};
    selection-color: {p.text_inverse};
}}
QHeaderView::section {{
    background-color: {p.panel_alt_bg};
    color: {p.text_secondary};
    padding: 8px;
    border: none;
    border-bottom: 1px solid {p.border};
}}

/* ScrollBar */
QScrollBar:vertical {{
    border: none;
    background: transparent;
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {p.scrollbar_handle};
    min-height: 20px;
    border-radius: 4px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

/* Tooltip */
QToolTip {{
    background-color: {p.panel_bg};
    color: {p.text_primary};
    border: 1px solid {p.border_strong};
    border-radius: 6px;
    padding: 6px 8px;
}}
"""


def get_overlay_stylesheet(theme_name: str | None = None) -> str:
    p = get_theme_palette(theme_name)
    return f"""
QWidget#capsule {{
    background-color: {p.panel_bg};
    border: 1px solid {p.border};
    border-radius: 20px;
}}
QLabel#statusIcon {{
    font-size: 16px;
}}
QLabel#transcript {{
    color: {p.text_primary};
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 16px;
}}
"""


STYLESHEET_SETTINGS = get_settings_stylesheet("dark")
STYLESHEET_OVERLAY = get_overlay_stylesheet("dark")
