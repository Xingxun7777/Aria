"""
History Browser Window
======================
Full-featured history browser for all Aria interactions.
Replaces the simple HistoryWindow popup with a proper window.
"""

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QFrame,
    QApplication,
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


def asr_record_actions(
    record_type_name: str, input_text: str, output_text: str
) -> dict:
    """
    Decide which extra actions an ASR record row offers.

    Returns dict with:
        copy_raw: True when a distinct raw transcript exists (input_text
            holds the raw chain, output_text the final text).
        reinject_text: the final committed text to re-inject ("" disables).
    """
    input_text = (input_text or "").strip()
    output_text = (output_text or "").strip()
    if record_type_name != "ASR":
        return {"copy_raw": False, "reinject_text": ""}
    return {
        "copy_raw": bool(input_text and output_text and input_text != output_text),
        "reinject_text": output_text or input_text,
    }


class HistoryRecordWidget(QFrame):
    """Single history record display widget."""

    copyClicked = Signal(str)  # text to copy
    copyRawClicked = Signal(str)  # raw transcript to copy
    reinjectClicked = Signal(str)  # text to re-inject into the focused app
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
        record_type_name: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._theme = styles.get_theme_palette()
        self._record_id = record_id
        self._record_date = record_date
        self._output_text = output_text
        self._input_text = input_text
        self._actions = asr_record_actions(record_type_name, input_text, output_text)
        self._metadata = metadata or {}
        delivery = self._metadata.get("delivery", {})
        if (
            isinstance(delivery, dict)
            and delivery.get("operation") in {"voice_edit", "voice_edit_undo"}
        ):
            # output_text is a content-free command marker, not user text. It
            # must never be offered as something History can inject.
            self._actions["reinject_text"] = ""
        self._delivery_failed = (
            record_type_name == "ASR"
            and isinstance(delivery, dict)
            and delivery.get("status")
            in {
                "failed",
                "target_changed",
                "target_unavailable",
                "target_changed_partial",
                "target_unavailable_partial",
                "word_com_failed",
                "word_com_partial",
                "terminal_manual_required",
                "terminal_plan_failed",
                "terminal_chunk_partial",
                "game_profile_unavailable",
                "game_plan_failed",
                "game_manual_required",
                "game_target_unavailable",
                "game_target_changed",
                "game_profile_changed",
                "game_open_failed",
                "game_chat_unverified",
                "game_text_failed",
                "game_text_partial",
            }
        )
        self._submit_unconfirmed = (
            record_type_name == "ASR"
            and isinstance(delivery, dict)
            and delivery.get("surface") == "game"
            and delivery.get("submit_status") == "unknown"
        )
        self._edit_unconfirmed = (
            record_type_name == "ASR"
            and isinstance(delivery, dict)
            and delivery.get("operation") in {"voice_edit", "voice_edit_undo"}
            and delivery.get("partial_possible") is True
        )

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

        if self._delivery_failed:
            delivery_badge = QLabel("上屏未确认")
            delivery_badge.setStyleSheet(
                f"""
                QLabel {{
                    color: {self._theme.danger};
                    background: {self._theme.danger_soft};
                    border: 1px solid {self._theme.danger};
                    border-radius: 4px;
                    padding: 1px 6px;
                    font-size: 10px;
                    font-weight: bold;
                }}
            """
            )
            header.addWidget(delivery_badge)
        elif self._submit_unconfirmed:
            submit_badge = QLabel("提交未确认")
            submit_badge.setStyleSheet(
                f"""
                QLabel {{
                    color: {self._theme.accent};
                    background: {self._theme.accent_soft};
                    border: 1px solid {self._theme.accent};
                    border-radius: 4px;
                    padding: 1px 6px;
                    font-size: 10px;
                    font-weight: bold;
                }}
            """
            )
            header.addWidget(submit_badge)
        elif self._edit_unconfirmed:
            edit_badge = QLabel("编辑未确认")
            edit_badge.setStyleSheet(
                f"""
                QLabel {{
                    color: {self._theme.accent};
                    background: {self._theme.accent_soft};
                    border: 1px solid {self._theme.accent};
                    border-radius: 4px;
                    padding: 1px 6px;
                    font-size: 10px;
                    font-weight: bold;
                }}
            """
            )
            header.addWidget(edit_badge)

        header.addStretch()

        _action_btn_style = f"""
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

        # Copy button
        copy_btn = QPushButton("复制")
        copy_btn.setFixedHeight(22)
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.setStyleSheet(_action_btn_style)
        copy_btn.clicked.connect(self._on_copy)
        header.addWidget(copy_btn)

        # ASR-only actions: copy raw transcript / re-inject into focused app
        if self._actions["copy_raw"]:
            raw_btn = QPushButton("原文")
            raw_btn.setFixedHeight(22)
            raw_btn.setCursor(Qt.PointingHandCursor)
            raw_btn.setToolTip("复制原始转写（润色前）")
            raw_btn.setStyleSheet(_action_btn_style)
            raw_btn.clicked.connect(self._on_copy_raw)
            header.addWidget(raw_btn)

        if self._actions["reinject_text"]:
            reinject_btn = QPushButton("重试" if self._delivery_failed else "上屏")
            reinject_btn.setFixedHeight(22)
            reinject_btn.setCursor(Qt.PointingHandCursor)
            reinject_btn.setToolTip("重新输入到当前焦点窗口")
            reinject_btn.setStyleSheet(_action_btn_style)
            reinject_btn.clicked.connect(self._on_reinject)
            header.addWidget(reinject_btn)

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

    def _on_copy_raw(self):
        self.copyRawClicked.emit(self._input_text)

    def _on_reinject(self):
        self.reinjectClicked.emit(self._actions["reinject_text"])

    def _on_delete(self):
        self.deleteClicked.emit(self._record_date, self._record_id)


class HistoryBrowserWindow(QWidget):
    """
    Full-featured history browser window.

    Layout:
    - Left sidebar (150px): date list + search
    - Right panel: type filter chips + scrollable record list
    - Bottom toolbar: export / clear / stats
    """

    closed = Signal()
    # Re-inject a history text into the focused app. Emitted with the text;
    # the actual foreground validation + injection is wired in main.py.
    reinjectRequested = Signal(str)

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

        # Type filter chips (single-select), colored via RECORD_TYPE_COLORS
        right_layout.addWidget(self._build_type_chips_bar())

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

    @staticmethod
    def _chip_rgba(hex_color: str, alpha: float) -> str:
        """'#RRGGBB' + alpha -> 'rgba(r, g, b, a)' for stylesheet use."""
        try:
            h = hex_color.lstrip("#")
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return f"rgba({r}, {g}, {b}, {alpha})"
        except (ValueError, IndexError):
            return f"rgba(128, 128, 128, {alpha})"

    def _build_type_chips_bar(self) -> QFrame:
        """Build the single-select RecordType filter chips row."""
        from aria.core.history.models import (
            RecordType,
            RECORD_TYPE_LABELS,
            RECORD_TYPE_COLORS,
        )

        bar = QFrame()
        bar.setStyleSheet(
            f"""
            QFrame {{
                background: transparent;
                border: none;
                border-bottom: 1px solid {self._theme.border};
            }}
        """
        )
        grid = QGridLayout(bar)
        grid.setContentsMargins(12, 8, 12, 8)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)

        entries = [(None, "全部", self._theme.accent)]
        for rt in RecordType:
            entries.append(
                (
                    rt.name,
                    RECORD_TYPE_LABELS.get(rt, rt.name),
                    RECORD_TYPE_COLORS.get(rt, "#6B7280"),
                )
            )

        self._type_chips = {}
        cols = 5
        for i, (value, label, color) in enumerate(entries):
            chip = QPushButton(label)
            chip.setCheckable(True)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setStyleSheet(
                f"""
                QPushButton {{
                    color: {self._theme.text_secondary};
                    background: transparent;
                    border: 1px solid {self._theme.border};
                    border-radius: 10px;
                    padding: 2px 10px;
                    font-size: 11px;
                }}
                QPushButton:hover {{
                    color: {color};
                    border-color: {self._chip_rgba(color, 0.55)};
                }}
                QPushButton:checked {{
                    color: {color};
                    background: {self._chip_rgba(color, 0.14)};
                    border-color: {self._chip_rgba(color, 0.45)};
                    font-weight: bold;
                }}
            """
            )
            chip.clicked.connect(lambda _checked=False, v=value: self._on_type_chip_clicked(v))
            grid.addWidget(chip, i // cols, i % cols)
            self._type_chips[value] = chip

        self._type_chips[None].setChecked(True)
        return bar

    def _on_type_chip_clicked(self, value):
        """Single-select chip behavior: set filter, reload records."""
        for chip_value, chip in self._type_chips.items():
            chip.setChecked(chip_value == value)
        self._current_type_filter = value
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
                record_type_name=record.record_type.name,
                metadata=record.metadata,
            )
            widget.copyClicked.connect(self._on_copy)
            widget.copyRawClicked.connect(self._on_copy)
            widget.reinjectClicked.connect(self._on_reinject)
            widget.deleteClicked.connect(self._on_delete_record)
            self._records_layout.insertWidget(i, widget)

    def _on_copy(self, text: str):
        """Copy text to clipboard."""
        if text:
            clipboard = QApplication.clipboard()
            clipboard.setText(text)
            _blog(f"Copied: {text[:50]}...")

    def _on_reinject(self, text: str):
        """Hand off re-injection to main.py wiring and get out of the way.

        The browser hides itself first so Windows returns focus to the
        previously active window — the injection target. The wired handler
        then validates the foreground window before pasting.
        """
        if not text:
            return
        _blog(f"Reinject requested: {text[:50]}...")
        self.hide()
        self.reinjectRequested.emit(text)

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
