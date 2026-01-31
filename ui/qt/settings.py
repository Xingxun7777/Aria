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
from aria.core.utils import get_config_path
from aria.core.hotword.utils import is_english_word

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
from aria.core.hotword import DEFAULT_POLISH_PROMPT
from aria.core.utils.phonetic import get_matcher

# Whisper 模型大小参考（用于提示用户）
WHISPER_MODEL_SIZES = {
    "large-v3-turbo": "1.5GB",
    "large-v3": "3GB",
    "medium": "1.5GB",
    "small": "500MB",
}


def check_whisper_model_exists(model_name: str) -> bool:
    """检查 Whisper 模型是否已下载到本地缓存。"""
    import os

    # faster-whisper 默认缓存路径
    cache_dir = (
        Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    )

    # 模型目录名称模式
    model_patterns = {
        "large-v3-turbo": "models--Systran--faster-whisper-large-v3-turbo",
        "large-v3": "models--Systran--faster-whisper-large-v3",
        "medium": "models--Systran--faster-whisper-medium",
        "small": "models--Systran--faster-whisper-small",
    }

    pattern = model_patterns.get(model_name)
    if pattern and (cache_dir / pattern).exists():
        return True
    return False


# Whisper 模型大小（字节，用于磁盘空间检测）
WHISPER_MODEL_BYTES = {
    "large-v3-turbo": 1.6 * 1024 * 1024 * 1024,  # 1.6GB
    "large-v3": 3.2 * 1024 * 1024 * 1024,  # 3.2GB
    "medium": 1.6 * 1024 * 1024 * 1024,  # 1.6GB
    "small": 0.5 * 1024 * 1024 * 1024,  # 0.5GB
}


def check_disk_space_for_whisper(model_name: str) -> tuple[bool, str]:
    """
    检查磁盘空间是否足够下载 Whisper 模型。

    Returns:
        (is_sufficient, message): 是否足够，提示信息
    """
    import os
    import shutil

    # 获取缓存目录
    cache_dir = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))

    # 确保目录存在（否则无法获取磁盘信息）
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 获取磁盘使用情况
        disk_usage = shutil.disk_usage(cache_dir)
        free_bytes = disk_usage.free
        free_gb = free_bytes / (1024 * 1024 * 1024)

        # 获取模型所需空间（加 20% 余量）
        required_bytes = WHISPER_MODEL_BYTES.get(model_name, 1.6 * 1024 * 1024 * 1024)
        required_with_margin = required_bytes * 1.2
        required_gb = required_with_margin / (1024 * 1024 * 1024)

        if free_bytes >= required_with_margin:
            return True, f"可用空间: {free_gb:.1f}GB"
        else:
            return False, (
                f"磁盘空间不足！\n"
                f"需要: {required_gb:.1f}GB\n"
                f"可用: {free_gb:.1f}GB\n"
                f"缓存目录: {cache_dir}"
            )
    except Exception as e:
        # 无法检测时默认允许继续
        return True, f"无法检测磁盘空间: {e}"


def check_faster_whisper_installed() -> bool:
    """检查 faster-whisper 是否已安装。"""
    try:
        import faster_whisper  # noqa: F401

        return True
    except ImportError:
        return False


