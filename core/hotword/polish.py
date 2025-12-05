"""
AI Polish Module (Layer 3)
==========================
Uses LLM to polish and correct ASR transcription output.
"""

import httpx
import time
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any
from urllib.parse import urlparse

from ..logging import get_system_logger

logger = get_system_logger()

# Shared default prompt template - single source of truth
# Based on Codex + Gemini tri-party analysis
# Key principle: Minimal Intervention Doctrine
DEFAULT_POLISH_PROMPT = """你是语音转文字清理助手，严格遵循"最小修改"原则。

【专业术语】{hotwords}

【任务层级】按优先级执行：
1. 热词纠正：识别术语的谐音/拼写变体（如 克劳德→Claude）
2. 标点整理：疑问句加"？"，长句适当断句，句末加句号
3. 分段排版：话题转换处用空行分段，提高可读性
4. 口语清理：删除冗余词（嗯、呃、那个、就是说）和重复表达

【严格禁止】
✗ 改变核心意思
✗ 删除语气词（呢/吧/哦/啊/吗）
✗ 添加原文没有的内容
✗ 使用 Markdown 格式

【示例】
输入：嗯帮我打开克劳德然后查一下吉他上有什么新项目就是看看有没有什么好的代码
输出：帮我打开 Claude，然后查一下 GitHub 上有什么新项目。

看看有没有什么好的代码。

输入：那个就是说你研究一下这个问题呃然后给我一个方案吧
输出：你研究一下这个问题，然后给我一个方案吧。

输入：用琶音模式生成一段旋律吧
输出：用琶音模式生成一段旋律吧。

原文：{text}

清理后："""

# Simple prompt without hotwords (fallback)
SIMPLE_POLISH_PROMPT = """你是语音转文字润色助手。任务：

1. 【修正谐音】修正中文同音字错误
2. 【禁止翻译】英文保持英文，中文保持中文
3. 【标点格式】添加合适标点，整理格式使语句通顺
4. 【保留语气】保留呢、吗、吧等语气词，不要改变句子的疑问/陈述性质
5. 【禁止格式】禁止添加任何Markdown格式（*、**、#等），只输出纯文本

原文：{text}

润色后："""


@dataclass
class PolishConfig:
    """AI polish configuration."""
    enabled: bool = False
    api_url: str = "http://localhost:3000"
    api_key: str = ""
    model: str = "google/gemini-2.5-flash-lite-preview-09-2025"
    timeout: float = 10.0

    # Prompt template - supports {text}, {domain_context}, {hotwords}
    # Uses the shared DEFAULT_POLISH_PROMPT constant
    prompt_template: str = DEFAULT_POLISH_PROMPT

    # Domain context and hotwords for intelligent correction
    domain_context: str = ""
    hotwords: list = None  # List of hotword strings

    def __post_init__(self):
        if self.hotwords is None:
            self.hotwords = []
        # Validate api_url format
        self._validate_api_url()

    def _validate_api_url(self) -> bool:
        """Validate api_url is a proper HTTP(S) URL."""
        if not self.api_url:
            return False
        try:
            parsed = urlparse(self.api_url)
            if parsed.scheme not in ('http', 'https'):
                from ..logging import get_system_logger
                get_system_logger().warning(
                    f"Invalid api_url scheme: {parsed.scheme!r}. Expected http or https."
                )
                return False
            if not parsed.netloc:
                from ..logging import get_system_logger
                get_system_logger().warning(
                    f"Invalid api_url: missing host in {self.api_url!r}"
                )
                return False
            return True
        except Exception:
            return False


