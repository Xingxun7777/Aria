"""
LLM Worker
==========
QRunnable-based worker for async LLM chat requests.
Supports streaming responses via periodic signal emission.
"""

import httpx
import time
import json
from typing import Optional, List, Dict

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


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


class LLMWorker(QRunnable):
    """
    Worker for LLM chat requests in background thread.

    Supports both streaming and non-streaming modes.
    Uses OpenAI-compatible API.

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

            if not self.api_url or not self.api_key:
                self.signals.error.emit(self.request_id, "API 配置缺失")
                return

            messages = self._build_messages()

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }

            payload = {
                "model": self.model,
                "messages": messages,
                "max_tokens": 4000,
                "temperature": 0.7,
                "stream": self.stream,
            }

            # Build full API URL
            base_url = self.api_url.rstrip("/")
            if base_url.endswith("/v1"):
                full_url = f"{base_url}/chat/completions"
            else:
                full_url = f"{base_url}/v1/chat/completions"

            if self.stream:
                self._run_streaming(full_url, headers, payload)
            else:
                self._run_non_streaming(full_url, headers, payload)

        except httpx.TimeoutException:
            self.signals.error.emit(self.request_id, "请求超时")
        except httpx.ConnectError:
            self.signals.error.emit(self.request_id, "无法连接到 API")
        except Exception as e:
            self.signals.error.emit(self.request_id, f"错误: {str(e)[:100]}")

    def _run_streaming(self, url: str, headers: dict, payload: dict):
        """Run with streaming response."""
        full_content = ""

        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    error_text = response.read().decode("utf-8", errors="ignore")
                    self.signals.error.emit(
                        self.request_id, f"API 错误 ({response.status_code})"
                    )
                    return

                for line in response.iter_lines():
                    if not line:
                        continue

                    # SSE format: data: {...}
                    if line.startswith("data: "):
                        data_str = line[6:]  # Remove "data: " prefix

                        if data_str.strip() == "[DONE]":
                            break

                        try:
                            data = json.loads(data_str)
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")

                            if content:
                                full_content += content
                                self.signals.streamUpdate.emit(
                                    self.request_id, full_content
                                )

                        except json.JSONDecodeError:
                            continue

        if full_content:
            self.signals.finished.emit(self.request_id, full_content)
        else:
            self.signals.error.emit(self.request_id, "没有收到回复")

    def _run_non_streaming(self, url: str, headers: dict, payload: dict):
        """Run without streaming."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, headers=headers, json=payload)

            if response.status_code != 200:
                self.signals.error.emit(
                    self.request_id, f"API 错误 ({response.status_code})"
                )
                return

            result = response.json()
            content = result["choices"][0]["message"]["content"].strip()

            if content:
                self.signals.finished.emit(self.request_id, content)
            else:
                self.signals.error.emit(self.request_id, "回复为空")
