"""
Translation Worker
==================
QRunnable-based worker for async translation requests.
Uses QThreadPool to avoid blocking the main UI thread.
"""

import httpx
import time
from typing import Optional

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


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

    Uses OpenAI-compatible API (same as AIPolisher).

    Usage:
        worker = TranslationWorker(action, config)
        worker.signals.finished.connect(on_translation_done)
        worker.signals.error.connect(on_translation_error)
        QThreadPool.globalInstance().start(worker)
    """

    # Translation prompt template
    TRANSLATE_PROMPT = """请翻译以下文本。

要求：
1. 如果是中文，翻译成英文
2. 如果是英文或其他语言，翻译成中文
3. 直接输出翻译结果，禁止添加任何解释、注释或括号说明
4. 保持原文的格式和段落结构

原文：
{text}

翻译："""

    def __init__(
        self,
        request_id: str,
        source_text: str,
        api_url: str,
        api_key: str,
        model: str = "google/gemini-2.5-flash-lite-preview-09-2025",
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
            return f"""将以下文本翻译成英文。直接输出翻译结果，禁止添加任何解释或说明。

原文：
{self.source_text}

翻译："""
        elif self.target_lang == "zh":
            return f"""将以下文本翻译成中文。直接输出翻译结果，禁止添加任何解释或说明。

原文：
{self.source_text}

翻译："""
        elif self.target_lang == "ja":
            return f"""将以下文本翻译成日文。直接输出翻译结果，禁止添加任何解释或说明。

原文：
{self.source_text}

翻译："""
        else:
            return self.TRANSLATE_PROMPT.format(text=self.source_text)

    @Slot()
    def run(self):
        """Execute translation request."""
        try:
            # Validate input
            if not self.source_text or len(self.source_text.strip()) < 1:
                self.signals.error.emit(self.request_id, "文本为空")
                return

            if not self.api_url or not self.api_key:
                self.signals.error.emit(self.request_id, "API 配置缺失")
                return

            # Build request
            prompt = self._build_prompt()

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }

            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2000,  # Translation may be longer than original
                "temperature": 0.1,
            }

            # Build full API URL
            base_url = self.api_url.rstrip("/")
            if base_url.endswith("/v1"):
                full_url = f"{base_url}/chat/completions"
            else:
                full_url = f"{base_url}/v1/chat/completions"

            # Make request
            with httpx.Client(timeout=self.timeout) as client:
                start_time = time.time()
                response = client.post(full_url, headers=headers, json=payload)
                elapsed = time.time() - start_time

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
                    self.signals.error.emit(self.request_id, error_msg)
                    return

                result = response.json()
                translated = result["choices"][0]["message"]["content"].strip()

                if not translated:
                    self.signals.error.emit(self.request_id, "翻译结果为空")
                    return

                # Guard print for pythonw.exe compatibility (sys.stdout is None)
                import sys

                if sys.stdout is not None:
                    print(
                        f"[TranslationWorker] Completed in {elapsed:.2f}s: "
                        f"{self.source_text[:30]}... -> {translated[:30]}..."
                    )
                self.signals.finished.emit(self.request_id, translated)

        except httpx.TimeoutException:
            self.signals.error.emit(self.request_id, "请求超时")
        except httpx.ConnectError:
            self.signals.error.emit(self.request_id, "无法连接到 API")
        except Exception as e:
            self.signals.error.emit(self.request_id, f"翻译失败: {str(e)[:100]}")


class TranslationWorkerFactory:
    """
    Factory for creating TranslationWorker instances.

    Caches API configuration for reuse.
    """

    def __init__(
        self,
        api_url: str = "",
        api_key: str = "",
        model: str = "google/gemini-2.5-flash-lite-preview-09-2025",
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
