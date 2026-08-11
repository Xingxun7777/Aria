"""
Translation Worker
==================
QRunnable-based worker for async translation requests.
Uses QThreadPool to avoid blocking the main UI thread.
"""

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from aria.core.ai.gateway import AIErrorCategory, AIRequestSpec, chat


# Debug logging for pythonw.exe compatibility
_DEBUG_LOG = (
    Path(__file__).parent.parent.parent.parent / "DebugLog" / "wakeword_debug.log"
)


def _worker_log(msg: str):
    """Write translation worker debug message (pythonw.exe safe)."""
    import datetime

    from core.debug import append_log_line

    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [TWORKER] {msg}"
    if sys.stdout is not None:
        print(line)
    append_log_line(_DEBUG_LOG, line)


def _error_message(category: AIErrorCategory | None, fallback: str = "翻译失败") -> str:
    mapping = {
        AIErrorCategory.NOT_CONFIGURED: "AI 未配置",
        AIErrorCategory.AUTH: "API 认证失败",
        AIErrorCategory.RATE_LIMITED: "API 请求过于频繁",
        AIErrorCategory.SERVER_ERROR: "API 服务异常",
        AIErrorCategory.TIMEOUT: "请求超时",
        AIErrorCategory.CONNECT: "无法连接到 API",
        AIErrorCategory.PROTOCOL: "API 响应异常",
        AIErrorCategory.EMPTY: "翻译结果为空",
        AIErrorCategory.CANCELLED: "请求已取消",
    }
    if category is None:
        return fallback
    return mapping.get(category, fallback)


class TranslationSignals(QObject):
    """
    Signals for TranslationWorker.

    Using a separate QObject for signals because QRunnable doesn't inherit QObject.
    """

    # Emitted when translation completes: (request_id, translated_text)
    finished = Signal(str, str)

    # Emitted on error: (request_id, error_message)
    error = Signal(str, str)


