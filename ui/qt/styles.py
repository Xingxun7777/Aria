# styles.py
# QSS dark glass theme for VoiceType

THEME = {
    "bg_dark": "#161618",
    "bg_glass": "rgba(22, 22, 24, 240)",
    "text_white": "#FFFFFF",
    "text_gray": "#9CA3AF",
    "accent_blue": "#2563EB",
    "accent_red": "#EF4444",
    "border": "#33FFFFFF"
}

STYLESHEET_SETTINGS = """
QMainWindow {
    background-color: #161618;
    border: 1px solid #33FFFFFF;
    border-radius: 12px;
}

/* Sidebar */
QListWidget {
    background-color: rgba(0, 0, 0, 50);
    border: none;
    border-right: 1px solid #33FFFFFF;
    outline: none;
    padding-top: 20px;
}
QListWidget::item {
    height: 48px;
    padding-left: 16px;
    color: #9CA3AF;
    border-radius: 8px;
    margin: 4px 8px;
}
QListWidget::item:selected {
    background-color: rgba(37, 99, 235, 50);
    color: #FFFFFF;
    border: 1px solid rgba(37, 99, 235, 80);
}
QListWidget::item:hover {
    background-color: rgba(255, 255, 255, 20);
}

/* Content area */
QWidget#contentArea {
    background-color: transparent;
}

QLabel {
    color: #E5E7EB;
    font-family: 'Microsoft YaHei';
}

/* Input fields */
QLineEdit, QTextEdit, QPlainTextEdit {
    background-color: rgba(0, 0, 0, 80);
    border: 1px solid #33FFFFFF;
    border-radius: 6px;
    color: #FFFFFF;
    padding: 8px;
    selection-background-color: #2563EB;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border: 1px solid #2563EB;
    background-color: rgba(0, 0, 0, 120);
}

/* Buttons */
QPushButton {
    background-color: rgba(255, 255, 255, 20);
    border: 1px solid #33FFFFFF;
    border-radius: 6px;
    color: #E5E7EB;
    padding: 6px 12px;
}
QPushButton:hover {
    background-color: rgba(255, 255, 255, 40);
    color: #FFFFFF;
}
QPushButton#primaryBtn {
    background-color: #2563EB;
    border: 1px solid #3B82F6;
}
QPushButton#primaryBtn:hover {
    background-color: #1D4ED8;
}
QPushButton#dangerBtn {
    background-color: rgba(239, 68, 68, 40);
    border: 1px solid #EF4444;
    color: #EF4444;
}
QPushButton#dangerBtn:hover {
    background-color: rgba(239, 68, 68, 80);
}

/* ComboBox */
QComboBox {
    background-color: rgba(0, 0, 0, 80);
    border: 1px solid #33FFFFFF;
    border-radius: 6px;
    color: #FFFFFF;
    padding: 6px 12px;
}
QComboBox:hover {
    border: 1px solid #2563EB;
}
QComboBox::drop-down {
    border: none;
}
QComboBox QAbstractItemView {
    background-color: #161618;
    border: 1px solid #33FFFFFF;
    color: #FFFFFF;
    selection-background-color: #2563EB;
}

/* RadioButton */
QRadioButton {
    color: #E5E7EB;
    spacing: 8px;
}
QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border: 2px solid #33FFFFFF;
    border-radius: 9px;
    background-color: transparent;
}
QRadioButton::indicator:checked {
    border: 2px solid #2563EB;
    background-color: #2563EB;
}

/* CheckBox */
QCheckBox {
    color: #E5E7EB;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 2px solid #33FFFFFF;
    border-radius: 4px;
    background-color: transparent;
}
QCheckBox::indicator:checked {
    border: 2px solid #2563EB;
    background-color: #2563EB;
}

/* TableWidget */
QTableWidget {
    background-color: rgba(0, 0, 0, 50);
    border: 1px solid #33FFFFFF;
    border-radius: 6px;
    gridline-color: #33FFFFFF;
    color: #FFFFFF;
}
QTableWidget::item {
    padding: 8px;
}
QTableWidget::item:selected {
    background-color: rgba(37, 99, 235, 50);
}
QHeaderView::section {
    background-color: rgba(0, 0, 0, 80);
    color: #9CA3AF;
    padding: 8px;
    border: none;
    border-bottom: 1px solid #33FFFFFF;
}

/* ScrollBar */
QScrollBar:vertical {
    border: none;
    background: transparent;
    width: 8px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: rgba(255, 255, 255, 40);
    min-height: 20px;
    border-radius: 4px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
"""

STYLESHEET_OVERLAY = """
QWidget#capsule {
    background-color: rgba(10, 10, 10, 220);
    border: 1px solid rgba(255, 255, 255, 40);
    border-radius: 20px;
}
QLabel#statusIcon {
    font-size: 16px;
}
QLabel#transcript {
    color: white;
    font-family: 'Microsoft YaHei';
    font-size: 16px;
}
"""
