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
# Based on Codex + Gemini tri-party analysis (v4.0)
# v4.0: Added phonetic alias table for cross-lingual matching
# Key insight: LLM needs explicit Chinese-sound → English-term mappings
DEFAULT_POLISH_PROMPT = """任务：修正语音识别文本的错别字和标点。

【判断流程】按顺序检查：
1. 原文是否包含"无意义乱码"或"中文音译英文"？→ 查音译表替换
2. 是否有同音字错误？→ 结合语境替换
3. 都不是？→ 仅修正标点，保留原文

【音译对照表】常见中文音译 → 英文术语
- 咖啡鱼/卡飞UI/康飞 → ComfyUI（AI绘图工具）
- 克劳德/克老德/cloud code → claude code（AI编程助手）
- 迪普seek/deep seek → deepseek（AI模型）
- 吉米奈/杰米尼 → gemini（AI模型）
- 可德克斯/code x → codex（AI编程助手）
- 阿尔特拉think/ultra think → ultrathink（思考模式）
- 威斯帕/威士忌 → whisper（语音识别）
- 帕特龙/派特on → patreon（创作者平台）

【中文参考词汇】{hotwords_chinese}

【英文参考词汇】{hotwords_english}

【跨语言替换示例 - 应该替换】
原文：这个咖啡鱼啊，用起来效果还挺好的。
修正：这个ComfyUI啊，用起来效果还挺好的。
理由："咖啡鱼"是ComfyUI的中文音译乱码，技术语境

原文：我还是用这个复conu i工作的话
修正：我还是用这个ComfyUI工作的话，
理由："复conu i"是无意义乱码，发音近似ComfyUI

原文：我用cloud code来调试bug
修正：我用claude code来调试bug。
理由：编程语境 + cloud code 是明显的误识别

【不要替换的情况】
原文：I will try to think about it
修正：I will try to think about it。
理由：正常英语句子，不是ultrathink的误识别

原文：check the cloud service status
修正：check the cloud service status。
理由：cloud在这里就是正确的词，讨论云服务

【禁止】
- 回答或补充内容
- 改变句子原意
- 把通顺的表达强行改成专业术语

原文：{text}
修正："""

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
    # Also supports {hotwords_critical} and {hotwords_context} for tiered system
    # Uses the shared DEFAULT_POLISH_PROMPT constant
    prompt_template: str = DEFAULT_POLISH_PROMPT

    # Domain context and hotwords for intelligent correction
    domain_context: str = ""
    hotwords: list = None  # List of hotword strings (all weight >= 0.5)

    # Tiered hotwords (set by HotWordManager, v3.1 with English support)
    hotwords_critical: list = None  # weight = 1.0: mandatory vocabulary (中英文)
    hotwords_strong: list = None  # weight = 0.5: Chinese reference words
    hotwords_english: list = None  # weight = 0.5: English reference (stricter rules)
    hotwords_context: list = None  # unused in v3.1 (kept for backwards compat)

    def __post_init__(self):
        if self.hotwords is None:
            self.hotwords = []
        if self.hotwords_critical is None:
            self.hotwords_critical = []
        if self.hotwords_strong is None:
            self.hotwords_strong = []
        if self.hotwords_english is None:
            self.hotwords_english = []
        if self.hotwords_context is None:
            self.hotwords_context = []
        # Validate api_url format
        self._validate_api_url()

    def _validate_api_url(self) -> bool:
        """Validate api_url is a proper HTTP(S) URL."""
        if not self.api_url:
            return False
        try:
            parsed = urlparse(self.api_url)
            if parsed.scheme not in ("http", "https"):
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
        from .utils import is_cjk_word

        template = self.config.prompt_template

        # v3.1: Separate Chinese and English hotwords
        # Chinese hotwords = critical (Chinese only) + strong (Chinese reference)
        # English hotwords = critical (English only) + hotwords_english

        # Split critical tier into Chinese and English
        critical_chinese = []
        critical_english = []
        if self.config.hotwords_critical:
            for word in self.config.hotwords_critical[:15]:
                if is_cjk_word(word):
                    critical_chinese.append(word)
                else:
                    critical_english.append(word)

        # Build Chinese hotwords list
        chinese_hotwords = critical_chinese[:]
        if self.config.hotwords_strong:
            chinese_hotwords.extend(self.config.hotwords_strong[:15])  # Unified limit

        # Build English hotwords list
        english_hotwords = critical_english[:]
        if self.config.hotwords_english:
            english_hotwords.extend(
                self.config.hotwords_english[:25]
            )  # Increased limit

        # Combined list for backwards compatibility
        all_hotwords = chinese_hotwords + english_hotwords
        hotwords_str = ", ".join(all_hotwords) if all_hotwords else "无"

        # Format for new v3.1 template
        hotwords_chinese_str = ", ".join(chinese_hotwords) if chinese_hotwords else "无"
        hotwords_english_str = ", ".join(english_hotwords) if english_hotwords else "无"

        # Format domain context
        domain_context = self.config.domain_context or "通用"

        # Replace placeholders with graceful fallback chain
        try:
            return template.format(
                text=text,
                hotwords=hotwords_str,  # backwards compat
                hotwords_chinese=hotwords_chinese_str,  # v3.1
                hotwords_english=hotwords_english_str,  # v3.1
                domain_context=domain_context,
                hotwords_critical=(
                    ", ".join(self.config.hotwords_critical[:15])
                    if self.config.hotwords_critical
                    else "无"
                ),
                hotwords_strong=(
                    ", ".join(self.config.hotwords_strong[:15])
                    if self.config.hotwords_strong
                    else "无"
                ),
                hotwords_context=(
                    ", ".join(self.config.hotwords_context[:15])
                    if self.config.hotwords_context
                    else "无"
                ),
            )
        except KeyError as e:
            # Fallback 1: Try backwards compatible format (v2.x templates with {hotwords} only)
            logger.warning(
                f"Template missing placeholder {e}, trying backwards compatible format"
            )
            try:
                return template.format(
                    text=text,
                    hotwords=hotwords_str,
                    domain_context=domain_context,
                )
            except KeyError as e2:
                # Fallback 2: Minimal format that preserves hotwords
                logger.error(
                    f"Template also missing {e2}, using minimal format with hotwords"
                )
                return f"润色以下文字（参考词汇：{hotwords_str}）：\n\n{text}"

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
                "Authorization": f"Bearer {self.config.api_key}",
            }

            # System message for JSON output mode
            system_msg = '你是文本修正工具。返回JSON格式：{"text": "修正后的文本"}'

            # Request JSON output to prevent explanations
            json_prompt = f'{prompt}\n\n输出JSON：{{"text": "修正后的文本"}}'

            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": json_prompt},
                ],
                "max_tokens": 1000,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }

            # Build full API URL - handle both /api and /api/v1 base URLs
            base_url = self.config.api_url.rstrip("/")
            if base_url.endswith("/v1"):
                full_url = f"{base_url}/chat/completions"
            else:
                full_url = f"{base_url}/v1/chat/completions"

            # Call API
            response = client.post(full_url, headers=headers, json=payload)

            if response.status_code != 200:
                logger.warning(f"Polish API error: {response.status_code}")
                return text

            result = response.json()
            content = result["choices"][0]["message"]["content"].strip()

            # Parse JSON response to extract text field
            try:
                import json

                parsed = json.loads(content)
                polished = parsed.get("text", content)
            except json.JSONDecodeError:
                # Fallback: use raw content if not valid JSON
                polished = content

            # Basic validation - polished text shouldn't be empty or too different
            if not polished or len(polished) < 1:
                logger.warning("Polish returned empty result")
                return text

            # LENGTH PROTECTION: Reject if too much content removed
            # This is a mechanical safety net - prompts can fail, code doesn't
            original_len = len(text)
            polished_len = len(polished)
            if polished_len < original_len * 0.8:
                logger.warning(
                    f"Polish rejected: removed {100 - polished_len * 100 // original_len}% content "
                    f"({original_len} -> {polished_len} chars)"
                )
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
            "http_status": 0,
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
                "Authorization": f"Bearer {self.config.api_key}",
            }

            # System message for JSON output mode
            system_msg = '你是文本修正工具。返回JSON格式：{"text": "修正后的文本"}'

            # Request JSON output to prevent explanations
            json_prompt = f'{prompt}\n\n输出JSON：{{"text": "修正后的文本"}}'

            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": json_prompt},
                ],
                "max_tokens": 1000,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }

            # Build full API URL - handle both /api and /api/v1 base URLs
            base_url = self.config.api_url.rstrip("/")
            if base_url.endswith("/v1"):
                full_url = f"{base_url}/chat/completions"
            else:
                full_url = f"{base_url}/v1/chat/completions"
            debug_info["full_api_url"] = full_url

            # Call API with timing
            start_time = time.time()
            response = client.post(full_url, headers=headers, json=payload)
            api_time = (time.time() - start_time) * 1000
            debug_info["api_time_ms"] = api_time
            debug_info["http_status"] = response.status_code

            if response.status_code != 200:
                debug_info["error"] = (
                    f"HTTP {response.status_code}: {response.text[:200]}"
                )
                logger.warning(f"Polish API error: {response.status_code}")
                return debug_info

            result = response.json()
            content = result["choices"][0]["message"]["content"].strip()

            # Parse JSON response to extract text field
            try:
                import json

                parsed = json.loads(content)
                polished = parsed.get("text", content)
            except json.JSONDecodeError:
                # Fallback: use raw content if not valid JSON
                polished = content

            if not polished or len(polished) < 1:
                debug_info["error"] = "Empty response from API"
                logger.warning("Polish returned empty result")
                return debug_info

            # LENGTH PROTECTION: Reject if too much content removed
            original_len = len(text)
            polished_len = len(polished)
            if polished_len < original_len * 0.8:
                debug_info["error"] = (
                    f"Removed {100 - polished_len * 100 // original_len}% content"
                )
                logger.warning(
                    f"Polish rejected: removed too much content "
                    f"({original_len} -> {polished_len} chars)"
                )
                return debug_info

            debug_info["output_text"] = polished
            debug_info["changed"] = polished != text

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
