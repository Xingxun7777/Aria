"""
Selection Processor
===================
Process selected text with LLM based on voice commands.
"""

from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from aria.core.hotword.polish import AIPolisher

from .commands import SelectionCommand, CommandType


@dataclass
class ProcessingResult:
    """Result of selection processing."""

    success: bool
    output_text: Optional[str] = None
    error: Optional[str] = None
    processing_time_ms: float = 0.0


class SelectionProcessor:
    """
    Process selected text with LLM.

    Reuses the existing AIPolisher infrastructure for LLM calls.
    """

    # Maximum text length to process (chars)
    MAX_TEXT_LENGTH = 10000

    def __init__(self, polisher: "AIPolisher"):
        """
        Initialize processor.

        Args:
            polisher: AIPolisher instance for LLM calls
        """
        self.polisher = polisher

    def process(
        self, selected_text: str, command: SelectionCommand
    ) -> ProcessingResult:
        """
        Process selected text with the given command.

        Args:
            selected_text: Text to process
            command: Parsed voice command

        Returns:
            ProcessingResult with output or error
        """
        import time

        start_time = time.time()

        # Validate input
        if not selected_text or not selected_text.strip():
            return ProcessingResult(success=False, error="Empty text")

        if len(selected_text) > self.MAX_TEXT_LENGTH:
            return ProcessingResult(
                success=False,
                error=f"Text too long ({len(selected_text)} chars, max {self.MAX_TEXT_LENGTH})",
            )

        # Build prompt
        prompt = self._build_prompt(selected_text, command)

        # Call LLM via polisher
        try:
            result = self._call_llm(prompt)

            if result:
                # Validate and clean response
                cleaned = self._clean_response(result, selected_text)
                processing_time = (time.time() - start_time) * 1000

                return ProcessingResult(
                    success=True,
                    output_text=cleaned,
                    processing_time_ms=processing_time,
                )
            else:
                return ProcessingResult(
                    success=False, error="LLM returned empty response"
                )

        except Exception as e:
            return ProcessingResult(success=False, error=str(e))

    def _build_prompt(self, text: str, command: SelectionCommand) -> str:
        """Build the full prompt for LLM."""
        prefix = command.get_prompt_prefix()

        # For custom commands, the prefix already includes the instruction
        if command.command_type == CommandType.CUSTOM:
            return f"{prefix}\n{text}"

        return f"{prefix}\n\n{text}"

    def _call_llm(self, prompt: str) -> Optional[str]:
        """
        Call LLM with the prompt.

        Uses the polisher's API configuration but with custom prompt.
        """
        if not self.polisher:
            return None

        import httpx

        try:
            # Get polisher config
            config = self.polisher.config

            # Build headers
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.api_key}",
            }

            # Build payload with custom prompt
            payload = {
                "model": config.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2000,  # Allow longer responses for translation/rewriting
                "temperature": 0.3,
            }

            # Build full API URL
            base_url = config.api_url.rstrip("/")
            if base_url.endswith("/v1"):
                full_url = f"{base_url}/chat/completions"
            else:
                full_url = f"{base_url}/v1/chat/completions"

            # Call API
            with httpx.Client(timeout=config.timeout) as client:
                response = client.post(full_url, headers=headers, json=payload)

                if response.status_code != 200:
                    print(f"[SELECTION] API error: {response.status_code}")
                    return None

                result = response.json()
                return result["choices"][0]["message"]["content"].strip()

        except Exception as e:
            print(f"[SELECTION] LLM call error: {e}")
            return None

    def _clean_response(self, response: str, original: str) -> str:
        """
        Clean and validate LLM response.

        Removes common prefixes/suffixes that LLMs add.
        """
        if not response:
            return original

        result = response.strip()

        # Remove common explanation prefixes
        prefixes_to_remove = [
            "Here's the",
            "Here is the",
            "The polished version:",
            "Translation:",
            "翻译结果：",
            "润色后：",
            "修改后：",
            "以下是",
            "处理结果：",
        ]

        for prefix in prefixes_to_remove:
            if result.lower().startswith(prefix.lower()):
                # Find the actual content after the prefix
                lines = result.split("\n", 1)
                if len(lines) > 1:
                    result = lines[1].strip()
                break

        # Remove surrounding quotes if present
        if (result.startswith('"') and result.endswith('"')) or (
            result.startswith("'") and result.endswith("'")
        ):
            result = result[1:-1]

        # Remove trailing explanations in parentheses (Note: ...), (注：...) etc.
        import re

        # Match (Note: ...), (注: ...), (说明: ...) at the end
        result = re.sub(r"\s*\([Nn]ote:.*?\)\s*$", "", result)
        result = re.sub(r"\s*\(注[：:].*?\)\s*$", "", result)
        result = re.sub(r"\s*\(说明[：:].*?\)\s*$", "", result)
        result = re.sub(r"\s*\(备注[：:].*?\)\s*$", "", result)
        result = result.strip()

        # Sanity check: if result is too short, might be an error
        if len(result) < len(original) * 0.1 and len(original) > 50:
            # Response is suspiciously short, return original
            return original

        return result
