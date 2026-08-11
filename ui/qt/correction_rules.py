"""User-visible manager for local explicit ASR correction rules."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import styles


class CorrectionRulesWindow(QWidget):
    """Local rule table with explicit add and disable intents.

    Persistence stays in the backend.  This window owns no file handles and
    never logs rule operands.
    """

    addRequested = Signal(str, str)
    clearRequested = Signal(str)  # stable rule_id, never row index
    refreshRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme = styles.DARK_THEME
        self._init_window()
        self._init_ui()

    def _init_window(self) -> None:
        self.setWindowTitle("Aria 纠正规则")
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.resize(680, 430)
        self.setMinimumSize(560, 360)

    def _init_ui(self) -> None:
        self.setStyleSheet(
            f"background-color: {self._theme.window_bg}; color: {self._theme.text_primary};"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        title = QLabel("显式纠正规则")
        title.setStyleSheet(
            f"color: {self._theme.text_primary}; font-size: 18px; font-weight: 700;"
        )
        root.addWidget(title)

        hint = QLabel(
            "只影响之后的语音识别，不修改当前文档。规则仅保存在本机，不采集窗口标题或上下文。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"color: {self._theme.text_secondary}; font-size: 12px;"
        )
        root.addWidget(hint)

        self.status_frame = QFrame()
        self.status_frame.setObjectName("correctionStatus")
        status_layout = QHBoxLayout(self.status_frame)
        status_layout.setContentsMargins(10, 7, 10, 7)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.status_label)
        self.status_frame.hide()
        root.addWidget(self.status_frame)

        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("识别成了…")
        self.source_edit.setMaxLength(128)
        self.replacement_edit = QLineEdit()
        self.replacement_edit.setPlaceholderText("应该是…")
        self.replacement_edit.setMaxLength(256)
        self.add_btn = self._make_button("添加规则", primary=True)
        self.add_btn.clicked.connect(self._on_add)
        add_row.addWidget(self.source_edit, 2)
        arrow = QLabel("→")
        arrow.setStyleSheet(
            f"color: {self._theme.text_secondary}; font-size: 16px;"
        )
        add_row.addWidget(arrow)
        add_row.addWidget(self.replacement_edit, 2)
        add_row.addWidget(self.add_btn)
        root.addLayout(add_row)

        edit_style = f"""
            QLineEdit {{
                background-color: {self._theme.panel_bg};
                color: {self._theme.text_primary};
                border: 1px solid {self._theme.border};
                border-radius: 6px;
                padding: 7px 9px;
                selection-background-color: {self._theme.accent_soft};
            }}
            QLineEdit:focus {{ border-color: {self._theme.accent_border}; }}
        """
        self.source_edit.setStyleSheet(edit_style)
        self.replacement_edit.setStyleSheet(edit_style)
        self.replacement_edit.returnPressed.connect(self._on_add)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["识别原词", "替换为", "添加时间"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        self.table.setStyleSheet(
            f"""
            QTableWidget {{
                background-color: {self._theme.panel_bg};
                alternate-background-color: {self._theme.button_bg};
                color: {self._theme.text_primary};
                border: 1px solid {self._theme.border};
                border-radius: 8px;
                gridline-color: {self._theme.separator};
            }}
            QTableWidget::item {{ padding: 6px; }}
            QTableWidget::item:selected {{
                background-color: {self._theme.accent_soft};
                color: {self._theme.text_primary};
            }}
            QHeaderView::section {{
                background-color: {self._theme.button_bg};
                color: {self._theme.text_secondary};
                border: none;
                border-bottom: 1px solid {self._theme.border};
                padding: 7px;
            }}
            """
        )
        root.addWidget(self.table, 1)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.count_label = QLabel("0 条生效规则")
        self.count_label.setStyleSheet(
            f"color: {self._theme.text_muted}; font-size: 12px;"
        )
        action_row.addWidget(self.count_label)
        action_row.addStretch(1)
        self.refresh_btn = self._make_button("刷新")
        self.refresh_btn.clicked.connect(self.refreshRequested.emit)
        action_row.addWidget(self.refresh_btn)
        self.clear_btn = self._make_button("停用选中", danger=True)
        self.clear_btn.clicked.connect(self._on_clear)
        action_row.addWidget(self.clear_btn)
        self.close_btn = self._make_button("关闭")
        self.close_btn.clicked.connect(self.hide)
        action_row.addWidget(self.close_btn)
        root.addLayout(action_row)

    def _make_button(
        self, text: str, *, primary: bool = False, danger: bool = False
    ) -> QPushButton:
        button = QPushButton(text)
        button.setCursor(Qt.PointingHandCursor)
        button.setFixedHeight(32)
        if primary:
            background = self._theme.accent
            hover = self._theme.accent_hover
            color = "#111111"
            border = self._theme.accent_border
        elif danger:
            background = self._theme.danger_soft
            hover = self._theme.button_hover_bg
            color = self._theme.danger
            border = self._theme.danger
        else:
            background = self._theme.button_bg
            hover = self._theme.button_hover_bg
            color = self._theme.text_secondary
            border = self._theme.border
        button.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {background}; color: {color};
                border: 1px solid {border}; border-radius: 6px;
                padding: 0 12px; font-weight: 600;
            }}
            QPushButton:hover {{ background-color: {hover}; }}
            """
        )
        return button

    def set_rules(self, rules: list[dict]) -> None:
        selected_id = self.selected_rule_id()
        self.table.setRowCount(0)
        for row_data in rules:
            row = self.table.rowCount()
            self.table.insertRow(row)
            source_item = QTableWidgetItem(str(row_data.get("source") or ""))
            source_item.setData(Qt.UserRole, str(row_data.get("rule_id") or ""))
            replacement_item = QTableWidgetItem(
                str(row_data.get("replacement") or "")
            )
            created_item = QTableWidgetItem(str(row_data.get("created_at") or ""))
            self.table.setItem(row, 0, source_item)
            self.table.setItem(row, 1, replacement_item)
            self.table.setItem(row, 2, created_item)
            if selected_id and source_item.data(Qt.UserRole) == selected_id:
                self.table.selectRow(row)
        self.count_label.setText(f"{len(rules)} 条生效规则")
        self.clear_btn.setEnabled(bool(rules))

    def selected_rule_id(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return str(item.data(Qt.UserRole) or "") if item else ""

    def set_status(self, message: str, *, level: str = "info") -> None:
        palette = {
            "success": (self._theme.success, self._theme.accent_soft),
            "warning": (self._theme.accent_hover, self._theme.accent_soft),
            "error": (self._theme.danger, self._theme.danger_soft),
            "info": (self._theme.text_secondary, self._theme.button_bg),
        }
        color, background = palette.get(level, palette["info"])
        self.status_label.setText(str(message or ""))
        self.status_label.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.status_frame.setStyleSheet(
            "QFrame#correctionStatus {"
            f"background: {background}; border: 1px solid {color}; "
            "border-radius: 6px;}"
        )
        self.status_frame.setVisible(bool(message))

    def show_for_management(self) -> None:
        self.refreshRequested.emit()
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_add(self) -> None:
        source = self.source_edit.text()
        replacement = self.replacement_edit.text()
        if not source or not replacement:
            self.set_status("请同时填写识别原词和正确写法。", level="info")
            return
        self.addRequested.emit(source, replacement)

    def _on_clear(self) -> None:
        rule_id = self.selected_rule_id()
        if not rule_id:
            self.set_status("请先选择一条需要停用的规则。", level="info")
            return
        self.clearRequested.emit(rule_id)


__all__ = ["CorrectionRulesWindow"]
