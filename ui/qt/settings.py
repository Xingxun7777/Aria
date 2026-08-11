# settings.py
# Settings window for all configurable options
# Based on F3 spec section 4.5

import json
import re
import sys
import uuid
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
    QListView,
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
    QFileDialog,
    QKeySequenceEdit,
    QSlider,
    QGroupBox,
    QDialog,
    QDialogButtonBox,
    QProgressDialog,
    QAbstractItemView,
    QHeaderView,
    QScrollArea,
    QStyledItemDelegate,
    QTabWidget,
    QFrame,
)
from PySide6.QtCore import Qt, Signal, QThread, QObject, QTimer
from PySide6.QtGui import QKeySequence
from . import styles

# Import shared prompt constant from hotword module
from aria.core.hotword import (
    DEFAULT_POLISH_PROMPT,
    POLISH_PROMPT_LOOSE,
    POLISH_PROMPT_STRUCTURED,
)
from aria.core.utils.phonetic import get_matcher


CUSTOM_INSTRUCTION_ID_ROLE = Qt.UserRole + 10
CUSTOM_INSTRUCTION_TRUST_WRITABLE_ROLE = Qt.UserRole + 11


CUSTOM_INSTRUCTION_PRESETS = [
    # Use Windows shell namespaces / app protocols instead of
    # profile-specific absolute paths.  These resolve through Known Folders, so they
    # still work when Desktop/Documents/Downloads are redirected to OneDrive,
    # a non-C drive, or a localized user profile path.
    {
        "label": "打开我的电脑 / 此电脑",
        "phrase": "打开我的电脑",
        "aliases": ["打开此电脑", "打开这台电脑"],
        "command": "shell:MyComputerFolder",
        "mode": "open",
    },
    {
        "label": "打开资源管理器",
        "phrase": "打开资源管理器",
        "aliases": ["打开文件管理器"],
        "command": "explorer.exe",
        "mode": "open",
    },
    {
        "label": "打开下载文件夹",
        "phrase": "打开下载文件夹",
        "aliases": ["打开下载目录", "打开下载"],
        "command": "shell:Downloads",
        "mode": "open",
    },
    {
        "label": "打开桌面文件夹",
        "phrase": "打开桌面文件夹",
        "aliases": ["打开桌面目录"],
        "command": "shell:Desktop",
        "mode": "open",
    },
    {
        "label": "打开文档文件夹",
        "phrase": "打开文档文件夹",
        "aliases": ["打开我的文档", "打开文档"],
        "command": "shell:Personal",
        "mode": "open",
    },
    {
        "label": "打开系统设置",
        "phrase": "打开系统设置",
        "aliases": ["打开设置", "打开电脑设置"],
        "command": "ms-settings:",
        "mode": "open",
    },
    {
        "label": "打开声音设置",
        "phrase": "打开声音设置",
        "aliases": ["打开音量设置", "打开麦克风设置"],
        "command": "ms-settings:sound",
        "mode": "open",
    },
    {
        "label": "打开任务管理器",
        "phrase": "打开任务管理器",
        "aliases": [],
        "command": "taskmgr.exe",
        "mode": "open",
    },
    {
        "label": "打开计算器",
        "phrase": "打开计算器",
        "aliases": [],
        "command": "calc.exe",
        "mode": "open",
    },
    {
        "label": "打开记事本",
        "phrase": "打开记事本",
        "aliases": [],
        "command": "notepad.exe",
        "mode": "open",
    },
    {
        "label": "打开截图工具",
        "phrase": "打开截图工具",
        "aliases": ["开始截图", "截图一下"],
        "command": "ms-screenclip:",
        "mode": "open",
    },
]


