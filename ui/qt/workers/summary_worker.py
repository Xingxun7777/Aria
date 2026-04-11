"""
Summary Worker
==============
QRunnable-based worker for async summarization requests.
Uses OpenAI-compatible API (same as AIPolisher).
"""

import httpx
import time
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

# Debug logging for pythonw.exe compatibility
_DEBUG_LOG = (
    Path(__file__).parent.parent.parent.parent / "DebugLog" / "wakeword_debug.log"
)


def _worker_log(msg: str):
    """Write summary worker debug message (pythonw.exe safe)."""
    import datetime

    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [SWORKER] {msg}\n"
    if sys.stdout is not None:
        print(line.strip())
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


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

    Uses OpenAI-compatible API (same as AIPolisher).
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

            if not self.api_url or not self.api_key:
                _worker_log("run() ERROR: missing API config")
                self.signals.error.emit(self.request_id, "API 配置缺失")
                return

            _worker_log("run() building prompt...")
            prompt = self._build_prompt()

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }

            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1500,
                "temperature": 0.2,
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
                summary = result["choices"][0]["message"]["content"].strip()
                _worker_log(f"run() summary: {len(summary)} chars")

                if not summary:
                    _worker_log("run() ERROR: empty summary result")
                    self.signals.error.emit(self.request_id, "总结结果为空")
                    return

                _worker_log("run() emitting finished signal...")
                self.signals.finished.emit(self.request_id, summary)
                _worker_log("run() finished signal emitted OK")

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
            self.signals.error.emit(self.request_id, f"总结失败: {str(e)[:100]}")
        finally:
            _worker_log(f"run() END: request_id={self.request_id}")
