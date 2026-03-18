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


def check_qwen_asr_installed() -> bool:
    """检查 qwen-asr 是否已安装。"""
    try:
        from aria.core.asr.qwen3_engine import check_qwen3_installation

        return check_qwen3_installation()
    except Exception:
        return False


def _is_portable_runtime() -> bool:
    """Check if running in portable (embedded Python) mode where pip is unavailable."""
    import sys

    # Portable build has no pip module and runs from _internal directory
    exe_path = sys.executable or ""
    return "_internal" in exe_path or "dist_portable" in exe_path


def install_qwen_asr(parent=None) -> tuple[bool, str]:
    """
    动态安装 qwen-asr 包。

    Args:
        parent: 父窗口（用于显示进度对话框）

    Returns:
        (success, message): 是否成功，消息
    """
    import subprocess
    import sys

    # Portable mode: pip is not available, qwen-asr should be pre-bundled
    if _is_portable_runtime():
        return False, (
            "便携版不支持动态安装依赖。\n"
            "qwen-asr 应已包含在便携包中。\n"
            "如仍缺失，请重新下载完整的便携包。"
        )

    # 显示安装进度对话框
    from PySide6.QtWidgets import QProgressDialog
    from PySide6.QtCore import Qt

    progress = QProgressDialog(
        "正在安装 Qwen3-ASR 引擎依赖...\n这可能需要 1-2 分钟",
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
                "qwen-asr",
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
            return True, "qwen-asr 安装成功"
        else:
            error_msg = result.stderr or result.stdout or "未知错误"
            return False, f"安装失败: {error_msg}"

    except subprocess.TimeoutExpired:
        progress.close()
        return False, "安装超时（超过 5 分钟）"
    except Exception as e:
        progress.close()
        return False, f"安装出错: {e}"


# Qwen3 模型大小参考
QWEN3_MODEL_SIZES = {
    "Qwen/Qwen3-ASR-1.7B": "3.4GB",
    "Qwen/Qwen3-ASR-0.6B": "1.2GB",
}


def check_qwen3_model_exists(model_name: str) -> bool:
    """
    检查 Qwen3 模型是否已下载到本地缓存。

    Args:
        model_name: 模型名称，如 "Qwen/Qwen3-ASR-1.7B"

    Returns:
        True 如果模型已存在
    """
    import os
    from pathlib import Path

    # HuggingFace 默认缓存路径
    cache_dir = (
        Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    )

    # 模型目录名称模式: models--{org}--{model}
    # e.g., "Qwen/Qwen3-ASR-1.7B" -> "models--Qwen--Qwen3-ASR-1.7B"
    model_dir_name = f"models--{model_name.replace('/', '--')}"
    model_path = cache_dir / model_dir_name

    if model_path.exists():
        # 检查是否有 snapshots 目录（表示模型已完整下载）
        snapshots = model_path / "snapshots"
        if snapshots.exists() and any(snapshots.iterdir()):
            return True
    return False


def get_gpu_vram_mb() -> int | None:
    """
    获取 GPU 显存大小（MB）。

    Returns:
        显存大小 MB，或 None 如果无法检测
    """
    try:
        import torch

        if torch.cuda.is_available():
            # 获取第一个 GPU 的总显存
            props = torch.cuda.get_device_properties(0)
            return props.total_memory // (1024 * 1024)
    except Exception:
        pass
    return None


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

            # Build URL: handle various input formats
            api_url = self.api_url.rstrip("/")
            if api_url.endswith("/v1/chat/completions"):
                full_url = api_url
            elif api_url.endswith("/v1"):
                full_url = f"{api_url}/chat/completions"
            else:
                full_url = f"{api_url}/v1/chat/completions"

            response = requests.post(
                full_url,
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
        self._theme = styles.get_theme_palette()
        self.setWindowTitle("Aria 设置")
        self.resize(900, 650)
        self.setStyleSheet(styles.get_settings_stylesheet())

        self.config_path = config_path or get_config_path("hotwords.json")
        self.config = {}

        self._init_ui()
        self.load_config()

    def _label_style(
        self,
        role: str = "secondary",
        *,
        font_size: int = 11,
        extra: str = "",
        bold: bool = False,
    ) -> str:
        role_colors = {
            "primary": self._theme.text_primary,
            "secondary": self._theme.text_secondary,
            "muted": self._theme.text_muted,
            "accent": self._theme.accent,
            "warning": "#B45309" if self._theme.name == "light" else "#F59E0B",
        }
        color = role_colors.get(role, self._theme.text_secondary)
        weight = " font-weight: bold;" if bold else ""
        suffix = f" {extra.strip()}" if extra.strip() else ""
        return f"color: {color}; font-size: {font_size}px;{weight}{suffix}"

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
            self._label_style("muted", font_size=11, extra="margin-left: 50px;")
        )
        wakeword_layout.addWidget(self.pinyin_hint)

        # Example commands hint
        example_hint = QLabel('例: "瑶瑶开启自动发送"、"瑶瑶休眠"')
        example_hint.setStyleSheet(
            self._label_style("secondary", font_size=11, extra="margin-top: 5px;")
        )
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

        translate_hint = QLabel('"翻译成英文/中文" 命令的结果输出方式')
        translate_hint.setStyleSheet(self._label_style("secondary"))
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
        subtitle.setStyleSheet(
            self._label_style("secondary", extra="margin-bottom: 10px;")
        )
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

        hint_label = QLabel("描述您的使用领域，可提高整体识别准确率（可选）")
        hint_label.setStyleSheet(self._label_style("muted", extra="margin-left: 70px;"))
        layout.addWidget(hint_label)

        layout.addSpacing(20)

        # --- Main: Vocabulary list with weights ---
        list_header = QLabel("<b>词汇列表</b>")
        layout.addWidget(list_header)

        # Store reference for dynamic update based on engine
        self._hotword_guide_label = QLabel(
            "权重越高，识别偏向越强。新词默认 0.3（轻量提示）"
        )
        self._hotword_guide_label.setStyleSheet(
            self._label_style("secondary", font_size=12, extra="margin-bottom: 5px;")
        )
        layout.addWidget(self._hotword_guide_label)

        # Threshold note with weight explanation (dynamic based on engine)
        self._hotword_threshold_note = QLabel(
            "0 禁用：完全排除，不参与任何流程\n"
            "0.1 谨慎：不影响语音识别，仅在 AI 润色时严格约束（只纠正乱码）\n"
            "0.3 仅润色：不影响语音识别，仅在 AI 润色时作为参考\n"
            "0.5 标准：进入语音识别 + 正则替换 + AI 润色\n"
            "1 强制：识别偏置最大化 + 拼音模糊匹配 + 强制替换"
        )
        self._hotword_threshold_note.setStyleSheet(
            self._label_style("muted", font_size=11, extra="margin-bottom: 10px;")
        )
        self._hotword_threshold_note.setWordWrap(True)
        layout.addWidget(self._hotword_threshold_note)

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
        self.advanced_group = QGroupBox("高级选项 - 手动纠错规则")
        self.advanced_group.setCheckable(True)
        self.advanced_group.setChecked(False)  # Default collapsed
        advanced_layout = QVBoxLayout()

        adv_hint = QLabel(
            "大部分谐音错误会被自动纠正。只有在遇到重复识别问题时才需要手动添加规则。"
        )
        adv_hint.setStyleSheet(self._label_style("secondary"))
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

        # 5-tier weight options (v3.4):
        # - 0: disabled (excluded from all layers)
        # - 0.1: cautious (no ASR bias, L4 polish strict constraint only)
        # - 0.3: hint (post-processing only, excluded from Qwen3 ASR context) — default for new words
        # - 0.5: reference (ASR context + regex + polish)
        # - 1.0: lock (ASR 3x repeat + pinyin fuzzy + force replace)
        weight_options = [
            (0, "0 - 禁用"),
            (0.1, "0.1 - 谨慎"),
            (0.3, "0.3 - 仅润色"),
            (0.5, "0.5 - 标准"),
            (1.0, "1 - 强制"),
        ]

        # Check if English word (for display hint only)
        # v3.1: English hotwords at 0.5 now work in polish layer with stricter rules
        # No longer auto-upgrade to 1.0
        is_english = self._is_english_word(word)

        # Find closest weight option
        closest_idx = 3  # Initial value, overwritten by loop below
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
                self._label_style("accent", font_size=10, bold=True)
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
            self._add_vocab_row(word, 0.3)

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
                    self._add_vocab_row(word, 0.3)
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
        self.radio_off = QRadioButton("关闭润色 (直接输出识别结果)")
        self.radio_fast = QRadioButton("本地润色 (需自行配置模型)")
        self.radio_quality = QRadioButton("高质量模式 (Gemini API, ~1.7s)")
        mode_group.addButton(self.radio_off)
        mode_group.addButton(self.radio_fast)
        mode_group.addButton(self.radio_quality)

        layout.addWidget(self.radio_off)
        layout.addWidget(self.radio_fast)
        layout.addWidget(self.radio_quality)

        layout.addSpacing(20)

        # v1.2: 润色偏好（个性化偏好 + 一键开关）
        skill_group = QGroupBox("润色偏好")
        skill_layout = QVBoxLayout(skill_group)

        # 口语过滤开关
        self.chk_filter_filler = QCheckBox("口语过滤")
        self.chk_filter_filler.setChecked(True)
        filler_hint = QLabel('自动去除"嗯"、"那个"、"就是说"等口语填充词')
        filler_hint.setStyleSheet("color: #888; font-size: 12px; margin-left: 24px;")
        skill_layout.addWidget(self.chk_filter_filler)
        skill_layout.addWidget(filler_hint)

        skill_layout.addSpacing(8)

        # 自动结构化开关
        self.chk_auto_structure = QCheckBox("自动结构化")
        self.chk_auto_structure.setChecked(False)
        structure_hint = QLabel("将口述长文本自动整理为带换行、编号的结构化文本")
        structure_hint.setStyleSheet("color: #888; font-size: 12px; margin-left: 24px;")
        skill_layout.addWidget(self.chk_auto_structure)
        skill_layout.addWidget(structure_hint)

        skill_layout.addSpacing(12)

        # 个性化规则
        rules_label = QLabel("个性化规则（每行一条）：")
        skill_layout.addWidget(rules_label)
        self.personalization_rules_edit = QPlainTextEdit()
        self.personalization_rules_edit.setPlaceholderText(
            "例如：\n不要把口语化的表达改成书面语\n英文专有名词保留原始大小写\n每句话单独成段"
        )
        self.personalization_rules_edit.setMaximumHeight(100)
        skill_layout.addWidget(self.personalization_rules_edit)

        layout.addWidget(skill_group)

        layout.addSpacing(20)

        # Reply style (for "帮我回复" feature)
        reply_group = QGroupBox("回复风格")
        reply_layout = QVBoxLayout(reply_group)
        reply_hint = QLabel(
            '设定 AI 回复消息时的风格偏好（选中文字说"帮我回复"时生效）'
        )
        reply_hint.setStyleSheet("color: #888; font-size: 12px;")
        reply_layout.addWidget(reply_hint)
        self.reply_style_edit = QPlainTextEdit()
        self.reply_style_edit.setPlaceholderText(
            "例如：\n回复简短一些，像朋友聊天\n语气专业正式\n用轻松幽默的方式回复"
        )
        self.reply_style_edit.setMaximumHeight(80)
        reply_layout.addWidget(self.reply_style_edit)
        layout.addWidget(reply_group)

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

        # === 主 API 设置 ===
        main_group = QGroupBox("主 API（默认）")
        main_form = QFormLayout(main_group)

        self.api_url = QLineEdit()
        self.api_url.setPlaceholderText("http://localhost:3000")
        main_form.addRow("API 地址:", self.api_url)

        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("sk-...")
        main_form.addRow("API 密钥:", self.api_key)

        self.model = QLineEdit()
        self.model.setPlaceholderText("google/gemini-2.5-flash-lite-preview-09-2025")
        main_form.addRow("模型名称:", self.model)

        self.timeout = QSpinBox()
        self.timeout.setRange(5, 120)
        self.timeout.setValue(10)  # Match PolishConfig default (10s)
        self.timeout.setSuffix(" 秒")
        main_form.addRow("超时时间:", self.timeout)

        layout.addWidget(main_group)

        # === 备用 API 设置（智能轮询） ===
        backup_group = QGroupBox("备用 API（智能轮询）")
        backup_layout = QVBoxLayout(backup_group)

        backup_hint = QLabel(
            "当主 API 连续响应慢或出错时，自动切换到备用 API。\n"
            "每次程序启动默认使用主 API。"
        )
        backup_hint.setStyleSheet(
            self._label_style("secondary", extra="margin-bottom: 10px;")
        )
        backup_hint.setWordWrap(True)
        backup_layout.addWidget(backup_hint)

        backup_form = QFormLayout()

        self.api_url_backup = QLineEdit()
        self.api_url_backup.setPlaceholderText("留空则不启用备用 API")
        backup_form.addRow("备用 API 地址:", self.api_url_backup)

        self.api_key_backup = QLineEdit()
        self.api_key_backup.setEchoMode(QLineEdit.Password)
        self.api_key_backup.setPlaceholderText("留空则使用主 API 密钥")
        backup_form.addRow("备用 API 密钥:", self.api_key_backup)

        self.model_backup = QLineEdit()
        self.model_backup.setPlaceholderText("留空则使用主模型")
        backup_form.addRow("备用模型名称:", self.model_backup)

        backup_layout.addLayout(backup_form)

        # 轮询参数
        polling_layout = QHBoxLayout()

        polling_layout.addWidget(QLabel("慢响应阈值:"))
        self.slow_threshold = QSpinBox()
        self.slow_threshold.setRange(1000, 30000)
        self.slow_threshold.setValue(3000)
        self.slow_threshold.setSuffix(" ms")
        self.slow_threshold.setToolTip("响应时间超过此值视为慢")
        polling_layout.addWidget(self.slow_threshold)

        polling_layout.addSpacing(20)

        polling_layout.addWidget(QLabel("切换阈值:"))
        self.switch_count = QSpinBox()
        self.switch_count.setRange(1, 10)
        self.switch_count.setValue(2)
        self.switch_count.setSuffix(" 次")
        self.switch_count.setToolTip("连续慢响应达到此次数后切换 API")
        polling_layout.addWidget(self.switch_count)

        polling_layout.addStretch()
        backup_layout.addLayout(polling_layout)

        layout.addWidget(backup_group)

        layout.addSpacing(20)

        btn_layout = QHBoxLayout()
        self._api_test_button = QPushButton("测试主 API")
        self._api_test_button.clicked.connect(self._test_api_connection)
        btn_layout.addWidget(self._api_test_button)

        self._api_test_backup_button = QPushButton("测试备用 API")
        self._api_test_backup_button.clicked.connect(self._test_backup_api_connection)
        btn_layout.addWidget(self._api_test_backup_button)

        btn_save = QPushButton("保存 API 设置")
        btn_save.setObjectName("primaryBtn")
        btn_save.clicked.connect(self.save_config)
        btn_layout.addWidget(btn_save)

        layout.addLayout(btn_layout)

        layout.addStretch()
        return w

    def _test_backup_api_connection(self):
        """Test backup API connection."""
        api_url = self.api_url_backup.text().strip()
        if not api_url:
            QMessageBox.warning(self, "错误", "请先填写备用 API 地址")
            return

        # Use backup key if set, otherwise use main key
        api_key = self.api_key_backup.text().strip() or self.api_key.text().strip()
        # Use backup model if set, otherwise use main model
        model = self.model_backup.text().strip() or self.model.text().strip()

        # Prevent concurrent tests
        if (
            hasattr(self, "_api_thread")
            and self._api_thread is not None
            and self._api_thread.isRunning()
        ):
            return

        # Disable button during test
        self._api_test_backup_button.setEnabled(False)
        self._api_test_backup_button.setText("测试中...")

        # Create worker and thread
        self._api_thread = QThread()
        self._api_worker = ApiTestWorker(api_url, api_key, model)
        self._api_worker.moveToThread(self._api_thread)

        # Connect signals
        self._api_thread.started.connect(self._api_worker.run)
        self._api_worker.finished.connect(self._on_backup_api_test_finished)
        self._api_worker.finished.connect(self._api_thread.quit)
        self._api_worker.finished.connect(self._api_worker.deleteLater)
        self._api_thread.finished.connect(self._api_thread.deleteLater)

        # Start test
        self._api_thread.start()

    def _on_backup_api_test_finished(
        self, success: bool, message: str, status_code: int
    ):
        """Handle backup API test result."""
        self._api_test_backup_button.setEnabled(True)
        self._api_test_backup_button.setText("测试备用 API")

        if success:
            QMessageBox.information(
                self, "成功", f"备用 API 连接成功！\n\n状态码: {status_code}"
            )
        elif status_code > 0:
            QMessageBox.warning(
                self,
                "连接失败",
                f"备用 API 返回错误\n\n状态码: {status_code}\n响应: {message}",
            )
        else:
            QMessageBox.warning(self, "连接失败", message)

        # Clear thread reference AFTER all UI updates (allow future tests)
        self._api_thread = None

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
            self._api_test_button.setText("测试主 API")

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

        # Clear thread reference AFTER all UI updates (allow future tests)
        self._api_thread = None

    def _on_engine_changed(self, index: int):
        """Handle ASR engine selection change - show/hide corresponding settings."""
        # index: 0=FunASR, 1=Qwen3
        self.funasr_group.setVisible(index == 0)
        self.qwen3_group.setVisible(index == 1)

        # Update hotword explanation based on engine
        self._update_hotword_explanation(index)

    def _update_hotword_explanation(self, engine_index: int):
        """Update hotword explanation labels based on selected ASR engine."""
        # Check if labels exist (they are created in hotwords tab)
        if not hasattr(self, "_hotword_guide_label"):
            return

        if engine_index == 1:  # Qwen3
            self._hotword_guide_label.setText(
                "Qwen3 模式 — 权重越高，识别偏向越强。新词默认 0.3（轻量提示）"
            )
            self._hotword_threshold_note.setText(
                "0 禁用：完全排除，不参与任何流程\n"
                "0.1 谨慎：不影响语音识别，仅在 AI 润色时严格约束（只纠正乱码）\n"
                "0.3 仅润色：不进入语音识别，仅在 AI 润色时作为参考词\n"
                "0.5 标准：写入识别上下文（出现1次）+ 正则替换 + AI 润色\n"
                "1 强制：识别上下文中重复3次（最强偏置）+ 拼音模糊 + 强制替换"
            )
        else:  # FunASR
            self._hotword_guide_label.setText(
                "FunASR 模式 — 权重越高，识别偏向越强。新词默认 0.3（轻量提示）"
            )
            self._hotword_threshold_note.setText(
                "0 禁用：完全排除，不参与任何流程\n"
                "0.1 谨慎：不影响语音识别，仅在 AI 润色时严格约束（只纠正乱码）\n"
                "0.3 仅润色：ASR 弱提示（分数30）+ AI 润色参考\n"
                "0.5 标准：ASR 标准识别（分数60）+ 正则替换 + AI 润色\n"
                "1 强制：ASR 强锁定（分数100）+ 拼音模糊匹配 + 强制替换"
            )

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
        self.engine_combo.addItems(
            [
                "FunASR (中文优化，离线即用)",
                "Qwen3-ASR (推荐，52语言)",
            ]
        )
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        engine_layout.addRow("引擎:", self.engine_combo)

        engine_info = QLabel("切换引擎后需要重启应用")
        engine_info.setStyleSheet(self._label_style("warning", font_size=12))
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
        funasr_info.setStyleSheet(self._label_style("muted", font_size=12))
        funasr_layout.addRow("", funasr_info)

        layout.addWidget(self.funasr_group)

        # === Qwen3-ASR Settings (visible when Qwen3 selected) ===
        self.qwen3_group = QGroupBox("Qwen3-ASR 设置")
        qwen3_layout = QFormLayout(self.qwen3_group)

        self.qwen3_model = QComboBox()
        self.qwen3_model.addItems(
            [
                "自动选择 - 根据显存自动决定 (推荐)",
                "1.7B - 最高准确度，约4GB显存",
                "0.6B - 轻量快速，约2GB显存",
            ]
        )
        self.qwen3_model.setCurrentIndex(0)
        qwen3_layout.addRow("模型:", self.qwen3_model)

        self.qwen3_device = QComboBox()
        self.qwen3_device.addItems(["cuda", "cpu"])
        qwen3_layout.addRow("设备:", self.qwen3_device)

        self.qwen3_dtype = QComboBox()
        self.qwen3_dtype.addItems(
            ["bfloat16 - 推荐 (RTX 30/40/50系)", "float16 - 旧显卡兼容"]
        )
        qwen3_layout.addRow("精度:", self.qwen3_dtype)

        qwen3_info = QLabel(
            "Qwen3-ASR: 阿里最新语音识别模型\n"
            "• 支持52种语言/方言，中英文混合识别优秀\n"
            "• 首次使用需下载模型（1.7B约3.4GB，0.6B约1.2GB）"
        )
        qwen3_info.setStyleSheet(self._label_style("muted", font_size=12))
        qwen3_layout.addRow("", qwen3_info)

        layout.addWidget(self.qwen3_group)
        self.qwen3_group.setVisible(False)  # Default: hidden

        # Legacy compatibility: keep old names for save_config detection
        self.asr_model = self.funasr_model
        self.asr_device = self.funasr_device

        # VAD settings
        vad_group = QGroupBox("VAD (语音活动检测)")
        vad_layout = QFormLayout(vad_group)

        self.chk_noise_filter = QCheckBox("噪声过滤")
        self.chk_noise_filter.setChecked(True)
        self.chk_noise_filter.setToolTip(
            "过滤环境噪声产生的无意义文字（嗯、啊、呃等）\n"
            "不会影响正常短回复（好的、行、可以等）"
        )
        vad_layout.addRow(self.chk_noise_filter)

        self.chk_screen_ocr = QCheckBox("屏幕识别 → ASR")
        self.chk_screen_ocr.setChecked(True)
        self.chk_screen_ocr.setToolTip(
            "说话时自动识别屏幕文字，注入 ASR 识别上下文\n"
            "帮助更准确地识别屏幕上出现的专业术语和名词\n"
            "例如：屏幕显示「骨骼参数」时，语音就不会被识别为「谷歌参数」"
        )
        vad_layout.addRow(self.chk_screen_ocr)

        self.chk_screen_ocr_polish = QCheckBox("屏幕识别 → 润色 (实验性)")
        self.chk_screen_ocr_polish.setChecked(False)
        self.chk_screen_ocr_polish.setToolTip(
            "将屏幕文字同时传给润色层 LLM\n"
            "可帮助纠正英文术语的中文音译（如「布兰德」→「Blender」）\n"
            "但可能偶尔导致 LLM 输出屏幕内容，默认关闭"
        )
        polish_warn = QLabel(
            "注意：此功能为实验性，可能导致润色输出异常内容。"
            "已有安全保护，异常时会自动回退原文。"
        )
        polish_warn.setStyleSheet("color: #CC8800; font-size: 11px; margin-left: 24px;")
        polish_warn.setWordWrap(True)
        vad_layout.addRow(self.chk_screen_ocr_polish)
        vad_layout.addRow(polish_warn)

        self.vad_threshold = QDoubleSpinBox()
        self.vad_threshold.setRange(0.1, 0.9)
        self.vad_threshold.setSingleStep(0.1)
        self.vad_threshold.setValue(0.2)
        self.vad_threshold.setToolTip(
            "语音活动检测灵敏度\n"
            "值越小越灵敏，越容易检测到小声说话\n"
            "推荐: 0.2 (默认) | 安静环境: 0.1 | 嘈杂环境: 0.4"
        )
        vad_layout.addRow("语音检测阈值:", self.vad_threshold)

        self.vad_energy_threshold = QDoubleSpinBox()
        self.vad_energy_threshold.setRange(0.0005, 0.02)
        self.vad_energy_threshold.setSingleStep(0.0005)
        self.vad_energy_threshold.setDecimals(4)
        self.vad_energy_threshold.setValue(0.003)
        self.vad_energy_threshold.setToolTip(
            "音频能量门控 — 低于此值的音频直接丢弃，不送识别\n"
            "用于过滤键盘声、鼠标声等非语音触发\n"
            "想小声说话被识别: 调低此值 (如 0.001)\n"
            "推荐: 0.003 (默认) | 小声说话: 0.001 | 安静环境: 0.0005"
        )
        vad_layout.addRow("能量阈值:", self.vad_energy_threshold)

        self.vad_min_silence = QSpinBox()
        self.vad_min_silence.setRange(100, 2000)
        self.vad_min_silence.setValue(1200)
        self.vad_min_silence.setSuffix(" ms")
        self.vad_min_silence.setToolTip(
            "检测到多长时间的静音后，认为一句话说完了\n"
            "值越小切分越快 (适合快节奏) | 值越大等待越久 (适合慢语速)\n"
            "推荐: 1200ms (默认)"
        )
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
        typewriter_hint = QLabel("注意：此模式适用于不支持 Ctrl+V 的普通应用")
        typewriter_hint.setStyleSheet(
            self._label_style("warning", extra="margin-left: 20px;")
        )
        output_layout.addWidget(typewriter_hint)

        typewriter_warn1 = QLabel("不适用于使用 DirectInput 的游戏（大多数 3D 游戏）")
        typewriter_warn1.setStyleSheet(
            self._label_style("muted", extra="margin-left: 20px;")
        )
        output_layout.addWidget(typewriter_warn1)

        typewriter_warn2 = QLabel("ℹ️ 仅适用于普通应用，游戏请用管理员启动 Aria")
        typewriter_warn2.setStyleSheet(
            self._label_style("muted", extra="margin-left: 20px;")
        )
        output_layout.addWidget(typewriter_warn2)

        # Typewriter delay
        typewriter_delay_layout = QHBoxLayout()
        typewriter_delay_label = QLabel("逐字间隔:")
        typewriter_delay_label.setStyleSheet("margin-left: 20px;")
        self.typewriter_delay = QSpinBox()
        self.typewriter_delay.setRange(5, 100)
        self.typewriter_delay.setValue(15)
        self.typewriter_delay.setSuffix(" ms")
        self.typewriter_delay.setToolTip("打字机模式下每个字符之间的间隔时间")
        typewriter_delay_layout.addWidget(typewriter_delay_label)
        typewriter_delay_layout.addWidget(self.typewriter_delay)
        typewriter_delay_layout.addStretch()
        output_layout.addLayout(typewriter_delay_layout)

        output_layout.addSpacing(10)

        self.chk_elevation_check = QCheckBox("权限检测 (检测高权限窗口)")
        self.chk_elevation_check.setToolTip(
            "检测目标窗口是否以管理员权限运行\n"
            "如果 Aria 权限低于目标窗口，会提示用户"
        )
        self.chk_elevation_check.setChecked(True)  # Default enabled
        output_layout.addWidget(self.chk_elevation_check)

        layout.addWidget(output_group)

        # Local polish model (advanced, user self-configured)
        local_group = QGroupBox("本地润色模型")
        local_layout = QFormLayout(local_group)

        # Usage guide button
        btn_local_guide = QPushButton("使用说明")
        btn_local_guide.setToolTip("了解如何下载和配置本地润色模型")
        btn_local_guide.clicked.connect(self._show_local_polish_guide)
        local_layout.addRow(btn_local_guide)

        self.local_model_path = QLineEdit()
        self.local_model_path.setPlaceholderText("请填入 .gguf 模型文件路径")
        local_layout.addRow("模型路径:", self.local_model_path)

        self.local_n_gpu_layers = QSpinBox()
        self.local_n_gpu_layers.setRange(-1, 100)
        self.local_n_gpu_layers.setValue(-1)
        self.local_n_gpu_layers.setToolTip(
            "GPU 加速层数\n" "-1 = 全部层放 GPU (推荐)\n" "0 = 纯 CPU 推理"
        )
        local_layout.addRow("GPU 层数:", self.local_n_gpu_layers)

        self.local_n_ctx = QSpinBox()
        self.local_n_ctx.setRange(128, 4096)
        self.local_n_ctx.setValue(512)
        self.local_n_ctx.setSingleStep(128)
        self.local_n_ctx.setToolTip(
            "上下文窗口大小 (token 数)\n"
            "需要容纳 prompt + 输入文本\n"
            "推荐: 512 (默认)"
        )
        local_layout.addRow("上下文窗口:", self.local_n_ctx)

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
            weight = weights.get(word, 0.3)
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
        polish_mode = self.config.get("polish_mode", "quality")
        if polish_mode == "off":
            self.radio_off.setChecked(True)
        elif polish_mode == "fast":
            self.radio_fast.setChecked(True)
        else:
            self.radio_quality.setChecked(True)

        # v1.2: Load polish skill settings
        self.chk_filter_filler.setChecked(self.config.get("filter_filler_words", True))
        self.chk_auto_structure.setChecked(self.config.get("auto_structure", False))
        self.personalization_rules_edit.setPlainText(
            self.config.get("personalization_rules", "")
        )

        # Load reply style
        self.reply_style_edit.setPlainText(self.config.get("reply_style", ""))

        # Load prompt template
        polish = self.config.get("polish", {})
        prompt_template = polish.get("prompt_template", self.DEFAULT_PROMPT)
        self.prompt_edit.setPlainText(prompt_template)

        # === API tab ===
        self.api_url.setText(polish.get("api_url", ""))
        self.api_key.setText(polish.get("api_key", ""))
        self.model.setText(polish.get("model", ""))
        self.timeout.setValue(polish.get("timeout", 10))

        # 备用 API 配置
        self.api_url_backup.setText(polish.get("api_url_backup", ""))
        self.api_key_backup.setText(polish.get("api_key_backup", ""))
        self.model_backup.setText(polish.get("model_backup", ""))
        self.slow_threshold.setValue(int(polish.get("slow_threshold_ms", 3000)))
        self.switch_count.setValue(polish.get("switch_after_slow_count", 2))

        # === Advanced tab ===
        # ASR engine selection (0=FunASR, 1=Qwen3)
        asr_engine = self.config.get("asr_engine", "qwen3")
        engine_index_map = {"funasr": 0, "qwen3": 1}
        # Backward compat: whisper/fireredasr configs map to qwen3
        if asr_engine in ("whisper", "fireredasr"):
            asr_engine = "qwen3"
        self.engine_combo.setCurrentIndex(engine_index_map.get(asr_engine, 1))
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

        # Qwen3-ASR settings
        qwen3 = self.config.get("qwen3", {})
        qwen3_model = qwen3.get("model_name", "auto")
        # Map model name to combo index (0=auto, 1=1.7B, 2=0.6B)
        if "0.6B" in qwen3_model:
            self.qwen3_model.setCurrentIndex(2)
        elif qwen3_model == "auto":
            self.qwen3_model.setCurrentIndex(0)
        else:
            self.qwen3_model.setCurrentIndex(1)

        qwen3_device = qwen3.get("device", "cuda")
        idx = self.qwen3_device.findText(qwen3_device)
        if idx >= 0:
            self.qwen3_device.setCurrentIndex(idx)

        qwen3_dtype = qwen3.get("torch_dtype", "bfloat16")
        if "float16" in qwen3_dtype and "bfloat16" not in qwen3_dtype:
            self.qwen3_dtype.setCurrentIndex(1)
        else:
            self.qwen3_dtype.setCurrentIndex(0)

        # VAD settings
        vad = self.config.get("vad", {})
        self.chk_noise_filter.setChecked(vad.get("noise_filter", True))
        self.chk_screen_ocr.setChecked(vad.get("screen_ocr", True))
        self.chk_screen_ocr_polish.setChecked(vad.get("screen_ocr_polish", False))
        self.vad_threshold.setValue(vad.get("threshold", 0.2))
        self.vad_energy_threshold.setValue(vad.get("energy_threshold", 0.003))
        self.vad_min_silence.setValue(vad.get("min_silence_ms", 1200))

        # Local polish
        local_polish = self.config.get("local_polish", {})
        self.local_model_path.setText(local_polish.get("model_path", ""))
        self.local_n_gpu_layers.setValue(local_polish.get("n_gpu_layers", -1))
        self.local_n_ctx.setValue(local_polish.get("n_ctx", 512))

        # === Output settings ===
        output_cfg = self.config.get("output", {})
        self.chk_typewriter_mode.setChecked(output_cfg.get("typewriter_mode", False))
        self.typewriter_delay.setValue(output_cfg.get("typewriter_delay_ms", 15))
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

    def _show_local_polish_guide(self):
        """Show usage guide for local polish model setup."""
        guide_text = (
            "本地润色使用 llama.cpp 运行 GGUF 格式的语言模型，"
            "在本地完成文本润色，无需联网。\n\n"
            "配置步骤：\n\n"
            "1. 下载模型\n"
            "   推荐从 Hugging Face 下载 GGUF 格式模型，例如：\n"
            "   - Qwen3.5-2B (Q4_K_M, ~1.5GB)\n"
            "   - Qwen2.5-1.5B-Instruct (Q4_K_M, ~1GB)\n"
            '   搜索 "unsloth/Qwen3.5-2B-GGUF" 即可找到\n\n'
            "2. 放置模型\n"
            "   将 .gguf 文件放到 Aria 目录下的 models/ 文件夹\n\n"
            "3. 填写路径\n"
            "   在上方「模型路径」填入文件路径，例如：\n"
            "   models/Qwen3.5-2B-Q4_K_M.gguf\n\n"
            "4. 切换模式\n"
            "   在「智能润色」标签页选择「本地润色」模式\n\n"
            "GPU 层数：-1 表示全部放 GPU（推荐），0 表示纯 CPU\n"
            "上下文窗口：默认 512 即可，一般不需要修改"
        )
        QMessageBox.information(self, "本地润色使用说明", guide_text)

    def save_config(self):
        """Save configuration to hotwords.json."""
        # Track if restart-required settings changed
        restart_needed = False
        old_engine = self.config.get("asr_engine", "qwen3")
        old_funasr = self.config.get("funasr", {})
        old_vad = self.config.get("vad", {})

        # === General tab ===
        if "general" not in self.config:
            self.config["general"] = {}
        hotkey_seq = self.hotkey_edit.keySequence().toString()
        # Convert Qt format to Aria format for storage
        self.config["general"]["hotkey"] = (
            self._qt_to_hotkey(hotkey_seq) if hotkey_seq else "grave"
        )
        # Save device name, or "" for system default (index 0)
        if self.audio_device.currentIndex() == 0:
            new_audio_device = ""  # System default / auto-detect
        else:
            new_audio_device = self.audio_device.currentText()
        if self.config.get("general", {}).get("audio_device", "") != new_audio_device:
            restart_needed = True
        self.config["general"]["audio_device"] = new_audio_device
        self.config["general"]["start_active"] = self.chk_start_active.isChecked()

        # Handle auto startup (create/remove shortcut)
        self._set_auto_startup(self.chk_auto_startup.isChecked())

        # === Hotwords tab ===
        # Preserve enable_initial_prompt (no UI control; don't overwrite user's manual edits)
        if "enable_initial_prompt" not in self.config:
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
        if self.radio_off.isChecked():
            self.config["polish_mode"] = "off"
        elif self.radio_fast.isChecked():
            self.config["polish_mode"] = "fast"
        else:
            self.config["polish_mode"] = "quality"

        # v1.2: Save polish skill settings
        self.config["filter_filler_words"] = self.chk_filter_filler.isChecked()
        self.config["auto_structure"] = self.chk_auto_structure.isChecked()
        self.config["personalization_rules"] = (
            self.personalization_rules_edit.toPlainText()
        )
        self.config["reply_style"] = self.reply_style_edit.toPlainText()

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

        # 备用 API 配置（智能轮询）
        backup_url = self.api_url_backup.text().strip()
        if backup_url:
            self.config["polish"]["api_url_backup"] = backup_url
            self.config["polish"]["api_key_backup"] = self.api_key_backup.text().strip()
            self.config["polish"]["model_backup"] = self.model_backup.text().strip()
            self.config["polish"]["slow_threshold_ms"] = float(
                self.slow_threshold.value()
            )
            self.config["polish"]["switch_after_slow_count"] = self.switch_count.value()
        else:
            # 清除备用 API 配置（如果之前有）
            self.config["polish"].pop("api_url_backup", None)
            self.config["polish"].pop("api_key_backup", None)
            self.config["polish"].pop("model_backup", None)
            self.config["polish"].pop("slow_threshold_ms", None)
            self.config["polish"].pop("switch_after_slow_count", None)

        # === Advanced tab - ASR Engine Selection ===
        engine_map = {0: "funasr", 1: "qwen3"}
        new_engine = engine_map.get(self.engine_combo.currentIndex(), "qwen3")
        if old_engine != new_engine:
            restart_needed = True

        # 切换到 Qwen3 时的完整检查流程
        if new_engine == "qwen3" and old_engine != "qwen3":
            qwen3_model_map = ["auto", "Qwen/Qwen3-ASR-1.7B", "Qwen/Qwen3-ASR-0.6B"]
            qwen3_model = qwen3_model_map[self.qwen3_model.currentIndex()]
            model_size = QWEN3_MODEL_SIZES.get(qwen3_model, "3.4GB")
            short_name = "1.7B" if "1.7B" in qwen3_model else "0.6B"

            # Step 1: 检查 qwen-asr 是否已安装
            if not check_qwen_asr_installed():
                reply = QMessageBox.question(
                    self,
                    "需要安装依赖",
                    "切换到 Qwen3-ASR 需要安装 qwen-asr 引擎。\n\n"
                    "是否现在安装？（约 50MB，需要 1-2 分钟）",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
                )

                if reply == QMessageBox.Yes:
                    success, msg = install_qwen_asr(self)
                    if not success:
                        QMessageBox.critical(
                            self,
                            "安装失败",
                            f"qwen-asr 安装失败:\n\n{msg}\n\n" "请检查网络连接后重试。",
                        )
                        return  # 安装失败，不保存配置
                    else:
                        QMessageBox.information(
                            self, "安装成功", "Qwen3-ASR 引擎依赖已安装成功！"
                        )
                else:
                    # 用户取消安装，恢复引擎选择
                    self.engine_combo.setCurrentIndex(0)  # 恢复为 FunASR
                    return

            # Step 2: 显存预警 (1.7B 需要约 4-6GB 显存)
            if "1.7B" in qwen3_model:
                vram_mb = get_gpu_vram_mb()
                if vram_mb is not None and vram_mb < 6000:  # 6GB 阈值
                    vram_gb = vram_mb / 1024
                    reply = QMessageBox.warning(
                        self,
                        "显存预警",
                        f"检测到 GPU 显存较小: {vram_gb:.1f}GB\n\n"
                        f"Qwen3-ASR 1.7B 建议显存 ≥ 6GB。\n"
                        f"显存不足可能导致:\n"
                        f"• 启动失败 (OOM)\n"
                        f"• 识别速度变慢\n\n"
                        f"建议选择 0.6B 轻量版 (约需 2GB 显存)。\n\n"
                        f"是否继续使用 1.7B？",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No,
                    )
                    if reply == QMessageBox.No:
                        # 自动切换到 0.6B (index 2: auto=0, 1.7B=1, 0.6B=2)
                        self.qwen3_model.setCurrentIndex(2)
                        QMessageBox.information(
                            self,
                            "已切换",
                            "已自动切换到 Qwen3-ASR 0.6B 轻量版。",
                        )

            # Step 3: 检查模型是否已存在
            # 重新获取当前选择（可能被显存警告修改了）
            qwen3_model = qwen3_model_map[self.qwen3_model.currentIndex()]

            # auto 模式跳过下载提示（启动时引擎会根据显存自动选择并下载）
            if qwen3_model != "auto":
                model_size = QWEN3_MODEL_SIZES.get(qwen3_model, "3.4GB")
                short_name = "1.7B" if "1.7B" in qwen3_model else "0.6B"

                if not check_qwen3_model_exists(qwen3_model):
                    QMessageBox.information(
                        self,
                        "首次使用 Qwen3-ASR",
                        f"下次启动时将自动下载 Qwen3-ASR 模型:\n\n"
                        f"模型: {short_name}\n"
                        f"大小: 约 {model_size}\n"
                        f"预计时间: 2-5 分钟\n\n"
                        f"下载期间会显示进度提示。\n"
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

        # === Advanced tab - Qwen3-ASR ===
        if "qwen3" not in self.config:
            self.config["qwen3"] = {}

        # Map combo index to model name (0=auto, 1=1.7B, 2=0.6B)
        qwen3_model_map = ["auto", "Qwen/Qwen3-ASR-1.7B", "Qwen/Qwen3-ASR-0.6B"]
        new_qwen3_model = qwen3_model_map[self.qwen3_model.currentIndex()]
        new_qwen3_device = self.qwen3_device.currentText()
        new_qwen3_dtype = (
            "float16" if self.qwen3_dtype.currentIndex() == 1 else "bfloat16"
        )

        old_qwen3 = self.config.get("qwen3", {})
        if (
            old_qwen3.get("model_name") != new_qwen3_model
            or old_qwen3.get("device") != new_qwen3_device
            or old_qwen3.get("torch_dtype") != new_qwen3_dtype
        ):
            restart_needed = True

        self.config["qwen3"]["model_name"] = new_qwen3_model
        self.config["qwen3"]["device"] = new_qwen3_device
        self.config["qwen3"]["torch_dtype"] = new_qwen3_dtype
        # Preserve language (no UI control; don't overwrite user's manual edits)
        if "language" not in self.config["qwen3"]:
            self.config["qwen3"]["language"] = "Chinese"

        # === Advanced tab - VAD (hot-reload handles these, no restart needed) ===
        if "vad" not in self.config:
            self.config["vad"] = {}
        self.config["vad"]["noise_filter"] = self.chk_noise_filter.isChecked()
        self.config["vad"]["screen_ocr"] = self.chk_screen_ocr.isChecked()
        self.config["vad"]["screen_ocr_polish"] = self.chk_screen_ocr_polish.isChecked()
        self.config["vad"]["threshold"] = self.vad_threshold.value()
        self.config["vad"]["energy_threshold"] = self.vad_energy_threshold.value()
        self.config["vad"]["min_silence_ms"] = self.vad_min_silence.value()

        # === Local polish ===
        if "local_polish" not in self.config:
            self.config["local_polish"] = {}
        self.config["local_polish"]["model_path"] = self.local_model_path.text()
        self.config["local_polish"]["n_gpu_layers"] = self.local_n_gpu_layers.value()
        self.config["local_polish"]["n_ctx"] = self.local_n_ctx.value()
        # Auto-enable local polish when fast mode is selected
        self.config["local_polish"]["enabled"] = self.radio_fast.isChecked()

        # === Output settings ===
        if "output" not in self.config:
            self.config["output"] = {}
        self.config["output"]["typewriter_mode"] = self.chk_typewriter_mode.isChecked()
        self.config["output"]["typewriter_delay_ms"] = self.typewriter_delay.value()
        self.config["output"]["check_elevation"] = self.chk_elevation_check.isChecked()

        # === Translation settings (merge-update to preserve future keys) ===
        if "translation" not in self.config:
            self.config["translation"] = {}
        self.config["translation"]["output_mode"] = self.translate_mode.currentData()

        # === Wakeword - save to wakeword.json ===
        wakeword_path = self.config_path.parent / "wakeword.json"
        new_wakeword = self.wakeword_edit.text().strip() or "瑶瑶"
        try:
            # Load existing wakeword config (with corruption resilience)
            wakeword_config = {"enabled": True, "wakeword": "瑶瑶", "commands": {}}
            if wakeword_path.exists():
                try:
                    with open(wakeword_path, "r", encoding="utf-8") as f:
                        wakeword_config = json.load(f)
                except (json.JSONDecodeError, ValueError):
                    # Corrupted file — use defaults (will be overwritten below)
                    print(f"[WARN] wakeword.json corrupted, resetting to defaults")

            # Update wakeword
            wakeword_config["wakeword"] = new_wakeword

            # Save back (atomic write)
            import os as _os

            _tmp_wk = str(wakeword_path) + ".tmp"
            with open(_tmp_wk, "w", encoding="utf-8") as f:
                json.dump(wakeword_config, f, ensure_ascii=False, indent=2)
                f.flush()
                _os.fsync(f.fileno())
            _os.replace(_tmp_wk, str(wakeword_path))
        except Exception as e:
            print(f"Failed to save wakeword config: {e}")

        # Save to file (atomic write to prevent corruption)
        try:
            import os

            tmp_path = str(self.config_path) + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(self.config_path))

            # Only show message if restart needed (important info)
            if restart_needed:
                QMessageBox.information(
                    self,
                    "设置已保存",
                    "设置已保存。\n\n语音识别引擎设置更改需要重启应用才能生效。\n（VAD 和输出设置会自动热重载，无需重启）",
                )
            else:
                # Visual feedback: temporarily change button text to confirm save
                sender = self.sender()
                if sender and hasattr(sender, "setText"):
                    original_text = sender.text()
                    sender.setText("已保存")
                    # Restore original text after 1.5 seconds
                    QTimer.singleShot(1500, lambda: sender.setText(original_text))

            self.settingsSaved.emit(self.config)
        except Exception as e:
            # Clean up stale .tmp file on failure
            try:
                import os as _cleanup_os

                _tmp = str(self.config_path) + ".tmp"
                if _cleanup_os.path.exists(_tmp):
                    _cleanup_os.remove(_tmp)
            except Exception:
                pass
            QMessageBox.critical(self, "错误", f"保存失败: {e}")

    def _populate_audio_devices(self):
        """Populate audio device dropdown at startup."""
        self.audio_device.clear()
        # First item: system default (saves as "" for auto-detect)
        self.audio_device.addItem("系统默认 (自动检测)", userData="")
        devices = get_audio_input_devices()
        for name, device_id in devices:
            self.audio_device.addItem(name, userData=device_id)

    # --- Auto-startup via Registry HKCU\Run (v2, replaces Startup folder .lnk) ---

    _REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _REGISTRY_VALUE_NAME = "AriaDictation"

    def _detect_launch_config(self) -> dict:
        """
        Detect the correct launch configuration for the current environment.

        Returns dict with keys:
            target_path: Executable to run (absolute path)
            arguments: Command-line arguments
            working_dir: Working directory for the process
            mode: "portable" or "dev"
        """
        import sys

        project_dir = Path(__file__).parent.parent.parent.resolve()

        # Portable build: has AriaRuntime.exe in _internal/
        aria_runtime = project_dir / "_internal" / "AriaRuntime.exe"
        aria_vbs = project_dir / "Aria.vbs"

        if aria_runtime.exists() and aria_vbs.exists():
            # Portable mode: use wscript.exe (absolute) + Aria.vbs
            import os

            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            wscript = Path(system_root) / "System32" / "wscript.exe"
            return {
                "target_path": str(wscript),
                "arguments": f'"{aria_vbs}"',
                "working_dir": str(project_dir),
                "mode": "portable",
            }

        # Dev mode: use pythonw.exe (windowless Python)
        pythonw = project_dir / ".venv" / "Scripts" / "pythonw.exe"
        if not pythonw.exists():
            # Fallback: sibling of current Python executable
            exe_pythonw = Path(sys.executable).with_name("pythonw.exe")
            if exe_pythonw.exists():
                pythonw = exe_pythonw

        launcher_py = project_dir / "launcher.py"

        return {
            "target_path": str(pythonw) if pythonw.exists() else "",
            "arguments": f'"{launcher_py}"',
            "working_dir": str(project_dir),
            "mode": "dev",
        }

    def _build_startup_command(self) -> str:
        """Build the startup command string for registry."""
        config = self._detect_launch_config()
        if not config["target_path"]:
            return ""
        return f'"{config["target_path"]}" {config["arguments"]}'

    def _is_auto_startup_enabled(self) -> bool:
        """
        Check if auto startup is enabled AND points to current install path.

        Returns False if:
        - Registry key doesn't exist
        - Registry value doesn't match current expected command (stale/moved)
        """
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._REGISTRY_KEY) as key:
                value, _ = winreg.QueryValueEx(key, self._REGISTRY_VALUE_NAME)
                expected = self._build_startup_command()
                return bool(expected) and value == expected
        except FileNotFoundError:
            return False
        except Exception as e:
            print(f"[AutoStartup] Registry read error: {e}")
            return False

    def _set_auto_startup(self, enabled: bool) -> None:
        """
        Set or remove auto-startup via Registry HKCU\\Run.

        Always reconciles: if enabled, overwrites any stale value with current path.
        Also cleans up legacy Startup folder .lnk if present.
        """
        import winreg

        # Migrate: clean up old Startup folder shortcut if it exists
        self._cleanup_legacy_startup_shortcut()

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                self._REGISTRY_KEY,
                0,
                winreg.KEY_ALL_ACCESS,
            ) as key:
                if enabled:
                    cmd = self._build_startup_command()
                    if not cmd:
                        QMessageBox.warning(
                            self,
                            "自启动设置失败",
                            "未找到 Python 运行环境（pythonw.exe）。\n"
                            "请确认 .venv 虚拟环境已正确安装。",
                        )
                        self.chk_auto_startup.setChecked(False)
                        return

                    # Always write (reconcile) — fixes stale path after project move
                    winreg.SetValueEx(
                        key, self._REGISTRY_VALUE_NAME, 0, winreg.REG_SZ, cmd
                    )
                    print(f"[AutoStartup] Registry set: {cmd}")
                else:
                    try:
                        winreg.DeleteValue(key, self._REGISTRY_VALUE_NAME)
                        print("[AutoStartup] Registry value removed")
                    except FileNotFoundError:
                        pass  # Already absent
        except Exception as e:
            QMessageBox.warning(self, "自启动设置失败", f"注册表操作异常：{e}")
            self.chk_auto_startup.setChecked(False)

    def _cleanup_legacy_startup_shortcut(self) -> None:
        """Remove old Startup folder .lnk shortcut if it exists (migration from v1)."""
        import os

        try:
            startup_folder = (
                Path(os.environ.get("APPDATA", ""))
                / "Microsoft"
                / "Windows"
                / "Start Menu"
                / "Programs"
                / "Startup"
            )
            legacy_lnk = startup_folder / "Aria.lnk"
            if legacy_lnk.exists():
                legacy_lnk.unlink()
                print(f"[AutoStartup] Cleaned up legacy shortcut: {legacy_lnk}")
        except Exception:
            pass  # Best-effort cleanup, don't block

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
            mode: "off", "fast", or "quality"
        """
        if mode == "off":
            self.radio_off.setChecked(True)
        elif mode == "fast":
            self.radio_fast.setChecked(True)
        else:
            self.radio_quality.setChecked(True)

    def get_polish_mode(self) -> str:
        """Get current polish mode selection."""
        if self.radio_off.isChecked():
            return "off"
        return "fast" if self.radio_fast.isChecked() else "quality"
