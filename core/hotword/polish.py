"""
AI Polish Module (Layer 3)
==========================
Uses LLM to polish and correct ASR transcription output.
"""

import httpx
import time
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any

from ..logging import get_system_logger

logger = get_system_logger()

# Shared default prompt template - single source of truth
# Optimized based on Codex+Gemini analysis for Chinese ASR correction
# NOTE: Domain-specific terms are handled by hotword system (Layer 2), NOT here
DEFAULT_POLISH_PROMPT = """你是语音转文字润色助手。任务：

1. 【修正谐音】修正中文同音字错误
2. 【禁止翻译】英文保持英文，中文保持中文
3. 【标点格式】添加合适标点，整理格式使语句通顺
4. 【保留语气】保留呢、吗、吧等语气词，不要改变句子的疑问/陈述性质

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
    
    # Prompt template - {text} will be replaced with ASR output
    # Uses the shared DEFAULT_POLISH_PROMPT constant
    prompt_template: str = DEFAULT_POLISH_PROMPT


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
            
            # Build request
            prompt = self.config.prompt_template.format(text=text)
            
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

            # Build request
            prompt = self.config.prompt_template.format(text=text)
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