class TableLineEditDelegate(QStyledItemDelegate):
    """Make QTableWidget's inline text editor tall enough to read while typing.

    Qt creates a temporary QLineEdit when users double-click editable cells.
    Without an explicit delegate, the editor can inherit a very small geometry
    from the item rect on high-DPI / dark-theme tables, making it look like a
    flat strip.  This delegate is intentionally generic and is shared by all
    editable settings tables.
    """

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        editor.setObjectName("tableCellEditor")
        editor.setMinimumHeight(30)
        editor.setFrame(True)
        return editor

    def updateEditorGeometry(self, editor, option, index):
        rect = option.rect.adjusted(2, 2, -2, -2)
        if rect.height() < 30:
            delta = 30 - rect.height()
            rect.adjust(0, -(delta // 2), 0, delta - (delta // 2))
        editor.setGeometry(rect)


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
    检查 Qwen3 模型是否已存在（bundled 或 HF 缓存）。

    Args:
        model_name: 模型名称，如 "Qwen/Qwen3-ASR-1.7B"

    Returns:
        True 如果模型已存在
    """
    import os
    from pathlib import Path

    # 1. Check bundled model (portable/full distribution)
    if "/" in model_name:
        try:
            from aria.core.utils.paths import get_models_path

            local_name = model_name.split("/")[-1]
            bundled_path = get_models_path(local_name)
            if bundled_path.is_dir() and any(bundled_path.glob("*.safetensors")):
                return True
        except Exception:
            pass

    # 2. Check HuggingFace cache
    cache_dir = (
        Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    )
    model_dir_name = f"models--{model_name.replace('/', '--')}"
    model_path = cache_dir / model_dir_name

    if model_path.exists():
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
        """Execute API test in worker thread via shared AI gateway."""
        from aria.core.ai.gateway import AIErrorCategory, AIRequestSpec, chat

        category_messages = {
            AIErrorCategory.NOT_CONFIGURED: "AI 未配置，请填写地址、密钥与模型",
            AIErrorCategory.AUTH: "API 认证失败，请检查密钥",
            AIErrorCategory.RATE_LIMITED: "API 请求过于频繁，请稍后重试",
            AIErrorCategory.SERVER_ERROR: "API 服务异常",
            AIErrorCategory.TIMEOUT: "API 连接超时，请检查地址是否正确",
            AIErrorCategory.CONNECT: "无法连接到 API 服务器，请检查地址是否正确",
            AIErrorCategory.PROTOCOL: "API 响应异常",
            AIErrorCategory.EMPTY: "API 返回空内容",
            AIErrorCategory.CANCELLED: "请求已取消",
        }

        try:
            result = chat(
                AIRequestSpec(
                    api_url=self.api_url,
                    api_key=self.api_key,
                    model=self.model,
                    messages=[{"role": "user", "content": "Hi"}],
                    timeout_s=5.0,
                    purpose="api_test",
                    max_tokens=5,
                )
            )
            status_code = int(result.status_code or 0)
            if result.ok:
                self.finished.emit(True, "API 连接成功！", status_code or 200)
                return

            category = result.error.value if result.error else "UNKNOWN"
            base = category_messages.get(
                result.error, f"API 测试失败 ({category})"
            )
            detail = (result.detail or "").strip()
            if status_code > 0 and detail:
                message = f"{base} [{status_code}/{category}] {detail}"
            elif status_code > 0:
                message = f"{base} [{status_code}/{category}]"
            elif detail:
                message = f"{base} [{category}] {detail}"
            else:
                message = f"{base} [{category}]"
            self.finished.emit(False, message[:200], status_code)

        except Exception as e:
            self.finished.emit(False, str(e)[:200], 0)


# Preset API providers for quick setup
# URLs verified via web research — NO hardcoded model names (use "获取模型列表" instead)
# "v1_in_url": True means the URL already ends with /v1, don't append again
API_PRESETS = {
    "DeepSeek": {
        "url": "https://api.deepseek.com",
        "key_hint": "sk-...",
        "recommended_model": "deepseek-v4-flash",
    },
    "OpenRouter": {
        "url": "https://openrouter.ai/api/v1",
        "key_hint": "sk-or-v1-...",
    },
    "OpenAI": {
        "url": "https://api.openai.com/v1",
        "key_hint": "sk-... / sk-proj-...",
    },
    "硅基流动 (SiliconFlow)": {
        "url": "https://api.siliconflow.cn/v1",
        "key_hint": "sk-...",
    },
    "Groq": {
        "url": "https://api.groq.com/openai/v1",
        "key_hint": "gsk_...",
    },
    "月之暗面 (Moonshot)": {
        "url": "https://api.moonshot.cn/v1",
        "key_hint": "sk-...",
    },
    "阶跃星辰 (Stepfun)": {
        "url": "https://api.stepfun.com/v1",
        "key_hint": "sk-...",
    },
    "通义千问 (DashScope)": {
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_hint": "sk-...",
    },
    "智谱 (Zhipu GLM)": {
        "url": "https://open.bigmodel.cn/api/paas/v4",
        "key_hint": "(从 bigmodel.cn 获取)",
    },
    "百川 (Baichuan)": {
        "url": "https://api.baichuan-ai.com/v1",
        "key_hint": "sk-...",
    },
    "火山引擎 (豆包)": {
        "url": "https://ark.cn-beijing.volces.com/api/v3",
        "key_hint": "(从火山方舟获取)",
    },
    "Together AI": {
        "url": "https://api.together.xyz/v1",
        "key_hint": "(从 together.ai 获取)",
    },
    "Mistral AI": {
        "url": "https://api.mistral.ai/v1",
        "key_hint": "(从 console.mistral.ai 获取)",
    },
    "本地 Ollama": {
        "url": "http://localhost:11434/v1",
        "key_hint": "(无需密钥，填任意字符)",
    },
    "本地 LM Studio": {
        "url": "http://localhost:1234/v1",
        "key_hint": "(无需密钥，填任意字符)",
    },
    "自定义": {
        "url": "",
        "key_hint": "",
    },
}


class ModelFetchWorker(QObject):
    """Worker for fetching available models from /v1/models endpoint."""

    finished = Signal(bool, list, str)  # success, model_list, error_msg

    def __init__(self, api_url: str, api_key: str):
        super().__init__()
        self.api_url = api_url
        self.api_key = api_key

    def run(self):
        import requests

        try:
            api_url = self.api_url.rstrip("/")
            if api_url.endswith("/models"):
                full_url = api_url
            elif api_url.endswith(("/v1", "/v3", "/v4")):
                full_url = f"{api_url}/models"
            elif "/v1/" in api_url or "/v3/" in api_url or "/v4/" in api_url:
                full_url = f"{api_url}/models"
            else:
                full_url = f"{api_url}/v1/models"

            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            response = requests.get(full_url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                models = []
                for m in data.get("data", []):
                    model_id = m.get("id", "")
                    if model_id:
                        models.append(model_id)
                models.sort()
                self.finished.emit(True, models, "")
            else:
                self.finished.emit(
                    False, [], f"HTTP {response.status_code}: {response.text[:100]}"
                )
        except requests.exceptions.Timeout:
            self.finished.emit(False, [], "请求超时")
        except requests.exceptions.ConnectionError:
            self.finished.emit(False, [], "无法连接服务器")
        except Exception as e:
            self.finished.emit(False, [], str(e))


class SettingsWindow(QMainWindow):
    """Settings window with configurable option tabs."""

    # Signal emitted when settings are saved
    settingsSaved = Signal(dict)
    apiSwitchBackRequested = Signal()

    # Default prompt templates — v5.0 双模板。
    # DEFAULT_PROMPT 保留为 LOOSE 别名，向后兼容老代码引用。
    DEFAULT_PROMPT = DEFAULT_POLISH_PROMPT
    DEFAULT_PROMPT_LOOSE = POLISH_PROMPT_LOOSE
    DEFAULT_PROMPT_STRUCTURED = POLISH_PROMPT_STRUCTURED

    def __init__(self, config_path: Optional[Path] = None):
        super().__init__()
        self._theme = styles.get_theme_palette()
        self.setWindowTitle("Aria 设置")
        self.resize(1040, 700)
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

    def _make_combo_popup_opaque(
        self, combo: QComboBox, *, row_height: int = 36
    ) -> None:
        """Use an opaque popup view so dropdown text never blends with tables below."""
        popup_bg = "#232328" if self._theme.name == "dark" else "#FFFFFF"
        hover_bg = "#2F2F35" if self._theme.name == "dark" else "#F3F4F6"
        view = QListView(combo)
        view.setUniformItemSizes(True)
        view.setAlternatingRowColors(False)
        view.setAutoFillBackground(True)
        view.viewport().setAutoFillBackground(True)
        view.setStyleSheet(
            f"""
            QListView {{
                background-color: {popup_bg};
                color: {self._theme.text_primary};
                border: 1px solid {self._theme.border};
                outline: 0;
                padding: 4px;
            }}
            QListView::item {{
                min-height: {int(row_height)}px;
                padding: 6px 10px;
                background-color: {popup_bg};
                color: {self._theme.text_primary};
            }}
            QListView::item:hover {{
                background-color: {hover_bg};
            }}
            QListView::item:selected {{
                background-color: {self._theme.accent};
                color: white;
            }}
            """
        )
        combo.setView(view)
        combo.setMaxVisibleItems(10)

    def _init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Sidebar
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(200)
        self.sidebar.addItems(["常规", "专业词汇", "语音指令", "润色", "API", "高级"])
        self.sidebar.currentRowChanged.connect(self.change_page)

        # Content area
        self.pages = QStackedWidget()
        self.pages.setObjectName("contentArea")
        self._table_editor_delegate = TableLineEditDelegate(self)

        self.pages.addWidget(self._wrap_in_scroll(self._create_general_tab()))
        self.pages.addWidget(self._wrap_in_scroll(self._create_hotwords_tab()))
        self.pages.addWidget(self._wrap_in_scroll(self._create_voice_commands_tab()))
        self.pages.addWidget(self._wrap_in_scroll(self._create_polish_tab()))
        self.pages.addWidget(self._wrap_in_scroll(self._create_api_tab()))
        self.pages.addWidget(self._wrap_in_scroll(self._create_advanced_tab()))

        # Right column: stacked pages above a persistent save bar
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self.pages, 1)

        footer = QWidget()
        footer.setObjectName("footerBar")
        footer.setStyleSheet(
            f"#footerBar {{ border-top: 1px solid {self._theme.separator}; }}"
        )
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(20, 12, 20, 12)
        footer_layout.addStretch(1)
        self.btn_save_all = QPushButton("保存")
        self.btn_save_all.setObjectName("primaryBtn")
        self.btn_save_all.setMinimumWidth(150)
        self.btn_save_all.clicked.connect(self.save_config)
        footer_layout.addWidget(self.btn_save_all)
        right_layout.addWidget(footer)

        layout.addWidget(self.sidebar)
        layout.addWidget(right, 1)

        self.sidebar.setCurrentRow(0)

        # Never let the window grow past the screen; pages scroll internally.
        self.setMinimumSize(880, 560)
        try:
            from PySide6.QtGui import QGuiApplication

            _scr = QGuiApplication.primaryScreen()
            if _scr is not None:
                self.setMaximumHeight(_scr.availableGeometry().height())
        except Exception:
            pass

    def _wrap_in_scroll(self, content: QWidget) -> QWidget:
        """Wrap a page in a vertical scroll area so no page can force the window
        taller than the screen. Pages that already embed their own QScrollArea
        (e.g. the 语音指令 page) are returned unchanged to avoid nested scrollbars."""
        existing = content.findChild(QScrollArea)
        if existing is not None:
            existing.viewport().setStyleSheet("background: transparent;")
            return content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setStyleSheet("background: transparent;")
        scroll.setWidget(content)
        return scroll

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
        self.hotkey_edit.setToolTip("点击此处后按下想要的快捷键进行设置")
        hotkey_layout.addWidget(self.hotkey_edit)
        btn_clear_hotkey = QPushButton("清除")
        btn_clear_hotkey.setToolTip("清除当前快捷键，恢复为默认的 ` 键")
        btn_clear_hotkey.clicked.connect(lambda: self.hotkey_edit.clear())
        hotkey_layout.addWidget(btn_clear_hotkey)
        form.addRow("录音热键:", hotkey_layout)

        # Audio device
        self.audio_device = QComboBox()
        self.audio_device.setToolTip(
            "选择麦克风设备，「系统默认」会自动使用当前默认录音设备"
        )
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

        self.chk_auto_update = QCheckBox("启动时自动检查更新")
        self.chk_auto_update.setToolTip(
            "每次启动时后台检查 GitHub 是否有新版本，有则提示"
        )
        layout.addWidget(self.chk_auto_update)

        layout.addSpacing(20)

        # Translation settings
        translate_group = QGroupBox("翻译设置")
        translate_layout = QFormLayout(translate_group)

        self.translate_mode = QComboBox()
        self.translate_mode.setToolTip(
            "弹窗：悬浮窗展示翻译结果；剪贴板：翻译后自动复制"
        )
        self.translate_mode.addItem("弹窗显示", "popup")
        self.translate_mode.addItem("复制到剪贴板", "clipboard")
        translate_layout.addRow("翻译输出方式:", self.translate_mode)

        translate_hint = QLabel('"翻译成英文/中文" 命令的结果输出方式')
        translate_hint.setStyleSheet(self._label_style("secondary"))
        translate_layout.addRow("", translate_hint)

        layout.addWidget(translate_group)

        layout.addSpacing(20)

        # Data management
        data_group = QGroupBox("数据管理")
        data_layout = QVBoxLayout(data_group)

        btn_open_history = QPushButton("打开历史记录文件夹")
        btn_open_history.setToolTip("导出所有历史记录为 txt 文件并打开文件夹")
        btn_open_history.clicked.connect(self._open_history_folder)
        data_layout.addWidget(btn_open_history)

        btn_open_highlights = QPushButton("打开重点记录")
        btn_open_highlights.setToolTip("打开「记一下」命令保存的重要内容")
        btn_open_highlights.clicked.connect(self._open_highlights_file)
        data_layout.addWidget(btn_open_highlights)

        layout.addWidget(data_group)

        layout.addStretch()

        return w

    # ==========================================================================
    # Tab 2: Voice Commands
    # ==========================================================================
    def _create_voice_commands_tab(self) -> QWidget:
        w = QWidget()
        outer_layout = QVBoxLayout(w)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(30, 30, 30, 30)

        layout.addWidget(QLabel("<h2>语音指令</h2>"))

        wakeword_group = QGroupBox("语音唤醒词")
        wakeword_layout = QVBoxLayout(wakeword_group)

        wakeword_input_layout = QHBoxLayout()
        wakeword_label = QLabel("唤醒词:")
        self.wakeword_edit = QLineEdit()
        self.wakeword_edit.setPlaceholderText("小助手")
        self.wakeword_edit.setToolTip("语音指令的触发前缀，说「唤醒词 + 指令」执行操作")
        self.wakeword_edit.textChanged.connect(self._on_wakeword_text_changed)
        wakeword_input_layout.addWidget(wakeword_label)
        wakeword_input_layout.addWidget(self.wakeword_edit)
        wakeword_layout.addLayout(wakeword_input_layout)

        self.pinyin_hint = QLabel("")
        self.pinyin_hint.setStyleSheet(
            self._label_style("muted", font_size=11, extra="margin-left: 50px;")
        )
        wakeword_layout.addWidget(self.pinyin_hint)

        example_hint = QLabel(
            '例: "小助手打开我的电脑"、"小助手打开下载文件夹"、"小助手休眠"'
        )
        example_hint.setStyleSheet(
            self._label_style("secondary", font_size=11, extra="margin-top: 5px;")
        )
        wakeword_layout.addWidget(example_hint)
        layout.addWidget(wakeword_group)

        built_in_group = QGroupBox("内置语音指令")
        built_in_layout = QVBoxLayout(built_in_group)
        built_in_intro = QLabel(
            "这些是 Aria 自带的语音指令，随唤醒词一起生效。"
            "为避免误触发，系统控制、独立选区工具和键盘快捷键暂时只展示，不在这里自由改写。"
        )
        built_in_intro.setWordWrap(True)
        built_in_intro.setStyleSheet(self._label_style("secondary"))
        built_in_layout.addWidget(built_in_intro)

        built_in_table = QTableWidget(0, 3)
        built_in_table.setHorizontalHeaderLabels(["类别", "常用说法", "效果"])
        built_in_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        built_in_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        built_in_table.verticalHeader().setVisible(False)
        built_in_table.setMinimumHeight(170)
        built_in_table.setColumnWidth(0, 110)
        built_in_table.setColumnWidth(1, 300)
        built_in_header = built_in_table.horizontalHeader()
        built_in_header.setSectionResizeMode(0, QHeaderView.Fixed)
        built_in_header.setSectionResizeMode(1, QHeaderView.Interactive)
        built_in_header.setSectionResizeMode(2, QHeaderView.Stretch)

        built_in_rows = [
            (
                "基础控制",
                "开启/关闭自动发送、休眠、醒来、深度休眠",
                "控制监听、发送和模型加载状态",
            ),
            (
                "自然修改",
                "再长一点、语气温和些、把逻辑理顺",
                "直接按你的描述修改上一段输入",
            ),
            (
                "选区工具",
                "翻译、总结/归纳、帮我回复、问问AI",
                "处理选中文字并显示独立结果",
            ),
            ("记录提醒", "重点记一下、提醒我五分钟后喝水", "记录重点或创建定时提醒"),
            ("打开路径", "帮我打开、打开目录", "打开选中的文件、目录或 URL"),
            (
                "键盘快捷键",
                "发送、删除、撤销、复制、粘贴、全选、保存",
                "对当前窗口发送常用快捷键",
            ),
        ]
        for category, examples, effect in built_in_rows:
            row = built_in_table.rowCount()
            built_in_table.insertRow(row)
            built_in_table.setRowHeight(row, 34)
            built_in_table.setItem(row, 0, QTableWidgetItem(category))
            built_in_table.setItem(row, 1, QTableWidgetItem(examples))
            built_in_table.setItem(row, 2, QTableWidgetItem(effect))
        built_in_layout.addWidget(built_in_table)

        guide_layout = QHBoxLayout()
        btn_guide = QPushButton("查看完整语音指令指南")
        btn_guide.setToolTip("查看所有内置语音指令和用法示例")
        btn_guide.clicked.connect(self._show_voice_guide)
        guide_layout.addWidget(btn_guide)
        guide_layout.addStretch()
        built_in_layout.addLayout(guide_layout)
        layout.addWidget(built_in_group)

        command_group = QGroupBox("我的语音指令")
        command_layout = QVBoxLayout(command_group)

        intro = QLabel(
            "说「唤醒词 + 语音指令」即可启动软件、打开文件夹/网页，或执行你预设的指令。\n"
            "这里复用唤醒词的拼音思路，只在已配置的指令短语内做近音匹配；不会写入全局热词。"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(self._label_style("secondary", extra="margin-bottom: 8px;"))
        command_layout.addWidget(intro)

        hint = QLabel(
            "例：唤醒词是「小助手」，指令短语填「打开我的电脑」，目标填 shell:MyComputerFolder；"
            "说「小助手打开我的电脑」即可执行。我的语音指令会自动做拼音近音匹配，"
            "近音别名一般不用填，只有另一种常用叫法/缩写才需要补。指令短语至少 3 个字才会启用。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(self._label_style("muted", font_size=11))
        command_layout.addWidget(hint)

        preset_layout = QHBoxLayout()
        preset_layout.addWidget(QLabel("常用预设:"))
        self.custom_instruction_preset_combo = QComboBox()
        for preset in CUSTOM_INSTRUCTION_PRESETS:
            self.custom_instruction_preset_combo.addItem(preset["label"], preset)
        self.custom_instruction_preset_combo.setMinimumHeight(30)
        self._make_combo_popup_opaque(
            self.custom_instruction_preset_combo, row_height=34
        )
        preset_layout.addWidget(self.custom_instruction_preset_combo, 1)

        btn_import_preset = QPushButton("导入选中预设")
        btn_import_preset.setToolTip(
            "只导入到下方表格，保存后才会生效；导入后可继续修改"
        )
        btn_import_preset.clicked.connect(
            self._import_selected_custom_instruction_preset
        )
        preset_layout.addWidget(btn_import_preset)

        btn_import_recommended = QPushButton("导入全部常用")
        btn_import_recommended.setToolTip(
            "导入文件夹、设置、工具类常用预设；已有短语会自动跳过"
        )
        btn_import_recommended.clicked.connect(
            self._import_recommended_custom_instruction_presets
        )
        preset_layout.addWidget(btn_import_recommended)
        command_layout.addLayout(preset_layout)

        preset_note = QLabel(
            "预设不会写入热词；只有导入并保存到我的语音指令表后才会响应语音。"
        )
        preset_note.setWordWrap(True)
        preset_note.setStyleSheet(self._label_style("muted", font_size=11))
        command_layout.addWidget(preset_note)

        self.custom_instruction_table = QTableWidget(0, 6)
        self.custom_instruction_table.setItemDelegate(self._table_editor_delegate)
        self.custom_instruction_table.setHorizontalHeaderLabels(
            ["启用", "指令短语", "近音别名", "启动目标 / 指令", "方式", "管理员"]
        )
        self.custom_instruction_table.itemChanged.connect(
            self._on_custom_instruction_item_changed
        )
        self.custom_instruction_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.custom_instruction_table.setColumnWidth(0, 54)
        self.custom_instruction_table.setColumnWidth(1, 150)
        self.custom_instruction_table.setColumnWidth(2, 180)
        self.custom_instruction_table.setColumnWidth(3, 300)
        self.custom_instruction_table.setColumnWidth(4, 130)
        self.custom_instruction_table.setColumnWidth(5, 72)
        self.custom_instruction_table.setMinimumHeight(260)
        header = self.custom_instruction_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.setSectionResizeMode(5, QHeaderView.Fixed)
        command_layout.addWidget(self.custom_instruction_table)

        btn_layout = QHBoxLayout()

        btn_add = QPushButton("+ 添加语音指令")
        btn_add.clicked.connect(self._add_custom_instruction_row)
        btn_layout.addWidget(btn_add)

        btn_browse = QPushButton("选择启动程序...")
        btn_browse.setToolTip("给当前选中行选择 exe、快捷方式或任意文件")
        btn_browse.clicked.connect(self._browse_custom_instruction_target)
        btn_layout.addWidget(btn_browse)

        btn_remove = QPushButton("- 删除选中")
        btn_remove.clicked.connect(self._remove_custom_instruction_rows)
        btn_layout.addWidget(btn_remove)

        btn_clean_admin = QPushButton("清理残留管理员任务")
        btn_clean_admin.clicked.connect(self._clean_stale_elevated_tasks)
        btn_layout.addWidget(btn_clean_admin)

        btn_layout.addStretch()
        command_layout.addLayout(btn_layout)

        note = QLabel(
            "方式说明：打开路径/URL 适合 exe、lnk、文件夹、网页；"
            "高级指令适合带参数的启动器，但不会把语音识别文本拼进指令，也不会隐式打开 shell。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(self._label_style("warning", font_size=11))
        command_layout.addWidget(note)
        layout.addWidget(command_group)

        layout.addStretch()

        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        return w

    def _create_custom_instructions_tab(self) -> QWidget:
        """Backward-compatible internal alias for the renamed 语音指令 page."""
        return self._create_voice_commands_tab()

    def _split_custom_instruction_aliases(self, aliases_text: str) -> list[str]:
        return [
            alias.strip()
            for alias in re.split(r"[,，、;；\n]", aliases_text or "")
            if alias.strip()
        ]

    def _custom_instruction_match_key(self, text: str) -> str:
        return re.sub(r"[\s，,。.、：:；;！!？?\"'“”‘’（）()【】\[\]]", "", text or "")

    def _add_custom_instruction_row(self, entry: Optional[dict] = None):
        if not isinstance(entry, dict):
            entry = {}
        signals_blocked = self.custom_instruction_table.blockSignals(True)
        row = self.custom_instruction_table.rowCount()
        try:
            self.custom_instruction_table.insertRow(row)
            self.custom_instruction_table.setRowHeight(row, 40)

            enabled_item = QTableWidgetItem("")
            enabled_item.setFlags(
                Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable
            )
            enabled_item.setCheckState(
                Qt.Checked if entry.get("enabled", True) else Qt.Unchecked
            )
            entry_id = str(entry.get("id") or uuid.uuid4().hex).strip()
            enabled_item.setData(CUSTOM_INSTRUCTION_ID_ROLE, entry_id)
            enabled_item.setData(
                CUSTOM_INSTRUCTION_TRUST_WRITABLE_ROLE,
                bool(entry.get("trust_writable_target", False)),
            )
            self.custom_instruction_table.setItem(row, 0, enabled_item)

            phrase = (
                entry.get("phrase") or entry.get("trigger") or entry.get("name") or ""
            )
            aliases = entry.get("aliases", [])
            if isinstance(aliases, list):
                aliases_text = "，".join(
                    str(alias) for alias in aliases if str(alias).strip()
                )
            else:
                aliases_text = str(aliases or "")

            self.custom_instruction_table.setItem(row, 1, QTableWidgetItem(str(phrase)))
            self.custom_instruction_table.setItem(
                row, 2, QTableWidgetItem(aliases_text)
            )
            self.custom_instruction_table.setItem(
                row,
                3,
                QTableWidgetItem(
                    str(entry.get("command") or entry.get("target") or "")
                ),
            )

            mode_combo = QComboBox()
            mode_combo.addItem("打开路径/URL", "open")
            mode_combo.addItem("高级指令", "command")
            mode_combo.setMinimumHeight(30)
            mode_combo.setSizeAdjustPolicy(
                QComboBox.AdjustToMinimumContentsLengthWithIcon
            )
            mode_combo.setMinimumContentsLength(9)
            self._make_combo_popup_opaque(mode_combo, row_height=30)
            mode_combo.setMaxVisibleItems(2)
            mode = str(entry.get("mode") or "open").lower()
            idx = mode_combo.findData("command" if mode == "shell" else mode)
            if idx >= 0:
                mode_combo.setCurrentIndex(idx)
            self.custom_instruction_table.setCellWidget(row, 4, mode_combo)

            elevate_item = QTableWidgetItem("")
            elevate_item.setFlags(
                Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable
            )
            elevate_item.setCheckState(
                Qt.Checked if entry.get("elevate", False) else Qt.Unchecked
            )
            self.custom_instruction_table.setItem(row, 5, elevate_item)
        finally:
            self.custom_instruction_table.blockSignals(signals_blocked)
        return row

    def _custom_instruction_metadata_item(self, row: int) -> Optional[QTableWidgetItem]:
        return self.custom_instruction_table.item(row, 0)

    def _set_custom_instruction_trust_writable(self, row: int, trusted: bool) -> None:
        metadata_item = self._custom_instruction_metadata_item(row)
        if metadata_item is not None:
            metadata_item.setData(CUSTOM_INSTRUCTION_TRUST_WRITABLE_ROLE, bool(trusted))

    def _get_custom_instruction_trust_writable(self, row: int) -> bool:
        metadata_item = self._custom_instruction_metadata_item(row)
        if metadata_item is None:
            return False
        return bool(metadata_item.data(CUSTOM_INSTRUCTION_TRUST_WRITABLE_ROLE))

    def _on_custom_instruction_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 5 or item.checkState() != Qt.Checked:
            if item.column() == 5 and item.checkState() != Qt.Checked:
                self._set_custom_instruction_trust_writable(item.row(), False)
            return

        command_item = self.custom_instruction_table.item(item.row(), 3)
        target = command_item.text().strip() if command_item else ""
        try:
            from aria.core.wakeword.elevation import validate_elevated_target

            ok, reason = validate_elevated_target(target, trust_writable=False)
        except Exception as exc:
            ok, reason = False, str(exc)

        if ok:
            self._set_custom_instruction_trust_writable(item.row(), False)
            return

        if "user-writable directory" in reason:
            message = (
                f"此程序路径位于当前用户可写的位置：{target}\n\n"
                "注册无感提权快捷方式后，如该路径下的程序文件或其加载的 DLL/配置被同用户级别的恶意软件篡改，将不再触发 UAC，直接以管理员权限运行。\n\n"
                "仅当你确信此目录的内容来源可信、不会被恶意写入时，才应启用此模式。\n\n"
                "继续启用？"
            )
            reply = QMessageBox.warning(
                self,
                "管理员快捷方式风险确认",
                message,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self._set_custom_instruction_trust_writable(item.row(), True)
                return
        else:
            QMessageBox.warning(self, "无法启用管理员快捷方式", reason)

        signals_blocked = self.custom_instruction_table.blockSignals(True)
        try:
            item.setCheckState(Qt.Unchecked)
        finally:
            self.custom_instruction_table.blockSignals(signals_blocked)
        self._set_custom_instruction_trust_writable(item.row(), False)

    def _custom_instruction_row_trigger_keys(self, row: int) -> set[str]:
        keys: set[str] = set()
        phrase_item = self.custom_instruction_table.item(row, 1)
        aliases_item = self.custom_instruction_table.item(row, 2)
        texts = [phrase_item.text() if phrase_item else ""]
        if aliases_item:
            texts.extend(self._split_custom_instruction_aliases(aliases_item.text()))
        for text in texts:
            key = self._custom_instruction_match_key(text)
            if key:
                keys.add(key)
        return keys

    def _custom_instruction_preset_trigger_keys(self, preset: dict) -> set[str]:
        texts = [str(preset.get("phrase") or "")]
        aliases = preset.get("aliases", [])
        if isinstance(aliases, str):
            texts.extend(self._split_custom_instruction_aliases(aliases))
        elif isinstance(aliases, list):
            texts.extend(str(alias) for alias in aliases)
        return {
            key for text in texts if (key := self._custom_instruction_match_key(text))
        }

    def _custom_instruction_phrase_exists(self, phrase: str) -> bool:
        phrase_key = self._custom_instruction_match_key(phrase)
        if not phrase_key:
            return False
        for row in range(self.custom_instruction_table.rowCount()):
            if phrase_key in self._custom_instruction_row_trigger_keys(row):
                self.custom_instruction_table.setCurrentCell(row, 1)
                return True
        return False

    def _custom_instruction_preset_conflicts(self, preset: dict) -> bool:
        preset_keys = self._custom_instruction_preset_trigger_keys(preset)
        if not preset_keys:
            return False
        for row in range(self.custom_instruction_table.rowCount()):
            if preset_keys & self._custom_instruction_row_trigger_keys(row):
                self.custom_instruction_table.setCurrentCell(row, 1)
                return True
        return False

    def _find_duplicate_custom_instruction_trigger(
        self, entries: list[dict]
    ) -> Optional[str]:
        seen: dict[str, str] = {}
        for entry in entries:
            if not entry.get("enabled", True):
                continue
            phrase = str(entry.get("phrase") or "").strip()
            aliases = entry.get("aliases", [])
            triggers = [phrase]
            if isinstance(aliases, list):
                triggers.extend(str(alias).strip() for alias in aliases)
            for trigger in triggers:
                key = self._custom_instruction_match_key(trigger)
                if not key:
                    continue
                if key in seen and seen[key] != phrase:
                    return trigger or phrase
                seen[key] = phrase
        return None

    def _import_custom_instruction_preset(self, preset: dict) -> bool:
        if not isinstance(preset, dict):
            return False
        phrase = str(preset.get("phrase") or "").strip()
        if not phrase or self._custom_instruction_preset_conflicts(preset):
            return False
        row = self._add_custom_instruction_row(preset.copy())
        self.custom_instruction_table.setCurrentCell(row, 1)
        return True

    def _import_selected_custom_instruction_preset(self):
        preset = self.custom_instruction_preset_combo.currentData()
        added = self._import_custom_instruction_preset(preset)
        if not added:
            QMessageBox.information(
                self, "已存在", "这个预设已经在我的语音指令表里了。"
            )

    def _import_recommended_custom_instruction_presets(self):
        added = 0
        skipped = 0
        for preset in CUSTOM_INSTRUCTION_PRESETS:
            if self._import_custom_instruction_preset(preset):
                added += 1
            else:
                skipped += 1
        QMessageBox.information(
            self,
            "常用预设已导入",
            f"已导入 {added} 条常用预设，跳过 {skipped} 条已存在项。\n\n"
            "请检查短语和目标，确认后点击「保存语音指令」。",
        )

    def _remove_custom_instruction_rows(self):
        rows = {item.row() for item in self.custom_instruction_table.selectedItems()}
        current = self.custom_instruction_table.currentRow()
        if current >= 0:
            rows.add(current)
        for row in sorted(rows, reverse=True):
            self.custom_instruction_table.removeRow(row)

    def _browse_custom_instruction_target(self):
        row = self.custom_instruction_table.currentRow()
        if row < 0:
            self._add_custom_instruction_row()
            row = self.custom_instruction_table.rowCount() - 1

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择启动程序或文件",
            str(Path.home()),
            "可执行/快捷方式 (*.exe *.lnk *.bat *.cmd);;所有文件 (*.*)",
        )
        if file_path:
            self.custom_instruction_table.setItem(row, 3, QTableWidgetItem(file_path))

    def _collect_custom_instructions(self) -> list[dict]:
        commands = []
        for row in range(self.custom_instruction_table.rowCount()):
            enabled_item = self.custom_instruction_table.item(row, 0)
            phrase_item = self.custom_instruction_table.item(row, 1)
            aliases_item = self.custom_instruction_table.item(row, 2)
            command_item = self.custom_instruction_table.item(row, 3)
            mode_widget = self.custom_instruction_table.cellWidget(row, 4)
            elevate_item = self.custom_instruction_table.item(row, 5)

            phrase = phrase_item.text().strip() if phrase_item else ""
            command = command_item.text().strip() if command_item else ""
            if not phrase and not command:
                continue

            aliases_text = aliases_item.text().strip() if aliases_item else ""
            mode = "open"
            if isinstance(mode_widget, QComboBox):
                mode = mode_widget.currentData() or "open"
            entry_id = ""
            trust_writable = False
            if enabled_item is not None:
                entry_id = str(
                    enabled_item.data(CUSTOM_INSTRUCTION_ID_ROLE) or ""
                ).strip()
                trust_writable = bool(
                    enabled_item.data(CUSTOM_INSTRUCTION_TRUST_WRITABLE_ROLE)
                )
            if not entry_id:
                entry_id = uuid.uuid4().hex

            commands.append(
                {
                    "id": entry_id,
                    "enabled": (
                        enabled_item.checkState() == Qt.Checked
                        if enabled_item is not None
                        else True
                    ),
                    "phrase": phrase,
                    "aliases": self._split_custom_instruction_aliases(aliases_text),
                    "command": command,
                    "mode": mode,
                    "elevate": (
                        elevate_item.checkState() == Qt.Checked
                        if elevate_item is not None
                        else False
                    ),
                    "trust_writable_target": trust_writable,
                    "phonetic": True,
                }
            )
        return commands

    def _clean_stale_elevated_tasks(self):
        try:
            from aria.core.wakeword.elevation import (
                list_aria_tasks,
                unregister_elevated_task,
            )
        except Exception as exc:
            QMessageBox.warning(self, "清理失败", f"无法加载管理员任务工具：{exc}")
            return

        current_ids = {
            entry.get("id")
            for entry in self._collect_custom_instructions()
            if entry.get("id")
        }
        tasks = list_aria_tasks()
        orphans = [
            task
            for task in tasks
            if task.get("entry_id_from_description")
            and task.get("entry_id_from_description") not in current_ids
        ]
        if not orphans:
            QMessageBox.information(self, "清理管理员任务", "无残留任务")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("清理残留管理员任务")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("选择要删除的 Aria 管理员任务："))
        task_list = QListWidget()
        for task in orphans:
            label = (
                f"{task.get('task_name', '')}  "
                f"entry_id={task.get('entry_id_from_description', '')}  "
                f"last_run={task.get('last_run', '')}"
            )
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, task.get("entry_id_from_description", ""))
            task_list.addItem(item)
        layout.addWidget(task_list)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return

        errors = []
        deleted = 0
        for index in range(task_list.count()):
            item = task_list.item(index)
            if item.checkState() != Qt.Checked:
                continue
            entry_id = str(item.data(Qt.UserRole) or "")
            ok, err = unregister_elevated_task(entry_id)
            if ok:
                deleted += 1
            else:
                errors.append(f"{entry_id}: {err}")
        if errors:
            QMessageBox.warning(
                self,
                "清理管理员任务",
                "部分任务删除失败：\n" + "\n".join(errors[:5]),
            )
        else:
            QMessageBox.information(
                self, "清理管理员任务", f"已删除 {deleted} 个残留任务"
            )

    # ==========================================================================
    # Tab 3: Hotwords (Simplified UX - redesign)
    # ==========================================================================
    def _create_hotwords_tab(self) -> QWidget:
        """Hotwords page — restructured into 3 sub-tabs:

          1. 我的词汇          — user-curated proper nouns + weights
          2. 自动学习的热词    — screen-OCR-driven, LLM-gated session hotwords
          3. 纠错规则          — legacy manual replacement rules

        The "我的词汇" / "纠错规则" sub-tabs preserve every widget that
        save_config / load_config / on_engine_changed already reference
        (self.vocab_table, self._hotword_weights, self.replace_table,
        self.domain_ctx, self.advanced_group, etc) so this restructure is
        UI-only and does not touch persistence.
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(30, 30, 30, 30)

        # --- Header ---
        layout.addWidget(QLabel("<h2>专业词汇</h2>"))
        subtitle = QLabel(
            "管理识别相关的所有词汇：手配的专业词、屏幕自动学的热词、"
            "以及最后一道兜底的纠错规则。"
        )
        subtitle.setStyleSheet(
            self._label_style("secondary", extra="margin-bottom: 10px;")
        )
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        layout.addSpacing(8)

        # --- Sub-tabs ---
        sub_tabs = QTabWidget()
        sub_tabs.addTab(self._create_my_words_subtab(), "我的词汇")
        sub_tabs.addTab(self._create_auto_hotword_subtab(), "自动学习的热词")
        sub_tabs.addTab(self._create_rules_subtab(), "纠错规则")
        layout.addWidget(sub_tabs, 1)

        layout.addSpacing(8)

        # Hidden: keep enable_initial_prompt always true (no UI control)
        self._enable_initial_prompt = True

        return w

    def _create_my_words_subtab(self) -> QWidget:
        """Sub-tab 1: user-curated hotwords (the original page content)."""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)

        intro = QLabel(
            "添加您常用的专业术语、品牌名、人名等，系统会自动识别并纠正谐音错误"
        )
        intro.setStyleSheet(
            self._label_style("secondary", extra="margin-bottom: 10px;")
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # --- Usage context ---
        context_layout = QHBoxLayout()
        context_label = QLabel("使用场景:")
        self.domain_ctx = QLineEdit()
        self.domain_ctx.setPlaceholderText("如：编程开发、医疗诊断、法律咨询...")
        self.domain_ctx.setToolTip(
            "填写后会同时影响语音识别和 AI 润色，帮助区分同音词（如3D建模→顶点而非景点）"
        )
        context_layout.addWidget(context_label)
        context_layout.addWidget(self.domain_ctx, 1)
        layout.addLayout(context_layout)

        hint_label = QLabel("描述您的使用领域，可提高整体识别准确率（可选）")
        hint_label.setStyleSheet(self._label_style("muted", extra="margin-left: 70px;"))
        layout.addWidget(hint_label)

        layout.addSpacing(16)

        list_header = QLabel("<b>词汇列表</b>")
        layout.addWidget(list_header)

        # Keep the primary actions above the independently scrolling table.
        # Users naturally wheel over the table, which does not move the outer
        # settings page; placing these controls after the table made them
        # unreachable without deliberately scrolling the whole page first.
        btn_layout = QHBoxLayout()
        btn_add_word = QPushButton("+ 添加")
        btn_add_word.setToolTip("添加一个专业词汇，支持中英文")
        btn_add_word.clicked.connect(self._add_prompt_word)
        btn_layout.addWidget(btn_add_word)

        btn_import = QPushButton("批量导入")
        btn_import.setToolTip("一次导入多个词汇，每行一个")
        btn_import.clicked.connect(self._batch_import_words)
        btn_layout.addWidget(btn_import)

        btn_remove_word = QPushButton("删除选中")
        btn_remove_word.setToolTip("删除表格中选中的词汇（可多选）")
        btn_remove_word.clicked.connect(self._remove_prompt_words)
        btn_layout.addWidget(btn_remove_word)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self._hotword_guide_label = QLabel(
            "权重越高，识别偏向越强。新词默认 0.3（轻量提示）"
        )
        self._hotword_guide_label.setStyleSheet(
            self._label_style("secondary", font_size=12, extra="margin-bottom: 5px;")
        )
        layout.addWidget(self._hotword_guide_label)

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

        self.vocab_table = QTableWidget(0, 3)
        self.vocab_table.setItemDelegate(self._table_editor_delegate)
        self.vocab_table.setHorizontalHeaderLabels(["词汇", "权重", "类型"])
        self.vocab_table.horizontalHeader().setStretchLastSection(False)
        self.vocab_table.setColumnWidth(0, 200)
        self.vocab_table.setColumnWidth(1, 130)
        self.vocab_table.setColumnWidth(2, 40)
        self.vocab_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.vocab_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.vocab_table.setMinimumHeight(220)
        self.vocab_table.verticalHeader().setVisible(False)
        layout.addWidget(self.vocab_table, 1)

        self._hotword_weights = {}
        return w

    def _create_rules_subtab(self) -> QWidget:
        """Sub-tab 3: legacy manual replacement rules."""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)

        # Keep self.advanced_group as a reference so on_engine_changed and
        # other consumers still find the same widget; nest it as the body of
        # this sub-tab.
        self.advanced_group = QGroupBox("手动纠错规则（兜底）")
        self.advanced_group.setCheckable(False)
        adv_layout = QVBoxLayout()

        adv_hint = QLabel(
            "大部分谐音错误已经被前面两层（专业词汇 + 自动学习）自动处理。"
            "只有在遇到反复识别错误时，才需要在这里加一条强制替换规则。"
        )
        adv_hint.setStyleSheet(self._label_style("secondary"))
        adv_hint.setWordWrap(True)
        adv_layout.addWidget(adv_hint)
        adv_layout.addSpacing(10)

        self.replace_table = QTableWidget(0, 2)
        self.replace_table.setItemDelegate(self._table_editor_delegate)
        self.replace_table.setHorizontalHeaderLabels(["识别错误", "替换为"])
        self.replace_table.horizontalHeader().setStretchLastSection(True)
        self.replace_table.setMinimumHeight(160)
        adv_layout.addWidget(self.replace_table, 1)

        tbl_btn = QHBoxLayout()
        btn_add_rule = QPushButton("+ 添加规则")
        btn_add_rule.setToolTip("添加一条手动纠错规则，如「景点 → 顶点」")
        btn_add_rule.clicked.connect(self._add_replacement_row)
        tbl_btn.addWidget(btn_add_rule)
        btn_remove_rule = QPushButton("- 删除选中")
        btn_remove_rule.setToolTip("删除选中的纠错规则")
        btn_remove_rule.clicked.connect(self._remove_replacement_row)
        tbl_btn.addWidget(btn_remove_rule)
        tbl_btn.addStretch()
        adv_layout.addLayout(tbl_btn)

        self.advanced_group.setLayout(adv_layout)
        layout.addWidget(self.advanced_group, 1)
        return w

    # ----------------------------------------------------- auto-hotword UI

    def _create_auto_hotword_subtab(self) -> QWidget:
        """Sub-tab 2: screen-OCR-driven auto hotwords gated by an LLM reviewer.

        Data flows in two directions:
        - approval / rejection / config edits stay LOCAL to this UI until
          the user clicks 保存 (config goes into hotwords.json) or interacts
          with a row's action buttons (decisions go into data/auto_hotwords.json
          via SessionHotwordTracker.manual_override / save).
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)

        # --- Header / enable switch ---
        intro = QLabel(
            "屏幕反复出现的中文专名（动漫角色、产品名、技术术语等）会被"
            "持续累积，每隔数小时由 LLM 自动审核一批，通过的词会作为"
            "<b>弱位</b>热词自动注入语音识别。"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(self._label_style("secondary"))
        layout.addWidget(intro)

        # --- OCR mode tri-radio (single source of truth) ---
        # Drives chk_screen_ocr (在通用 / VAD 子 tab) + chk_screen_ocr_polish +
        # chk_auto_hotword_enabled together so users don't have to coordinate
        # three flags. Backend persists via AriaApp.set_ocr_mode.
        ocr_mode_box = QGroupBox("屏幕识别策略")
        ocr_mode_layout = QVBoxLayout()

        self.ocr_mode_group = QButtonGroup(w)
        self.radio_ocr_off = QRadioButton(
            "关闭 — 不读屏幕，不学新词（最省成本 / 最高隐私）"
        )
        self.radio_ocr_auto = QRadioButton(
            "仅自动学习 — 读屏幕但只喂给热词池，不进润色提示词（推荐，缓存命中高）"
        )
        self.radio_ocr_full = QRadioButton(
            "开启OCR — 实时屏幕文本进入润色提示词（识别最准 / API 成本最高）"
        )
        self.radio_ocr_off.setToolTip(
            "完全关闭屏幕识别相关链路：不截屏、不 OCR、不学习、不注入润色。"
        )
        self.radio_ocr_auto.setToolTip(
            "继续读屏幕识别新名词、累积自动热词；但屏幕原文不会写入润色 LLM 提示词，"
            "DeepSeek 前缀缓存命中率从 ~20% 提升到 ~85-95%，月成本可控制在 ¥10 以内。"
        )
        self.radio_ocr_full.setToolTip(
            "每次说话都把屏幕原文塞进润色 LLM 提示词。识别最准但提示词每帧都变，"
            "前缀缓存基本失效，月 API 成本约 ¥30-45。"
        )
        self.ocr_mode_group.addButton(self.radio_ocr_off)
        self.ocr_mode_group.addButton(self.radio_ocr_auto)
        self.ocr_mode_group.addButton(self.radio_ocr_full)
        ocr_mode_layout.addWidget(self.radio_ocr_off)
        ocr_mode_layout.addWidget(self.radio_ocr_auto)
        ocr_mode_layout.addWidget(self.radio_ocr_full)
        ocr_mode_box.setLayout(ocr_mode_layout)
        layout.addWidget(ocr_mode_box)

        # When the user picks a tier, immediately reflect the change on the
        # three underlying checkboxes so save_config() writes consistent state
        # even if the user never opened the VAD tab.
        self.radio_ocr_off.toggled.connect(
            lambda checked: checked and self._apply_ocr_mode_to_flags("off")
        )
        self.radio_ocr_auto.toggled.connect(
            lambda checked: checked and self._apply_ocr_mode_to_flags("auto")
        )
        self.radio_ocr_full.toggled.connect(
            lambda checked: checked and self._apply_ocr_mode_to_flags("full")
        )
        layout.addSpacing(6)

        self.chk_auto_hotword_enabled = QCheckBox("自动学习（由上方策略控制）")
        self.chk_auto_hotword_enabled.setToolTip(
            "请用上方“关闭 / 仅自动学习 / 开启OCR”选择；这里仅显示实际状态。"
        )
        self.chk_auto_hotword_enabled.setEnabled(False)
        layout.addWidget(self.chk_auto_hotword_enabled)
        layout.addSpacing(6)

        # --- Status / actions row ---
        status_row = QHBoxLayout()
        self.lbl_auto_hotword_stats = QLabel("已批准 0 ｜ 待审 0 ｜ 已拒绝 0")
        self.lbl_auto_hotword_stats.setStyleSheet(
            self._label_style("muted", font_size=12)
        )
        status_row.addWidget(self.lbl_auto_hotword_stats)
        status_row.addStretch()

        btn_refresh = QPushButton("刷新")
        btn_refresh.setToolTip("从 data/auto_hotwords.json 重新读取最新状态")
        btn_refresh.clicked.connect(self._refresh_auto_hotword_tables)
        status_row.addWidget(btn_refresh)

        btn_review_now = QPushButton("立即审查")
        btn_review_now.setToolTip("马上让 LLM 审一次当前 pending 池，不等自动审查间隔")
        btn_review_now.clicked.connect(self._run_auto_hotword_review_now)
        status_row.addWidget(btn_review_now)
        layout.addLayout(status_row)
        layout.addSpacing(4)

        # --- Three sub-tables (approved / pending / rejected) ---
        self._auto_hotword_inner_tabs = QTabWidget()

        self.tbl_auto_hotword_approved = self._make_auto_hotword_table(
            extra_actions=[("→ 拒绝", "drop"), ("重置", "reset")]
        )
        self._auto_hotword_inner_tabs.addTab(
            self._wrap_with_action_bar(
                self.tbl_auto_hotword_approved,
                [("→ 拒绝", "drop"), ("重置", "reset")],
                target_status="approved",
            ),
            "已批准（注入 ASR）",
        )

        self.tbl_auto_hotword_pending = self._make_auto_hotword_table(
            extra_actions=[("→ 批准", "keep"), ("→ 拒绝", "drop")]
        )
        self._auto_hotword_inner_tabs.addTab(
            self._wrap_with_action_bar(
                self.tbl_auto_hotword_pending,
                [("→ 批准", "keep"), ("→ 拒绝", "drop")],
                target_status="pending",
            ),
            "待审查（积累中）",
        )

        self.tbl_auto_hotword_rejected = self._make_auto_hotword_table(
            extra_actions=[("→ 批准", "keep"), ("重置", "reset")]
        )
        self._auto_hotword_inner_tabs.addTab(
            self._wrap_with_action_bar(
                self.tbl_auto_hotword_rejected,
                [("→ 批准", "keep"), ("重置", "reset")],
                target_status="rejected",
            ),
            "已拒绝（黑名单）",
        )

        layout.addWidget(self._auto_hotword_inner_tabs, 1)

        layout.addSpacing(8)

        # --- Reviewer LLM API config (collapsible) ---
        self.grp_auto_hotword_api = QGroupBox("审查 LLM 配置（默认同步主 API）")
        self.grp_auto_hotword_api.setCheckable(True)
        self.grp_auto_hotword_api.setChecked(False)
        api_layout = QFormLayout()

        self.txt_auto_hotword_api_url = QLineEdit()
        self.txt_auto_hotword_api_url.setPlaceholderText(
            "留空 = 复用「润色」页设的主 API 地址"
        )
        api_layout.addRow("API URL:", self.txt_auto_hotword_api_url)

        self.txt_auto_hotword_api_key = QLineEdit()
        self.txt_auto_hotword_api_key.setEchoMode(QLineEdit.Password)
        self.txt_auto_hotword_api_key.setPlaceholderText("留空 = 复用主 API Key")
        api_layout.addRow("API Key:", self.txt_auto_hotword_api_key)

        self.txt_auto_hotword_model = QLineEdit()
        self.txt_auto_hotword_model.setPlaceholderText(
            "留空 = 复用主模型；可填 deepseek-v4-flash / deepseek-v4-pro 等"
        )
        api_layout.addRow("模型:", self.txt_auto_hotword_model)

        self.spn_auto_hotword_review_interval = QSpinBox()
        self.spn_auto_hotword_review_interval.setRange(1, 24)
        self.spn_auto_hotword_review_interval.setValue(6)
        self.spn_auto_hotword_review_interval.setSuffix(" 小时")
        self.spn_auto_hotword_review_interval.setToolTip(
            "距上次成功审查满 N 小时且候选池足够时，自动送审一批。"
            "默认 6 小时 = 每天最多约 4 批。"
        )
        api_layout.addRow("自动审查间隔:", self.spn_auto_hotword_review_interval)

        self.spn_auto_hotword_min_batch = QSpinBox()
        self.spn_auto_hotword_min_batch.setRange(1, 200)
        self.spn_auto_hotword_min_batch.setValue(8)
        self.spn_auto_hotword_min_batch.setSuffix(" 个")
        self.spn_auto_hotword_min_batch.setToolTip(
            "自动审查至少攒够多少个待审候选才触发；手动「立即审查」不受此限制。"
        )
        api_layout.addRow("自动审查最小批次:", self.spn_auto_hotword_min_batch)

        self.spn_auto_hotword_min_count = QSpinBox()
        self.spn_auto_hotword_min_count.setRange(1, 50)
        self.spn_auto_hotword_min_count.setValue(3)
        self.spn_auto_hotword_min_count.setSuffix(" 次")
        self.spn_auto_hotword_min_count.setToolTip(
            "屏幕上同一个词出现满 N 次才会被送进审查队列"
        )
        api_layout.addRow("送审词频阈值:", self.spn_auto_hotword_min_count)

        self.spn_auto_hotword_max_terms = QSpinBox()
        self.spn_auto_hotword_max_terms.setRange(10, 200)
        self.spn_auto_hotword_max_terms.setValue(50)
        self.spn_auto_hotword_max_terms.setSuffix(" 个/次")
        self.spn_auto_hotword_max_terms.setToolTip("一次审查最多送多少个候选词给 LLM")
        api_layout.addRow("单次审查上限:", self.spn_auto_hotword_max_terms)

        self.chk_auto_hotword_review_on_startup = QCheckBox("启动时跑一次审查")
        api_layout.addRow("", self.chk_auto_hotword_review_on_startup)

        self.grp_auto_hotword_api.setLayout(api_layout)
        layout.addWidget(self.grp_auto_hotword_api)

        # First populate
        self._refresh_auto_hotword_tables()
        # The reviewer runs in the backend thread/process. Keep this page from
        # showing stale "已批准 2" counts after a background review finishes.
        self._auto_hotword_refresh_timer = QTimer(w)
        self._auto_hotword_refresh_timer.setInterval(15000)
        self._auto_hotword_refresh_timer.timeout.connect(
            self._refresh_auto_hotword_tables
        )
        self._auto_hotword_refresh_timer.start()
        return w

    def _make_auto_hotword_table(self, extra_actions=None) -> QTableWidget:
        """Build a 4-column read-only table for auto-hotword inspection.

        Columns: term / count / titles / reviewer reason.
        Per-row mutation is done from the bottom action bar via selected rows
        (see `_wrap_with_action_bar`), not from inline buttons — keeps the
        table lightweight and consistent with the rest of the settings UI.
        """
        tbl = QTableWidget(0, 4)
        tbl.setItemDelegate(self._table_editor_delegate)
        tbl.setHorizontalHeaderLabels(
            ["词", "出现次数", "来源窗口（截取）", "LLM 理由"]
        )
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.setColumnWidth(0, 180)
        tbl.setColumnWidth(1, 80)
        tbl.setColumnWidth(2, 220)
        tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        tbl.setSelectionMode(QAbstractItemView.ExtendedSelection)
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.setMinimumHeight(220)
        return tbl

    def _wrap_with_action_bar(
        self, table: QTableWidget, actions: list, target_status: str
    ) -> QWidget:
        """Wrap a table with an action bar that operates on selected rows."""
        wrap = QWidget()
        wlayout = QVBoxLayout(wrap)
        wlayout.setContentsMargins(0, 0, 0, 0)
        wlayout.addWidget(table, 1)

        bar = QHBoxLayout()
        bar.addStretch()
        for label, decision in actions:
            btn = QPushButton(label)
            btn.clicked.connect(
                lambda _checked=False, t=table, d=decision: self._apply_auto_hotword_decision(
                    t, d
                )
            )
            bar.addWidget(btn)
        wlayout.addLayout(bar)
        return wrap

    def _load_auto_hotword_tracker(self):
        """Lazy-load a SessionHotwordTracker pointing at the same data file
        the running app uses. Returns None when imports fail (e.g. running
        the settings window standalone for layout debugging)."""
        try:
            from aria.core.hotword.session_tracker import SessionHotwordTracker
            from aria.core.utils.paths import get_base_path
        except Exception:
            return None
        try:
            data_path = get_base_path() / "data" / "auto_hotwords.json"
            return SessionHotwordTracker(data_path)
        except Exception:
            return None

    def _refresh_auto_hotword_tables(self):
        """Re-read data/auto_hotwords.json and repaint the three tables."""
        tracker = self._load_auto_hotword_tracker()
        if tracker is None:
            self.lbl_auto_hotword_stats.setText("（无法读取自动热词数据文件）")
            return
        # Snapshot the internal dict directly so we have access to status,
        # last_seen and review_reason for richer rendering — public methods
        # only expose subsets.
        with tracker._lock:
            entries = list(tracker._terms.items())
        approved, pending, rejected = [], [], []
        for term, e in entries:
            row = (
                term,
                int(e.get("count", 0)),
                e.get("titles") or [],
                e.get("review_reason", ""),
            )
            status = e.get("status", "pending")
            if status == "approved":
                approved.append(row)
            elif status == "rejected":
                rejected.append(row)
            else:
                pending.append(row)
        # Sort by count desc within each bucket
        approved.sort(key=lambda r: -r[1])
        pending.sort(key=lambda r: -r[1])
        rejected.sort(key=lambda r: -r[1])

        self._populate_auto_hotword_table(self.tbl_auto_hotword_approved, approved)
        self._populate_auto_hotword_table(self.tbl_auto_hotword_pending, pending)
        self._populate_auto_hotword_table(self.tbl_auto_hotword_rejected, rejected)
        enabled = bool(
            hasattr(self, "chk_auto_hotword_enabled")
            and self.chk_auto_hotword_enabled.isChecked()
        )
        if not enabled:
            stats_text = (
                "自动学习未开启｜它只学习屏幕上重复出现的专名，不直接从口述累积"
            )
        elif not entries:
            stats_text = "自动学习已开启｜尚未从屏幕发现候选专名"
        else:
            stats_text = (
                f"已批准 {len(approved)} ｜ 待审 {len(pending)} ｜ "
                f"已拒绝 {len(rejected)}"
            )
        self.lbl_auto_hotword_stats.setText(stats_text)

    def _populate_auto_hotword_table(self, table: QTableWidget, rows: list):
        table.setRowCount(0)
        for term, count, titles, reason in rows:
            r = table.rowCount()
            table.insertRow(r)
            table.setItem(r, 0, QTableWidgetItem(term))
            table.setItem(r, 1, QTableWidgetItem(str(count)))
            t_str = " / ".join(titles[:3]) if titles else ""
            table.setItem(r, 2, QTableWidgetItem(t_str))
            table.setItem(r, 3, QTableWidgetItem(reason))

    def _apply_auto_hotword_decision(self, table: QTableWidget, decision: str):
        """Apply manual override (keep/drop/reset) to all selected rows."""
        rows = sorted({i.row() for i in table.selectedItems()})
        if not rows:
            QMessageBox.information(self, "提示", "请先选中要操作的词（可多选）")
            return
        tracker = self._load_auto_hotword_tracker()
        if tracker is None:
            QMessageBox.warning(self, "失败", "无法读取自动热词数据文件")
            return
        terms = []
        for row in rows:
            item = table.item(row, 0)
            if item:
                terms.append(item.text())
        changed = 0
        for t in terms:
            if tracker.manual_override(t, decision, reason="用户在设置面板手动调整"):
                changed += 1
        try:
            tracker.save()
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", f"无法写入数据文件：{exc}")
            return
        self._refresh_auto_hotword_tables()
        QMessageBox.information(
            self, "完成", f"已对 {changed} 个词执行『{decision}』操作。"
        )

    def _run_auto_hotword_review_now(self):
        """Trigger an out-of-cycle LLM review on the running Aria process.

        We can't just call the reviewer from the settings window — Aria runs
        in a different process. So this writes a sentinel file the running
        AriaApp polls; if AriaApp isn't running, we fall back to running the
        review directly here (single-process dev case).
        """
        # Best-effort: try to talk to the running AriaApp first.
        try:
            from aria.core.utils.paths import get_base_path

            sentinel = get_base_path() / "data" / "auto_hotword_review_request.flag"
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("requested by settings UI\n", encoding="utf-8")
            QMessageBox.information(
                self,
                "已请求审查",
                "已通知 Aria 在后台跑一次审查。完成后可点「刷新」查看结果。",
            )
        except Exception as exc:
            QMessageBox.warning(self, "失败", f"无法发起审查请求：{exc}")

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
            "输入词汇（支持中英文混合）:\n\n示例：DeepSeek、GitHub、第一性原理",
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
            "每行一个词汇（或用逗号、顿号分隔）:\n\n示例:\npython\ngithub\nreview",
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
        self.radio_off.setToolTip("语音识别结果直接输出，不经过任何润色处理")
        self.radio_fast = QRadioButton("本地润色 (需自行配置模型)")
        self.radio_fast.setToolTip(
            "使用本地 GGUF 模型离线润色，需在高级设置中配置模型路径"
        )
        self.radio_quality = QRadioButton("高质量模式 (API 润色, ~1.7s)")
        self.radio_quality.setToolTip(
            "通过云端 API 调用大语言模型润色，效果最好但需要网络"
        )
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

        # 润色风格 — 3 档单选（逐字保真 / 顺畅口语 / 结构化文档）。
        # UI 权威状态写入 polish_style；同时派生两个 config bool 供引擎/manager 读：
        #   逐字保真   filter_filler_words=False auto_structure=False → LOOSE
        #   顺畅口语   filter_filler_words=True  auto_structure=False → CLEAN
        #   结构化文档 filter_filler_words=True  auto_structure=True  → STRUCTURED
        style_label = QLabel("润色风格：")
        skill_layout.addWidget(style_label)

        self.style_group = QButtonGroup(w)
        self.radio_style_verbatim = QRadioButton("逐字保真")
        self.radio_style_verbatim.setToolTip(
            "只修错别字、标点和明显识别错误，保留你说的每个词和口头禅（呃/就是/那个）"
        )
        self.radio_style_smooth = QRadioButton("顺畅口语")
        self.radio_style_smooth.setToolTip(
            "去掉口头禅和结巴重复，保持单行不分段，读起来通顺自然"
        )
        self.radio_style_structured = QRadioButton("结构化文档")
        self.radio_style_structured.setToolTip(
            "在顺畅口语基础上重组语序、分段、编号，适合长段落 / 邮件 / 文档"
        )
        self.style_group.addButton(self.radio_style_verbatim)
        self.style_group.addButton(self.radio_style_smooth)
        self.style_group.addButton(self.radio_style_structured)
        skill_layout.addWidget(self.radio_style_verbatim)
        skill_layout.addWidget(self.radio_style_smooth)
        skill_layout.addWidget(self.radio_style_structured)

        # 动态说明（随选中风格刷新）
        self._structure_hint = QLabel()
        self._structure_hint.setWordWrap(True)
        self._structure_hint.setStyleSheet(
            self._label_style("muted", font_size=12, extra="margin-left: 24px;")
        )
        skill_layout.addWidget(self._structure_hint)

        skill_layout.addSpacing(8)

        # CLI/终端独立开关：即使全局选「逐字保真」，CLI 里也自动去口水（保持单行）。
        self.chk_cli_destutter = QCheckBox("在 CLI / 终端里自动去口水（保持单行）")
        self.chk_cli_destutter.setToolTip(
            "对终端 / 命令行 / AI 编程工具口述时，自动去掉口头禅和结巴，并保持单行"
            "（分段、编号会被终端当成命令执行）。关掉则 CLI 跟随上面的全局风格。"
        )
        self.chk_cli_destutter.setChecked(True)
        skill_layout.addWidget(self.chk_cli_destutter)

        # 联动：选中风格变化时刷新说明文字 + 切换 prompt 编辑器 tab
        self.radio_style_verbatim.toggled.connect(self._update_structure_hint)
        self.radio_style_smooth.toggled.connect(self._update_structure_hint)
        self.radio_style_structured.toggled.connect(self._update_structure_hint)
        self.radio_style_verbatim.setChecked(True)  # 初值，load 会按配置覆盖
        self._update_structure_hint()

        skill_layout.addSpacing(12)

        # 个性化规则
        rules_label = QLabel("个性化规则（每行一条）：")
        skill_layout.addWidget(rules_label)
        self.personalization_rules_edit = QPlainTextEdit()
        self.personalization_rules_edit.setToolTip(
            "用自然语言描述你的润色偏好，每行一条规则"
        )
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
        reply_hint.setStyleSheet(self._label_style("muted", font_size=12))
        reply_layout.addWidget(reply_hint)
        self.reply_style_edit = QPlainTextEdit()
        self.reply_style_edit.setToolTip("定义 AI 帮你回复消息时的语气和风格")
        self.reply_style_edit.setPlaceholderText(
            "例如：\n回复简短一些，像朋友聊天\n语气专业正式\n用轻松幽默的方式回复"
        )
        self.reply_style_edit.setMaximumHeight(80)
        reply_layout.addWidget(self.reply_style_edit)
        layout.addWidget(reply_group)

        layout.addSpacing(20)

        # Prompt editor — v5.0 双模板（loose / structured 各一个 tab）
        layout.addWidget(QLabel("<b>高质量模式 Prompt 模板（按润色模式编辑）：</b>"))

        self._prompt_tabs = QTabWidget()

        # 保守模式 tab —— 不勾选"结构化文本"时使用
        loose_tab = QWidget()
        loose_layout = QVBoxLayout(loose_tab)
        loose_layout.setContentsMargins(0, 8, 0, 0)
        loose_hint = QLabel("不勾选「结构化文本」时使用：")
        loose_hint.setStyleSheet(self._label_style("muted", font_size=12))
        loose_layout.addWidget(loose_hint)
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setToolTip(
            "保守模式 Prompt：只修错字、标点、明显误识。"
            "支持 {text}、{hotwords_chinese}、{hotwords_english} 变量"
        )
        self.prompt_edit.setPlainText(self.DEFAULT_PROMPT_LOOSE)
        self.prompt_edit.setMinimumHeight(220)
        loose_layout.addWidget(self.prompt_edit)
        self._prompt_tabs.addTab(loose_tab, "保守模式")

        # 结构化模式 tab —— 勾选"结构化文本"时使用
        structured_tab = QWidget()
        structured_layout = QVBoxLayout(structured_tab)
        structured_layout.setContentsMargins(0, 8, 0, 0)
        structured_hint = QLabel("勾选「结构化文本」时使用：")
        structured_hint.setStyleSheet(self._label_style("muted", font_size=12))
        structured_layout.addWidget(structured_hint)
        self.prompt_edit_structured = QPlainTextEdit()
        self.prompt_edit_structured.setToolTip(
            "结构化模式 Prompt：在保守模式基础上重组语序、分段、编号。"
            "支持 {text}、{hotwords_chinese}、{hotwords_english} 变量"
        )
        self.prompt_edit_structured.setPlainText(self.DEFAULT_PROMPT_STRUCTURED)
        self.prompt_edit_structured.setMinimumHeight(220)
        structured_layout.addWidget(self.prompt_edit_structured)
        self._prompt_tabs.addTab(structured_tab, "结构化模式")

        layout.addWidget(self._prompt_tabs)
        # 自动切到当前选中的模式 tab，方便用户对照查看
        self._prompt_tabs.setCurrentIndex(
            1 if self.radio_style_structured.isChecked() else 0
        )

        # Restore default buttons —— 分别恢复两套模板
        btn_layout = QHBoxLayout()
        btn_restore_current = QPushButton("恢复当前模式默认 Prompt")
        btn_restore_current.setToolTip(
            "将当前 tab 的 Prompt 模板恢复为系统默认内容，自定义修改将丢失"
        )
        btn_restore_current.setObjectName("dangerBtn")
        btn_restore_current.clicked.connect(self._restore_default_prompt)
        btn_layout.addWidget(btn_restore_current)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addStretch()

        return w

    def _restore_default_prompt(self):
        """恢复当前选中 tab 的 Prompt 模板。

        v5.0 起每种润色模式各有一套独立模板，此操作只恢复当前显示的那一套。
        """
        is_structured = self._prompt_tabs.currentIndex() == 1
        mode_label = "结构化模式" if is_structured else "保守模式"
        reply = QMessageBox.question(
            self,
            "确认",
            f"确定要把【{mode_label}】的 Prompt 模板恢复为默认值吗？\n"
            f"另一个模式的模板不受影响。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            if is_structured:
                self.prompt_edit_structured.setPlainText(self.DEFAULT_PROMPT_STRUCTURED)
            else:
                self.prompt_edit.setPlainText(self.DEFAULT_PROMPT_LOOSE)

    def _update_structure_hint(self, checked=None) -> None:
        """根据当前选中的润色风格刷新说明文字 + 切换 prompt 编辑器 tab。

        作为 QRadioButton.toggled 的槽函数时会收到一个 bool（忽略，统一以当前
        选中状态为准）；也可无参手动调用（初始化 / load 后）。
        """
        if self.radio_style_structured.isChecked():
            self._structure_hint.setText(
                "结构化文档：去口水的基础上，可重组语序、合并跨句、按 1. 2. 3. 分段编号。\n"
                "适合长段落口述、邮件、文档；终端里会自动降级为「顺畅口语」（单行）。"
            )
            tab_idx = 1
        elif self.radio_style_smooth.isChecked():
            self._structure_hint.setText(
                "顺畅口语：去掉「呃 / 嗯 / 就是 / 那个」等口头禅和结巴重复，保持单行不分段。\n"
                "适合聊天、消息、给 AI 或终端口述指令。"
            )
            tab_idx = 0
        else:
            self._structure_hint.setText(
                "逐字保真：只修错别字、标点和明显识别错误，保留你说的每个词。\n"
                "适合需要保留原话的场景；CLI 里若开了下方开关仍会自动去口水。"
            )
            tab_idx = 0
        if hasattr(self, "_prompt_tabs"):
            self._prompt_tabs.setCurrentIndex(tab_idx)

    # ==========================================================================
    # Tab 4: API
    # ==========================================================================
    def _create_api_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(30, 30, 30, 30)

        layout.addWidget(QLabel("<h2>API 设置</h2>"))

        quick_group = QGroupBox("推荐配置：开启高质量屏幕感知")
        quick_layout = QVBoxLayout(quick_group)
        quick_hint = QLabel(
            "填写 DeepSeek API 后，Aria 会把当前屏幕 OCR 作为动态上下文交给 AI 润色层，"
            "明显提升人名、专业术语、医学/化学名词、3D/编程术语的同音纠错准确率。\n"
            "API Key 只保存在本机 config/hotwords.json；如果 API 超时或失败，会自动回退原始识别结果。"
        )
        quick_hint.setWordWrap(True)
        quick_hint.setStyleSheet(
            self._label_style("secondary", extra="margin-bottom: 8px;")
        )
        quick_layout.addWidget(quick_hint)

        quick_btn_layout = QHBoxLayout()
        self._deepseek_quick_btn = QPushButton("一键填入 DeepSeek 推荐配置")
        self._deepseek_quick_btn.setToolTip(
            "自动填写 API 地址和推荐模型；之后只需要粘贴你的 DeepSeek API Key 并点击测试"
        )
        self._deepseek_quick_btn.clicked.connect(self._fill_deepseek_recommended)
        quick_btn_layout.addWidget(self._deepseek_quick_btn)
        quick_btn_layout.addStretch()
        quick_layout.addLayout(quick_btn_layout)
        layout.addWidget(quick_group)

        # === 主 API 设置 ===
        main_group = QGroupBox("主 API（默认）")
        main_form = QFormLayout(main_group)

        # API URL + auto-recognize button
        url_layout = QHBoxLayout()
        self.api_url = QLineEdit()
        self.api_url.setPlaceholderText("输入服务商名称或完整地址，如 deepseek")
        url_layout.addWidget(self.api_url, 1)

        self._auto_fill_btn = QPushButton("识别")
        self._auto_fill_btn.setToolTip(
            "输入服务商名称后点击自动填写完整地址\n"
            "支持: deepseek, openrouter, openai, siliconflow,\n"
            "groq, moonshot, stepfun, dashscope, zhipu,\n"
            "baichuan, together, mistral, ollama, lmstudio"
        )
        self._auto_fill_btn.clicked.connect(self._auto_fill_api_url)
        url_layout.addWidget(self._auto_fill_btn)
        main_form.addRow("API 地址:", url_layout)

        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("sk-...")
        self.api_key.setToolTip("API 密钥仅保存在本地配置文件中，不会上传到任何服务器")
        main_form.addRow("API 密钥:", self.api_key)

        # Model: editable combo + fetch button
        model_layout = QHBoxLayout()
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.NoInsert)
        self.model.lineEdit().setPlaceholderText("填写或从列表选择模型")
        model_layout.addWidget(self.model, 1)

        self._fetch_models_btn = QPushButton("获取模型列表")
        self._fetch_models_btn.setToolTip("从 API 获取可用模型列表")
        self._fetch_models_btn.clicked.connect(self._fetch_models)
        model_layout.addWidget(self._fetch_models_btn)
        main_form.addRow("模型名称:", model_layout)

        self.timeout = QSpinBox()
        self.timeout.setRange(5, 120)
        self.timeout.setValue(10)
        self.timeout.setSuffix(" 秒")
        self.timeout.setToolTip("API 请求超时时间，超时后会跳过润色直接输出原文")
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

        backup_url_layout = QHBoxLayout()
        self.api_url_backup = QLineEdit()
        self.api_url_backup.setPlaceholderText(
            "留空则不启用；或输入服务商名称如 openrouter"
        )
        backup_url_layout.addWidget(self.api_url_backup, 1)

        self._auto_fill_backup_btn = QPushButton("识别")
        self._auto_fill_backup_btn.setToolTip(
            "输入服务商名称后点击自动填写完整地址（不会填模型，请自行选择）"
        )
        self._auto_fill_backup_btn.clicked.connect(self._auto_fill_backup_api_url)
        backup_url_layout.addWidget(self._auto_fill_backup_btn)
        backup_form.addRow("备用 API 地址:", backup_url_layout)

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

        status_group = QGroupBox("当前 API 状态")
        status_layout = QVBoxLayout(status_group)
        self.lbl_api_current_status = QLabel("当前：尚未收到运行状态")
        self.lbl_api_current_status.setStyleSheet(
            self._label_style("primary", font_size=12, bold=True)
        )
        self.lbl_api_current_status.setWordWrap(True)
        status_layout.addWidget(self.lbl_api_current_status)

        self.lbl_api_status_detail = QLabel(
            "打开高质量模式后，完成一次润色会显示耗时和切换原因。"
        )
        self.lbl_api_status_detail.setStyleSheet(self._label_style("secondary"))
        self.lbl_api_status_detail.setWordWrap(True)
        status_layout.addWidget(self.lbl_api_status_detail)

        status_actions = QHBoxLayout()
        self.btn_api_switch_primary = QPushButton("切回主 API")
        self.btn_api_switch_primary.setToolTip(
            "将后续润色请求切回主 API，并清空慢响应计数"
        )
        self.btn_api_switch_primary.setEnabled(False)
        self.btn_api_switch_primary.clicked.connect(self.apiSwitchBackRequested.emit)
        status_actions.addWidget(self.btn_api_switch_primary)
        status_actions.addStretch()
        status_layout.addLayout(status_actions)
        backup_layout.addWidget(status_group)

        layout.addWidget(backup_group)

        layout.addSpacing(20)

        # === ASR rescue (cloud second-pass transcription) ===
        rescue_group = QGroupBox("语音识别救援")
        rescue_layout = QVBoxLayout(rescue_group)

        rescue_hint = QLabel(
            "本地识别超时/失败时，把这段录音发送到阿里云百炼 "
            "Qwen3-ASR-Flash 做二次转写补救。需要百炼 API Key，"
            "按量计费（约 0.9 元/小时音频）。"
        )
        rescue_hint.setWordWrap(True)
        rescue_hint.setStyleSheet(self._label_style("secondary", font_size=11))
        rescue_layout.addWidget(rescue_hint)

        self.chk_asr_rescue_cloud = QCheckBox("启用云端二次转写补救")
        self.chk_asr_rescue_cloud.setChecked(False)
        rescue_layout.addWidget(self.chk_asr_rescue_cloud)

        rescue_form = QFormLayout()
        self.asr_rescue_api_key = QLineEdit()
        self.asr_rescue_api_key.setEchoMode(QLineEdit.Password)
        self.asr_rescue_api_key.setPlaceholderText("百炼 DashScope API Key（sk-...）")
        rescue_form.addRow("API 密钥:", self.asr_rescue_api_key)
        rescue_layout.addLayout(rescue_form)

        layout.addWidget(rescue_group)

        layout.addSpacing(20)

        # === Usage statistics (cost monitoring) ===
        self._build_usage_stats_section(layout)

        layout.addSpacing(20)

        btn_layout = QHBoxLayout()
        self._api_test_button = QPushButton("测试主 API")
        self._api_test_button.clicked.connect(self._test_api_connection)
        btn_layout.addWidget(self._api_test_button)

        self._api_test_backup_button = QPushButton("测试备用 API")
        self._api_test_backup_button.clicked.connect(self._test_backup_api_connection)
        btn_layout.addWidget(self._api_test_backup_button)

        layout.addLayout(btn_layout)

        layout.addStretch()
        return w

    # ---------------------------------------------------- usage statistics

    def _build_usage_stats_section(self, layout):
        """Cost monitoring: today / 7-day / 30-day totals broken down by call type.

        Reads from `data/cost/cost_*.jsonl` via CostTracker.aggregate(); this
        is purely retrospective — no live API calls happen when this panel is
        rendered. Refresh button re-reads the JSONL files.
        """
        grp = QGroupBox("用量统计与成本（基于 DeepSeek V4 Flash 实际定价）")
        grp.setCheckable(True)
        grp.setChecked(True)
        v = QVBoxLayout()

        hint = QLabel(
            "每次 LLM 调用（润色 / 自动热词审核 / 选区命令）都会按 "
            "<b>cache hit / cache miss / output</b> 三段记账，原始数据写在 "
            "<code>data/cost/cost_YYYY-MM-DD.jsonl</code>，可自行用 jq/Python 复盘。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(self._label_style("secondary", font_size=11))
        v.addWidget(hint)
        v.addSpacing(6)

        # Top summary line
        self.lbl_usage_summary = QLabel("(尚未读取数据)")
        self.lbl_usage_summary.setStyleSheet(
            self._label_style("primary", font_size=13, bold=True)
        )
        v.addWidget(self.lbl_usage_summary)
        v.addSpacing(4)

        # 7-day per-day bar (text-based)
        self.lbl_usage_per_day = QLabel("")
        self.lbl_usage_per_day.setWordWrap(True)
        self.lbl_usage_per_day.setStyleSheet(self._label_style("muted", font_size=11))
        v.addWidget(self.lbl_usage_per_day)
        v.addSpacing(4)

        # By-call-type table (compact)
        self.tbl_usage_by_type = QTableWidget(0, 5)
        self.tbl_usage_by_type.setItemDelegate(self._table_editor_delegate)
        self.tbl_usage_by_type.setHorizontalHeaderLabels(
            ["调用类型", "次数", "Prompt tokens", "Output tokens", "成本(¥)"]
        )
        self.tbl_usage_by_type.horizontalHeader().setStretchLastSection(False)
        self.tbl_usage_by_type.setColumnWidth(0, 200)
        self.tbl_usage_by_type.setColumnWidth(1, 80)
        self.tbl_usage_by_type.setColumnWidth(2, 130)
        self.tbl_usage_by_type.setColumnWidth(3, 130)
        self.tbl_usage_by_type.setColumnWidth(4, 100)
        self.tbl_usage_by_type.verticalHeader().setVisible(False)
        self.tbl_usage_by_type.setMinimumHeight(140)
        self.tbl_usage_by_type.setEditTriggers(QAbstractItemView.NoEditTriggers)
        v.addWidget(self.tbl_usage_by_type)

        # Range selector + actions
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("查看范围:"))
        self.cmb_usage_range = QComboBox()
        self.cmb_usage_range.addItems(["今天", "近 7 天", "近 30 天"])
        self.cmb_usage_range.currentIndexChanged.connect(
            lambda _i: self._refresh_usage_stats()
        )
        ctrl.addWidget(self.cmb_usage_range)
        ctrl.addStretch()
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self._refresh_usage_stats)
        ctrl.addWidget(btn_refresh)
        btn_open_dir = QPushButton("打开成本日志目录")
        btn_open_dir.clicked.connect(self._open_cost_log_dir)
        ctrl.addWidget(btn_open_dir)
        v.addLayout(ctrl)

        grp.setLayout(v)
        layout.addWidget(grp)

        # Initial populate (best-effort)
        try:
            self._refresh_usage_stats()
        except Exception:
            self.lbl_usage_summary.setText("(初次加载失败 — 点刷新重试)")

    def _refresh_usage_stats(self):
        """Reread cost JSONLs for the chosen range and update widgets."""
        try:
            from aria.core.utils.cost_tracker import CostTracker
        except Exception as exc:
            self.lbl_usage_summary.setText(f"(无法加载 CostTracker: {exc})")
            return
        import datetime as _dt

        end = _dt.date.today()
        idx = self.cmb_usage_range.currentIndex()
        days = (1, 7, 30)[idx if 0 <= idx < 3 else 1]
        start = end - _dt.timedelta(days=days - 1)

        agg = CostTracker.get_instance().aggregate(start_date=start, end_date=end)
        total = agg["total"]
        by_type = agg["by_type"]
        by_day = agg["by_day"]

        cost = total["cost_cny"]
        calls = total["calls"]
        avg_cost = (cost / calls) if calls else 0.0
        self.lbl_usage_summary.setText(
            f"<b>{days} 天合计:</b> {calls} 次调用 ｜ ¥{cost:.4f} ｜ "
            f"均价 ¥{avg_cost:.5f}/次 ｜ "
            f"prompt {total['prompt_tokens']:,} tok ｜ "
            f"cached {total['cached_tokens']:,} ｜ "
            f"miss {total['miss_tokens']:,} ｜ "
            f"output {total['completion_tokens']:,}"
        )

        # 7-day daily bar (always show last 7 days regardless of range)
        last7_start = end - _dt.timedelta(days=6)
        agg7 = CostTracker.get_instance().aggregate(
            start_date=last7_start, end_date=end
        )
        max_cost = max((d["cost_cny"] for d in agg7["by_day"].values()), default=0.0)
        bar_lines = ["近 7 天每日开销："]
        for date_str, day in sorted(agg7["by_day"].items()):
            ratio = (day["cost_cny"] / max_cost) if max_cost > 0 else 0
            bar = "█" * int(ratio * 30)
            bar_lines.append(
                f"  {date_str}  {day['cost_cny']:7.4f} 元  "
                f"{day['calls']:5d} 次  {bar}"
            )
        self.lbl_usage_per_day.setText(
            "<pre style='font-family: Consolas, monospace; font-size: 11px;'>"
            + "\n".join(bar_lines)
            + "</pre>"
        )

        # By-type table
        self.tbl_usage_by_type.setRowCount(0)
        sorted_types = sorted(by_type.items(), key=lambda kv: -kv[1]["cost_cny"])
        for ct, st in sorted_types:
            r = self.tbl_usage_by_type.rowCount()
            self.tbl_usage_by_type.insertRow(r)
            self.tbl_usage_by_type.setItem(r, 0, QTableWidgetItem(ct))
            self.tbl_usage_by_type.setItem(r, 1, QTableWidgetItem(str(st["calls"])))
            self.tbl_usage_by_type.setItem(
                r,
                2,
                QTableWidgetItem(
                    f"{st['prompt_tokens']:,} (cached {st['cached_tokens']:,})"
                ),
            )
            self.tbl_usage_by_type.setItem(
                r, 3, QTableWidgetItem(f"{st['completion_tokens']:,}")
            )
            self.tbl_usage_by_type.setItem(
                r, 4, QTableWidgetItem(f"{st['cost_cny']:.4f}")
            )

    def _open_cost_log_dir(self):
        """Show the data/cost folder in the OS file explorer."""
        try:
            from aria.core.utils.paths import get_base_path
            import subprocess
            import os

            cost_dir = get_base_path() / "data" / "cost"
            cost_dir.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(cost_dir))
            else:
                subprocess.Popen(["xdg-open", str(cost_dir)])
        except Exception as exc:
            QMessageBox.warning(self, "失败", f"无法打开目录: {exc}")

    def _test_backup_api_connection(self):
        """Test backup API connection."""
        api_url = self.api_url_backup.text().strip()
        if not api_url:
            QMessageBox.warning(self, "错误", "请先填写备用 API 地址")
            return

        # Use backup key if set, otherwise use main key
        api_key = self.api_key_backup.text().strip() or self.api_key.text().strip()
        # Use backup model if set, otherwise use main model
        model = self.model_backup.text().strip() or self.model.currentText().strip()

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

    def update_api_status(self, status_payload):
        """Update live API failover status labels."""
        try:
            if isinstance(status_payload, str):
                status = json.loads(status_payload) if status_payload else {}
            else:
                status = dict(status_payload or {})
        except Exception:
            status = {}

        current_api = status.get("current_api") or "未启用"
        host = status.get("current_host") or status.get("current_url") or "未配置"
        model = status.get("model") or "未配置"
        using_backup = bool(status.get("using_backup"))
        backup_enabled = bool(status.get("backup_enabled"))
        enabled = bool(status.get("enabled"))
        status_message = status.get("status_message") or "尚未收到运行状态"

        if not enabled:
            current_text = "当前：高质量 API 润色未启用"
            detail_text = status_message
            role = "muted"
        else:
            current_text = f"当前：{current_api} · {host} · {model}"
            role = "warning" if using_backup else "primary"
            last_ms = float(status.get("last_response_ms") or 0.0)
            if last_ms > 0:
                threshold = float(status.get("slow_threshold_ms") or 0.0)
                detail_text = f"{status_message}（慢阈值 {threshold:.0f}ms）"
            else:
                detail_text = status_message
            if not backup_enabled:
                detail_text = f"{detail_text}；备用 API 未配置"

        self.lbl_api_current_status.setText(current_text)
        self.lbl_api_current_status.setStyleSheet(
            self._label_style(role, font_size=12, bold=True)
        )
        self.lbl_api_status_detail.setText(detail_text)
        self.btn_api_switch_primary.setEnabled(bool(status.get("can_switch_primary")))

    # Fuzzy-match keyword → preset name (shared by main & backup auto-fill)
    _PROVIDER_KEYWORDS = {
        "deepseek": "DeepSeek",
        "openrouter": "OpenRouter",
        "openai": "OpenAI",
        "silicon": "硅基流动 (SiliconFlow)",
        "siliconflow": "硅基流动 (SiliconFlow)",
        "硅基": "硅基流动 (SiliconFlow)",
        "groq": "Groq",
        "moonshot": "月之暗面 (Moonshot)",
        "月之暗面": "月之暗面 (Moonshot)",
        "kimi": "月之暗面 (Moonshot)",
        "stepfun": "阶跃星辰 (Stepfun)",
        "阶跃": "阶跃星辰 (Stepfun)",
        "dashscope": "通义千问 (DashScope)",
        "通义": "通义千问 (DashScope)",
        "千问": "通义千问 (DashScope)",
        "qwen": "通义千问 (DashScope)",
        "zhipu": "智谱 (Zhipu GLM)",
        "智谱": "智谱 (Zhipu GLM)",
        "glm": "智谱 (Zhipu GLM)",
        "baichuan": "百川 (Baichuan)",
        "百川": "百川 (Baichuan)",
        "volcengine": "火山引擎 (豆包)",
        "火山": "火山引擎 (豆包)",
        "豆包": "火山引擎 (豆包)",
        "doubao": "火山引擎 (豆包)",
        "together": "Together AI",
        "mistral": "Mistral AI",
        "ollama": "本地 Ollama",
        "lmstudio": "本地 LM Studio",
        "lm studio": "本地 LM Studio",
    }

    def _match_preset(self, raw: str):
        """Fuzzy-match raw input to an API_PRESETS entry. Returns preset dict or None.

        Shows user-facing warnings for empty / already-URL / unrecognized input.
        Returns None in those cases so the caller just stops.
        """
        if not raw:
            QMessageBox.information(
                self, "提示", "请先输入服务商名称，如 deepseek、openrouter"
            )
            return None

        if raw.startswith("http://") or raw.startswith("https://"):
            return None

        matched_name = None
        for keyword, preset_name in self._PROVIDER_KEYWORDS.items():
            if keyword in raw:
                matched_name = preset_name
                break

        if not matched_name:
            QMessageBox.warning(
                self,
                "未识别",
                f"无法识别「{raw}」\n\n"
                "支持: deepseek, openrouter, openai, siliconflow, groq,\n"
                "moonshot/kimi, stepfun, dashscope/通义, zhipu/智谱,\n"
                "baichuan, 火山/豆包, together, mistral, ollama, lmstudio",
            )
            return None

        return API_PRESETS.get(matched_name)

    def _auto_fill_api_url(self):
        """Main API: fill URL + key hint + recommended model (客户默认一键装好)."""
        preset = self._match_preset(self.api_url.text().strip().lower())
        if not preset:
            return
        self.api_url.setText(preset["url"])
        self.api_key.setPlaceholderText(preset.get("key_hint", "sk-..."))
        self.model.clear()
        recommended = preset.get("recommended_model", "")
        if recommended:
            self.model.setCurrentText(recommended)

    def _fill_deepseek_recommended(self):
        """One-click setup for most users: DeepSeek + quality polish mode."""

        preset = API_PRESETS["DeepSeek"]
        self.api_url.setText(preset["url"])
        self.api_key.setPlaceholderText(preset.get("key_hint", "sk-..."))
        self.model.clear()
        self.model.setCurrentText(
            preset.get("recommended_model", "deepseek-v4-flash")
        )
        self.timeout.setValue(max(self.timeout.value(), 20))
        if hasattr(self, "radio_quality"):
            self.radio_quality.setChecked(True)
        QMessageBox.information(
            self,
            "已填入 DeepSeek 推荐配置",
            "已自动填写 API 地址、模型和超时时间。\n\n"
            "接下来：\n"
            "1. 粘贴你的 DeepSeek API Key\n"
            "2. 点击「测试主 API」\n"
            "3. 测试成功后点击「保存 API 设置」\n\n"
            "保存后，「屏幕感知增强」会把当前屏幕上下文用于专名和专业术语纠错。",
        )

    def _auto_fill_backup_api_url(self):
        """Backup API: only fill URL + key hint. Model is left to the user."""
        preset = self._match_preset(self.api_url_backup.text().strip().lower())
        if not preset:
            return
        self.api_url_backup.setText(preset["url"])
        self.api_key_backup.setPlaceholderText(preset.get("key_hint", "sk-..."))

    def _fetch_models(self):
        """Fetch available models from /v1/models endpoint."""
        api_url = self.api_url.text().strip()
        api_key = self.api_key.text().strip()

        if not api_url:
            QMessageBox.warning(self, "错误", "请先填写 API 地址")
            return

        if (
            hasattr(self, "_model_thread")
            and self._model_thread is not None
            and self._model_thread.isRunning()
        ):
            return

        self._fetch_models_btn.setEnabled(False)
        self._fetch_models_btn.setText("获取中...")

        self._model_thread = QThread()
        self._model_worker = ModelFetchWorker(api_url, api_key)
        self._model_worker.moveToThread(self._model_thread)
        self._model_thread.started.connect(self._model_worker.run)
        self._model_worker.finished.connect(self._on_models_fetched)
        self._model_worker.finished.connect(self._model_thread.quit)
        self._model_worker.finished.connect(self._model_worker.deleteLater)
        self._model_thread.finished.connect(self._model_thread.deleteLater)
        self._model_thread.start()

    def _on_models_fetched(self, success: bool, models: list, error_msg: str):
        """Handle model list fetch result."""
        self._fetch_models_btn.setEnabled(True)
        self._fetch_models_btn.setText("获取模型列表")

        if success and models:
            current = self.model.currentText()
            self.model.clear()
            for m in models:
                self.model.addItem(m)
            if current:
                self.model.setCurrentText(current)
            QMessageBox.information(self, "成功", f"获取到 {len(models)} 个可用模型")
        elif success and not models:
            QMessageBox.information(self, "提示", "API 返回了空模型列表")
        else:
            QMessageBox.warning(self, "获取失败", f"无法获取模型列表:\n{error_msg}")

        self._model_thread = None

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
        model = self.model.currentText().strip()

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
        # index: 0=FunASR, 1=Qwen3 (torch), 2=Qwen3 sherpa (lightweight),
        # 3=Qwen3 llama.cpp (GPU accelerated)
        self.funasr_group.setVisible(index == 0)
        self.qwen3_group.setVisible(index == 1)
        if hasattr(self, "qwen3_sherpa_group"):
            self.qwen3_sherpa_group.setVisible(index == 2)
        if hasattr(self, "qwen3_llamacpp_group"):
            self.qwen3_llamacpp_group.setVisible(index == 3)

        # Update hotword explanation based on engine
        self._update_hotword_explanation(index)

    def _update_hotword_explanation(self, engine_index: int):
        """Update hotword explanation labels based on selected ASR engine."""
        # Check if labels exist (they are created in hotwords tab)
        if not hasattr(self, "_hotword_guide_label"):
            return

        if engine_index in (1, 2, 3):  # Qwen3 family: same biasing model
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
                    f"拼音: {pinyin_str} (同音字均可识别，如「小溪」说成「小西」也能唤醒)"
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
        self.engine_combo.setToolTip(
            "Qwen3 支持多语言和上下文热词；FunASR 中文识别速度更快"
        )
        self.engine_combo.addItems(
            [
                "FunASR (中文优化，离线即用)",
                "Qwen3-ASR (推荐，52语言)",
                "Qwen3-ASR 轻量 (sherpa，无需显卡)",
                "Qwen3-ASR GPU 加速 (llama.cpp)",
            ]
        )
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        engine_layout.addRow("引擎:", self.engine_combo)

        engine_info = QLabel("引擎/模型保存后会自动热切换，无需重启")
        engine_info.setStyleSheet(self._label_style("muted", font_size=12))
        engine_layout.addRow("", engine_info)

        layout.addWidget(engine_group)

        # === FunASR Settings (visible when FunASR selected) ===
        self.funasr_group = QGroupBox("FunASR 设置")
        funasr_layout = QFormLayout(self.funasr_group)

        self.funasr_model = QComboBox()
        self.funasr_model.setToolTip(
            "大模型准确度更高但占用更多显存，显存不足时选小模型"
        )
        self.funasr_model.addItems(
            [
                "大模型 (paraformer-zh) - 推荐，准确度高",
                "小模型 (SenseVoice) - 显存<8GB时使用",
            ]
        )
        self.funasr_model.setCurrentIndex(0)
        funasr_layout.addRow("模型:", self.funasr_model)

        self.funasr_device = QComboBox()
        self.funasr_device.setToolTip("cuda 使用 GPU 加速，cpu 不需要显卡但速度较慢")
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
        self.qwen3_model.setToolTip("自动选择会根据显存大小决定用 1.7B 还是 0.6B")
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
        self.qwen3_device.setToolTip("cuda 使用 GPU 加速，cpu 不需要显卡但速度较慢")
        self.qwen3_device.addItems(["cuda", "cpu"])
        qwen3_layout.addRow("设备:", self.qwen3_device)

        self.qwen3_dtype = QComboBox()
        self.qwen3_dtype.setToolTip(
            "RTX 30/40/50 系显卡用 bfloat16，GTX 16/20 系用 float16"
        )
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

        # === Qwen3-ASR sherpa (lightweight) info (visible when selected) ===
        # Deliberately read-only: model_dir/provider/num_threads live in the
        # qwen3_sherpa block of hotwords.json (power-user opt-in this cycle).
        self.qwen3_sherpa_group = QGroupBox("Qwen3-ASR 轻量版设置")
        qwen3_sherpa_layout = QFormLayout(self.qwen3_sherpa_group)
        qwen3_sherpa_info = QLabel(
            "轻量版基于 sherpa-onnx int8 (0.6B)，纯 CPU 运行，不占用显卡。\n"
            "• 热词/上下文偏置与 Qwen3-ASR 完全一致\n"
            "• 模型目录、线程数等高级参数在 config/hotwords.json 的\n"
            "  qwen3_sherpa 配置块中修改（默认无需改动）"
        )
        qwen3_sherpa_info.setStyleSheet(self._label_style("muted", font_size=12))
        qwen3_sherpa_layout.addRow("", qwen3_sherpa_info)
        layout.addWidget(self.qwen3_sherpa_group)
        self.qwen3_sherpa_group.setVisible(False)  # Default: hidden

        # === Qwen3-ASR llama.cpp (GPU accelerated) info (visible when selected) ===
        # Deliberately read-only: server/model paths and server knobs live in
        # the qwen3_llamacpp block of hotwords.json (power-user opt-in this
        # cycle), same policy as the sherpa block above.
        self.qwen3_llamacpp_group = QGroupBox("Qwen3-ASR GPU 加速版设置")
        qwen3_llamacpp_layout = QFormLayout(self.qwen3_llamacpp_group)
        qwen3_llamacpp_info = QLabel(
            "GPU 加速版基于 llama.cpp CUDA + Qwen3-ASR 1.7B GGUF，\n"
            "由常驻 llama-server 子进程提供服务（约占 4GB 显存）。\n"
            "• 识别质量与 Qwen3-ASR 1.7B 一致，速度约为其 5 倍\n"
            "• 热词/上下文偏置与 Qwen3-ASR 完全一致\n"
            "• llama-server 路径、模型路径、端口等在 config/hotwords.json 的\n"
            "  qwen3_llamacpp 配置块中修改"
        )
        qwen3_llamacpp_info.setStyleSheet(self._label_style("muted", font_size=12))
        qwen3_llamacpp_layout.addRow("", qwen3_llamacpp_info)
        layout.addWidget(self.qwen3_llamacpp_group)
        self.qwen3_llamacpp_group.setVisible(False)  # Default: hidden

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

        self.chk_screen_ocr = QCheckBox("屏幕感知")
        self.chk_screen_ocr.setChecked(False)
        self.chk_screen_ocr.setToolTip(
            "自动感知当前窗口内容，提升专业术语识别准确率\n"
            "• 读取窗口标题和页面关键词作为识别上下文\n"
            "• 例如：浏览某个罕见专名页面时，音近错字会按屏幕写法纠正"
        )
        vad_layout.addRow(self.chk_screen_ocr)

        self.chk_screen_ocr_polish = QCheckBox("屏幕感知增强")
        self.chk_screen_ocr_polish.setChecked(False)
        self.chk_screen_ocr_polish.setToolTip(
            "把屏幕文字同时传给 AI 润色层做专名纠错\n"
            "• 例：屏幕显示「DeepSeek V4」，说错为「迪普sick V 4」也能改对\n"
            "• 例：屏幕标题里的罕见人名/术语，会作为高置信参考\n"
            "每月 API 成本约增加 ¥10-20。异常时自动回退原文。"
        )
        vad_layout.addRow(self.chk_screen_ocr_polish)

        self.chk_screen_ocr_use_dml = QCheckBox("启用 DirectML OCR 加速(实验性)")
        self.chk_screen_ocr_use_dml.setChecked(True)
        self.chk_screen_ocr_use_dml.setToolTip(
            "使用 GPU DirectML 跑 PP-OCRv5 屏幕 OCR\n"
            "• 默认开启:DirectML 在独立 OCR worker 中运行,崩溃只会触发 CPU 回退\n"
            "• 如果本机 DML 反复失败或输出异常,可关闭此项保留 CPU OCR\n"
            "• 诊断时可运行 tools/diagnostics/check_ocr_backend.py 查看 v5_dml/v5_cpu 状态"
        )
        vad_layout.addRow(self.chk_screen_ocr_use_dml)

        self.chk_screen_ocr_force_cpu = QCheckBox("强制 CPU OCR(诊断用)")
        self.chk_screen_ocr_force_cpu.setChecked(False)
        self.chk_screen_ocr_force_cpu.setToolTip(
            "跳过 DirectML GPU 加速,强制使用 CPU 做屏幕 OCR\n"
            "• 打开后即使启用 DirectML,也会直接走 CPU OCR\n"
            "• 打开场景:排查 OCR 后端或临时确认 CPU 路径\n"
            "• 打开后 OCR 会变慢(450-1000ms),但稳定性最高"
        )
        vad_layout.addRow(self.chk_screen_ocr_force_cpu)

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
        self.vad_min_silence.setRange(100, 3000)
        self.vad_min_silence.setValue(1500)
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
            "逐字符输入模式，完全不占用剪贴板。\n\n"
            "优点：\n"
            "  - 不影响剪贴板内容（复制的图片/文件不会丢失）\n"
            "  - 适用于远程桌面（ToDesk/向日葵/RDP）\n"
            "  - 不会触发富文本编辑器的粘贴对话框\n"
            "  - 不会误按快捷键（只发送纯文字）\n\n"
            "缺点：\n"
            "  - 速度较慢（100字约1.5秒 vs 剪贴板瞬间完成）\n"
            "  - 换行会变为空格（防止在聊天框误触发送）\n"
            "  - 在代码编辑器中会触发自动补全\n"
            "  - 不适用于 DirectInput 游戏"
        )
        output_layout.addWidget(self.chk_typewriter_mode)

        typewriter_hint = QLabel(
            "关闭 = 剪贴板模式（快速，适合日常）  |  "
            "开启 = 打字机模式（兼容性强，适合远程桌面）"
        )
        typewriter_hint.setWordWrap(True)
        typewriter_hint.setStyleSheet(
            self._label_style("secondary", extra="margin-left: 20px;", font_size=11)
        )
        output_layout.addWidget(typewriter_hint)

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
        self.local_model_path.setToolTip(
            "GGUF 格式的量化模型文件完整路径，如 C:\\models\\qwen2.5-1.5b-q4_k_m.gguf"
        )
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
        self.chk_auto_update.setChecked(
            general.get("auto_check_update", True)
        )  # Default: check updates

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
        # 润色风格：优先读 polish_style；老配置（无此键）按 auto_structure 迁移，
        # 忽略旧的 filter_filler_words（v5.0 前是死开关，不代表去口水意图），
        # 以保持老用户升级后全局行为不变（非结构化 → 逐字保真）。
        _style = self.config.get("polish_style")
        if _style not in ("verbatim", "smooth", "structured"):
            _style = (
                "structured" if self.config.get("auto_structure", False) else "verbatim"
            )
        if _style == "structured":
            self.radio_style_structured.setChecked(True)
        elif _style == "smooth":
            self.radio_style_smooth.setChecked(True)
        else:
            self.radio_style_verbatim.setChecked(True)
        self.chk_cli_destutter.setChecked(self.config.get("cli_destutter", True))
        self.personalization_rules_edit.setPlainText(
            self.config.get("personalization_rules", "")
        )

        # Load reply style
        self.reply_style_edit.setPlainText(self.config.get("reply_style", ""))

        # Load prompt templates — v5.0 双模板
        polish = self.config.get("polish", {})
        prompt_template_loose = polish.get("prompt_template", self.DEFAULT_PROMPT_LOOSE)
        # 容错：JSON 里仍可能存在旧版单字段；如果没写 structured 就用默认值
        prompt_template_structured = polish.get(
            "prompt_template_structured", self.DEFAULT_PROMPT_STRUCTURED
        )
        # 旧用户保存的可能是 v4 旧模板，让 sanitize 在 PolishConfig 里兜底，
        # UI 只负责把 JSON 里的字符串原样展示给用户编辑。
        self.prompt_edit.setPlainText(prompt_template_loose)
        self.prompt_edit_structured.setPlainText(prompt_template_structured)
        # 加载后，按当前润色风格刷新说明 + 选中对应 tab
        self._update_structure_hint()

        # === API tab ===
        # Keys are stored DPAPI-encrypted at rest; show plaintext in the
        # (password-masked) fields so save/re-save round-trips cleanly.
        from aria.core.utils.secrets import reveal_secret

        self.api_url.setText(polish.get("api_url", ""))
        self.api_key.setText(reveal_secret(polish.get("api_key", "")))
        self.model.setCurrentText(polish.get("model", ""))
        self.timeout.setValue(polish.get("timeout", 10))

        # 备用 API 配置
        self.api_url_backup.setText(polish.get("api_url_backup", ""))
        self.api_key_backup.setText(reveal_secret(polish.get("api_key_backup", "")))
        self.model_backup.setText(polish.get("model_backup", ""))
        self.slow_threshold.setValue(int(polish.get("slow_threshold_ms", 3000)))
        self.switch_count.setValue(polish.get("switch_after_slow_count", 2))

        # 语音识别救援（asr_rescue）
        asr_rescue = self.config.get("asr_rescue", {}) or {}
        if hasattr(self, "chk_asr_rescue_cloud"):
            self.chk_asr_rescue_cloud.setChecked(
                bool(asr_rescue.get("cloud_enabled", False))
            )
            self.asr_rescue_api_key.setText(
                reveal_secret(asr_rescue.get("api_key", ""))
            )

        # === Auto-hotword config (block: auto_hotword) ===
        # Q5: empty api_url/api_key/model = inherit from polish.* at runtime
        ah = self.config.get("auto_hotword", {}) or {}
        if hasattr(self, "chk_auto_hotword_enabled"):
            self.chk_auto_hotword_enabled.setChecked(ah.get("enabled") is True)
            self.txt_auto_hotword_api_url.setText(ah.get("api_url", ""))
            self.txt_auto_hotword_api_key.setText(reveal_secret(ah.get("api_key", "")))
            self.txt_auto_hotword_model.setText(ah.get("model", ""))
            self.spn_auto_hotword_review_interval.setValue(
                int(ah.get("review_interval_hours", 6))
            )
            self.spn_auto_hotword_min_batch.setValue(int(ah.get("min_batch_size", 8)))
            self.spn_auto_hotword_min_count.setValue(
                int(ah.get("min_count_for_review", 3))
            )
            self.spn_auto_hotword_max_terms.setValue(
                int(ah.get("max_terms_per_review", 50))
            )
            self.chk_auto_hotword_review_on_startup.setChecked(
                bool(ah.get("review_on_startup", False))
            )

        # === Advanced tab ===
        # ASR engine selection (0=FunASR, 1=Qwen3, 2=Qwen3 sherpa, 3=Qwen3 llama.cpp)
        asr_engine = self.config.get("asr_engine", "qwen3")
        engine_index_map = {
            "funasr": 0,
            "qwen3": 1,
            "qwen3_sherpa": 2,
            "qwen3_llamacpp": 3,
        }
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
        self.chk_screen_ocr.setChecked(vad.get("screen_ocr", False))
        self.chk_screen_ocr_polish.setChecked(vad.get("screen_ocr_polish") is True)
        self.chk_screen_ocr_use_dml.setChecked(vad.get("screen_ocr_use_dml", True))
        self.chk_screen_ocr_force_cpu.setChecked(vad.get("screen_ocr_force_cpu", False))

        # Sync the OCR mode tri-radio with the underlying flags. We collapse
        # the three checkboxes back to a tier so the user sees a coherent
        # state even if hotwords.json was hand-edited.
        if hasattr(self, "radio_ocr_off"):
            ocr_on = bool(vad.get("screen_ocr", False))
            polish_on = vad.get("screen_ocr_polish") is True
            ah_on = ah.get("enabled") is True
            if not ocr_on and not polish_on and not ah_on:
                resolved = "off"
            elif ocr_on and polish_on and ah_on:
                resolved = "full"
            else:
                resolved = (
                    "off"
                    if not ocr_on
                    else ("full" if polish_on else ("auto" if ah_on else "off"))
                )
            # blockSignals so we don't bounce back through
            # _apply_ocr_mode_to_flags during initial load.
            for r in (self.radio_ocr_off, self.radio_ocr_auto, self.radio_ocr_full):
                r.blockSignals(True)
            self.radio_ocr_off.setChecked(resolved == "off")
            self.radio_ocr_auto.setChecked(resolved == "auto")
            self.radio_ocr_full.setChecked(resolved == "full")
            for r in (self.radio_ocr_off, self.radio_ocr_auto, self.radio_ocr_full):
                r.blockSignals(False)
        self.vad_threshold.setValue(vad.get("threshold", 0.2))
        self.vad_energy_threshold.setValue(vad.get("energy_threshold", 0.003))
        self.vad_min_silence.setValue(vad.get("min_silence_ms", 1500))

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
                wakeword = wakeword_config.get("wakeword", "小助手")
                self.wakeword_edit.setText(wakeword)
                self.custom_instruction_table.setRowCount(0)
                entries = wakeword_config.get("custom_instructions", [])
                for entry in entries or []:
                    self._add_custom_instruction_row(entry)
            except Exception:
                self.wakeword_edit.setText("小助手")
                self.custom_instruction_table.setRowCount(0)
        else:
            self.wakeword_edit.setText("小助手")
            self.custom_instruction_table.setRowCount(0)

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
            "   将 .gguf 文件放到 Aria 目录下的 models/llamacpp_gguf/ 文件夹\n\n"
            "3. 填写路径\n"
            "   在上方「模型路径」填入文件路径，例如：\n"
            "   models/llamacpp_gguf/Qwen3.5-2B-Q4_K_M.gguf\n\n"
            "4. 切换模式\n"
            "   在「智能润色」标签页选择「本地润色」模式\n\n"
            "GPU 层数：-1 表示全部放 GPU（推荐），0 表示纯 CPU\n"
            "上下文窗口：默认 512 即可，一般不需要修改"
        )
        QMessageBox.information(self, "本地润色使用说明", guide_text)

    def _sync_elevated_custom_instruction_tasks(
        self,
        previous_entries: list[dict],
        new_entries: list[dict],
        sender: Optional[QPushButton] = None,
    ) -> bool:
        try:
            from aria.core.wakeword.elevation import (
                register_elevated_task,
                task_exists,
                unregister_elevated_task,
                validate_elevated_target,
            )
        except Exception as exc:
            QMessageBox.warning(
                self, "管理员快捷方式失败", f"无法加载管理员任务工具：{exc}"
            )
            return False

        old_by_id = {
            str(entry.get("id") or ""): entry
            for entry in previous_entries
            if entry.get("id")
        }
        new_by_id = {
            str(entry.get("id") or ""): entry
            for entry in new_entries
            if entry.get("id")
        }
        to_unregister = [
            entry
            for entry_id, entry in old_by_id.items()
            if entry.get("elevate")
            and (entry_id not in new_by_id or not new_by_id[entry_id].get("elevate"))
        ]
        to_register = []
        for entry_id, entry in new_by_id.items():
            if not entry.get("elevate"):
                continue
            ok, reason = validate_elevated_target(
                entry.get("command", ""),
                bool(entry.get("trust_writable_target", False)),
            )
            if not ok:
                QMessageBox.warning(
                    self,
                    "管理员快捷方式无效",
                    f"语音指令「{entry.get('phrase', '')}」无法启用管理员快捷方式：\n{reason}",
                )
                return False
            old_entry = old_by_id.get(entry_id)
            if not old_entry or not old_entry.get("elevate"):
                to_register.append(entry)
                continue
            if (
                old_entry.get("command") != entry.get("command")
                or old_entry.get("working_dir", "") != entry.get("working_dir", "")
                or bool(old_entry.get("trust_writable_target", False))
                != bool(entry.get("trust_writable_target", False))
            ):
                to_register.append(entry)

        total_steps = len(to_unregister) + len(to_register)
        if total_steps == 0:
            return True

        if sender and hasattr(sender, "setEnabled"):
            sender.setEnabled(False)
        progress = QProgressDialog(
            "正在注册管理员快捷方式...", "", 0, total_steps, self
        )
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.ApplicationModal)
        progress.setValue(0)
        progress.show()

        errors = []
        step = 0
        try:
            for entry in to_unregister:
                entry_id = str(entry.get("id") or "")
                if not task_exists(entry_id):
                    step += 1
                    progress.setValue(step)
                    continue
                ok, err = unregister_elevated_task(entry_id)
                if not ok:
                    errors.append(f"删除「{entry.get('phrase', '')}」失败：{err}")
                step += 1
                progress.setValue(step)

            for entry in to_register:
                ok, err = register_elevated_task(
                    str(entry.get("id") or ""),
                    str(entry.get("phrase") or ""),
                    str(entry.get("command") or ""),
                    str(entry.get("working_dir") or ""),
                    trust_writable_target=bool(
                        entry.get("trust_writable_target", False)
                    ),
                )
                if not ok:
                    errors.append(f"注册「{entry.get('phrase', '')}」失败：{err}")
                step += 1
                progress.setValue(step)
        finally:
            progress.close()
            if sender and hasattr(sender, "setEnabled"):
                sender.setEnabled(True)

        if errors:
            QMessageBox.warning(
                self,
                "管理员快捷方式失败",
                "部分管理员快捷方式处理失败，配置未保存：\n" + "\n".join(errors[:6]),
            )
            return False
        return True

    def save_config(self):
        """Save configuration to hotwords.json."""
        # Track if restart-required settings changed
        restart_needed = False
        asr_hot_reload_needed = False
        old_engine = self.config.get("asr_engine", "qwen3")
        old_funasr = self.config.get("funasr", {})
        old_vad = self.config.get("vad", {})
        save_sender = self.sender()

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
        self.config["general"]["auto_check_update"] = self.chk_auto_update.isChecked()

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
        # 润色风格 → polish_style（UI 权威）+ 派生两个 bool（引擎/manager 读这俩）。
        if self.radio_style_structured.isChecked():
            _style = "structured"
        elif self.radio_style_smooth.isChecked():
            _style = "smooth"
        else:
            _style = "verbatim"
        self.config["polish_style"] = _style
        self.config["filter_filler_words"] = _style in ("smooth", "structured")
        self.config["auto_structure"] = _style == "structured"
        self.config["cli_destutter"] = self.chk_cli_destutter.isChecked()
        self.config["personalization_rules"] = (
            self.personalization_rules_edit.toPlainText()
        )
        self.config["reply_style"] = self.reply_style_edit.toPlainText()

        # === API settings ===
        # Encrypt keys at rest (DPAPI); plaintext passthrough on non-Windows.
        from aria.core.utils.secrets import protect_secret

        if "polish" not in self.config:
            self.config["polish"] = {}
        # Auto-enable API polish when quality mode is selected
        self.config["polish"]["enabled"] = self.radio_quality.isChecked()
        self.config["polish"]["api_url"] = self.api_url.text()
        self.config["polish"]["api_key"] = protect_secret(self.api_key.text())
        self.config["polish"]["model"] = self.model.currentText()
        self.config["polish"]["timeout"] = self.timeout.value()
        # v5.0 双模板：保守模式 + 结构化模式 各保存一份
        self.config["polish"]["prompt_template"] = self.prompt_edit.toPlainText()
        self.config["polish"][
            "prompt_template_structured"
        ] = self.prompt_edit_structured.toPlainText()

        # 备用 API 配置（智能轮询）
        backup_url = self.api_url_backup.text().strip()
        if backup_url:
            self.config["polish"]["api_url_backup"] = backup_url
            self.config["polish"]["api_key_backup"] = protect_secret(
                self.api_key_backup.text().strip()
            )
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

        # === ASR rescue (cloud second-pass) ===
        # Only the two user-facing keys are owned by this UI; other asr_rescue
        # tuning keys (model/timeout_s/max_audio_s/...) persist untouched.
        if hasattr(self, "chk_asr_rescue_cloud"):
            rescue_block = self.config.setdefault("asr_rescue", {})
            rescue_block["cloud_enabled"] = self.chk_asr_rescue_cloud.isChecked()
            rescue_block["api_key"] = protect_secret(
                self.asr_rescue_api_key.text().strip()
            )

        # === Auto-hotword config ===
        # Empty api_url/api_key/model are intentional: at runtime AriaApp
        # falls back to polish.* (Q5). We persist exactly what the user typed.
        if hasattr(self, "chk_auto_hotword_enabled"):
            ah_block = self.config.setdefault("auto_hotword", {})
            ah_block["enabled"] = self.chk_auto_hotword_enabled.isChecked()
            ah_block["api_url"] = self.txt_auto_hotword_api_url.text().strip()
            ah_block["api_key"] = protect_secret(
                self.txt_auto_hotword_api_key.text().strip()
            )
            ah_block["model"] = self.txt_auto_hotword_model.text().strip()
            # daily_review_hour is legacy-only; the backend now uses the
            # distance-based keys below.
            ah_block["review_interval_hours"] = int(
                self.spn_auto_hotword_review_interval.value()
            )
            ah_block["min_batch_size"] = int(self.spn_auto_hotword_min_batch.value())
            ah_block["min_count_for_review"] = int(
                self.spn_auto_hotword_min_count.value()
            )
            ah_block["max_terms_per_review"] = int(
                self.spn_auto_hotword_max_terms.value()
            )
            ah_block["review_on_startup"] = (
                self.chk_auto_hotword_review_on_startup.isChecked()
            )

        # === Advanced tab - ASR Engine Selection ===
        engine_map = {0: "funasr", 1: "qwen3", 2: "qwen3_sherpa", 3: "qwen3_llamacpp"}
        new_engine = engine_map.get(self.engine_combo.currentIndex(), "qwen3")
        if old_engine != new_engine:
            asr_hot_reload_needed = True

        # 切换到轻量 sherpa 引擎：只做前置提示，不阻塞保存（加载失败时
        # 后端热切换会自动回滚到原引擎并提示）。
        if new_engine == "qwen3_sherpa" and old_engine != "qwen3_sherpa":
            sherpa_problem = ""
            try:
                from aria.core.asr.sherpa_engine import (
                    check_sherpa_installation,
                    resolve_sherpa_model_dir,
                )

                if not check_sherpa_installation():
                    sherpa_problem = (
                        "未检测到 sherpa-onnx 依赖（pip install sherpa-onnx==1.13.4）"
                    )
                else:
                    sherpa_cfg = self.config.get("qwen3_sherpa", {}) or {}
                    model_dir = resolve_sherpa_model_dir(
                        str(sherpa_cfg.get("model_dir", "") or "")
                    )
                    if not Path(model_dir).is_dir():
                        sherpa_problem = f"未找到轻量模型目录:\n{model_dir}"
            except Exception as probe_exc:
                sherpa_problem = f"轻量引擎检测失败: {probe_exc}"
            if sherpa_problem:
                QMessageBox.warning(
                    self,
                    "轻量引擎可能不可用",
                    f"{sherpa_problem}\n\n"
                    "保存后如果切换失败，会自动回退到当前引擎。",
                )

        # 切换到 llama.cpp GPU 加速引擎：同样只做前置提示，不阻塞保存
        # （加载失败时后端热切换会自动回滚到原引擎并提示）。
        if new_engine == "qwen3_llamacpp" and old_engine != "qwen3_llamacpp":
            llamacpp_problem = ""
            try:
                from aria.core.asr.llamacpp_engine import (
                    resolve_llamacpp_path,
                    default_mmproj_for,
                    probe_port_status,
                    DEFAULT_LLAMACPP_SERVER,
                    DEFAULT_LLAMACPP_MODEL,
                    DEFAULT_LLAMACPP_PORT,
                )

                llamacpp_cfg = self.config.get("qwen3_llamacpp", {}) or {}
                server_path = resolve_llamacpp_path(
                    str(llamacpp_cfg.get("server_path", "") or ""),
                    DEFAULT_LLAMACPP_SERVER,
                )
                model_path = resolve_llamacpp_path(
                    str(llamacpp_cfg.get("model_path", "") or ""),
                    DEFAULT_LLAMACPP_MODEL,
                )
                mmproj_raw = str(llamacpp_cfg.get("mmproj_path", "") or "").strip()
                mmproj_path = (
                    resolve_llamacpp_path(mmproj_raw, "")
                    if mmproj_raw
                    else default_mmproj_for(model_path)
                )
                if not Path(server_path).is_file():
                    llamacpp_problem = f"未找到 llama-server:\n{server_path}"
                elif not Path(model_path).is_file():
                    llamacpp_problem = f"未找到 GGUF 模型:\n{model_path}"
                elif not Path(mmproj_path).is_file():
                    llamacpp_problem = f"未找到 mmproj 文件:\n{mmproj_path}"
                else:
                    try:
                        port = int(
                            llamacpp_cfg.get("port", DEFAULT_LLAMACPP_PORT)
                            or DEFAULT_LLAMACPP_PORT
                        )
                    except (TypeError, ValueError):
                        port = DEFAULT_LLAMACPP_PORT
                    port_status = probe_port_status(
                        port, server_path=server_path, model_path=model_path
                    )
                    if port_status == "foreign":
                        llamacpp_problem = (
                            f"端口 {port} 已被其他程序占用；"
                            "请在 qwen3_llamacpp.port 换一个端口"
                        )
                    # "orphan"（上次残留的 llama-server）不算问题：
                    # 引擎加载时会自动接管清理后重新启动。
            except Exception as probe_exc:
                llamacpp_problem = f"GPU 加速引擎检测失败: {probe_exc}"
            if llamacpp_problem:
                QMessageBox.warning(
                    self,
                    "GPU 加速引擎可能不可用",
                    f"{llamacpp_problem}\n\n"
                    "保存后如果切换失败，会自动回退到当前引擎。",
                )

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
                        f"保存后将尝试后台加载 Qwen3-ASR 模型:\n\n"
                        f"模型: {short_name}\n"
                        f"大小: 约 {model_size}\n"
                        f"预计时间: 2-5 分钟\n\n"
                        f"首次加载可能需要下载，期间语音识别会短暂不可用。\n"
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
            asr_hot_reload_needed = True

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
            asr_hot_reload_needed = True

        # Model size change within Qwen3: check if the new model exists
        if (
            new_engine == "qwen3"
            and old_qwen3.get("model_name") != new_qwen3_model
            and new_qwen3_model != "auto"
            and not check_qwen3_model_exists(new_qwen3_model)
        ):
            short_name = "1.7B" if "1.7B" in new_qwen3_model else "0.6B"
            model_size = QWEN3_MODEL_SIZES.get(new_qwen3_model, "3.4GB")
            QMessageBox.information(
                self,
                "模型需要下载",
                f"切换到 Qwen3-ASR {short_name} 需要下载模型:\n\n"
                f"大小: 约 {model_size}\n"
                f"预计时间: 2-5 分钟\n\n"
                f"保存后会尝试后台加载/下载，无需重启。\n"
                "(已自动配置国内镜像加速)",
            )

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
        self.config["vad"][
            "screen_ocr_use_dml"
        ] = self.chk_screen_ocr_use_dml.isChecked()
        self.config["vad"][
            "screen_ocr_force_cpu"
        ] = self.chk_screen_ocr_force_cpu.isChecked()
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
        new_wakeword = self.wakeword_edit.text().strip() or "小助手"
        custom_instructions = self._collect_custom_instructions()
        incomplete_custom_instructions = [
            entry.get("phrase") or entry.get("command", "")
            for entry in custom_instructions
            if bool(entry.get("phrase")) != bool(entry.get("command"))
        ]
        if incomplete_custom_instructions:
            preview = "、".join(incomplete_custom_instructions[:3])
            if len(incomplete_custom_instructions) > 3:
                preview += "……"
            QMessageBox.warning(
                self,
                "语音指令未完成",
                f"语音指令「{preview}」还没有填写完整。\n\n"
                "每一行都需要同时填写「指令短语」和「启动目标/指令」；"
                "请补全，或者删除这行后再保存。",
            )
            return
        duplicate_trigger = self._find_duplicate_custom_instruction_trigger(
            custom_instructions
        )
        if duplicate_trigger:
            QMessageBox.warning(
                self,
                "语音指令重复",
                f"语音指令短语或近音别名「{duplicate_trigger}」重复。\n\n"
                "请删除重复行，或把其中一条禁用后再保存。",
            )
            return
        try:
            import os as _os

            # Load existing wakeword config (with corruption resilience)
            wakeword_config = {"enabled": True, "wakeword": "小助手", "commands": {}}
            if wakeword_path.exists():
                try:
                    with open(wakeword_path, "r", encoding="utf-8") as f:
                        wakeword_config = json.load(f)
                except (json.JSONDecodeError, ValueError):
                    # Corrupted file — use defaults (will be overwritten below)
                    print(f"[WARN] wakeword.json corrupted, resetting to defaults")

            previous_custom_instructions = wakeword_config.get(
                "custom_instructions", []
            )
            if not isinstance(previous_custom_instructions, list):
                previous_custom_instructions = []
            if not self._sync_elevated_custom_instruction_tasks(
                previous_custom_instructions,
                custom_instructions,
                save_sender if isinstance(save_sender, QPushButton) else None,
            ):
                return

            # Update wakeword + auto-enable when user sets a wakeword
            wakeword_config["wakeword"] = new_wakeword
            if new_wakeword:
                wakeword_config["enabled"] = True

            wakeword_config["custom_instructions"] = custom_instructions

            # Also update commands.json prefix to match new wakeword
            commands_path = self.config_path.parent / "commands.json"
            try:
                if commands_path.exists():
                    with open(commands_path, "r", encoding="utf-8") as f:
                        cmd_config = json.load(f)
                    cmd_config["prefix"] = new_wakeword
                    if new_wakeword:
                        cmd_config["enabled"] = True
                    _tmp_cmd = str(commands_path) + ".tmp"
                    with open(_tmp_cmd, "w", encoding="utf-8") as f:
                        json.dump(cmd_config, f, ensure_ascii=False, indent=2)
                        f.flush()
                        _os.fsync(f.fileno())
                    _os.replace(_tmp_cmd, str(commands_path))
            except Exception as e:
                print(f"Failed to sync commands.json prefix: {e}")

            # Save back (atomic write)
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

            # Only show modal message if restart is truly needed. ASR engine/model
            # settings are hot-reloaded by the backend after settingsSaved.
            if restart_needed:
                QMessageBox.information(
                    self,
                    "设置已保存",
                    "设置已保存。\n\n音频输入设备等系统级设置可能需要重启应用才能完全生效。\n"
                    "语音识别引擎/模型、VAD 和输出设置会自动热重载。",
                )
            else:
                # Visual feedback: temporarily change button text to confirm save
                sender = save_sender
                if sender and hasattr(sender, "setText"):
                    original_text = sender.text()
                    sender.setText(
                        "已保存，正在切换模型" if asr_hot_reload_needed else "已保存"
                    )
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

    def _show_voice_guide(self):
        """Show a dialog with all available voice commands."""
        guide_text = (
            "<h3>语音指令指南</h3>"
            "<p>所有指令格式：<b>「唤醒词」+ 指令</b>（唤醒词可在「设置 → 语音指令」中设置）</p>"
            "<hr>"
            "<h4>基础控制</h4>"
            "<table cellpadding='3'>"
            "<tr><td><b>开启自动发送</b></td><td>说完话自动按回车发送</td></tr>"
            "<tr><td><b>关闭自动发送</b></td><td>取消自动发送</td></tr>"
            "<tr><td><b>休眠 / 别听了</b></td><td>暂停语音监听（模型保留在显存）</td></tr>"
            "<tr><td><b>醒来 / 继续听</b></td><td>恢复语音监听</td></tr>"
            "<tr><td><b>深度休眠 / 关闭引擎</b></td><td>卸载ASR模型，释放显存（唤醒需数秒重载）</td></tr>"
            "</table>"
            "<h4>修改上一段输入（无需固定命令）</h4>"
            "<p>说出唤醒词后直接描述要求，例如：再长一点、语气更温和、把逻辑理顺、改得更简短。"
            "Aria 会按你的原话修改上一段输入，不再使用润色、扩写、缩写或重写等固定选区命令。</p>"
            "<h4>独立选区工具（需先选中文字）</h4>"
            "<table cellpadding='3'>"
            "<tr><td><b>翻译成英文/中文/日文</b></td><td>翻译选中内容（弹窗显示）</td></tr>"
            "<tr><td><b>什么意思</b></td><td>自动检测语言并翻译</td></tr>"
            "<tr><td><b>总结 / 归纳</b></td><td>总结大量选中文字（弹窗显示）</td></tr>"
            "<tr><td><b>帮我回复 [风格]</b></td><td>根据选中的消息生成回复建议</td></tr>"
            "<tr><td><b>问问AI</b></td><td>围绕选中文字打开 AI 对话</td></tr>"
            "</table>"
            "<h4>智能功能</h4>"
            "<table cellpadding='3'>"
            "<tr><td><b>重点记一下 [内容]</b></td><td>语音记录重要事项</td></tr>"
            "<tr><td><b>提醒我 [时间] [事项]</b></td><td>设置定时提醒闹钟</td></tr>"
            "<tr><td><b>每三十分钟提醒我一次 [事项]</b></td><td>设置固定间隔重复提醒</td></tr>"
            "<tr><td><b>停止这个提醒 / 取消刚才的提醒</b></td><td>关闭当前或刚设置的提醒</td></tr>"
            "<tr><td><b>关闭所有重复提醒</b></td><td>停止全部循环提醒</td></tr>"
            "<tr><td><b>帮我打开</b></td><td>打开选中的文件路径/URL</td></tr>"
            "<tr><td><b>我的语音指令</b></td><td>执行「设置 → 语音指令」中配置的启动指令</td></tr>"
            "</table>"
            "<h4>键盘快捷键</h4>"
            "<table cellpadding='3'>"
            "<tr><td><b>发送 / 换行</b></td><td>按 Enter</td></tr>"
            "<tr><td><b>删除</b></td><td>按 Backspace</td></tr>"
            "<tr><td><b>撤销 / 重做</b></td><td>按 Ctrl+Z / Ctrl+Y</td></tr>"
            "<tr><td><b>复制 / 粘贴 / 剪切 / 全选 / 保存</b></td><td>按对应 Ctrl 快捷键</td></tr>"
            "</table>"
            "<h4>提醒时间格式示例</h4>"
            "<p style='margin-left:10px'>"
            "三小时后、半小时后、十分钟后、五天后<br>"
            "晚上八点、明天下午两点、后天上午十点半<br>"
            "下周五、下下周一下午三点、今晚八点<br>"
            "循环：每十分钟、每半小时、每两小时、每天</p>"
            "<hr>"
            f"<p style='color:{self._theme.text_muted};font-size:11px'>"
            "提示：唤醒词使用拼音匹配，同音字均可识别。"
            "我的语音指令也支持近音匹配，但只匹配你配置过的短语。"
            "指令间有 500ms 冷却防误触。</p>"
        )
        msg = QMessageBox(self)
        msg.setWindowTitle("Aria 语音指令指南")
        msg.setTextFormat(Qt.RichText)
        msg.setText(guide_text)
        msg.setStandardButtons(QMessageBox.Ok)
        msg.exec()

    def _open_history_folder(self):
        """Export all history to readable txt files and open the folder."""
        import json as _json
        import os

        try:
            project_dir = Path(__file__).parent.parent.parent.resolve()
            history_dir = project_dir / "data" / "history"
            export_dir = project_dir / "data" / "history_txt"
            export_dir.mkdir(parents=True, exist_ok=True)

            exported = 0
            if history_dir.exists():
                for jsonl_file in sorted(history_dir.glob("*.jsonl")):
                    date_str = jsonl_file.stem
                    try:
                        lines = []
                        for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                            if not line.strip():
                                continue
                            r = _json.loads(line)
                            ts = r.get("timestamp", "")[:19].replace("T", " ")
                            rtype = r.get("record_type", "")
                            inp = r.get("input_text", "")
                            out = r.get("output_text", "")
                            text_line = f"[{ts}] [{rtype}]"
                            if inp:
                                text_line += f" {inp}"
                            if out and out != inp:
                                text_line += f" -> {out}"
                            lines.append(text_line)
                        if lines:
                            txt_file = export_dir / f"{date_str}.txt"
                            txt_file.write_text("\n".join(lines), encoding="utf-8")
                            exported += 1
                    except Exception:
                        pass

            # Use os.startfile — most reliable on Windows, works with pythonw.exe
            os.startfile(str(export_dir))
        except Exception as e:
            QMessageBox.warning(self, "错误", f"打开历史记录失败: {e}")

    def _open_highlights_file(self):
        """Open the highlights file (important notes from save_highlight command only)."""
        import os

        try:
            project_dir = Path(__file__).parent.parent.parent.resolve()
            highlights_file = project_dir / "data" / "highlights.txt"

            # highlights.txt is written to ONLY by executor._save_highlight()
            # Do NOT import from InsightStore — that contains ALL voice records
            if not highlights_file.exists():
                highlights_file.parent.mkdir(parents=True, exist_ok=True)
                highlights_file.write_text(
                    "暂无重点记录。\n\n"
                    "使用方法：对 Aria 说「小助手重点记一下 xxxxx」即可保存重点内容。\n",
                    encoding="utf-8",
                )

            # Open with default text editor
            os.startfile(str(highlights_file))
        except Exception as e:
            QMessageBox.warning(self, "错误", f"打开重点记录失败: {e}")

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

        # Find the Aria root directory by searching upward.
        # Portable: search for Aria.vbs (only exists at true root, not in source copy)
        # Dev: search for launcher.py at a level that also has ui/ (not inside _internal)
        start = Path(__file__).resolve()
        root = None
        # Pass 1: look for Aria.vbs (portable-only marker, unambiguous)
        for parent in start.parents:
            if (parent / "Aria.vbs").exists():
                root = parent
                break
        # Pass 2: dev mode — look for launcher.py + app.py at same level
        if root is None:
            for parent in start.parents:
                if (
                    (parent / "launcher.py").exists()
                    and (parent / "app.py").exists()
                    and (parent / "ui").is_dir()
                ):
                    root = parent
                    break
        if root is None:
            root = Path(__file__).parent.parent.parent.resolve()

        # Portable build: has Aria.vbs + _internal/AriaRuntime.exe
        aria_runtime = root / "_internal" / "AriaRuntime.exe"
        aria_vbs = root / "Aria.vbs"

        if aria_runtime.exists() and aria_vbs.exists():
            import os

            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            wscript = Path(system_root) / "System32" / "wscript.exe"
            return {
                "target_path": str(wscript),
                "arguments": f'"{aria_vbs}"',
                "working_dir": str(root),
                "mode": "portable",
            }

        # Dev mode: use pythonw.exe (windowless Python)
        pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
        if not pythonw.exists():
            exe_pythonw = Path(sys.executable).with_name("pythonw.exe")
            if exe_pythonw.exists():
                pythonw = exe_pythonw

        launcher_py = root / "launcher.py"

        return {
            "target_path": str(pythonw) if pythonw.exists() else "",
            "arguments": f'"{launcher_py}"',
            "working_dir": str(root),
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

    # ── OCR mode (three-tier) ──
    def _apply_ocr_mode_to_flags(self, mode: str) -> None:
        """When the user picks an OCR tier in the auto-hotword sub-tab, push the
        choice into the three underlying checkboxes so save_config()'s existing
        path writes consistent state. Backend persistence + tracker
        teardown/spinup are handled by AriaApp.set_ocr_mode (called from
        main.py when popup ocrModeChanged fires); this method is purely a
        UI mirror.
        """
        if mode == "off":
            screen_ocr = False
            polish = False
            ah = False
        elif mode == "auto":
            screen_ocr = True
            polish = False
            ah = True
        else:  # "full"
            screen_ocr = True
            polish = True
            ah = True

        if hasattr(self, "chk_screen_ocr"):
            self.chk_screen_ocr.setChecked(screen_ocr)
        if hasattr(self, "chk_screen_ocr_polish"):
            self.chk_screen_ocr_polish.setChecked(polish)
        if hasattr(self, "chk_auto_hotword_enabled"):
            self.chk_auto_hotword_enabled.setChecked(ah)

    def set_ocr_mode(self, mode: str) -> None:
        """External sync (called from main.py / popup menu)."""
        if mode not in ("off", "auto", "full") or not hasattr(self, "radio_ocr_off"):
            return
        for r in (self.radio_ocr_off, self.radio_ocr_auto, self.radio_ocr_full):
            r.blockSignals(True)
        if mode == "off":
            self.radio_ocr_off.setChecked(True)
        elif mode == "auto":
            self.radio_ocr_auto.setChecked(True)
        else:
            self.radio_ocr_full.setChecked(True)
        for r in (self.radio_ocr_off, self.radio_ocr_auto, self.radio_ocr_full):
            r.blockSignals(False)
        # Mirror to underlying checkboxes too so a subsequent 保存 stays
        # coherent without requiring the user to re-toggle the radio.
        self._apply_ocr_mode_to_flags(mode)

    def get_ocr_mode(self) -> str:
        if not hasattr(self, "radio_ocr_off"):
            return "auto"
        if self.radio_ocr_off.isChecked():
            return "off"
        if self.radio_ocr_full.isChecked():
            return "full"
        return "auto"
