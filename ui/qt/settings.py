# settings.py
# Settings window with 5 tabs for all configurable options
# Based on F3 spec section 4.5

import json
import re
import sys
from pathlib import Path
from typing import Optional, Callable

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from voicetype.core.utils import get_config_path

from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QListWidget,
    QStackedWidget,
    QLabel,
    QLineEdit,
    QCheckBox,
    QComboBox,
    QPushButton,
    QPlainTextEdit,
    QFormLayout,
    QRadioButton,
    QButtonGroup,
    QTableWidget,
    QTableWidgetItem,
    QSpinBox,
    QDoubleSpinBox,
    QListWidgetItem,
    QInputDialog,
    QMessageBox,
    QKeySequenceEdit,
    QSlider,
    QGroupBox,
    QAbstractItemView,
)
from PySide6.QtCore import Qt, Signal, QThread, QObject, QTimer
from PySide6.QtGui import QKeySequence
from . import styles

# Import shared prompt constant from hotword module
from voicetype.core.hotword import DEFAULT_POLISH_PROMPT
from voicetype.core.utils.phonetic import get_matcher


def get_audio_input_devices() -> list:
    """
    Get list of available audio input devices.
    Returns list of (name, device_id) tuples for QComboBox.
    """
    try:
        import sounddevice as sd

        devices = []
        default_device = sd.default.device[0]

        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                name = d["name"]
                if i == default_device:
                    name = f"[默认] {name}"
                devices.append((name, i))

        return devices
    except Exception as e:
        print(f"Failed to enumerate audio devices: {e}")
        return [("默认麦克风", None)]


class ApiTestWorker(QObject):
    """Worker for async API connection testing."""

    finished = Signal(bool, str, int)  # success, message, status_code

    def __init__(self, api_url: str, api_key: str, model: str):
        super().__init__()
        self.api_url = api_url
        self.api_key = api_key
        self.model = model

    def run(self):
        """Execute API test in worker thread."""
        import requests

        try:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            data = {
                "model": self.model or "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 5,
            }

            response = requests.post(
                f"{self.api_url}/v1/chat/completions",
                headers=headers,
                json=data,
                timeout=5,  # Reduced timeout for faster feedback
            )

            if response.status_code == 200:
                self.finished.emit(True, "API 连接成功！", response.status_code)
            else:
                self.finished.emit(False, response.text[:200], response.status_code)

        except requests.exceptions.Timeout:
            self.finished.emit(False, "API 连接超时，请检查地址是否正确", 0)
        except requests.exceptions.ConnectionError:
            self.finished.emit(False, "无法连接到 API 服务器，请检查地址是否正确", 0)
        except Exception as e:
            self.finished.emit(False, str(e), 0)


