"""
Summary Worker
==============
QRunnable-based worker for async summarization requests.
Uses the shared AI gateway (OpenAI-compatible).
"""

import sys
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from aria.core.ai.gateway import AIErrorCategory, AIRequestSpec, chat

# Debug logging for pythonw.exe compatibility
_DEBUG_LOG = (
    Path(__file__).parent.parent.parent.parent / "DebugLog" / "wakeword_debug.log"
)


def _worker_log(msg: str):
    """Write summary worker debug message (pythonw.exe safe)."""
    import datetime

    from core.debug import append_log_line

    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [SWORKER] {msg}"
    if sys.stdout is not None:
        print(line)
    append_log_line(_DEBUG_LOG, line)


def _error_message(category: AIErrorCategory | None, fallback: str = "总结失败") -> str:
    mapping = {
        AIErrorCategory.NOT_CONFIGURED: "AI 未配置",
        AIErrorCategory.AUTH: "API 认证失败",
        AIErrorCategory.RATE_LIMITED: "API 请求过于频繁",
        AIErrorCategory.SERVER_ERROR: "API 服务异常",
        AIErrorCategory.TIMEOUT: "请求超时",
        AIErrorCategory.CONNECT: "无法连接到 API",
        AIErrorCategory.PROTOCOL: "API 响应异常",
        AIErrorCategory.EMPTY: "总结结果为空",
        AIErrorCategory.CANCELLED: "请求已取消",
    }
    if category is None:
        return fallback
    return mapping.get(category, fallback)


class SummarySignals(QObject):
    """
    Signals for SummaryWorker.

    Using a separate QObject for signals because QRunnable doesn't inherit QObject.
    """

    # Emitted when summary completes: (request_id, summary_text)
    finished = Signal(str, str)

    # Emitted on error: (request_id, error_message)
    error = Signal(str, str)


class SummaryWorker(QRunnable):
    """
    Worker for performing summarization in background thread.

    Uses the shared AI gateway (OpenAI-compatible).
    """

    SUMMARY_PROMPT = """请对以下文本做高质量中文总结。

要求：
1. 无论原文语言，一律输出中文
2. 先给一句话概括（不超过30字）
3. 再给要点列表（5-12条），覆盖主要内容/观点/结论
4. 保留关键术语/专有名词/数字/时间，不随意增删信息
5. 不添加原文中没有的信息，不要编造
6. 只输出摘要内容，不要解释过程
7. 如果存在关键结论/行动项/数据，请在要点中明确指出

输出格式：
一句话概括：...
要点：
- ...
- ...

原文：
{text}
"""

    def __init__(
        self,
        request_id: str,
        source_text: str,
        api_url: str,
        api_key: str,
        model: str = "",
        timeout: float = 30.0,
    ):
        """
        Initialize summary worker.

        Args:
            request_id: Unique ID for this request (for matching responses)
            source_text: Text to summarize
            api_url: API base URL (OpenAI-compatible)
            api_key: API key
            model: Model name
            timeout: Request timeout in seconds
        """
        super().__init__()

        self.request_id = request_id
        self.source_text = source_text
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

        self.signals = SummarySignals()
        self.setAutoDelete(True)

    def _build_prompt(self) -> str:
        """Build summary prompt."""
        return self.SUMMARY_PROMPT.format(text=self.source_text)

    @Slot()
    def run(self):
        """Execute summary request."""
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
            _worker_log(f"run() calling gateway model={self.model!r}")

            result = chat(
                AIRequestSpec(
                    api_url=self.api_url,
                    api_key=self.api_key,
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    timeout_s=self.timeout,
                    purpose="summary",
                    max_tokens=1500,
                    temperature=0.2,
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

            summary = result.text
            _worker_log(f"run() summary: {len(summary)} chars")
            _worker_log("run() emitting finished signal...")
            self.signals.finished.emit(self.request_id, summary)
            _worker_log("run() finished signal emitted OK")

        except Exception as e:
            _worker_log(f"run() EXCEPTION: {type(e).__name__}: {e}")
            import traceback

            _worker_log(f"run() TRACEBACK: {traceback.format_exc()}")
            self.signals.error.emit(self.request_id, f"总结失败: {str(e)[:100]}")
        finally:
            _worker_log(f"run() END: request_id={self.request_id}")