def install_faster_whisper(parent=None) -> tuple[bool, str]:
    """
    动态安装 faster-whisper 包。

    Args:
        parent: 父窗口（用于显示进度对话框）

    Returns:
        (success, message): 是否成功，消息
    """
    import subprocess
    import sys

    # 显示安装进度对话框
    from PySide6.QtWidgets import QProgressDialog
    from PySide6.QtCore import Qt

    progress = QProgressDialog(
        "正在安装 Whisper 引擎依赖...\n这可能需要 1-2 分钟",
        None,  # 不显示取消按钮
        0,
        0,  # 不确定进度
        parent,
    )
    progress.setWindowTitle("安装依赖")
    progress.setWindowModality(Qt.WindowModal)
    progress.setMinimumDuration(0)
    progress.show()

    try:
        # 使用当前 Python 解释器的 pip 安装
        # 使用清华镜像加速
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "faster-whisper",
                "-i",
                "https://pypi.tuna.tsinghua.edu.cn/simple",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=300,  # 5 分钟超时
        )

        progress.close()

        if result.returncode == 0:
            return True, "faster-whisper 安装成功"
        else:
            error_msg = result.stderr or result.stdout or "未知错误"
            return False, f"安装失败: {error_msg}"

    except subprocess.TimeoutExpired:
        progress.close()
        return False, "安装超时（超过 5 分钟）"
    except Exception as e:
        progress.close()
        return False, f"安装出错: {e}"


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
        self.setWindowTitle("Aria 设置")
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
        self.chk_auto_startup.setToolTip("在 Windows 启动时自动运行 Aria")
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

        guide_label = QLabel("权重: 0=禁用, 0.3=提示(仅ASR), 0.5=参考, 1=锁定")
        guide_label.setStyleSheet("color: #666; font-size: 12px; margin-bottom: 5px;")
        layout.addWidget(guide_label)

        # Threshold note with weight explanation
        threshold_note = QLabel(
            "💡 中文热词: 权重≥0.5 时生效\n"
            "💡 英文热词: 0.5=参考(严格规则), 1.0=锁定(强制替换)，标记为 EN"
        )
        threshold_note.setStyleSheet(
            "color: #888; font-size: 11px; margin-bottom: 10px;"
        )
        threshold_note.setWordWrap(True)
        layout.addWidget(threshold_note)

        # Table widget with word and weight columns
        self.vocab_table = QTableWidget(0, 3)
        self.vocab_table.setHorizontalHeaderLabels(["词汇", "权重", "类型"])
        self.vocab_table.horizontalHeader().setStretchLastSection(False)
        self.vocab_table.setColumnWidth(0, 200)  # Word column
        self.vocab_table.setColumnWidth(1, 130)  # Weight dropdown column
        self.vocab_table.setColumnWidth(2, 40)  # EN label column
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

    def _is_english_word(self, word: str) -> bool:
        """Check if a word is primarily English (no CJK characters)."""
        # Use shared utility function (DRY - single source of truth)
        return is_english_word(word)

    def _add_vocab_row(self, word: str, weight: float = 1.0):
        """Add a vocabulary row with word and weight dropdown."""
        row = self.vocab_table.rowCount()
        self.vocab_table.insertRow(row)
        self.vocab_table.setRowHeight(row, 36)  # Ensure enough height for ComboBox

        # Word column (read-only)
        word_item = QTableWidgetItem(word)
        word_item.setFlags(word_item.flags() & ~Qt.ItemIsEditable)
        self.vocab_table.setItem(row, 0, word_item)

        # Simplified 4-tier weight options (v3.0):
        # - 0: disabled
        # - 0.3: hint (ASR only, not sent to polish)
        # - 0.5: reference (sent to polish for Chinese words)
        # - 1.0: lock (mandatory, always sent to polish)
        weight_options = [
            (0, "0 - 禁用"),
            (0.3, "0.3 - 提示"),
            (0.5, "0.5 - 参考"),
            (1.0, "1 - 锁定"),
        ]

        # Check if English word (for display hint only)
        # v3.1: English hotwords at 0.5 now work in polish layer with stricter rules
        # No longer auto-upgrade to 1.0
        is_english = self._is_english_word(word)

        # Find closest weight option
        closest_idx = 3  # Default to "1 - 锁定" (index 3)
        min_diff = float("inf")
        for i, (val, _) in enumerate(weight_options):
            diff = abs(val - weight)
            if diff < min_diff:
                min_diff = diff
                closest_idx = i

        # Weight dropdown (ComboBox)
        combo = QComboBox()
        combo.setMinimumWidth(120)
        combo.setMinimumHeight(28)
        # Add items with display text only
        for val, text in weight_options:
            combo.addItem(text)
        combo.setCurrentIndex(closest_idx)
        # Store mapping for later use
        combo.setProperty("weight_values", [v for v, _ in weight_options])
        self.vocab_table.setCellWidget(row, 1, combo)

        # Show hint label for English words
        hint_label = QLabel("")
        if is_english:
            hint_label.setText("EN")
            hint_label.setStyleSheet(
                "color: #0d6efd; font-size: 10px; font-weight: bold;"
            )
            hint_label.setToolTip(
                "英文热词：0.5 参考级使用更严格规则，1.0 锁定级强制替换"
            )
        self.vocab_table.setCellWidget(row, 2, hint_label)

        # Connect dropdown to store weight
        def on_combo_change(index, w=word, opts=weight_options):
            value = opts[index][0]  # Get actual float value from options
            self._hotword_weights[w] = value

        combo.currentIndexChanged.connect(on_combo_change)

        # Store initial weight
        self._hotword_weights[word] = weight_options[closest_idx][0]

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

    def _on_engine_changed(self, index: int):
        """Handle ASR engine selection change - show/hide corresponding settings."""
        is_whisper = index == 1
        self.funasr_group.setVisible(not is_whisper)
        self.whisper_group.setVisible(is_whisper)

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

        # === ASR Engine Selection ===
        engine_group = QGroupBox("语音识别引擎")
        engine_layout = QFormLayout(engine_group)

        self.engine_combo = QComboBox()
        self.engine_combo.addItems(["FunASR (推荐，中文优化)", "Whisper (多语言支持)"])
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        engine_layout.addRow("引擎:", self.engine_combo)

        engine_info = QLabel("切换引擎后需要重启应用")
        engine_info.setStyleSheet("color: #ff8c00; font-size: 12px;")
        engine_layout.addRow("", engine_info)

        layout.addWidget(engine_group)

        # === FunASR Settings (visible when FunASR selected) ===
        self.funasr_group = QGroupBox("FunASR 设置")
        funasr_layout = QFormLayout(self.funasr_group)

        self.funasr_model = QComboBox()
        self.funasr_model.addItems(
            [
                "大模型 (paraformer-zh) - 推荐，准确度高",
                "小模型 (SenseVoice) - 显存<8GB时使用",
            ]
        )
        self.funasr_model.setCurrentIndex(0)
        funasr_layout.addRow("模型:", self.funasr_model)

        self.funasr_device = QComboBox()
        self.funasr_device.addItems(["cuda", "cpu"])
        funasr_layout.addRow("设备:", self.funasr_device)

        funasr_info = QLabel("大模型约需3GB显存，小模型约需1.5GB显存")
        funasr_info.setStyleSheet("color: #888; font-size: 12px;")
        funasr_layout.addRow("", funasr_info)

        layout.addWidget(self.funasr_group)

        # === Whisper Settings (visible when Whisper selected) ===
        self.whisper_group = QGroupBox("Whisper 设置")
        whisper_layout = QFormLayout(self.whisper_group)

        self.whisper_model = QComboBox()
        self.whisper_model.addItems(
            [
                "large-v3-turbo - 推荐，速度快",
                "large-v3 - 最高准确度，较慢",
                "medium - 中等，适合弱显卡",
                "small - 快速，准确度较低",
            ]
        )
        self.whisper_model.setCurrentIndex(0)
        whisper_layout.addRow("模型:", self.whisper_model)

        self.whisper_device = QComboBox()
        self.whisper_device.addItems(["cuda", "cpu"])
        whisper_layout.addRow("设备:", self.whisper_device)

        self.whisper_compute = QComboBox()
        self.whisper_compute.addItems(["float16 - 推荐", "int8 - 省显存"])
        whisper_layout.addRow("精度:", self.whisper_compute)

        whisper_info = QLabel("首次使用需下载模型（约1-3GB），请耐心等待")
        whisper_info.setStyleSheet("color: #888; font-size: 12px;")
        whisper_layout.addRow("", whisper_info)

        layout.addWidget(self.whisper_group)
        self.whisper_group.setVisible(False)  # Default: hidden

        # Legacy compatibility: keep old names for save_config detection
        self.asr_model = self.funasr_model
        self.asr_device = self.funasr_device

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

        # Output settings (typewriter mode for game compatibility)
        output_group = QGroupBox("输出设置")
        output_layout = QVBoxLayout(output_group)

        self.chk_typewriter_mode = QCheckBox("打字机模式 (逐字符输入)")
        self.chk_typewriter_mode.setToolTip(
            "适用于不支持 Ctrl+V 粘贴的应用程序\n"
            "开启后将逐字符发送，速度较慢但兼容性更好"
        )
        output_layout.addWidget(self.chk_typewriter_mode)

        # Warning about limitations
        typewriter_hint = QLabel("⚠️ 此模式适用于不支持 Ctrl+V 的普通应用")
        typewriter_hint.setStyleSheet(
            "color: #ff8c00; font-size: 11px; margin-left: 20px;"
        )
        output_layout.addWidget(typewriter_hint)

        typewriter_warn1 = QLabel("❌ 对使用 DirectInput 的游戏（大多数 3D 游戏）无效")
        typewriter_warn1.setStyleSheet(
            "color: #888; font-size: 11px; margin-left: 20px;"
        )
        output_layout.addWidget(typewriter_warn1)

        typewriter_warn2 = QLabel("⛔ 在反作弊游戏中使用可能导致账号封禁！")
        typewriter_warn2.setStyleSheet(
            "color: #dc3545; font-size: 11px; margin-left: 20px;"
        )
        output_layout.addWidget(typewriter_warn2)

        output_layout.addSpacing(10)

        self.chk_elevation_check = QCheckBox("权限检测 (检测高权限窗口)")
        self.chk_elevation_check.setToolTip(
            "检测目标窗口是否以管理员权限运行\n"
            "如果 Aria 权限低于目标窗口，会提示用户"
        )
        self.chk_elevation_check.setChecked(True)  # Default enabled
        output_layout.addWidget(self.chk_elevation_check)

        layout.addWidget(output_group)

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
        # Convert Aria hotkey format to Qt format
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
        # ASR engine selection
        asr_engine = self.config.get("asr_engine", "funasr")
        if asr_engine == "whisper":
            self.engine_combo.setCurrentIndex(1)
        else:
            self.engine_combo.setCurrentIndex(0)
        # Trigger visibility update
        self._on_engine_changed(self.engine_combo.currentIndex())

        # FunASR settings
        funasr = self.config.get("funasr", {})
        funasr_model = funasr.get("model_name", "paraformer-zh")
        # Map model name to combo index (0=large/paraformer, 1=small/sensevoice)
        if "sensevoice" in funasr_model.lower():
            self.funasr_model.setCurrentIndex(1)
        else:
            self.funasr_model.setCurrentIndex(0)

        funasr_device = funasr.get("device", "cuda")
        idx = self.funasr_device.findText(funasr_device)
        if idx >= 0:
            self.funasr_device.setCurrentIndex(idx)

        # Whisper settings
        whisper = self.config.get("whisper", {})
        whisper_model = whisper.get("model_name", "large-v3-turbo")
        # Map model name to combo index
        whisper_model_map = {
            "large-v3-turbo": 0,
            "large-v3": 1,
            "medium": 2,
            "small": 3,
        }
        self.whisper_model.setCurrentIndex(whisper_model_map.get(whisper_model, 0))

        whisper_device = whisper.get("device", "cuda")
        idx = self.whisper_device.findText(whisper_device)
        if idx >= 0:
            self.whisper_device.setCurrentIndex(idx)

        whisper_compute = whisper.get("compute_type", "float16")
        if "int8" in whisper_compute:
            self.whisper_compute.setCurrentIndex(1)
        else:
            self.whisper_compute.setCurrentIndex(0)

        # VAD settings
        vad = self.config.get("vad", {})
        self.vad_threshold.setValue(vad.get("threshold", 0.2))
        self.vad_min_silence.setValue(vad.get("min_silence_ms", 1200))

        # Local polish
        local_polish = self.config.get("local_polish", {})
        self.local_model_path.setText(local_polish.get("model_path", ""))

        # === Output settings ===
        output_cfg = self.config.get("output", {})
        self.chk_typewriter_mode.setChecked(output_cfg.get("typewriter_mode", False))
        self.chk_elevation_check.setChecked(output_cfg.get("check_elevation", True))

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
        old_engine = self.config.get("asr_engine", "funasr")
        old_funasr = self.config.get("funasr", {})
        old_whisper = self.config.get("whisper", {})
        old_vad = self.config.get("vad", {})

        # === General tab ===
        if "general" not in self.config:
            self.config["general"] = {}
        hotkey_seq = self.hotkey_edit.keySequence().toString()
        # Convert Qt format to Aria format for storage
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
        # Auto-enable API polish when quality mode is selected
        self.config["polish"]["enabled"] = self.radio_quality.isChecked()
        self.config["polish"]["api_url"] = self.api_url.text()
        self.config["polish"]["api_key"] = self.api_key.text()
        self.config["polish"]["model"] = self.model.text()
        self.config["polish"]["timeout"] = self.timeout.value()
        self.config["polish"]["prompt_template"] = self.prompt_edit.toPlainText()

        # === Advanced tab - ASR Engine Selection ===
        new_engine = "funasr" if self.engine_combo.currentIndex() == 0 else "whisper"
        if old_engine != new_engine:
            restart_needed = True

        # 切换到 Whisper 时的完整检查流程
        if new_engine == "whisper" and old_engine != "whisper":
            whisper_model_map = ["large-v3-turbo", "large-v3", "medium", "small"]
            whisper_model = whisper_model_map[self.whisper_model.currentIndex()]
            model_size = WHISPER_MODEL_SIZES.get(whisper_model, "1.5GB")

            # Step 1: 检查 faster-whisper 是否已安装
            if not check_faster_whisper_installed():
                reply = QMessageBox.question(
                    self,
                    "需要安装依赖",
                    "切换到 Whisper 需要安装 faster-whisper 引擎。\n\n"
                    "是否现在安装？（约 100MB，需要 1-2 分钟）",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
                )

                if reply == QMessageBox.Yes:
                    success, msg = install_faster_whisper(self)
                    if not success:
                        QMessageBox.critical(
                            self,
                            "安装失败",
                            f"faster-whisper 安装失败:\n\n{msg}\n\n"
                            "请检查网络连接后重试。",
                        )
                        return  # 安装失败，不保存配置
                    else:
                        QMessageBox.information(
                            self, "安装成功", "Whisper 引擎依赖已安装成功！"
                        )
                else:
                    # 用户取消安装，恢复引擎选择
                    self.engine_combo.setCurrentIndex(0)  # 恢复为 FunASR
                    return

            # Step 2: 检查模型是否已存在
            if not check_whisper_model_exists(whisper_model):
                # Step 2a: 检查磁盘空间
                has_space, space_msg = check_disk_space_for_whisper(whisper_model)

                if not has_space:
                    # 磁盘空间不足，显示警告
                    QMessageBox.warning(
                        self,
                        "磁盘空间不足",
                        f"无法下载 Whisper 模型:\n\n{space_msg}\n\n"
                        "请清理磁盘空间后重试。",
                    )
                else:
                    # Step 2b: 空间足够，显示下载提醒
                    QMessageBox.information(
                        self,
                        "首次使用 Whisper",
                        f"下次启动时将自动下载 Whisper 模型:\n\n"
                        f"模型: {whisper_model}\n"
                        f"大小: 约 {model_size}\n"
                        f"预计时间: 2-5 分钟\n\n"
                        "请确保网络连接正常。\n"
                        "(已自动配置国内镜像加速)",
                    )

        self.config["asr_engine"] = new_engine

        # === Advanced tab - FunASR ===
        if "funasr" not in self.config:
            self.config["funasr"] = {}

        # Map combo index to model name
        funasr_model_idx = self.funasr_model.currentIndex()
        new_funasr_model = (
            "paraformer-zh" if funasr_model_idx == 0 else "iic/SenseVoiceSmall"
        )
        new_funasr_device = self.funasr_device.currentText()

        if (
            old_funasr.get("model_name") != new_funasr_model
            or old_funasr.get("device") != new_funasr_device
        ):
            restart_needed = True

        self.config["funasr"]["model_name"] = new_funasr_model
        self.config["funasr"]["device"] = new_funasr_device

        # === Advanced tab - Whisper ===
        if "whisper" not in self.config:
            self.config["whisper"] = {}

        # Map combo index to model name
        whisper_model_map = ["large-v3-turbo", "large-v3", "medium", "small"]
        new_whisper_model = whisper_model_map[self.whisper_model.currentIndex()]
        new_whisper_device = self.whisper_device.currentText()
        new_whisper_compute = (
            "int8" if self.whisper_compute.currentIndex() == 1 else "float16"
        )

        if (
            old_whisper.get("model_name") != new_whisper_model
            or old_whisper.get("device") != new_whisper_device
            or old_whisper.get("compute_type") != new_whisper_compute
        ):
            restart_needed = True

        self.config["whisper"]["model_name"] = new_whisper_model
        self.config["whisper"]["device"] = new_whisper_device
        self.config["whisper"]["compute_type"] = new_whisper_compute
        self.config["whisper"]["language"] = "zh"  # Default to Chinese

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

        # === Output settings ===
        if "output" not in self.config:
            self.config["output"] = {}
        self.config["output"]["typewriter_mode"] = self.chk_typewriter_mode.isChecked()
        self.config["output"]["check_elevation"] = self.chk_elevation_check.isChecked()
        # Keep existing typewriter_delay_ms if set, otherwise use default
        if "typewriter_delay_ms" not in self.config["output"]:
            self.config["output"]["typewriter_delay_ms"] = 15

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
        return startup_folder / "Aria.lnk"

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
                # Find Aria.vbs launcher
                project_dir = Path(__file__).parent.parent.parent
                launcher = project_dir / "Aria.vbs"

                # For portable build, launcher is in dist folder
                if not launcher.exists():
                    # Try to find in parent directories
                    for parent in [project_dir.parent, project_dir.parent.parent]:
                        candidate = parent / "Aria.vbs"
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
$Shortcut.Description = "Aria - Local AI Voice Dictation"
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
        """Convert Aria hotkey format to Qt QKeySequence format."""
        # Mapping from Aria format to Qt format
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
        """Convert Qt QKeySequence format to Aria hotkey format."""
        if not qt_hotkey:
            return "grave"
        # Mapping from Qt format to Aria format
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
