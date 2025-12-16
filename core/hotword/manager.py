"""
HotWord Manager
===============
Manages hotword configuration and builds ASR prompt.
"""

import json
import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from pathlib import Path

from ..logging import get_system_logger
from ..utils import get_config_path
from .polish import AIPolisher, PolishConfig
from .local_polish import LocalPolishEngine, LocalPolishConfig

logger = get_system_logger()


@dataclass
class HotWordConfig:
    """HotWord system configuration."""

    config_path: Optional[str] = None

    # Loaded from config file
    enable_initial_prompt: bool = True
    hotwords: List[str] = field(
        default_factory=list
    )  # Primary: target words user wants
    replacements: Dict[str, str] = field(
        default_factory=dict
    )  # Optional: explicit corrections
    domain_context: str = ""

    # Internal: merged list for ASR/AI (auto-generated)
    prompt_words: List[str] = field(default_factory=list)

    # Layer 3: Polish mode and configs
    polish_mode: str = "fast"  # "fast" = local Qwen, "quality" = Gemini API
    polish_config: Optional[PolishConfig] = None  # For quality mode
    local_polish_config: Optional[LocalPolishConfig] = None  # For fast mode

    def __post_init__(self):
        if self.config_path:
            self.load_from_file(self.config_path)

    def load_from_file(self, path: str) -> None:
        """Load configuration from JSON file."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.enable_initial_prompt = data.get("enable_initial_prompt", True)
            self.domain_context = data.get("domain_context", "")

            # Primary source: simple hotwords array (user only fills target words)
            self.hotwords = data.get("hotwords", [])

            # Optional: explicit replacements for edge cases (backward compatible)
            self.replacements = data.get("replacements", {})

            # Build prompt_words: merge hotwords + replacements values + legacy prompt_words
            legacy_prompt_words = data.get("prompt_words", [])
            replacement_values = list(set(self.replacements.values()))
            self.prompt_words = list(
                set(self.hotwords + replacement_values + legacy_prompt_words)
            )

            # Load polish mode
            self.polish_mode = data.get("polish_mode", "fast")

            # Load quality mode (API) polish config if present
            polish_data = data.get("polish", {})
            if polish_data:
                config_kwargs = {
                    "enabled": polish_data.get("enabled", False),
                    "api_url": polish_data.get("api_url", "http://localhost:3000"),
                    "api_key": polish_data.get("api_key", ""),
                    "model": polish_data.get(
                        "model", "google/gemini-2.5-flash-lite-preview-09-2025"
                    ),
                    "timeout": polish_data.get("timeout", 10.0),
                }
                # Allow optional prompt_template override from config
                if "prompt_template" in polish_data:
                    config_kwargs["prompt_template"] = polish_data["prompt_template"]
                self.polish_config = PolishConfig(**config_kwargs)

            # Load fast mode (local) polish config if present
            local_polish_data = data.get("local_polish", {})
            if local_polish_data:
                # Resolve model path relative to package dir
                model_path = local_polish_data.get("model_path", "")
                if model_path and not Path(model_path).is_absolute():
                    package_dir = Path(path).parent.parent
                    model_path = str(package_dir / model_path)

                self.local_polish_config = LocalPolishConfig(
                    enabled=local_polish_data.get("enabled", False),
                    model_path=model_path,
                    n_gpu_layers=local_polish_data.get("n_gpu_layers", -1),
                    n_ctx=local_polish_data.get("n_ctx", 512),
                )

            logger.info(
                f"Loaded {len(self.hotwords)} hotwords, {len(self.replacements)} replacements, polish_mode={self.polish_mode}"
            )

        except FileNotFoundError:
            logger.warning(f"HotWord config not found: {path}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in hotword config: {e}")

    def save_to_file(self, path: Optional[str] = None) -> None:
        """Save configuration to JSON file."""
        save_path = path or self.config_path
        if not save_path:
            raise ValueError("No config path specified for saving")

        data = {
            "enable_initial_prompt": self.enable_initial_prompt,
            "hotwords": self.hotwords,  # Primary: user-defined target words
            "replacements": self.replacements,  # Optional: explicit corrections
            "domain_context": self.domain_context,
            "polish_mode": self.polish_mode,
            # Note: prompt_words is auto-generated from hotwords, not saved
        }

        # Save quality mode (API) polish config if present
        if self.polish_config:
            data["polish"] = {
                "enabled": self.polish_config.enabled,
                "api_url": self.polish_config.api_url,
                "api_key": self.polish_config.api_key,
                "model": self.polish_config.model,
                "timeout": self.polish_config.timeout,
            }

        # Save fast mode (local) polish config if present
        if self.local_polish_config:
            data["local_polish"] = {
                "enabled": self.local_polish_config.enabled,
                "model_path": self.local_polish_config.model_path,
                "n_gpu_layers": self.local_polish_config.n_gpu_layers,
                "n_ctx": self.local_polish_config.n_ctx,
            }

        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(save_path), exist_ok=True)

            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            logger.info(f"Saved hotword config to {save_path}")

        except IOError as e:
            logger.error(f"Failed to save hotword config: {e}")
            raise


class HotWordManager:
    """
    Manages hotword vocabulary and builds ASR prompts.
    """

    DEFAULT_CONFIG_PATH = "config/hotwords.json"

    def __init__(self, config: Optional[HotWordConfig] = None):
        self.config = config or HotWordConfig()
        self._prompt_cache: Optional[str] = None
        self._polisher: Optional[AIPolisher] = None
        self._local_polisher: Optional[LocalPolishEngine] = None

    @classmethod
    def from_default(cls) -> "HotWordManager":
        """Create manager with default config path."""
        config_path = get_config_path("hotwords.json")

        if config_path.exists():
            config = HotWordConfig(config_path=str(config_path))
            return cls(config)
        else:
            logger.warning(f"Default config not found: {config_path}")
            return cls()

    def build_initial_prompt(self) -> str:
        """
        Build Whisper initial_prompt from hotwords.

        Format optimized based on Codex+Gemini analysis:
        - Natural sentence structure for better Whisper bias
        - Explicit instruction to keep English casing
        - Important hotwords at the start (224 token limit)
        """
        if not self.config.enable_initial_prompt:
            logger.info("initial_prompt disabled by config")
            return ""

        if self._prompt_cache is not None:
            return self._prompt_cache

        if not self.config.prompt_words:
            return ""

        # Build optimized prompt format
        # Use comma-separated list instead of Chinese punctuation for better tokenization
        vocab_str = ", ".join(self.config.prompt_words)

        # Natural sentence with explicit instruction to preserve English
        prompt_parts = []
        if self.config.domain_context:
            prompt_parts.append(f"场景：{self.config.domain_context}")

        prompt_parts.append(
            f"常见专有名词（请按原样输出英文大小写，不要翻译）：{vocab_str}"
        )

        self._prompt_cache = "。".join(prompt_parts)
        logger.debug(f"Built initial_prompt: {self._prompt_cache[:100]}...")
        return self._prompt_cache

    def get_replacements(self) -> Dict[str, str]:
        """Get replacement rules for post-processing."""
        return self.config.replacements.copy()

    def get_polisher(self) -> Optional[AIPolisher]:
        """Get AI polisher instance for quality mode (lazy init)."""
        if self.config.polish_config and self.config.polish_config.enabled:
            if self._polisher is None:
                # Inject hotwords and domain_context into polish config
                # Use hotwords (primary) merged with prompt_words (includes legacy)
                self.config.polish_config.hotwords = self.config.prompt_words
                self.config.polish_config.domain_context = self.config.domain_context
                self._polisher = AIPolisher(self.config.polish_config)
            return self._polisher
        return None

    def get_local_polisher(self) -> Optional[LocalPolishEngine]:
        """Get local polisher instance for fast mode (lazy init)."""
        if self.config.local_polish_config and self.config.local_polish_config.enabled:
            if self._local_polisher is None:
                self._local_polisher = LocalPolishEngine(
                    self.config.local_polish_config
                )
            return self._local_polisher
        return None

    def get_active_polisher(self):
        """
        Get the polisher based on current polish_mode setting.

        Returns:
            - LocalPolishEngine if mode is "fast" and local_polish enabled
            - AIPolisher if mode is "quality" and polish enabled
            - None if no polisher available

        Note: Both polisher types have polish() and polish_with_debug() methods.
        """
        if self.config.polish_mode == "fast":
            polisher = self.get_local_polisher()
            if polisher:
                return polisher
            # Fallback to API if local not available
            logger.warning("Local polisher not available, falling back to API")
            return self.get_polisher()
        else:  # "quality" mode
            polisher = self.get_polisher()
            if polisher:
                return polisher
            # Fallback to local if API not available
            logger.warning("API polisher not available, falling back to local")
            return self.get_local_polisher()

    @property
    def polish_mode(self) -> str:
        """Get current polish mode."""
        return self.config.polish_mode

    def set_polish_mode(self, mode: str) -> None:
        """
        Set polish mode and update active polisher.

        Args:
            mode: "fast" (local Qwen) or "quality" (Gemini API)
        """
        if mode not in ("fast", "quality"):
            logger.warning(f"Unknown polish mode: {mode}, defaulting to 'fast'")
            mode = "fast"

        self.config.polish_mode = mode
        logger.info(f"Polish mode changed to: {mode}")

        # Save to config file
        if self.config.config_path:
            self.config.save_to_file()

    def get_weighted_hotwords(self) -> List[str]:
        """
        Get hotwords with weight-based repetition for FunASR.

        Higher weight = more repetitions = higher recognition priority.
        Weight 1.0 = 1 occurrence, 0.5 = 1 occurrence, 2.0 = 2 occurrences.

        Returns:
            List of hotwords with repetitions based on weights.
        """
        # Load weights from config file
        weights = {}
        if self.config.config_path:
            try:
                with open(self.config.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                weights = data.get("hotword_weights", {})
            except Exception:
                pass

        # Weight mapping: UI value -> FunASR score
        # 0=disabled, 1=low(5), 2=normal(10), 3=high(20)
        weight_to_score = {0: 0, 1: 5, 2: 10, 3: 20}

        result = []
        for word in self.config.hotwords:
            ui_weight = weights.get(word, 2)  # Default to normal(2)
            if ui_weight <= 0:
                continue  # Skip disabled words
            score = weight_to_score.get(int(ui_weight), 10)
            # FunASR format: "word score" (e.g., "阿里巴巴 20")
            result.append(f"{word} {score}")

        return result

    def reload(self) -> None:
        """Reload configuration from file."""
        if self.config.config_path:
            self.config.load_from_file(self.config.config_path)
            self._prompt_cache = None
            self._polisher = None
            self._local_polisher = None
