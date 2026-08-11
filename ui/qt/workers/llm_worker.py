"""
LLM Worker
==========
QRunnable-based worker for async LLM chat requests.
Supports streaming responses via periodic signal emission.
"""

from typing import List, Dict

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from aria.core.ai.gateway import AIErrorCategory, AIRequestSpec, chat, stream


class LLMSignals(QObject):
    """
    Signals for LLMWorker.

    Using a separate QObject for signals because QRunnable doesn't inherit QObject.
    """

    # Streaming update: (request_id, partial_content)
    streamUpdate = Signal(str, str)

    # Completion: (request_id, final_content)
    finished = Signal(str, str)

    # Error: (request_id, error_message)
    error = Signal(str, str)


def _error_message(category: AIErrorCategory | None, fallback: str = "错误") -> str:
    mapping = {
        AIErrorCategory.NOT_CONFIGURED: "AI 未配置",
        AIErrorCategory.AUTH: "API 认证失败",
        AIErrorCategory.RATE_LIMITED: "API 请求过于频繁",
        AIErrorCategory.SERVER_ERROR: "API 服务异常",
        AIErrorCategory.TIMEOUT: "请求超时",
        AIErrorCategory.CONNECT: "无法连接到 API",
        AIErrorCategory.PROTOCOL: "API 响应异常",
        AIErrorCategory.EMPTY: "没有收到回复",
        AIErrorCategory.CANCELLED: "请求已取消",
    }
    if category is None:
        return fallback
    return mapping.get(category, fallback)


class LLMWorker(QRunnable):
    """
    Worker for LLM chat requests in background thread.

    Supports both streaming and non-streaming modes.
    Uses the shared AI gateway (OpenAI-compatible).

    Usage:
        worker = LLMWorker(request_id, messages, context, api_url, api_key, model)
        worker.signals.streamUpdate.connect(on_stream_update)
        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        QThreadPool.globalInstance().start(worker)
    """

    SYSTEM_PROMPT_WITH_CONTEXT = """你是一个智能助手，帮助用户理解和处理他们选中的文本。

用户选中了一段文本并向你提问。请基于选中的文本内容来回答用户的问题。

回答要求：
1. 准确理解用户的问题
2. 基于选中文本给出相关回答
3. 回答简洁清晰，避免冗长
4. 如果需要代码或格式，使用 Markdown 格式"""

    SYSTEM_PROMPT_NO_CONTEXT = """你是一个智能助手，用户通过语音向你提问。

回答要求：
1. 准确理解用户的问题
2. 回答简洁清晰，避免冗长
3. 如果需要代码或格式，使用 Markdown 格式"""

    def __init__(
        self,
        request_id: str,
        messages: List[Dict[str, str]],
        context_text: str,
        api_url: str,
        api_key: str,
        model: str = "",
        timeout: float = 60.0,
        stream: bool = True,
    ):
        """
        Initialize LLM worker.

        Args:
            request_id: Unique ID for this request
            messages: Conversation history [{"role": "user/assistant", "content": "..."}]
            context_text: The selected text as context
            api_url: API base URL
            api_key: API key
            model: Model name
            timeout: Request timeout in seconds
            stream: Whether to use streaming mode
        """
        super().__init__()

        self.request_id = request_id
        self.messages = messages
        self.context_text = context_text
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.stream = stream

        self.signals = LLMSignals()
        self.setAutoDelete(True)

    def _build_messages(self) -> List[Dict[str, str]]:
        """Build messages with system prompt and optional context."""
        if self.context_text:
            system_content = (
                self.SYSTEM_PROMPT_WITH_CONTEXT
                + f"\n\n选中的文本：\n```\n{self.context_text}\n```"
            )
        else:
            system_content = self.SYSTEM_PROMPT_NO_CONTEXT

        full_messages = [{"role": "system", "content": system_content}]
        full_messages.extend(self.messages)

        return full_messages

    @Slot()
    def run(self):
        """Execute LLM request."""
        try:
            if not self.messages:
                self.signals.error.emit(self.request_id, "没有消息")
                return

            messages = self._build_messages()
            spec = AIRequestSpec(
                api_url=self.api_url,
                api_key=self.api_key,
                model=self.model,
                messages=messages,
                timeout_s=self.timeout,
                purpose="chat",
                max_tokens=4000,
                temperature=0.7,
            )

            if self.stream:
                self._run_streaming(spec)
            else:
                self._run_non_streaming(spec)

        except Exception as e:
            self.signals.error.emit(self.request_id, f"错误: {str(e)[:100]}")

    def _run_streaming(self, spec: AIRequestSpec):
        """Run with streaming response via gateway.stream."""

        def _on_delta(partial: str) -> None:
            self.signals.streamUpdate.emit(self.request_id, partial)

        result = stream(spec, _on_delta)
        if result.ok and result.text:
            self.signals.finished.emit(self.request_id, result.text)
            return
        # Mid-stream interrupt may still carry partial text — prefer finishing it
        # so the UI keeps what the user already saw (matches pre-gateway behavior).
        if result.text:
            self.signals.finished.emit(self.request_id, result.text)
            return
        msg = _error_message(result.error, "没有收到回复")
        if result.status_code and result.error not in (
            AIErrorCategory.NOT_CONFIGURED,
            AIErrorCategory.TIMEOUT,
            AIErrorCategory.CONNECT,
            AIErrorCategory.EMPTY,
        ):
            msg = f"API 错误 ({result.status_code})"
        self.signals.error.emit(self.request_id, msg)

    def _run_non_streaming(self, spec: AIRequestSpec):
        """Run without streaming via gateway.chat."""
        result = chat(spec)
        if result.ok and result.text:
            self.signals.finished.emit(self.request_id, result.text)
            return
        msg = _error_message(result.error, "回复为空")
        if result.status_code and result.error not in (
            AIErrorCategory.NOT_CONFIGURED,
            AIErrorCategory.TIMEOUT,
            AIErrorCategory.CONNECT,
            AIErrorCategory.EMPTY,
        ):
            msg = f"API 错误 ({result.status_code})"
        self.signals.error.emit(self.request_id, msg)
