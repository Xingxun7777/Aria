"""
Reply Worker
=============
QRunnable-based worker for async reply generation requests.
Uses OpenAI-compatible API (same as AIPolisher).
"""

import httpx
import time
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

# Debug logging for pythonw.exe compatibility
_DEBUG_LOG = (
    Path(__file__).parent.parent.parent.parent / "DebugLog" / "wakeword_debug.log"
)


def _worker_log(msg: str):
    """Write reply worker debug message (pythonw.exe safe)."""
    import datetime

    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [RWORKER] {msg}\n"
    if sys.stdout is not None:
        print(line.strip())
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


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

    Uses OpenAI-compatible API (same as AIPolisher).

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
        model: str = "google/gemini-2.5-flash-lite-preview-09-2025",
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

            if not self.api_url or not self.api_key:
                _worker_log("run() ERROR: missing API config")
                self.signals.error.emit(self.request_id, "API 配置缺失")
                return

            _worker_log("run() building prompt...")
            prompt = self._build_prompt()
            if self.style_hint:
                _worker_log(f"run() style_hint: {self.style_hint}")

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }

            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1000,
                "temperature": 0.7,
            }

            base_url = self.api_url.rstrip("/")
            if base_url.endswith("/v1"):
                full_url = f"{base_url}/chat/completions"
            else:
                full_url = f"{base_url}/v1/chat/completions"

            _worker_log(
                f"run() making request to {full_url[:50]}... model={self.model}"
            )

            with httpx.Client(timeout=self.timeout) as client:
                start_time = time.time()
                _worker_log("run() sending POST request...")
                response = client.post(full_url, headers=headers, json=payload)
                elapsed = time.time() - start_time
                _worker_log(
                    f"run() got response: status={response.status_code}, elapsed={elapsed:.2f}s"
                )

                if response.status_code != 200:
                    error_msg = f"API 错误 ({response.status_code})"
                    try:
                        error_detail = (
                            response.json().get("error", {}).get("message", "")
                        )
                        if error_detail:
                            error_msg = f"{error_msg}: {error_detail[:100]}"
                    except Exception:
                        pass
                    _worker_log(f"run() API error: {error_msg}")
                    self.signals.error.emit(self.request_id, error_msg)
                    return

                result = response.json()
                reply_text = result["choices"][0]["message"]["content"].strip()
                _worker_log(f"run() reply: {len(reply_text)} chars")

                if not reply_text:
                    _worker_log("run() ERROR: empty reply result")
                    self.signals.error.emit(self.request_id, "回复结果为空")
                    return

                _worker_log("run() emitting finished signal...")
                try:
                    self.signals.finished.emit(self.request_id, reply_text)
                    _worker_log("run() finished signal emitted OK")
                except Exception as emit_e:
                    _worker_log(f"run() SIGNAL EMIT ERROR: {emit_e}")
                    raise

        except httpx.TimeoutException:
            _worker_log(f"run() TIMEOUT after {self.timeout}s")
            self.signals.error.emit(self.request_id, "请求超时")
        except httpx.ConnectError as ce:
            _worker_log(f"run() CONNECT ERROR: {ce}")
            self.signals.error.emit(self.request_id, "无法连接到 API")
        except Exception as e:
            _worker_log(f"run() EXCEPTION: {type(e).__name__}: {e}")
            import traceback

            _worker_log(f"run() TRACEBACK: {traceback.format_exc()}")
            self.signals.error.emit(self.request_id, f"回复生成失败: {str(e)[:100]}")
        finally:
            _worker_log(f"run() END: request_id={self.request_id}")
