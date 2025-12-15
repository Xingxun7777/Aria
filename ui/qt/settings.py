# settings.py
# Settings window with 5 tabs for all configurable options
# Based on F3 spec section 4.5

import json
import re
from pathlib import Path
from typing import Optional, Callable

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
from ...core.hotword import DEFAULT_POLISH_PROMPT
from ...core.utils.phonetic import get_matcher


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

        self.config_path = (
            config_path
            or Path(__file__).parent.parent.parent / "config" / "hotwords.json"
        )
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
        self.hotkey_edit.setKeySequence(QKeySequence("CapsLock"))
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
        self.chk_startup = QCheckBox("开机自启动")
        layout.addWidget(self.chk_startup)

        self.chk_minimize = QCheckBox("启动时最小化到托盘")
        layout.addWidget(self.chk_minimize)

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

        # --- Main: Vocabulary list ---
        list_header = QLabel("<b>词汇列表</b>")
        layout.addWidget(list_header)

        guide_label = QLabel("这些词会被优先识别，无需手动添加纠错规则")
        guide_label.setStyleSheet("color: #666; font-size: 12px; margin-bottom: 5px;")
        layout.addWidget(guide_label)

        # List widget with multi-select
        self.prompt_words_list = QListWidget()
        self.prompt_words_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.prompt_words_list.setMinimumHeight(150)
        layout.addWidget(self.prompt_words_list)

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
                self.prompt_words_list.item(i).text()
                for i in range(self.prompt_words_list.count())
            ]
            if word in existing:
                QMessageBox.warning(self, "重复", f"'{word}' 已在列表中")
                return
            self.prompt_words_list.addItem(word)

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
                self.prompt_words_list.item(i).text()
                for i in range(self.prompt_words_list.count())
            }
            added = 0
            for word in words:
                word = word.strip()
                if word and word not in existing:
                    self.prompt_words_list.addItem(word)
                    existing.add(word)
                    added += 1

            if added > 0:
                QMessageBox.information(self, "导入完成", f"成功添加 {added} 个词汇")
            else:
                QMessageBox.information(
                    self, "导入完成", "没有新词汇被添加（可能已存在）"
                )

    def _remove_prompt_words(self):
        for item in self.prompt_words_list.selectedItems():
            self.prompt_words_list.takeItem(self.prompt_words_list.row(item))

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

        # Whisper settings
        whisper_group = QGroupBox("Whisper ASR")
        whisper_layout = QFormLayout(whisper_group)

        self.whisper_model = QComboBox()
        self.whisper_model.addItems(
            ["tiny", "base", "small", "medium", "large", "large-v3"]
        )
        self.whisper_model.setCurrentText("large-v3")
        whisper_layout.addRow("模型:", self.whisper_model)

        self.whisper_device = QComboBox()
        self.whisper_device.addItems(["cuda", "cpu"])
        whisper_layout.addRow("设备:", self.whisper_device)

        self.whisper_language = QComboBox()
        self.whisper_language.addItems(["zh", "en", "ja", "auto"])
        self.whisper_language.setCurrentText("zh")
        whisper_layout.addRow("语言:", self.whisper_language)

        layout.addWidget(whisper_group)

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
        self.hotkey_edit.setKeySequence(QKeySequence(hotkey))

        # Audio device - find by name
        audio_device_name = general.get("audio_device", "")
        if audio_device_name:
            for i in range(self.audio_device.count()):
                if audio_device_name in self.audio_device.itemText(i):
                    self.audio_device.setCurrentIndex(i)
                    break

        self.chk_startup.setChecked(general.get("auto_startup", False))
        self.chk_minimize.setChecked(general.get("minimize_to_tray", False))

        # === Hotwords tab ===
        # Note: enable_initial_prompt is always true (no UI control)
        self._enable_initial_prompt = self.config.get("enable_initial_prompt", True)
        self.domain_ctx.setText(self.config.get("domain_context", ""))

        # Expand advanced group if there are existing replacements
        replacements = self.config.get("replacements", {})
        if replacements:
            self.advanced_group.setChecked(True)

        # Prompt words (support both "hotwords" and legacy "prompt_words")
        self.prompt_words_list.clear()
        words = self.config.get("hotwords", self.config.get("prompt_words", []))
        for word in words:
            self.prompt_words_list.addItem(word)

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
        # Whisper settings
        whisper = self.config.get("whisper", {})
        whisper_model = whisper.get("model", "large-v3")
        idx = self.whisper_model.findText(whisper_model)
        if idx >= 0:
            self.whisper_model.setCurrentIndex(idx)

        whisper_device = whisper.get("device", "cuda")
        idx = self.whisper_device.findText(whisper_device)
        if idx >= 0:
            self.whisper_device.setCurrentIndex(idx)

        whisper_lang = whisper.get("language", "zh")
        idx = self.whisper_language.findText(whisper_lang)
        if idx >= 0:
            self.whisper_language.setCurrentIndex(idx)

        # VAD settings
        vad = self.config.get("vad", {})
        self.vad_threshold.setValue(vad.get("threshold", 0.2))
        self.vad_min_silence.setValue(vad.get("min_silence_ms", 1200))

        # Local polish
        local_polish = self.config.get("local_polish", {})
        self.local_model_path.setText(local_polish.get("model_path", ""))

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
        old_whisper = self.config.get("whisper", {})
        old_vad = self.config.get("vad", {})

        # === General tab ===
        if "general" not in self.config:
            self.config["general"] = {}
        hotkey_seq = self.hotkey_edit.keySequence().toString()
        self.config["general"]["hotkey"] = hotkey_seq if hotkey_seq else "grave"
        self.config["general"]["audio_device"] = self.audio_device.currentText()
        self.config["general"]["auto_startup"] = self.chk_startup.isChecked()
        self.config["general"]["minimize_to_tray"] = self.chk_minimize.isChecked()

        # === Hotwords tab ===
        # Note: enable_initial_prompt is always true (no UI control needed)
        self.config["enable_initial_prompt"] = True
        self.config["domain_context"] = self.domain_ctx.text()

        # Hotwords (use "hotwords" key, remove legacy "prompt_words" if present)
        hotwords = []
        for i in range(self.prompt_words_list.count()):
            hotwords.append(self.prompt_words_list.item(i).text())
        self.config["hotwords"] = hotwords
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

        # === Advanced tab - Whisper ===
        if "whisper" not in self.config:
            self.config["whisper"] = {}
        new_whisper_model = self.whisper_model.currentText()
        new_whisper_device = self.whisper_device.currentText()
        new_whisper_lang = self.whisper_language.currentText()

        if (
            old_whisper.get("model") != new_whisper_model
            or old_whisper.get("device") != new_whisper_device
            or old_whisper.get("language") != new_whisper_lang
        ):
            restart_needed = True

        self.config["whisper"]["model"] = new_whisper_model
        self.config["whisper"]["device"] = new_whisper_device
        self.config["whisper"]["language"] = new_whisper_lang

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
                    "设置已保存。\n\n⚠️ Whisper/VAD 设置更改需要重启应用才能生效。",
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
