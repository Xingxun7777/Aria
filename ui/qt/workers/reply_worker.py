"""
Reply Worker
=============
QRunnable-based worker for async reply generation requests.
Uses the shared AI gateway (OpenAI-compatible).
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
    """Write reply worker debug message (pythonw.exe safe)."""
    import datetime

    from core.debug import append_log_line

    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [RWORKER] {msg}"
    if sys.stdout is not None:
        print(line)
    append_log_line(_DEBUG_LOG, line)


def _error_message(category: AIErrorCategory | None, fallback: str = "回复生成失败") -> str:
    mapping = {
        AIErrorCategory.NOT_CONFIGURED: "AI 未配置",
        AIErrorCategory.AUTH: "API 认证失败",
        AIErrorCategory.RATE_LIMITED: "API 请求过于频繁",
        AIErrorCategory.SERVER_ERROR: "API 服务异常",
        AIErrorCategory.TIMEOUT: "请求超时",
        AIErrorCategory.CONNECT: "无法连接到 API",
        AIErrorCategory.PROTOCOL: "API 响应异常",
        AIErrorCategory.EMPTY: "回复结果为空",
        AIErrorCategory.CANCELLED: "请求已取消",
    }
    if category is None:
        return fallback
    return mapping.get(category, fallback)


class ReplySignals(QObject):
    """
    Signals for ReplyWorker.

    Using a separate QObject for signals because QRunnable doesn't inherit QObject.
    """

    # Emitted when reply generation completes: (request_id, reply_text)
    finished = Signal(str, str)

    # Emitted on error: (request_id, error_message)
    error = Signal(str, str)


class ReplyWorker(QRunnable):
    """
    Worker for generating reply suggestions in background thread.

    Uses the shared AI gateway (OpenAI-compatible).

    Usage:
        worker = ReplyWorker(request_id, source_text, api_url, api_key)
        worker.signals.finished.connect(on_reply_done)
        worker.signals.error.connect(on_reply_error)
        QThreadPool.globalInstance().start(worker)
    """

    # Reply prompt template
    REPLY_PROMPT = """你是一个专业的回复助手。请根据收到的消息，生成一条得体、自然的回复。

要求：
1. 回复应当简洁、友好、专业
2. 根据消息的语气和内容，选择合适的回复风格
3. 如果消息是中文，用中文回复；如果是英文，用英文回复
4. 直接输出回复内容，禁止添加任何解释或说明
5. 不要在回复前加"回复："等前缀

{style_block}收到的消息：
{text}

回复："""

    REPLY_PROMPT_WITH_STYLE = """你是一个专业的回复助手。请根据收到的消息，生成一条得体、自然的回复。

要求：
1. 回复应当简洁、友好、专业
2. 根据消息的语气和内容，选择合适的回复风格
3. 如果消息是中文，用中文回复；如果是英文，用英文回复
4. 直接输出回复内容，禁止添加任何解释或说明
5. 不要在回复前加"回复："等前缀
6. 额外风格要求：{style_hint}

收到的消息：
{text}

回复："""

    def __init__(
        self,
        request_id: str,
        source_text: str,
        api_url: str,
        api_key: str,
        model: str = "",
        timeout: float = 20.0,
        style_hint: Optional[str] = None,
    ):
        """
        Initialize reply worker.

        Args:
            request_id: Unique ID for this request (for matching responses)
            source_text: The message to reply to
            api_url: API base URL (OpenAI-compatible)
            api_key: API key
            model: Model name
            timeout: Request timeout in seconds
            style_hint: Optional style instruction (e.g., "语气强硬一点")
        """
        super().__init__()

        self.request_id = request_id
        self.source_text = source_text
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.style_hint = style_hint

        self.signals = ReplySignals()
        self.setAutoDelete(True)

    def _build_prompt(self) -> str:
        """Build reply prompt, optionally incorporating style hint."""
        if self.style_hint and self.style_hint.strip():
            return self.REPLY_PROMPT_WITH_STYLE.format(
                text=self.source_text,
                style_hint=self.style_hint.strip(),
            )
        else:
            return self.REPLY_PROMPT.format(
                text=self.source_text,
                style_block="",
            )

    @Slot()
    def run(self):
        """Execute reply generation request."""
        _worker_log(
            f"run() START: request_id={self.request_id}, text_len={len(self.source_text)}"
        )
        try:
            if not self.source_text or len(self.source_text.strip()) < 1:
                _worker_log("run() ERROR: empty text")
                self.signals.error.emit(self.request_id, "文本为空")
                return

            _worker_log("run() building prompt...")
            prompt = self._build_prompt()
            if self.style_hint:
                _worker_log(f"run() style_hint: {self.style_hint}")

            _worker_log(f"run() calling gateway model={self.model!r}")
            result = chat(
                AIRequestSpec(
                    api_url=self.api_url,
                    api_key=self.api_key,
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    timeout_s=self.timeout,
                    purpose="reply",
                    max_tokens=1000,
                    temperature=0.7,
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

            reply_text = result.text
            _worker_log(f"run() reply: {len(reply_text)} chars")
            _worker_log("run() emitting finished signal...")
            try:
                self.signals.finished.emit(self.request_id, reply_text)
                _worker_log("run() finished signal emitted OK")
            except Exception as emit_e:
                _worker_log(f"run() SIGNAL EMIT ERROR: {emit_e}")
                raise

        except Exception as e:
            _worker_log(f"run() EXCEPTION: {type(e).__name__}: {e}")
            import traceback

            _worker_log(f"run() TRACEBACK: {traceback.format_exc()}")
            self.signals.error.emit(self.request_id, f"回复生成失败: {str(e)[:100]}")
        finally:
            _worker_log(f"run() END: request_id={self.request_id}")