class AIPolisher:
    """
    AI-powered text polisher using LLM.
    
    Uses OpenAI-compatible API to polish ASR output.
    """
    
    def __init__(self, config: PolishConfig):
        self.config = config
        self._client: Optional[httpx.Client] = None
    
    def _get_client(self) -> httpx.Client:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.Client(timeout=self.config.timeout)
        return self._client
    
    def _build_prompt(self, text: str) -> str:
        """Build the full prompt with hotwords and domain context."""
        template = self.config.prompt_template

        # Format hotwords as comma-separated list
        hotwords_str = ""
        if self.config.hotwords:
            max_hotwords = 30
            if len(self.config.hotwords) > max_hotwords:
                logger.warning(
                    f"Hotwords exceed limit: {len(self.config.hotwords)} > {max_hotwords}. "
                    f"Only first {max_hotwords} will be used in AI prompt."
                )
            hotwords_str = ", ".join(self.config.hotwords[:max_hotwords])

        # Build prompt with available placeholders
        if "{hotwords}" in template:
            # New simplified template
            return template.format(
                text=text,
                hotwords=hotwords_str or "无特定术语"
            )
        elif "{domain_context}" in template:
            # Legacy template with domain_context
            domain_context = self.config.domain_context or "通用语音输入"
            return template.format(
                text=text,
                domain_context=domain_context,
                hotwords=hotwords_str or "无特定术语"
            )
        else:
            # Simple template with only {text}
            return template.format(text=text)

    def polish(self, text: str) -> str:
        """
        Polish the transcribed text using LLM.

        Args:
            text: Raw ASR output

        Returns:
            Polished text, or original text if polish fails
        """
        if not self.config.enabled:
            return text

        if not text or len(text.strip()) < 2:
            return text

        try:
            client = self._get_client()

            # Build request with hotwords context
            prompt = self._build_prompt(text)
            
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}"
            }
            
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 500,
                "temperature": 0.1
            }
            
            # Build full API URL - handle both /api and /api/v1 base URLs
            base_url = self.config.api_url.rstrip('/')
            if base_url.endswith('/v1'):
                full_url = f"{base_url}/chat/completions"
            else:
                full_url = f"{base_url}/v1/chat/completions"

            # Call API
            response = client.post(
                full_url,
                headers=headers,
                json=payload
            )
            
            if response.status_code != 200:
                logger.warning(f"Polish API error: {response.status_code}")
                return text
            
            result = response.json()
            polished = result["choices"][0]["message"]["content"].strip()
            
            # Basic validation - polished text shouldn't be empty or too different
            if not polished or len(polished) < 1:
                logger.warning("Polish returned empty result")
                return text
            
            logger.debug(f"Polished: '{text}' -> '{polished}'")
            return polished
            
        except httpx.TimeoutException:
            logger.warning("Polish API timeout")
            return text
        except Exception as e:
            logger.error(f"Polish error: {e}")
            return text
    
    def polish_with_debug(self, text: str) -> Dict[str, Any]:
        """
        Polish text and return full debug information.

        Returns:
            Dict with keys: output_text, changed, api_time_ms, error, http_status, full_prompt
        """
        debug_info = {
            "enabled": self.config.enabled,
            "api_url": self.config.api_url,
            "full_api_url": "",  # Will be set after URL construction
            "model": self.config.model,
            "timeout": self.config.timeout,
            "input_text": text,
            "prompt_template": self.config.prompt_template,
            "full_prompt": "",
            "output_text": text,
            "changed": False,
            "api_time_ms": 0.0,
            "error": "",
            "http_status": 0
        }

        if not self.config.enabled:
            debug_info["error"] = "Polish disabled"
            return debug_info

        if not text or len(text.strip()) < 2:
            debug_info["error"] = "Text too short"
            return debug_info

        try:
            client = self._get_client()

            # Build request with hotwords context
            prompt = self._build_prompt(text)
            debug_info["full_prompt"] = prompt

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}"
            }

            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 500,
                "temperature": 0.1
            }

            # Build full API URL - handle both /api and /api/v1 base URLs
            base_url = self.config.api_url.rstrip('/')
            if base_url.endswith('/v1'):
                full_url = f"{base_url}/chat/completions"
            else:
                full_url = f"{base_url}/v1/chat/completions"
            debug_info["full_api_url"] = full_url

            # Call API with timing
            start_time = time.time()
            response = client.post(
                full_url,
                headers=headers,
                json=payload
            )
            api_time = (time.time() - start_time) * 1000
            debug_info["api_time_ms"] = api_time
            debug_info["http_status"] = response.status_code

            if response.status_code != 200:
                debug_info["error"] = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(f"Polish API error: {response.status_code}")
                return debug_info

            result = response.json()
            polished = result["choices"][0]["message"]["content"].strip()

            if not polished or len(polished) < 1:
                debug_info["error"] = "Empty response from API"
                logger.warning("Polish returned empty result")
                return debug_info

            debug_info["output_text"] = polished
            debug_info["changed"] = (polished != text)

            logger.debug(f"Polished: '{text}' -> '{polished}'")
            return debug_info

        except httpx.TimeoutException:
            debug_info["error"] = "Timeout"
            logger.warning("Polish API timeout")
            return debug_info
        except Exception as e:
            debug_info["error"] = str(e)
            logger.error(f"Polish error: {e}")
            return debug_info

    def close(self):
        """Close HTTP client."""
        if self._client:
            self._client.close()
            self._client = None