class SettingsWindow(QMainWindow):
    """Settings window with 5 configuration tabs."""

    # Signal emitted when settings are saved
    settingsSaved = Signal(dict)

    # Default prompt template - uses shared constant from hotword module
    DEFAULT_PROMPT = DEFAULT_POLISH_PROMPT

    def __init__(self, config_path: Optional[Path] = None):
        super().__init__()
        self.setWindowTitle("VoiceType 设置")
        self.resize(900, 650)
        self.setStyleSheet(styles.STYLESHEET_SETTINGS)

        self.config_path = config_path or get_config_path("hotwords.json")
        self.config = {}

        self._init_ui()
        self.load_config()

    def _init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Sidebar
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(200)
        self.sidebar.addItems(["常规", "专业词汇", "润色", "API", "高级"])
        self.sidebar.currentRowChanged.connect(self.change_page)

        # Content area
        self.pages = QStackedWidget()
        self.pages.setObjectName("contentArea")

        self.pages.addWidget(self._create_general_tab())
        self.pages.addWidget(self._create_hotwords_tab())
        self.pages.addWidget(self._create_polish_tab())
        self.pages.addWidget(self._create_api_tab())
        self.pages.addWidget(self._create_advanced_tab())

        layout.addWidget(self.sidebar)
        layout.addWidget(self.pages)

        self.sidebar.setCurrentRow(0)

    def change_page(self, index: int):
        self.pages.setCurrentIndex(index)

    # ==========================================================================
    # Tab 1: General
    # ==========================================================================
    def _create_general_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(30, 30, 30, 30)

        layout.addWidget(QLabel("<h2>常规设置</h2>"))

        form = QFormLayout()

        # Hotkey
        hotkey_layout = QHBoxLayout()
        self.hotkey_edit = QKeySequenceEdit()
        self.hotkey_edit.setKeySequence(
            QKeySequence("`")
        )  # Default: grave/backtick key
        hotkey_layout.addWidget(self.hotkey_edit)
        btn_clear_hotkey = QPushButton("清除")
        btn_clear_hotkey.clicked.connect(lambda: self.hotkey_edit.clear())
        hotkey_layout.addWidget(btn_clear_hotkey)
        form.addRow("录音热键:", hotkey_layout)

        # Audio device
        self.audio_device = QComboBox()
        self._populate_audio_devices()
        form.addRow("音频设备:", self.audio_device)

        layout.addLayout(form)
        layout.addSpacing(20)

        # Startup options
        self.chk_auto_startup = QCheckBox("开机自动启动")
        self.chk_auto_startup.setToolTip("在 Windows 启动时自动运行 VoiceType")
        layout.addWidget(self.chk_auto_startup)

        self.chk_start_active = QCheckBox("启动时激活语音（默认开始录音）")
        self.chk_start_active.setToolTip("勾选后，程序启动时自动进入录音待机状态")
        layout.addWidget(self.chk_start_active)

        layout.addSpacing(20)

        # Wakeword settings
        wakeword_group = QGroupBox("语音唤醒词")
        wakeword_layout = QVBoxLayout(wakeword_group)

        # Wakeword input
        wakeword_input_layout = QHBoxLayout()
        wakeword_label = QLabel("唤醒词:")
        self.wakeword_edit = QLineEdit()
        self.wakeword_edit.setPlaceholderText("瑶瑶")
        self.wakeword_edit.textChanged.connect(self._on_wakeword_text_changed)
        wakeword_input_layout.addWidget(wakeword_label)
        wakeword_input_layout.addWidget(self.wakeword_edit)
        wakeword_layout.addLayout(wakeword_input_layout)

        # Pinyin hint
        self.pinyin_hint = QLabel("")
        self.pinyin_hint.setStyleSheet(
            "color: #888; font-size: 11px; margin-left: 50px;"
        )
        wakeword_layout.addWidget(self.pinyin_hint)

        # Example commands hint
        example_hint = QLabel('💡 例: "瑶瑶开启自动发送"、"瑶瑶休眠"')
        example_hint.setStyleSheet("color: #666; font-size: 11px; margin-top: 5px;")
        wakeword_layout.addWidget(example_hint)

        layout.addWidget(wakeword_group)

        layout.addSpacing(20)

        # Translation settings
        translate_group = QGroupBox("翻译设置")
        translate_layout = QFormLayout(translate_group)

        self.translate_mode = QComboBox()
        self.translate_mode.addItem("弹窗显示", "popup")
        self.translate_mode.addItem("复制到剪贴板", "clipboard")
        translate_layout.addRow("翻译输出方式:", self.translate_mode)

        translate_hint = QLabel('💡 "翻译成英文/中文" 命令的结果输出方式')
        translate_hint.setStyleSheet("color: #666; font-size: 11px;")
        translate_layout.addRow("", translate_hint)

        layout.addWidget(translate_group)

        layout.addStretch()

        # Save button
        btn_save = QPushButton("保存设置")
        btn_save.setObjectName("primaryBtn")
        btn_save.clicked.connect(self.save_config)
        layout.addWidget(btn_save)

        return w

    # ==========================================================================
    # Tab 2: Hotwords (Simplified UX - 三方会谈 redesign)
    # ==========================================================================
    def _create_hotwords_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(30, 30, 30, 30)

        # --- Header ---
        layout.addWidget(QLabel("<h2>专业词汇</h2>"))
        subtitle = QLabel(
            "添加您常用的专业术语、品牌名、人名等，系统会自动识别并纠正谐音错误"
        )
        subtitle.setStyleSheet("color: #666; margin-bottom: 10px;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        layout.addSpacing(10)

        # --- Usage context (optional) ---
        context_layout = QHBoxLayout()
        context_label = QLabel("使用场景:")
        self.domain_ctx = QLineEdit()
        self.domain_ctx.setPlaceholderText("如：编程开发、医疗诊断、法律咨询...")
        context_layout.addWidget(context_label)
        context_layout.addWidget(self.domain_ctx, 1)
        layout.addLayout(context_layout)

        hint_label = QLabel("💡 描述您的使用领域，可提高整体识别准确率（可选）")
        hint_label.setStyleSheet("color: #888; font-size: 11px; margin-left: 70px;")
        layout.addWidget(hint_label)

        layout.addSpacing(20)

        # --- Main: Vocabulary list with weights ---
        list_header = QLabel("<b>词汇列表</b>")
        layout.addWidget(list_header)

        guide_label = QLabel("调整权重：0=禁用，1=低，2=正常，3=高")
        guide_label.setStyleSheet("color: #666; font-size: 12px; margin-bottom: 5px;")
        layout.addWidget(guide_label)

        # Table widget with word and weight columns
        self.vocab_table = QTableWidget(0, 3)
        self.vocab_table.setHorizontalHeaderLabels(["词汇", "权重", ""])
        self.vocab_table.horizontalHeader().setStretchLastSection(False)
        self.vocab_table.setColumnWidth(0, 200)  # Word column
        self.vocab_table.setColumnWidth(1, 180)  # Weight slider column
        self.vocab_table.setColumnWidth(2, 50)  # Weight value display
        self.vocab_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.vocab_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.vocab_table.setMinimumHeight(200)
        self.vocab_table.verticalHeader().setVisible(False)
        layout.addWidget(self.vocab_table)

        # Store weights dict for saving
        self._hotword_weights = {}

        # Action buttons
        btn_layout = QHBoxLayout()
        btn_add_word = QPushButton("+ 添加")
        btn_add_word.clicked.connect(self._add_prompt_word)
        btn_layout.addWidget(btn_add_word)

        btn_import = QPushButton("批量导入")
        btn_import.clicked.connect(self._batch_import_words)
        btn_layout.addWidget(btn_import)

        btn_remove_word = QPushButton("删除选中")
        btn_remove_word.clicked.connect(self._remove_prompt_words)
        btn_layout.addWidget(btn_remove_word)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addSpacing(20)

        # --- Advanced options (collapsible) ---
        self.advanced_group = QGroupBox("⚙️ 高级选项 - 手动纠错规则")
        self.advanced_group.setCheckable(True)
        self.advanced_group.setChecked(False)  # Default collapsed
        advanced_layout = QVBoxLayout()

        adv_hint = QLabel(
            "大部分谐音错误会被自动纠正。只有在遇到重复识别问题时才需要手动添加规则。"
        )
        adv_hint.setStyleSheet("color: #666; font-size: 11px;")
        adv_hint.setWordWrap(True)
        advanced_layout.addWidget(adv_hint)

        advanced_layout.addSpacing(10)

        # Replacements table
        self.replace_table = QTableWidget(0, 2)
        self.replace_table.setHorizontalHeaderLabels(["识别错误", "替换为"])
        self.replace_table.horizontalHeader().setStretchLastSection(True)
        self.replace_table.setMinimumHeight(120)
        advanced_layout.addWidget(self.replace_table)

        # Table buttons
        tbl_btn_layout = QHBoxLayout()
        btn_add_rule = QPushButton("+ 添加规则")
        btn_add_rule.clicked.connect(self._add_replacement_row)
        tbl_btn_layout.addWidget(btn_add_rule)

        btn_remove_rule = QPushButton("- 删除选中")
        btn_remove_rule.clicked.connect(self._remove_replacement_row)
        tbl_btn_layout.addWidget(btn_remove_rule)

        tbl_btn_layout.addStretch()
        advanced_layout.addLayout(tbl_btn_layout)

        self.advanced_group.setLayout(advanced_layout)
        layout.addWidget(self.advanced_group)

        layout.addStretch()

        # --- Save button ---
        btn_save = QPushButton("保存设置")
        btn_save.setObjectName("primaryBtn")
        btn_save.clicked.connect(self.save_config)
        layout.addWidget(btn_save)

        # Hidden: keep enable_initial_prompt always true (no UI control)
        self._enable_initial_prompt = True

        return w

    def _add_vocab_row(self, word: str, weight: float = 1.0):
        """Add a vocabulary row with word and weight slider."""
        row = self.vocab_table.rowCount()
        self.vocab_table.insertRow(row)

        # Word column (read-only)
        word_item = QTableWidgetItem(word)
        word_item.setFlags(word_item.flags() & ~Qt.ItemIsEditable)
        self.vocab_table.setItem(row, 0, word_item)

        # Clamp weight to valid range (0-3), convert old float values
        weight = max(0, min(3, int(weight + 0.5)))

        # Weight slider (0-3 range: 0=off, 1=low, 2=normal, 3=high)
        slider = QSlider(Qt.Horizontal)
        slider.setMinimum(0)
        slider.setMaximum(3)
        slider.setValue(weight)
        slider.setTickPosition(QSlider.TicksBelow)
        slider.setTickInterval(1)
        self.vocab_table.setCellWidget(row, 1, slider)

        # Weight value display
        weight_label = QLabel(str(weight))
        weight_label.setAlignment(Qt.AlignCenter)
        self.vocab_table.setCellWidget(row, 2, weight_label)

        # Connect slider to update label and store weight
        def on_slider_change(value, w=word, lbl=weight_label):
            lbl.setText(str(value))
            self._hotword_weights[w] = value

        slider.valueChanged.connect(on_slider_change)

        # Store initial weight
        self._hotword_weights[word] = weight

    def _add_prompt_word(self):
        text, ok = QInputDialog.getText(
            self,
            "添加专业词汇",
            "输入词汇（支持中英文混合）:\n\n示例：Claude、GitHub、第一性原理",
        )
        if ok and text.strip():
            word = text.strip()
            # Check duplicate
            existing = [
                self.vocab_table.item(i, 0).text()
                for i in range(self.vocab_table.rowCount())
            ]
            if word in existing:
                QMessageBox.warning(self, "重复", f"'{word}' 已在列表中")
                return
            self._add_vocab_row(word, 1.0)

    def _batch_import_words(self):
        """Batch import words from text input."""
        text, ok = QInputDialog.getMultiLineText(
            self,
            "批量导入",
            "每行一个词汇（或用逗号、顿号分隔）:\n\n示例:\nclaude\ngithub\n三方会谈",
        )
        if ok and text.strip():
            # Support multiple separators
            words = re.split(r"[,\n，、;；]", text)
            existing = {
                self.vocab_table.item(i, 0).text()
                for i in range(self.vocab_table.rowCount())
            }
            added = 0
            for word in words:
                word = word.strip()
                if word and word not in existing:
                    self._add_vocab_row(word, 1.0)
                    existing.add(word)
                    added += 1

            if added > 0:
                QMessageBox.information(self, "导入完成", f"成功添加 {added} 个词汇")
            else:
                QMessageBox.information(
                    self, "导入完成", "没有新词汇被添加（可能已存在）"
                )

    def _remove_prompt_words(self):
        rows = set()
        for item in self.vocab_table.selectedItems():
            rows.add(item.row())
        # Remove rows in reverse order to avoid index shifting
        for row in sorted(rows, reverse=True):
            word = self.vocab_table.item(row, 0).text()
            self._hotword_weights.pop(word, None)
            self.vocab_table.removeRow(row)

    def _add_replacement_row(self):
        row = self.replace_table.rowCount()
        self.replace_table.insertRow(row)
        self.replace_table.setItem(row, 0, QTableWidgetItem(""))
        self.replace_table.setItem(row, 1, QTableWidgetItem(""))

    def _remove_replacement_row(self):
        rows = set(item.row() for item in self.replace_table.selectedItems())
        for row in sorted(rows, reverse=True):
            self.replace_table.removeRow(row)

    # ==========================================================================
    # Tab 3: Polish (F3 core)
    # ==========================================================================
    def _create_polish_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(30, 30, 30, 30)

        layout.addWidget(QLabel("<h2>智能润色</h2>"))

        # Mode selection
        mode_group = QButtonGroup(w)
        self.radio_fast = QRadioButton("快速模式 (本地 Qwen, ~155ms)")
        self.radio_quality = QRadioButton("高质量模式 (Gemini API, ~1.7s)")
        mode_group.addButton(self.radio_fast)
        mode_group.addButton(self.radio_quality)

        layout.addWidget(self.radio_fast)
        layout.addWidget(self.radio_quality)

        layout.addSpacing(20)

        # Prompt editor
        layout.addWidget(QLabel("<b>高质量模式 Prompt 模板:</b>"))
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlainText(self.DEFAULT_PROMPT)
        self.prompt_edit.setMinimumHeight(200)
        layout.addWidget(self.prompt_edit)

        # Restore default button
        btn_layout = QHBoxLayout()
        btn_restore = QPushButton("恢复默认 Prompt")
        btn_restore.setObjectName("dangerBtn")
        btn_restore.clicked.connect(self._restore_default_prompt)
        btn_layout.addWidget(btn_restore)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addStretch()

        # Save button
        btn_save = QPushButton("保存润色设置")
        btn_save.setObjectName("primaryBtn")
        btn_save.clicked.connect(self.save_config)
        layout.addWidget(btn_save)

        return w

    def _restore_default_prompt(self):
        reply = QMessageBox.question(
            self,
            "确认",
            "确定要恢复默认 Prompt 模板吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.prompt_edit.setPlainText(self.DEFAULT_PROMPT)

    # ==========================================================================
    # Tab 4: API
    # ==========================================================================
    def _create_api_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(30, 30, 30, 30)

        layout.addWidget(QLabel("<h2>API 设置</h2>"))

        form = QFormLayout()

        self.api_url = QLineEdit()
        self.api_url.setPlaceholderText("http://localhost:3000")
        form.addRow("API 地址:", self.api_url)

        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("sk-...")
        form.addRow("API 密钥:", self.api_key)

        self.model = QLineEdit()
        self.model.setPlaceholderText("google/gemini-2.5-flash-lite-preview-09-2025")
        form.addRow("模型名称:", self.model)

        self.timeout = QSpinBox()
        self.timeout.setRange(5, 120)
        self.timeout.setValue(30)
        self.timeout.setSuffix(" 秒")
        form.addRow("超时时间:", self.timeout)

        layout.addLayout(form)

        layout.addSpacing(20)

        btn_layout = QHBoxLayout()
        self._api_test_button = QPushButton("测试连接")
        self._api_test_button.clicked.connect(self._test_api_connection)
        btn_layout.addWidget(self._api_test_button)

        btn_save = QPushButton("保存 API 设置")
        btn_save.setObjectName("primaryBtn")
        btn_save.clicked.connect(self.save_config)
        btn_layout.addWidget(btn_save)

        layout.addLayout(btn_layout)

        layout.addStretch()
        return w

    def _test_api_connection(self):
        """Test API connection with a simple request (non-blocking)."""
        # Prevent concurrent tests
        if (
            hasattr(self, "_api_thread")
            and self._api_thread is not None
            and self._api_thread.isRunning()
        ):
            return

        api_url = self.api_url.text().strip()
        api_key = self.api_key.text().strip()
        model = self.model.text().strip()

        if not api_url:
            QMessageBox.warning(self, "错误", "请先填写 API 地址")
            return

        # Disable button during test
        if hasattr(self, "_api_test_button"):
            self._api_test_button.setEnabled(False)
            self._api_test_button.setText("测试中...")

        # Create worker and thread
        self._api_thread = QThread()
        self._api_worker = ApiTestWorker(api_url, api_key, model)
        self._api_worker.moveToThread(self._api_thread)

        # Connect signals
        self._api_thread.started.connect(self._api_worker.run)
        self._api_worker.finished.connect(self._on_api_test_finished)
        self._api_worker.finished.connect(self._api_thread.quit)
        self._api_worker.finished.connect(self._api_worker.deleteLater)
        self._api_thread.finished.connect(self._api_thread.deleteLater)

        # Start test
        self._api_thread.start()

    def _on_api_test_finished(self, success: bool, message: str, status_code: int):
        """Handle API test result."""
        # Re-enable button using stored reference
        if hasattr(self, "_api_test_button"):
            self._api_test_button.setEnabled(True)
            self._api_test_button.setText("测试连接")

        if success:
            QMessageBox.information(self, "成功", f"{message}\n\n状态码: {status_code}")
        elif status_code > 0:
            QMessageBox.warning(
                self,
                "连接失败",
                f"API 返回错误\n\n状态码: {status_code}\n响应: {message}",
            )
        else:
            QMessageBox.warning(self, "连接失败", message)

    def _on_wakeword_text_changed(self, text: str):
        """Update pinyin hint when wakeword text changes."""
        if text.strip():
            try:
                matcher = get_matcher()
                pinyin = matcher.to_pinyin(text.strip())
                pinyin_str = " ".join(pinyin)
                self.pinyin_hint.setText(
                    f"拼音: {pinyin_str} (同音字均可识别，如：摇摇、妖妖)"
                )
            except Exception:
                self.pinyin_hint.setText("")
        else:
            self.pinyin_hint.setText("")

    # ==========================================================================
    # Tab 5: Advanced
    # ==========================================================================
    def _create_advanced_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(30, 30, 30, 30)

        layout.addWidget(QLabel("<h2>高级设置</h2>"))

        # ASR settings (FunASR)
        asr_group = QGroupBox("语音识别 (FunASR)")
        asr_layout = QFormLayout(asr_group)

        self.asr_model = QComboBox()
        self.asr_model.addItems(
            [
                "大模型 (paraformer-zh) - 推荐，准确度高",
                "小模型 (SenseVoice) - 显存<8GB时使用",
            ]
        )
        self.asr_model.setCurrentIndex(0)
        asr_layout.addRow("模型:", self.asr_model)

        self.asr_device = QComboBox()
        self.asr_device.addItems(["cuda", "cpu"])
        asr_layout.addRow("设备:", self.asr_device)

        # Model info label
        model_info = QLabel("大模型约需3GB显存，小模型约需1.5GB显存")
        model_info.setStyleSheet("color: #888; font-size: 12px;")
        asr_layout.addRow("", model_info)

        layout.addWidget(asr_group)

        # VAD settings
        vad_group = QGroupBox("VAD (语音活动检测)")
        vad_layout = QFormLayout(vad_group)

        self.vad_threshold = QDoubleSpinBox()
        self.vad_threshold.setRange(0.1, 0.9)
        self.vad_threshold.setSingleStep(0.1)
        self.vad_threshold.setValue(0.5)
        vad_layout.addRow("阈值:", self.vad_threshold)

        self.vad_min_silence = QSpinBox()
        self.vad_min_silence.setRange(100, 2000)
        self.vad_min_silence.setValue(500)
        self.vad_min_silence.setSuffix(" ms")
        vad_layout.addRow("最小静音:", self.vad_min_silence)

        layout.addWidget(vad_group)

        # Local polish model
        local_group = QGroupBox("本地润色模型")
        local_layout = QFormLayout(local_group)

        self.local_model_path = QLineEdit()
        self.local_model_path.setPlaceholderText(
            "models/qwen2.5-1.5b-instruct-q4_k_m.gguf"
        )
        local_layout.addRow("模型路径:", self.local_model_path)

        layout.addWidget(local_group)

        layout.addStretch()

        # Save button for advanced settings
        btn_save = QPushButton("保存高级设置")
        btn_save.setObjectName("primaryBtn")
        btn_save.clicked.connect(self.save_config)
        layout.addWidget(btn_save)

        return w

    # ==========================================================================
    # Config load/save
    # ==========================================================================
    def load_config(self):
        """Load configuration from hotwords.json."""
        if not self.config_path.exists():
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        except Exception as e:
            print(f"Failed to load config: {e}")
            return

        # === General tab ===
        general = self.config.get("general", {})
        hotkey = general.get("hotkey", "grave")
        # Convert VoiceType hotkey format to Qt format
        qt_hotkey = self._hotkey_to_qt(hotkey)
        self.hotkey_edit.setKeySequence(QKeySequence(qt_hotkey))

        # Audio device - find by name
        audio_device_name = general.get("audio_device", "")
        if audio_device_name:
            for i in range(self.audio_device.count()):
                if audio_device_name in self.audio_device.itemText(i):
                    self.audio_device.setCurrentIndex(i)
                    break

        self.chk_auto_startup.setChecked(self._is_auto_startup_enabled())
        self.chk_start_active.setChecked(
            general.get("start_active", True)
        )  # Default: active

        # === Hotwords tab ===
        # Note: enable_initial_prompt is always true (no UI control)
        self._enable_initial_prompt = self.config.get("enable_initial_prompt", True)
        self.domain_ctx.setText(self.config.get("domain_context", ""))

        # Expand advanced group if there are existing replacements
        replacements = self.config.get("replacements", {})
        if replacements:
            self.advanced_group.setChecked(True)

        # Prompt words with weights (support both "hotwords" and legacy "prompt_words")
        self.vocab_table.setRowCount(0)
        self._hotword_weights = {}
        words = self.config.get("hotwords", self.config.get("prompt_words", []))
        weights = self.config.get("hotword_weights", {})
        for word in words:
            weight = weights.get(word, 1.0)
            self._add_vocab_row(word, weight)

        # Replacements
        replacements = self.config.get("replacements", {})
        self.replace_table.setRowCount(0)
        for wrong, correct in replacements.items():
            row = self.replace_table.rowCount()
            self.replace_table.insertRow(row)
            self.replace_table.setItem(row, 0, QTableWidgetItem(wrong))
            self.replace_table.setItem(row, 1, QTableWidgetItem(correct))

        # === Polish tab ===
        polish_mode = self.config.get("polish_mode", "fast")
        if polish_mode == "fast":
            self.radio_fast.setChecked(True)
        else:
            self.radio_quality.setChecked(True)

        # Load prompt template
        polish = self.config.get("polish", {})
        prompt_template = polish.get("prompt_template", self.DEFAULT_PROMPT)
        self.prompt_edit.setPlainText(prompt_template)

        # === API tab ===
        self.api_url.setText(polish.get("api_url", ""))
        self.api_key.setText(polish.get("api_key", ""))
        self.model.setText(polish.get("model", ""))
        self.timeout.setValue(polish.get("timeout", 30))

        # === Advanced tab ===
        # FunASR settings
        funasr = self.config.get("funasr", {})
        funasr_model = funasr.get("model_name", "paraformer-zh")
        # Map model name to combo index (0=large/paraformer, 1=small/sensevoice)
        if "sensevoice" in funasr_model.lower():
            self.asr_model.setCurrentIndex(1)
        else:
            self.asr_model.setCurrentIndex(0)

        funasr_device = funasr.get("device", "cuda")
        idx = self.asr_device.findText(funasr_device)
        if idx >= 0:
            self.asr_device.setCurrentIndex(idx)

        # VAD settings
        vad = self.config.get("vad", {})
        self.vad_threshold.setValue(vad.get("threshold", 0.2))
        self.vad_min_silence.setValue(vad.get("min_silence_ms", 1200))

        # Local polish
        local_polish = self.config.get("local_polish", {})
        self.local_model_path.setText(local_polish.get("model_path", ""))

        # === Translation settings ===
        translation = self.config.get("translation", {})
        mode = translation.get("output_mode", "popup")
        idx = self.translate_mode.findData(mode)
        if idx >= 0:
            self.translate_mode.setCurrentIndex(idx)

        # Wakeword - load from wakeword.json
        wakeword_path = self.config_path.parent / "wakeword.json"
        if wakeword_path.exists():
            try:
                with open(wakeword_path, "r", encoding="utf-8") as f:
                    wakeword_config = json.load(f)
                wakeword = wakeword_config.get("wakeword", "瑶瑶")
                self.wakeword_edit.setText(wakeword)
            except Exception:
                self.wakeword_edit.setText("瑶瑶")
        else:
            self.wakeword_edit.setText("瑶瑶")

    def save_config(self):
        """Save configuration to hotwords.json."""
        # Track if restart-required settings changed
        restart_needed = False
        old_funasr = self.config.get("funasr", {})
        old_vad = self.config.get("vad", {})

        # === General tab ===
        if "general" not in self.config:
            self.config["general"] = {}
        hotkey_seq = self.hotkey_edit.keySequence().toString()
        # Convert Qt format to VoiceType format for storage
        self.config["general"]["hotkey"] = (
            self._qt_to_hotkey(hotkey_seq) if hotkey_seq else "grave"
        )
        self.config["general"]["audio_device"] = self.audio_device.currentText()
        self.config["general"]["start_active"] = self.chk_start_active.isChecked()

        # Handle auto startup (create/remove shortcut)
        self._set_auto_startup(self.chk_auto_startup.isChecked())

        # === Hotwords tab ===
        # Note: enable_initial_prompt is always true (no UI control needed)
        self.config["enable_initial_prompt"] = True
        self.config["domain_context"] = self.domain_ctx.text()

        # Hotwords with weights (use "hotwords" key, remove legacy "prompt_words" if present)
        hotwords = []
        for i in range(self.vocab_table.rowCount()):
            word_item = self.vocab_table.item(i, 0)
            if word_item:
                hotwords.append(word_item.text())
        self.config["hotwords"] = hotwords
        self.config["hotword_weights"] = self._hotword_weights.copy()
        self.config.pop("prompt_words", None)  # Remove legacy key

        # Replacements
        replacements = {}
        for row in range(self.replace_table.rowCount()):
            wrong_item = self.replace_table.item(row, 0)
            correct_item = self.replace_table.item(row, 1)
            if wrong_item and correct_item:
                wrong = wrong_item.text().strip()
                correct = correct_item.text().strip()
                if wrong and correct:
                    replacements[wrong] = correct
        self.config["replacements"] = replacements

        # === Polish tab ===
        self.config["polish_mode"] = (
            "fast" if self.radio_fast.isChecked() else "quality"
        )

        # === API settings ===
        if "polish" not in self.config:
            self.config["polish"] = {}
        self.config["polish"]["api_url"] = self.api_url.text()
        self.config["polish"]["api_key"] = self.api_key.text()
        self.config["polish"]["model"] = self.model.text()
        self.config["polish"]["timeout"] = self.timeout.value()
        self.config["polish"]["prompt_template"] = self.prompt_edit.toPlainText()

        # === Advanced tab - FunASR ===
        if "funasr" not in self.config:
            self.config["funasr"] = {}

        # Map combo index to model name
        asr_model_idx = self.asr_model.currentIndex()
        new_funasr_model = (
            "paraformer-zh" if asr_model_idx == 0 else "iic/SenseVoiceSmall"
        )
        new_funasr_device = self.asr_device.currentText()

        if (
            old_funasr.get("model_name") != new_funasr_model
            or old_funasr.get("device") != new_funasr_device
        ):
            restart_needed = True

        self.config["funasr"]["model_name"] = new_funasr_model
        self.config["funasr"]["device"] = new_funasr_device
        # Ensure asr_engine is set to funasr
        self.config["asr_engine"] = "funasr"

        # === Advanced tab - VAD ===
        if "vad" not in self.config:
            self.config["vad"] = {}
        new_vad_threshold = self.vad_threshold.value()
        new_vad_silence = self.vad_min_silence.value()

        if (
            old_vad.get("threshold") != new_vad_threshold
            or old_vad.get("min_silence_ms") != new_vad_silence
        ):
            restart_needed = True

        self.config["vad"]["threshold"] = new_vad_threshold
        self.config["vad"]["min_silence_ms"] = new_vad_silence

        # === Local polish ===
        if "local_polish" not in self.config:
            self.config["local_polish"] = {}
        self.config["local_polish"]["model_path"] = self.local_model_path.text()
        # Auto-enable local polish when fast mode is selected
        self.config["local_polish"]["enabled"] = self.radio_fast.isChecked()

        # === Translation settings ===
        self.config["translation"] = {"output_mode": self.translate_mode.currentData()}

        # === Wakeword - save to wakeword.json ===
        wakeword_path = self.config_path.parent / "wakeword.json"
        new_wakeword = self.wakeword_edit.text().strip() or "瑶瑶"
        try:
            # Load existing wakeword config
            if wakeword_path.exists():
                with open(wakeword_path, "r", encoding="utf-8") as f:
                    wakeword_config = json.load(f)
            else:
                wakeword_config = {"enabled": True, "wakeword": "瑶瑶", "commands": {}}

            # Update wakeword
            wakeword_config["wakeword"] = new_wakeword

            # Save back
            with open(wakeword_path, "w", encoding="utf-8") as f:
                json.dump(wakeword_config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Failed to save wakeword config: {e}")

        # Save to file
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)

            # Only show message if restart needed (important info)
            if restart_needed:
                QMessageBox.information(
                    self,
                    "设置已保存",
                    "设置已保存。\n\n⚠️ 语音识别/VAD 设置更改需要重启应用才能生效。",
                )
            else:
                # Visual feedback: temporarily change button text to confirm save
                sender = self.sender()
                if sender and hasattr(sender, "setText"):
                    original_text = sender.text()
                    sender.setText("已保存 ✓")
                    # Restore original text after 1.5 seconds
                    QTimer.singleShot(1500, lambda: sender.setText(original_text))

            self.settingsSaved.emit(self.config)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存失败: {e}")

    def _populate_audio_devices(self):
        """Populate audio device dropdown at startup."""
        self.audio_device.clear()
        devices = get_audio_input_devices()
        for name, device_id in devices:
            self.audio_device.addItem(name, userData=device_id)

    def _get_startup_shortcut_path(self) -> Path:
        """Get the path to the startup folder shortcut."""
        import os

        startup_folder = (
            Path(os.environ["APPDATA"])
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
        return startup_folder / "VoiceType.lnk"

    def _is_auto_startup_enabled(self) -> bool:
        """Check if auto startup shortcut exists."""
        return self._get_startup_shortcut_path().exists()

    def _set_auto_startup(self, enabled: bool) -> None:
        """Create or remove startup shortcut."""
        shortcut_path = self._get_startup_shortcut_path()

        if enabled:
            if shortcut_path.exists():
                return  # Already enabled

            try:
                # Find VoiceType.vbs launcher
                project_dir = Path(__file__).parent.parent.parent
                launcher = project_dir / "VoiceType.vbs"

                # For portable build, launcher is in dist folder
                if not launcher.exists():
                    # Try to find in parent directories
                    for parent in [project_dir.parent, project_dir.parent.parent]:
                        candidate = parent / "VoiceType.vbs"
                        if candidate.exists():
                            launcher = candidate
                            break

                if not launcher.exists():
                    print(f"[AutoStartup] Launcher not found, skipping")
                    return

                # Create shortcut using PowerShell
                import subprocess

                ps_script = f"""
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("{shortcut_path}")
$Shortcut.TargetPath = "wscript.exe"
$Shortcut.Arguments = '"{launcher}"'
$Shortcut.WorkingDirectory = "{launcher.parent}"
$Shortcut.Description = "VoiceType - Local AI Voice Dictation"
$Shortcut.Save()
"""
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_script],
                    capture_output=True,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        if hasattr(subprocess, "CREATE_NO_WINDOW")
                        else 0
                    ),
                )
                print(f"[AutoStartup] Created shortcut: {shortcut_path}")
            except Exception as e:
                print(f"[AutoStartup] Failed to create shortcut: {e}")
        else:
            if shortcut_path.exists():
                try:
                    shortcut_path.unlink()
                    print(f"[AutoStartup] Removed shortcut: {shortcut_path}")
                except Exception as e:
                    print(f"[AutoStartup] Failed to remove shortcut: {e}")

    def _hotkey_to_qt(self, hotkey: str) -> str:
        """Convert VoiceType hotkey format to Qt QKeySequence format."""
        # Mapping from VoiceType format to Qt format
        key_map = {
            "grave": "`",
            "backtick": "`",
            "tilde": "`",
            "capslock": "CapsLock",
            "caps": "CapsLock",
            "space": "Space",
            "tab": "Tab",
            "enter": "Return",
            "escape": "Escape",
            "backspace": "Backspace",
            "delete": "Delete",
            "insert": "Insert",
            "home": "Home",
            "end": "End",
            "pageup": "PgUp",
            "pagedown": "PgDown",
            "numlock": "NumLock",
            "scrolllock": "ScrollLock",
            "pause": "Pause",
            "printscreen": "Print",
        }
        # Handle combo keys like "ctrl+shift+space"
        parts = hotkey.lower().split("+")
        qt_parts = []
        for part in parts:
            if part in key_map:
                qt_parts.append(key_map[part])
            elif part == "ctrl":
                qt_parts.append("Ctrl")
            elif part == "shift":
                qt_parts.append("Shift")
            elif part == "alt":
                qt_parts.append("Alt")
            elif part == "win":
                qt_parts.append("Meta")
            elif len(part) == 1:
                qt_parts.append(part.upper())
            elif part.startswith("f") and part[1:].isdigit():
                qt_parts.append(part.upper())  # F1-F12
            else:
                qt_parts.append(part.capitalize())
        return "+".join(qt_parts)

    def _qt_to_hotkey(self, qt_hotkey: str) -> str:
        """Convert Qt QKeySequence format to VoiceType hotkey format."""
        if not qt_hotkey:
            return "grave"
        # Mapping from Qt format to VoiceType format
        key_map = {
            "`": "grave",
            "CapsLock": "capslock",
            "Space": "space",
            "Tab": "tab",
            "Return": "enter",
            "Escape": "escape",
            "Backspace": "backspace",
            "Delete": "delete",
            "Insert": "insert",
            "Home": "home",
            "End": "end",
            "PgUp": "pageup",
            "PgDown": "pagedown",
            "NumLock": "numlock",
            "ScrollLock": "scrolllock",
            "Pause": "pause",
            "Print": "printscreen",
            "Ctrl": "ctrl",
            "Shift": "shift",
            "Alt": "alt",
            "Meta": "win",
        }
        parts = qt_hotkey.split("+")
        vt_parts = []
        for part in parts:
            if part in key_map:
                vt_parts.append(key_map[part])
            else:
                vt_parts.append(part.lower())
        return "+".join(vt_parts)

    def get_selected_device_id(self) -> int:
        """Get the selected audio device ID."""
        return self.audio_device.currentData()

    def get_current_hotkey(self) -> str:
        """Get the current hotkey as string."""
        return self.hotkey_edit.keySequence().toString(QKeySequence.NativeText)

    def set_polish_mode(self, mode: str) -> None:
        """
        Set polish mode from external source (e.g., popup menu).

        Args:
            mode: "fast" or "quality"
        """
        if mode == "fast":
            self.radio_fast.setChecked(True)
        else:
            self.radio_quality.setChecked(True)

    def get_polish_mode(self) -> str:
        """Get current polish mode selection."""
        return "fast" if self.radio_fast.isChecked() else "quality"
