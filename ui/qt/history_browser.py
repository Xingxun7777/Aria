"""
History Browser Window
======================
Full-featured history browser for all Aria interactions.
Replaces the simple HistoryWindow popup with a proper window.
"""

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QFrame,
    QApplication,
    QComboBox,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QGraphicsDropShadowEffect,
    QFileDialog,
    QSplitter,
)
from PySide6.QtGui import QColor

from . import styles


def _blog(msg: str):
    """Browser debug logging (pythonw.exe safe)."""
    if sys.stdout is not None:
        print(f"[HISTORY_BROWSER] {msg}")


class HistoryRecordWidget(QFrame):
    """Single history record display widget."""

    copyClicked = Signal(str)  # text to copy
    deleteClicked = Signal(str, str)  # date, record_id

    def __init__(
        self,
        record_id: str,
        record_date: str,
        timestamp_str: str,
        type_label: str,
        type_color: str,
        input_text: str,
        output_text: str,
        parent=None,
    ):
        super().__init__(parent)
        self._theme = styles.get_theme_palette()
        self._record_id = record_id
        self._record_date = record_date
        self._output_text = output_text
        self._input_text = input_text

        self.setStyleSheet(
            f"""
            HistoryRecordWidget {{
                background-color: {self._theme.button_bg};
                border: 1px solid {self._theme.border};
                border-radius: 8px;
                padding: 4px;
            }}
            HistoryRecordWidget:hover {{
                border-color: {self._theme.accent_border};
            }}
        """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        # Header: timestamp + type badge + actions
        header = QHBoxLayout()

        time_label = QLabel(timestamp_str)
        time_label.setStyleSheet(
            f"""
            QLabel {{
                color: {self._theme.text_muted};
                font-size: 11px;
            }}
        """
        )
        header.addWidget(time_label)

        # Type badge
        badge = QLabel(type_label)
        badge.setStyleSheet(
            f"""
            QLabel {{
                color: {type_color};
                background: {type_color}22;
                border: 1px solid {type_color}44;
                border-radius: 4px;
                padding: 1px 6px;
                font-size: 10px;
                font-weight: bold;
            }}
        """
        )
        header.addWidget(badge)

        header.addStretch()

        # Copy button
        copy_btn = QPushButton("复制")
        copy_btn.setFixedHeight(22)
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.setStyleSheet(
            f"""
            QPushButton {{
                color: {self._theme.accent};
                background: transparent;
                border: 1px solid {self._theme.accent_border};
                border-radius: 4px;
                padding: 0 8px;
                font-size: 10px;
            }}
            QPushButton:hover {{
                background: {self._theme.accent_soft};
            }}
        """
        )
        copy_btn.clicked.connect(self._on_copy)
        header.addWidget(copy_btn)

        # Delete button
        del_btn = QPushButton("×")
        del_btn.setFixedSize(22, 22)
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.setStyleSheet(
            f"""
            QPushButton {{
                color: {self._theme.text_muted};
                background: transparent;
                border: none;
                font-size: 14px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                color: {self._theme.danger};
                background: {self._theme.danger_soft};
                border-radius: 4px;
            }}
        """
        )
        del_btn.clicked.connect(self._on_delete)
        header.addWidget(del_btn)

        layout.addLayout(header)

        # Input text (gray, smaller)
        if input_text:
            input_label = QLabel(input_text[:300])
            input_label.setWordWrap(True)
            input_label.setStyleSheet(
                f"""
                QLabel {{
                    color: {self._theme.text_muted};
                    font-size: 12px;
                    line-height: 1.3;
                }}
            """
            )
            layout.addWidget(input_label)

        # Output text (primary color, if different from input)
        if output_text and output_text != input_text:
            output_label = QLabel(output_text[:500])
            output_label.setWordWrap(True)
            output_label.setStyleSheet(
                f"""
                QLabel {{
                    color: {self._theme.text_primary};
                    font-size: 13px;
                    line-height: 1.4;
                }}
            """
            )
            layout.addWidget(output_label)

    def _on_copy(self):
        text = self._output_text if self._output_text else self._input_text
        self.copyClicked.emit(text)

    def _on_delete(self):
        self.deleteClicked.emit(self._record_date, self._record_id)


class HistoryBrowserWindow(QWidget):
    """
    Full-featured history browser window.

    Layout:
    - Left sidebar (150px): date list + type filter + search
    - Right panel: scrollable record list
    - Bottom toolbar: export / clear / stats
    """

    closed = Signal()

    def __init__(self, history_store=None, parent=None):
        super().__init__(parent)
        self._theme = styles.get_theme_palette()
        self._history_store = history_store
        self._current_date: Optional[str] = None
        self._current_type_filter: Optional[str] = None
        self._current_search: str = ""

        self._init_window()
        self._init_ui()

    def set_history_store(self, store):
        """Set the history store (can be set after construction)."""
        self._history_store = store

    def _init_window(self):
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Aria 历史记录")
        self.resize(700, 500)
        self.setMinimumSize(500, 350)

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Main container with background
        container = QFrame()
        container.setStyleSheet(
            f"""
            QFrame {{
                background-color: {self._theme.panel_bg};
                border: 1px solid {self._theme.border};
            }}
        """
        )
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        # Title bar
        title_bar = QFrame()
        title_bar.setFixedHeight(44)
        title_bar.setStyleSheet(
            f"""
            QFrame {{
                background-color: {self._theme.header_bg};
                border-bottom: 1px solid {self._theme.border};
            }}
        """
        )
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(16, 0, 16, 0)

        title = QLabel("历史记录")
        title.setStyleSheet(
            f"""
            QLabel {{
                color: {self._theme.text_primary};
                font-size: 15px;
                font-weight: bold;
                background: transparent;
                border: none;
            }}
        """
        )
        title_layout.addWidget(title)
        title_layout.addStretch()

        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet(
            f"""
            QLabel {{
                color: {self._theme.text_muted};
                font-size: 11px;
                background: transparent;
                border: none;
            }}
        """
        )
        title_layout.addWidget(self._stats_label)

        container_layout.addWidget(title_bar)

        # Content area: sidebar + records
        content = QSplitter(Qt.Horizontal)
        content.setStyleSheet(
            f"""
            QSplitter {{
                background: transparent;
                border: none;
            }}
            QSplitter::handle {{
                background: {self._theme.border};
                width: 1px;
            }}
        """
        )

        # === Left sidebar ===
        sidebar = QFrame()
        sidebar.setFixedWidth(160)
        sidebar.setStyleSheet(
            f"""
            QFrame {{
                background-color: {self._theme.sidebar_bg};
                border: none;
                border-right: 1px solid {self._theme.border};
            }}
        """
        )
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 8, 8, 8)
        sidebar_layout.setSpacing(6)

        # Search box
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("搜索...")
        self._search_edit.setStyleSheet(
            f"""
            QLineEdit {{
                background: {self._theme.input_bg};
                color: {self._theme.text_primary};
                border: 1px solid {self._theme.border};
                border-radius: 6px;
                padding: 4px 8px;
                font-size: 12px;
            }}
            QLineEdit:focus {{
                border-color: {self._theme.accent_border};
                background: {self._theme.input_focus_bg};
            }}
        """
        )
        self._search_edit.textChanged.connect(self._on_search_changed)
        sidebar_layout.addWidget(self._search_edit)

        # Type filter combo
        self._type_combo = QComboBox()
        self._type_combo.addItem("全部类型", None)
        from aria.core.history.models import RecordType, RECORD_TYPE_LABELS

        for rt in RecordType:
            self._type_combo.addItem(RECORD_TYPE_LABELS.get(rt, rt.name), rt.name)
        self._type_combo.setStyleSheet(
            f"""
            QComboBox {{
                background: {self._theme.input_bg};
                color: {self._theme.text_primary};
                border: 1px solid {self._theme.border};
                border-radius: 6px;
                padding: 4px 8px;
                font-size: 12px;
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            QComboBox QAbstractItemView {{
                background: {self._theme.panel_bg};
                color: {self._theme.text_primary};
                border: 1px solid {self._theme.border};
                selection-background-color: {self._theme.accent_soft};
            }}
        """
        )
        self._type_combo.currentIndexChanged.connect(self._on_type_filter_changed)
        sidebar_layout.addWidget(self._type_combo)

        # Date list label
        date_label = QLabel("日期")
        date_label.setStyleSheet(
            f"""
            QLabel {{
                color: {self._theme.text_muted};
                font-size: 11px;
                font-weight: bold;
                padding-top: 4px;
            }}
        """
        )
        sidebar_layout.addWidget(date_label)

        # Date list
        self._date_list = QListWidget()
        self._date_list.setStyleSheet(
            f"""
            QListWidget {{
                background: transparent;
                border: none;
                color: {self._theme.text_primary};
                font-size: 12px;
            }}
            QListWidget::item {{
                padding: 6px 8px;
                border-radius: 6px;
            }}
            QListWidget::item:hover {{
                background: {self._theme.sidebar_hover_bg};
            }}
            QListWidget::item:selected {{
                background: {self._theme.accent_soft};
                color: {self._theme.accent};
            }}
        """
        )
        self._date_list.currentItemChanged.connect(self._on_date_selected)
        sidebar_layout.addWidget(self._date_list)

        content.addWidget(sidebar)

        # === Right panel: records ===
        right_panel = QFrame()
        right_panel.setStyleSheet(
            f"""
            QFrame {{
                background: transparent;
                border: none;
            }}
        """
        )
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # Records scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            f"""
            QScrollArea {{
                border: none;
                background: transparent;
            }}
            QScrollBar:vertical {{
                background: {self._theme.scrollbar_track};
                width: 8px;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: {self._theme.scrollbar_handle};
                border-radius: 4px;
                min-height: 20px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {self._theme.border_strong};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
        """
        )

        self._records_widget = QWidget()
        self._records_widget.setStyleSheet("background: transparent;")
        self._records_layout = QVBoxLayout(self._records_widget)
        self._records_layout.setContentsMargins(12, 8, 12, 8)
        self._records_layout.setSpacing(6)
        self._records_layout.addStretch()

        scroll.setWidget(self._records_widget)
        right_layout.addWidget(scroll)

        # Empty state
        self._empty_label = QLabel("选择日期查看记录")
        self._empty_label.setStyleSheet(
            f"""
            QLabel {{
                color: {self._theme.text_muted};
                font-size: 13px;
                padding: 40px;
            }}
        """
        )
        self._empty_label.setAlignment(Qt.AlignCenter)
        right_layout.addWidget(self._empty_label)

        content.addWidget(right_panel)
        content.setStretchFactor(0, 0)
        content.setStretchFactor(1, 1)

        container_layout.addWidget(content)

        # === Bottom toolbar ===
        toolbar = QFrame()
        toolbar.setFixedHeight(40)
        toolbar.setStyleSheet(
            f"""
            QFrame {{
                background-color: {self._theme.header_bg};
                border-top: 1px solid {self._theme.border};
            }}
        """
        )
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 0, 12, 0)

        btn_style = f"""
            QPushButton {{
                color: {self._theme.text_secondary};
                background: transparent;
                border: 1px solid {self._theme.border};
                border-radius: 4px;
                padding: 4px 12px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                color: {self._theme.text_primary};
                background: {self._theme.button_hover_bg};
            }}
        """

        export_btn = QPushButton("导出 Markdown")
        export_btn.setCursor(Qt.PointingHandCursor)
        export_btn.setStyleSheet(btn_style)
        export_btn.clicked.connect(self._on_export)
        toolbar_layout.addWidget(export_btn)

        toolbar_layout.addStretch()

        clear_btn = QPushButton("清除历史")
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet(
            f"""
            QPushButton {{
                color: {self._theme.danger};
                background: transparent;
                border: 1px solid {self._theme.danger_border};
                border-radius: 4px;
                padding: 4px 12px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background: {self._theme.danger_soft};
            }}
        """
        )
        clear_btn.clicked.connect(self._on_clear)
        toolbar_layout.addWidget(clear_btn)

        container_layout.addWidget(toolbar)

        main_layout.addWidget(container)

    def showEvent(self, event):
        """Refresh data when shown."""
        super().showEvent(event)
        self._refresh_dates()

    def _refresh_dates(self):
        """Reload the date list from history store."""
        self._date_list.clear()
        if not self._history_store:
            return

        dates = self._history_store.get_dates(max_days=60)
        if not dates:
            self._empty_label.setText("暂无历史记录")
            self._empty_label.show()
            return

        for date_str in dates:
            # Format display: "03-17 (周一)" style
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                display = f"{dt.strftime('%m-%d')} ({weekdays[dt.weekday()]})"
            except ValueError:
                display = date_str

            item = QListWidgetItem(display)
            item.setData(Qt.UserRole, date_str)
            self._date_list.addItem(item)

        # Auto-select first (today)
        if self._date_list.count() > 0:
            self._date_list.setCurrentRow(0)

        # Update stats
        stats = self._history_store.get_stats()
        self._stats_label.setText(
            f"共 {stats['total_records']} 条记录，{stats['total_days']} 天"
        )

    def _on_date_selected(self, current, _previous):
        """Handle date selection change."""
        if current is None:
            return
        date_str = current.data(Qt.UserRole)
        self._current_date = date_str
        self._load_records()

    def _on_type_filter_changed(self, index):
        """Handle type filter change."""
        data = self._type_combo.itemData(index)
        self._current_type_filter = data
        self._load_records()

    def _on_search_changed(self, text):
        """Handle search text change with debounce."""
        self._current_search = text.strip()
        # Simple debounce: reload after user stops typing
        if not hasattr(self, "_search_timer"):
            self._search_timer = QTimer()
            self._search_timer.setSingleShot(True)
            self._search_timer.timeout.connect(self._load_records)
        self._search_timer.start(300)

    def _load_records(self):
        """Load and display records for current filters."""
        # Clear existing records
        while self._records_layout.count() > 1:  # Keep stretch
            item = self._records_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._history_store or not self._current_date:
            self._empty_label.setText("选择日期查看记录")
            self._empty_label.show()
            return

        from aria.core.history.models import (
            RecordType,
            RECORD_TYPE_LABELS,
            RECORD_TYPE_COLORS,
        )

        # Build filter
        record_type = None
        if self._current_type_filter:
            try:
                record_type = RecordType[self._current_type_filter]
            except KeyError:
                pass

        search = self._current_search if self._current_search else None

        records = self._history_store.query(
            date=self._current_date,
            record_type=record_type,
            search_text=search,
        )

        if not records:
            self._empty_label.setText("无匹配记录")
            self._empty_label.show()
            return

        self._empty_label.hide()

        for i, record in enumerate(records):
            try:
                dt = datetime.fromisoformat(record.timestamp)
                ts = dt.strftime("%H:%M:%S")
            except ValueError:
                ts = record.timestamp[:8]

            type_label = RECORD_TYPE_LABELS.get(record.record_type, "其他")
            type_color = RECORD_TYPE_COLORS.get(record.record_type, "#6B7280")

            widget = HistoryRecordWidget(
                record_id=record.id,
                record_date=self._current_date,
                timestamp_str=ts,
                type_label=type_label,
                type_color=type_color,
                input_text=record.input_text,
                output_text=record.output_text,
            )
            widget.copyClicked.connect(self._on_copy)
            widget.deleteClicked.connect(self._on_delete_record)
            self._records_layout.insertWidget(i, widget)

    def _on_copy(self, text: str):
        """Copy text to clipboard."""
        if text:
            clipboard = QApplication.clipboard()
            clipboard.setText(text)
            _blog(f"Copied: {text[:50]}...")

    def _on_delete_record(self, date: str, record_id: str):
        """Delete a single record."""
        if self._history_store:
            self._history_store.delete(date, record_id)
            self._load_records()

    def _on_export(self):
        """Export current day's records as Markdown."""
        if not self._history_store or not self._current_date:
            return

        markdown = self._history_store.export_markdown(self._current_date)
        if not markdown:
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出历史记录",
            f"aria_history_{self._current_date}.md",
            "Markdown (*.md)",
        )
        if file_path:
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(markdown)
                _blog(f"Exported to {file_path}")
            except Exception as e:
                _blog(f"Export failed: {e}")

    def _on_clear(self):
        """Clear all history for current date."""
        if not self._history_store or not self._current_date:
            return

        # Delete all records for the current date
        records = self._history_store.query(date=self._current_date, limit=9999)
        for r in records:
            self._history_store.delete(self._current_date, r.id)

        self._load_records()
        self._refresh_dates()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)
