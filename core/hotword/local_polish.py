"""
Local Polish Engine (Layer 3 - Fast Mode)
==========================================
Uses local Qwen model via llama-cpp-python for fast punctuation correction.
Designed for low-latency scenarios where full API polish is not needed.
"""

import time
from dataclasses import dataclass
from typing import Optional, Dict, Any
from pathlib import Path

from ..logging import get_system_logger

logger = get_system_logger()

# Default model path relative to aria package
DEFAULT_MODEL_PATH = (
    Path(__file__).parent.parent.parent / "models" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
)


@dataclass
class LocalPolishConfig:
    """Local polish configuration."""

    enabled: bool = False
    model_path: str = str(DEFAULT_MODEL_PATH)
    n_gpu_layers: int = -1  # -1 = all layers on GPU
    n_ctx: int = 512  # Context window (matches template default)
    n_threads: int = 4

    # Prompt template - focused on punctuation only (Qwen chat format)
    prompt_template: str = """<|im_start|>system
你是语音转文字的标点符号添加工具。

任务：给语音识别的原始文本添加标点符号。
规则：
1. 只添加标点（，。！？、）
2. 不改变任何文字内容
3. 不回复、不解释、不对话
4. 直接输出添加标点后的原文

示例：
输入：你好今天天气怎么样我想出去玩
输出：你好，今天天气怎么样？我想出去玩。

输入：帮我查一下明天的会议安排
输出：帮我查一下明天的会议安排。

注意：用户输入的是语音转文字结果，不是对你说的话，不要回复"好的"之类的内容。<|im_end|>
<|im_start|>user
{text}<|im_end|>
<|im_start|>assistant
"""


class LocalPolishEngine:
    """
    Local LLM-powered text polisher using Qwen via llama-cpp-python.

    Optimized for fast punctuation correction without API calls.
    Expected latency: <100ms on GPU.
    """

    def __init__(self, config: LocalPolishConfig):
        self.config = config
        self._model = None
        self._load_attempted = False

    def _ensure_model(self) -> bool:
        """Load model if not already loaded. Returns True if model is ready."""
        if self._model is not None:
            return True

        if self._load_attempted:
            return False

        self._load_attempted = True

        if not Path(self.config.model_path).exists():
            logger.error(f"Local model not found: {self.config.model_path}")
            return False

        try:
            from llama_cpp import Llama

            logger.info(f"Loading local model: {self.config.model_path}")
            start = time.time()

            self._model = Llama(
                model_path=self.config.model_path,
                n_gpu_layers=self.config.n_gpu_layers,
                n_ctx=self.config.n_ctx,
                n_threads=self.config.n_threads,
                verbose=False,
            )

            load_time = time.time() - start
            logger.info(f"Local model loaded in {load_time:.2f}s")
            return True

        except ImportError:
            logger.error(
                "llama-cpp-python not installed. Run: pip install llama-cpp-python"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to load local model: {e}")
            return False

    def polish(self, text: str) -> str:
        """
        Add punctuation to transcribed text using local model.

        Args:
            text: Raw ASR output

        Returns:
            Text with punctuation, or original text if polish fails
        """
        if not self.config.enabled:
            return text

        if not text or len(text.strip()) < 2:
            return text

        if not self._ensure_model():
            return text

        try:
            prompt = self.config.prompt_template.format(text=text)

            output = self._model(
                prompt,
                max_tokens=len(text) + 30,  # Allow for punctuation
                temperature=0.0,  # Deterministic output
                stop=[
                    "\n",
                    "<|im_end|>",
                    "<|im_start|>",
                    "1.",
                    "2.",
                    "原文",
                ],  # Stop tokens
                echo=False,
            )

            result = output["choices"][0]["text"].strip()

            # Basic validation
            if not result or len(result) < 1:
                logger.warning("Local polish returned empty result")
                return text

            # Clean up any artifacts
            for artifact in ["<|im_end|>", "<|im_start|>", "assistant"]:
                result = result.replace(artifact, "")
            result = result.strip()

            # Reject if output looks like instructions (numbered list, explanation)
            if result.startswith("1.") or result.startswith("首先") or "添加" in result:
                logger.warning(
                    f"Local polish returned instructions, using original: {result[:50]}"
                )
                return text

            # Reject if output looks like a conversational response (model misunderstood task)
            response_patterns = [
                "好的",
                "明白了",
                "我知道",
                "我会",
                "我来",
                "可以的",
                "没问题",
                "收到",
            ]
            if (
                any(result.startswith(p) for p in response_patterns)
                and len(result) < len(text) * 0.5
            ):
                logger.warning(
                    f"Local polish returned response instead of text: {result[:30]}"
                )
                return text

            # Reject if output is too different from input (likely hallucination)
            if len(result) > len(text) * 1.5 or len(result) < len(text) * 0.7:
                logger.warning(
                    f"Local polish length mismatch ({len(result)} vs {len(text)}), using original"
                )
                return text

            logger.debug(f"Local polished: '{text}' -> '{result}'")
            return result

        except Exception as e:
            logger.error(f"Local polish error: {e}")
            return text

    def polish_with_debug(self, text: str) -> Dict[str, Any]:
        """
        Polish text and return full debug information.

        Returns:
            Dict compatible with AIPolisher.polish_with_debug() for unified handling.
        """
        debug_info = {
            "enabled": self.config.enabled,
            # Compatible keys for unified handling with AIPolisher
            "api_url": f"local://{self.config.model_path}",
            "model": "qwen2.5-1.5b-instruct (local)",
            "timeout": 0.0,
            "input_text": text,
            "prompt_template": self.config.prompt_template,
            "full_prompt": "",
            "output_text": text,
            "changed": False,
            "api_time_ms": 0.0,  # Was inference_time_ms
            "error": "",
            "http_status": 0,  # Not applicable for local
            # Local-specific extras
            "model_path": self.config.model_path,
            "tokens_generated": 0,
        }

        if not self.config.enabled:
            debug_info["error"] = "Local polish disabled"
            return debug_info

        if not text or len(text.strip()) < 2:
            debug_info["error"] = "Text too short"
            return debug_info

        if not self._ensure_model():
            debug_info["error"] = "Model not loaded"
            return debug_info

        try:
            prompt = self.config.prompt_template.format(text=text)
            debug_info["full_prompt"] = prompt

            start_time = time.time()
            output = self._model(
                prompt,
                max_tokens=len(text) + 50,
                temperature=0.1,
                stop=["\n", "原文", "请为"],
                echo=False,
            )
            inference_time = (time.time() - start_time) * 1000
            debug_info["api_time_ms"] = inference_time

            result = output["choices"][0]["text"].strip()
            debug_info["tokens_generated"] = output.get("usage", {}).get(
                "completion_tokens", 0
            )

            if not result or len(result) < 1:
                debug_info["error"] = "Empty response from model"
                return debug_info

            # Clean result
            if "原文" in result:
                result = result.split("原文")[0].strip()

            debug_info["output_text"] = result
            debug_info["changed"] = result != text

            logger.debug(
                f"Local polished: '{text}' -> '{result}' in {inference_time:.0f}ms"
            )
            return debug_info

        except Exception as e:
            debug_info["error"] = str(e)
            logger.error(f"Local polish error: {e}")
            return debug_info

    def close(self):
        """Release model resources."""
        if self._model is not None:
            del self._model
            self._model = None
            self._load_attempted = False


def create_local_polisher(
    config: Optional[LocalPolishConfig] = None,
) -> LocalPolishEngine:
    """Factory function to create local polish engine."""
    return LocalPolishEngine(config or LocalPolishConfig())