class TranslationWorker(QRunnable):
    """
    Worker for performing translation in background thread.

    Uses the shared AI gateway (OpenAI-compatible).

    Usage:
        worker = TranslationWorker(action, config)
        worker.signals.finished.connect(on_translation_done)
        worker.signals.error.connect(on_translation_error)
        QThreadPool.globalInstance().start(worker)
    """

    # Translation prompt template
    TRANSLATE_PROMPT = """请翻译以下文本。

要求：
1. 如果是中文，翻译成英文；如果是英文或其他语言，翻译成中文
2. 译文自然流畅，符合目标语言表达习惯；必要时可意译，避免逐字直译
3. 保留专有名词/术语/数字/格式，不随意增删信息
4. 直接输出翻译结果，禁止添加任何解释、注释或括号说明
5. 保持原文的格式和段落结构

原文：
{text}

翻译："""

    def __init__(
        self,
        request_id: str,
        source_text: str,
        api_url: str,
        api_key: str,
        model: str = "",
        timeout: float = 15.0,
        source_lang: str = "auto",
        target_lang: str = "auto",
    ):
        """
        Initialize translation worker.

        Args:
            request_id: Unique ID for this request (for matching responses)
            source_text: Text to translate
            api_url: API base URL (OpenAI-compatible)
            api_key: API key
            model: Model name
            timeout: Request timeout in seconds
            source_lang: Source language (auto, zh, en)
            target_lang: Target language (auto, zh, en)
        """
        super().__init__()

        self.request_id = request_id
        self.source_text = source_text
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.source_lang = source_lang
        self.target_lang = target_lang

        self.signals = TranslationSignals()

        # Auto-delete when done
        self.setAutoDelete(True)

    def _build_prompt(self) -> str:
        """Build translation prompt based on language settings."""
        if self.source_lang == "auto" and self.target_lang == "auto":
            # Auto-detect: Chinese->English, else->Chinese
            return self.TRANSLATE_PROMPT.format(text=self.source_text)
        elif self.target_lang == "en":
            return f"""将以下文本翻译成英文。
要求：
1. 译文自然地道，符合英文表达习惯；必要时可意译，避免逐字直译
2. 保留专有名词/术语/数字/格式，不随意增删信息
3. 直接输出翻译结果，禁止添加任何解释或说明

原文：
{self.source_text}

翻译："""
        elif self.target_lang == "zh":
            return f"""将以下文本翻译成中文。
要求：
1. 译文自然流畅，符合中文表达习惯；必要时可意译，避免逐字直译
2. 保留专有名词/术语/数字/格式，不随意增删信息
3. 直接输出翻译结果，禁止添加任何解释或说明

原文：
{self.source_text}

翻译："""
        elif self.target_lang == "ja":
            return f"""将以下文本翻译成日文。
要求：
1. 译文自然流畅，符合日文表达习惯；必要时可意译，避免逐字直译
2. 保留专有名词/术语/数字/格式，不随意增删信息
3. 直接输出翻译结果，禁止添加任何解释或说明

原文：
{self.source_text}

翻译："""
        else:
            return self.TRANSLATE_PROMPT.format(text=self.source_text)

    @Slot()
    def run(self):
        """Execute translation request."""
        _worker_log(
            f"run() START: request_id={self.request_id}, text_len={len(self.source_text)}"
        )
        try:
            # Validate input
            if not self.source_text or len(self.source_text.strip()) < 1:
                _worker_log("run() ERROR: empty text")
                self.signals.error.emit(self.request_id, "文本为空")
                return

            _worker_log("run() building prompt...")
            prompt = self._build_prompt()
            _worker_log(f"run() calling gateway model={self.model!r}")

            result = chat(
                AIRequestSpec(
                    api_url=self.api_url,
                    api_key=self.api_key,
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    timeout_s=self.timeout,
                    purpose="translate",
                    max_tokens=2000,
                    temperature=0.1,
                )
            )
            _worker_log(
                f"run() gateway done: ok={result.ok}, status={result.status_code}, "
                f"elapsed={result.elapsed_ms}ms"
            )

            if not result.ok:
                msg = _error_message(result.error)
                if result.status_code and result.error not in (
                    AIErrorCategory.NOT_CONFIGURED,
                    AIErrorCategory.TIMEOUT,
                    AIErrorCategory.CONNECT,
                    AIErrorCategory.EMPTY,
                ):
                    msg = f"API 错误 ({result.status_code})"
                _worker_log(f"run() API error: {msg}")
                self.signals.error.emit(self.request_id, msg)
                return

            translated = result.text
            _worker_log(f"run() translated: {len(translated)} chars")
            _worker_log("run() emitting finished signal...")
            try:
                self.signals.finished.emit(self.request_id, translated)
                _worker_log("run() finished signal emitted OK")
            except Exception as emit_e:
                _worker_log(f"run() SIGNAL EMIT ERROR: {emit_e}")
                raise

        except Exception as e:
            _worker_log(f"run() EXCEPTION: {type(e).__name__}: {e}")
            import traceback

            _worker_log(f"run() TRACEBACK: {traceback.format_exc()}")
            self.signals.error.emit(self.request_id, f"翻译失败: {str(e)[:100]}")
        finally:
            _worker_log(f"run() END: request_id={self.request_id}")


class TranslationWorkerFactory:
    """
    Factory for creating TranslationWorker instances.

    Caches API configuration for reuse.
    """

    def __init__(
        self,
        api_url: str = "",
        api_key: str = "",
        model: str = "",
        timeout: float = 15.0,
    ):
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def create(
        self,
        request_id: str,
        source_text: str,
        source_lang: str = "auto",
        target_lang: str = "auto",
    ) -> TranslationWorker:
        """Create a new TranslationWorker with cached config."""
        return TranslationWorker(
            request_id=request_id,
            source_text=source_text,
            api_url=self.api_url,
            api_key=self.api_key,
            model=self.model,
            timeout=self.timeout,
            source_lang=source_lang,
            target_lang=target_lang,
        )

    def update_config(
        self,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        """Update cached configuration."""
        if api_url is not None:
            self.api_url = api_url
        if api_key is not None:
            self.api_key = api_key
        if model is not None:
            self.model = model
        if timeout is not None:
            self.timeout = timeout
