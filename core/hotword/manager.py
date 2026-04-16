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
from .utils import is_english_word as is_english_hotword

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

    # ASR engine type - affects polish layer behavior
    # Qwen3 handles English well at ASR layer, so we reduce English hotwords to LLM
    asr_engine_type: str = "qwen3"  # "qwen3", "funasr"

    # Layer 3: Polish mode and configs
    polish_mode: str = (
        "quality"  # "off" = disabled, "fast" = local LLM, "quality" = API polish
    )
    polish_config: Optional[PolishConfig] = None  # For quality mode
    local_polish_config: Optional[LocalPolishConfig] = None  # For fast mode

    # v1.2: 个性化偏好 + 一键开关
    personalization_rules: str = ""
    auto_structure: bool = False
    filter_filler_words: bool = True

    # v1.2: 窗口上下文感知
    screen_context_enabled: bool = True
    app_categories: Dict[str, str] = field(default_factory=dict)

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

            # Build prompt_words: merge hotwords + legacy prompt_words (stable order)
            # NOTE: replacement values are NOT included — they don't have explicit
            # weights and would pollute ASR context with unintended bias.
            # Replacements are applied in Layer 2 (regex) regardless.
            legacy_prompt_words = data.get("prompt_words", [])
            self.prompt_words = list(dict.fromkeys(self.hotwords + legacy_prompt_words))

            # Load polish mode
            self.polish_mode = data.get("polish_mode", "quality")

            # v1.2: Load personalization preferences
            self.personalization_rules = data.get("personalization_rules", "")
            self.auto_structure = data.get("auto_structure", False)
            self.filter_filler_words = data.get("filter_filler_words", True)

            # v1.2: Load screen context settings
            self.screen_context_enabled = data.get("screen_context_enabled", True)
            self.app_categories = data.get("app_categories", {})

            # Load quality mode (API) polish config if present
            polish_data = data.get("polish", {})
            if polish_data:
                config_kwargs = {
                    "enabled": polish_data.get("enabled", False),
                    "api_url": polish_data.get("api_url", "https://openrouter.ai/api"),
                    "api_key": polish_data.get("api_key", ""),
                    "model": polish_data.get(
                        "model", "deepseek/deepseek-chat-v3.1:free"
                    ),
                    "timeout": polish_data.get("timeout", 10.0),
                    # 智能轮询配置（备用 API）
                    "api_url_backup": polish_data.get("api_url_backup", ""),
                    "api_key_backup": polish_data.get("api_key_backup", ""),
                    "model_backup": polish_data.get("model_backup", ""),
                    "slow_threshold_ms": polish_data.get("slow_threshold_ms", 3000.0),
                    "switch_after_slow_count": polish_data.get(
                        "switch_after_slow_count", 2
                    ),
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
        """Save configuration to JSON file, preserving unknown fields."""
        save_path = path or self.config_path
        if not save_path:
            raise ValueError("No config path specified for saving")

        # Load existing config to preserve unknown fields (e.g., "general", "hotword_weights")
        data = {}
        if os.path.exists(save_path):
            try:
                with open(save_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                data = {}

        # Update only the fields managed by HotWordConfig
        data.update(
            {
                "enable_initial_prompt": self.enable_initial_prompt,
                "hotwords": self.hotwords,  # Primary: user-defined target words
                "replacements": self.replacements,  # Optional: explicit corrections
                "domain_context": self.domain_context,
                "polish_mode": self.polish_mode,
                # v1.2: personalization preferences
                "personalization_rules": self.personalization_rules,
                "auto_structure": self.auto_structure,
                "filter_filler_words": self.filter_filler_words,
                # v1.2: screen context settings
                "screen_context_enabled": self.screen_context_enabled,
                "app_categories": self.app_categories,
                # Note: prompt_words is auto-generated from hotwords, not saved
            }
        )

        # Save quality mode (API) polish config if present
        if self.polish_config:
            polish_save = {
                "enabled": self.polish_config.enabled,
                "api_url": self.polish_config.api_url,
                "api_key": self.polish_config.api_key,
                "model": self.polish_config.model,
                "timeout": self.polish_config.timeout,
            }
            # 保存智能轮询配置（仅当有备用 API 时）
            if self.polish_config.api_url_backup:
                polish_save["api_url_backup"] = self.polish_config.api_url_backup
                polish_save["api_key_backup"] = self.polish_config.api_key_backup
                polish_save["model_backup"] = self.polish_config.model_backup
                polish_save["slow_threshold_ms"] = self.polish_config.slow_threshold_ms
                polish_save["switch_after_slow_count"] = (
                    self.polish_config.switch_after_slow_count
                )
            data["polish"] = polish_save

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

            # Atomic write: write to temp file first, then replace
            # Prevents config corruption if crash occurs mid-write
            tmp_path = save_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, save_path)

            logger.info(f"Saved hotword config to {save_path}")

        except IOError as e:
            logger.error(f"Failed to save hotword config: {e}")
            # Clean up temp file on failure
            tmp_path = save_path + ".tmp"
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
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
        Build ASR initial_prompt from hotwords.

        Format optimized for ASR engine bias:
        - Natural sentence structure for better recognition bias
        - Explicit instruction to keep English casing
        - Only includes hotwords with weight >= 0.5 (low weight = excluded)
        """
        if not self.config.enable_initial_prompt:
            logger.info("initial_prompt disabled by config")
            return ""

        if self._prompt_cache is not None:
            return self._prompt_cache

        if not self.config.prompt_words:
            return ""

        # Load weights to filter low-priority hotwords
        weights = {}
        if self.config.config_path:
            try:
                with open(self.config.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                weights = data.get("hotword_weights", {})
            except Exception:
                pass

        # Filter: only include words with weight >= 0.5
        # This allows users to set low weight (0.3) to exclude from initial_prompt
        # while keeping them available for AI polish reference
        MIN_WEIGHT_FOR_PROMPT = 0.5
        filtered_words = [
            word
            for word in self.config.prompt_words
            if weights.get(word, 0.3) >= MIN_WEIGHT_FOR_PROMPT
        ]

        if not filtered_words:
            logger.info("No hotwords with weight >= 0.5, skipping initial_prompt")
            return ""

        # Build optimized prompt format
        # Use comma-separated list instead of Chinese punctuation for better tokenization
        vocab_str = ", ".join(filtered_words)
        logger.debug(
            f"initial_prompt includes {len(filtered_words)}/{len(self.config.prompt_words)} hotwords (weight >= {MIN_WEIGHT_FOR_PROMPT})"
        )

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

    def get_polish_hotwords_tiered(self) -> Dict[str, List[str]]:
        """
        Get hotwords for Polish, split into tiers by weight.

        5-tier system (v3.4, design consensus):
        - critical (weight = 1.0): Mandatory vocabulary, LLM must use these spellings
        - reference (weight = 0.5): Reference words, LLM should prefer if phonetically similar
        - hint (weight = 0.3): Light reference, included in polish for safe correction
        - cautious (weight = 0.1): Strict constraint, LLM only replaces garbled phonetic text
        - disabled (weight = 0): Completely excluded

        v3.4 Change (cautious tier):
        - 0.1 words completely bypass ASR (no bias at all)
        - Only participate in L4 polish with independent strict constraint block
        - LLM told "only replace when original text is meaningless garbled phonetics"

        v3.3 Change (hint tier enabled for polish):
        - 0.3 words were previously excluded from ALL layers for Qwen3 users
        - Now included in L4 polish as low-priority hints (appended to reference tier)
        - Still excluded from L1 Qwen3 context (prevents hallucination)
        - This gives 0.3 words actual corrective power without ASR bias risk

        v3.2 Change (Qwen3 optimization):
        - Qwen3-ASR handles English hotwords well at ASR layer
        - So for Qwen3 mode: only critical-tier English goes to LLM polish
        - This prevents LLM over-replacement of normal English words

        Returns:
            {"critical": [...], "reference": [...], "english_reference": [...], "cautious": [...]}
        """
        weights = self._load_weights()

        # Check if using Qwen3 ASR (handles English well at ASR layer)
        is_qwen3_mode = self.config.asr_engine_type == "qwen3"

        critical = []
        critical_english = []  # Separate for Qwen3 mode filtering
        reference = []  # Chinese reference
        english_reference = []  # English reference
        cautious = []  # Cautious tier (0.1): strict LLM constraint
        cautious_english = []

        for word in self.config.prompt_words:
            w = weights.get(word, 0.3)  # Default to hint tier

            # Check if English hotword
            is_english = is_english_hotword(word)

            if w >= 1.0:
                # Critical tier: both Chinese and English included
                if is_english:
                    critical_english.append(word)
                else:
                    critical.append(word)
            elif w >= 0.5:
                # Reference tier: separate Chinese and English
                if is_english:
                    english_reference.append(word)
                else:
                    reference.append(word)
            elif w >= 0.3:
                # Hint tier (v3.3): included in polish as low-priority reference
                # Not in Qwen3 context (no ASR bias), but LLM can correct if needed
                if is_english:
                    english_reference.append(word)
                else:
                    reference.append(word)
            elif w >= 0.1:
                # Cautious tier (v3.4): no ASR bias, strict LLM constraint only
                # Not affected by Qwen3 English filtering (0.1 words don't go through ASR,
                # LLM is the only correction channel)
                if is_english:
                    cautious_english.append(word)
                else:
                    cautious.append(word)
            # weight < 0.1 or 0: disabled, excluded from all layers

        # Qwen3 optimization: reduce English hotwords to LLM
        if is_qwen3_mode:
            # Only pass critical-tier English to LLM (音译乱码修正)
            # Reference-tier English already handled by Qwen3 ASR
            # Note: cautious_english is NOT filtered — 0.1 words don't go through ASR,
            # so LLM is their only correction channel
            logger.debug(
                f"Qwen3 mode: skipping {len(english_reference)} reference-tier "
                f"English hotwords for LLM polish"
            )
            english_reference = []  # Don't send reference-tier English to LLM

        # Merge critical tiers
        all_critical = critical + critical_english

        # Merge cautious tiers (not affected by Qwen3 English filter)
        all_cautious = (cautious + cautious_english)[:10]  # Cap at 10

        logger.debug(
            f"Polish tiers (asr={self.config.asr_engine_type}): "
            f"critical={len(all_critical)}, reference={len(reference)}, "
            f"english_reference={len(english_reference)}, cautious={len(all_cautious)}"
        )

        # Return structure with separate English tier
        return {
            "critical": all_critical,
            "reference": reference,
            "english_reference": english_reference,
            "cautious": all_cautious,
        }

    def _load_weights(self) -> Dict[str, float]:
        """Load hotword weights from config file."""
        weights = {}
        if self.config.config_path:
            try:
                with open(self.config.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                weights = data.get("hotword_weights", {})
            except Exception:
                pass
        return weights

    def get_hotwords_by_layer(self) -> Dict[str, List[str]]:
        """
        Get hotwords filtered by layer based on weight.

        Weight-to-layer mapping (v3.4, design consensus):
        - weight >= 0.3: Layer 1 (ASR) - hint to ASR (FunASR score only)
        - weight >= 0.1: Layer 2 (Regex) - deterministic replacement (safe, no hallucination)
        - weight >= 1.0: Layer 2.5 (Pinyin) - fuzzy matching allowed (aggressive)

        Note: L2 regex is safe at 0.1 because it only fires on exact pattern match
        in ASR output — cannot fabricate words that weren't spoken.

        Returns:
            {"layer1_asr": [...], "layer2_regex": [...], "layer2_5_pinyin": [...]}
        """
        weights = self._load_weights()

        layer1_asr = []  # weight >= 0.3
        layer2_regex = (
            []
        )  # weight >= 0.1 (v3.4: lowered from 0.3, deterministic = safe)
        layer2_5_pinyin = []  # weight >= 1.0

        for word in self.config.prompt_words:
            w = weights.get(word, 0.3)  # Default 0.3

            if w >= 0.3:
                layer1_asr.append(word)
            if w >= 0.1:
                layer2_regex.append(word)
            if w >= 1.0:
                layer2_5_pinyin.append(word)

        logger.debug(
            f"Hotwords by layer: ASR={len(layer1_asr)}, "
            f"Regex={len(layer2_regex)}, Pinyin={len(layer2_5_pinyin)}"
        )
        return {
            "layer1_asr": layer1_asr,
            "layer2_regex": layer2_regex,
            "layer2_5_pinyin": layer2_5_pinyin,
        }

    def get_asr_hotwords_with_score(self) -> List[tuple]:
        """
        Get hotwords with FunASR score based on weight.

        Simplified 3-tier score mapping (v3.0):
        - weight = 0 → skip (disabled)
        - weight = 0.3 → score 30 (hint only, ASR boost)
        - weight = 0.5 → score 60 (reference, standard recognition)
        - weight = 1.0 → score 100 (lock/maximum)

        Note: FunASR hotword system works primarily for Chinese.
        English hotwords may not get proper ASR boost regardless of score.

        Returns:
            List of (word, score) tuples for FunASR hotword parameter
        """
        weights = self._load_weights()
        result = []

        for word in self.config.prompt_words:
            w = weights.get(word, 0.3)

            if w < 0.3:
                continue  # 0 = disabled, 0.1 = cautious (no ASR bias)
            elif w < 0.4:
                score = 30  # Hint tier (0.3)
            elif w < 0.8:
                score = 60  # Reference tier (0.5)
            else:
                score = 100  # Critical tier (1.0)

            result.append((word, score))

        logger.debug(f"FunASR hotwords: {len(result)} words with scores")
        return result

    def get_polisher(self, ignore_enabled: bool = False) -> Optional[AIPolisher]:
        """Get AI polisher instance for quality mode (lazy init).

        Args:
            ignore_enabled: If True, skip the .enabled check (used by fallback path).
        """
        if self.config.polish_config and (
            ignore_enabled or self.config.polish_config.enabled
        ):
            if self._polisher is None:
                # Get tiered hotwords (v3.3: weight >= 0.3, includes hint tier)
                tiers = self.get_polish_hotwords_tiered()

                # Pass tiered structure to polisher config
                # v3.1: English hotwords now included with separate tier
                # - critical (1.0): mandatory replacement (Chinese + English)
                # - reference (0.5): Chinese reference words
                # - english_reference (0.5): English reference with stricter rules
                all_polish_hotwords = (
                    tiers["critical"]
                    + tiers["reference"]
                    + tiers["english_reference"]
                    + tiers["cautious"]
                )

                self.config.polish_config.hotwords = all_polish_hotwords
                self.config.polish_config.hotwords_critical = tiers["critical"]
                self.config.polish_config.hotwords_strong = tiers["reference"]
                self.config.polish_config.hotwords_english = tiers["english_reference"]
                self.config.polish_config.hotwords_cautious = tiers["cautious"]
                # hotwords_context left as default [] (v3.1 simplified tiers)
                self.config.polish_config.domain_context = self.config.domain_context
                # v1.2: Pass personalization preferences to polisher config
                self.config.polish_config.personalization_rules = (
                    self.config.personalization_rules
                )
                self.config.polish_config.auto_structure = self.config.auto_structure
                self.config.polish_config.filter_filler_words = (
                    self.config.filter_filler_words
                )
                self._polisher = AIPolisher(self.config.polish_config)
                logger.debug(
                    f"Polish hotwords: {len(all_polish_hotwords)}/{len(self.config.prompt_words)} "
                    f"(critical={len(tiers['critical'])}, chinese_ref={len(tiers['reference'])}, "
                    f"english_ref={len(tiers['english_reference'])}, cautious={len(tiers['cautious'])})"
                )
            return self._polisher
        return None

    def get_local_polisher(
        self, ignore_enabled: bool = False
    ) -> Optional[LocalPolishEngine]:
        """Get local polisher instance for fast mode (lazy init).

        Args:
            ignore_enabled: If True, skip the .enabled check (used by fallback path).
        """
        if self.config.local_polish_config and (
            ignore_enabled or self.config.local_polish_config.enabled
        ):
            if self._local_polisher is None:
                try:
                    self._local_polisher = LocalPolishEngine(
                        self.config.local_polish_config
                    )
                except Exception as e:
                    logger.error(f"Failed to create LocalPolishEngine: {e}")
                    return None
            return self._local_polisher
        return None

    def get_active_polisher(self):
        """
        Get the polisher based on current polish_mode setting.

        Returns:
            - None if mode is "off"
            - LocalPolishEngine if mode is "fast" and local_polish enabled
            - AIPolisher if mode is "quality" and polish enabled
            - None if no polisher available (no silent fallback)

        Note: Both polisher types have polish() and polish_with_debug() methods
              with compatible signatures including screen_context parameter.
        """
        if self.config.polish_mode == "off":
            return None
        elif self.config.polish_mode == "fast":
            polisher = self.get_local_polisher()
            if polisher:
                return polisher
            logger.warning("Local polisher not available, polish disabled")
            return None
        else:  # "quality" mode
            polisher = self.get_polisher()
            if polisher:
                return polisher
            logger.warning("API polisher not available, polish disabled")
            return None

    @property
    def polish_mode(self) -> str:
        """Get current polish mode."""
        return self.config.polish_mode

    def set_polish_mode(self, mode: str) -> None:
        """
        Set polish mode and update active polisher.

        Args:
            mode: "off" (disabled), "fast" (local Qwen), or "quality" (API)
        """
        if mode not in ("off", "fast", "quality"):
            logger.warning(f"Unknown polish mode: {mode}, defaulting to 'fast'")
            mode = "fast"

        self.config.polish_mode = mode

        # Auto-enable/disable polishers based on mode
        if self.config.local_polish_config:
            self.config.local_polish_config.enabled = mode == "fast"
        if self.config.polish_config:
            self.config.polish_config.enabled = mode == "quality"

        logger.info(
            f"Polish mode changed to: {mode} "
            f"(local={mode == 'fast'}, api={mode == 'quality'})"
        )

        # Save to config file
        if self.config.config_path:
            self.config.save_to_file()

    def to_qwen3_context(self) -> str:
        """
        Build Qwen3-ASR context (V4: structured natural-language format).

        V4 key insight (2026-04-03, A/B tested with TTS audio):
        Flat word lists do NOT bias Qwen3-ASR for phonetically ambiguous words
        (e.g., "PyTorch" always outputs as "拍拓"). Structured natural-language
        context with an explicit label ("用户常提到的专有名词：...") makes the
        model understand these are proper nouns to preserve as-is.

        Test results (5 audio × 4 formats):
        - Flat list:    PyTorch→拍拓 ❌, ComfyUI→Confluent ❌
        - Structured:   PyTorch ✅, ComfyUI ✅ (strict improvement)
        - Struct+OCR:   identical to Struct (no interference)
        - Normal speech: unaffected by context format

        V4 Strategy:
        1. Critical words (weight >= 1.0): listed first (attention priority)
        2. Reference words (weight >= 0.5): listed after critical
        3. All wrapped in structured label for LLM comprehension
        4. Domain context as separate paragraph

        Returns:
            Formatted context string for Qwen3-ASR
        """
        weights = self._load_weights()
        parts = []

        # Collect hotwords by tier (critical first for attention priority)
        critical = [
            word for word in self.config.prompt_words if weights.get(word, 0.3) >= 1.0
        ]
        reference = [
            word
            for word in self.config.prompt_words
            if 0.5 <= weights.get(word, 0.3) < 1.0
        ]

        # Build structured hotword section
        # The label "用户常提到的专有名词" tells the LLM-based ASR that these
        # are proper nouns to output verbatim, not random tokens to ignore.
        all_hotwords = critical + reference
        if all_hotwords:
            hotword_str = ", ".join(all_hotwords)
            parts.append(f"用户常提到的专有名词：{hotword_str}")

        # Domain context as separate paragraph (natural text, not label)
        if self.config.domain_context:
            parts.append(self.config.domain_context)

        context = "\n".join(parts)

        # Token estimation and safety check
        est_tokens = self._estimate_tokens(context)
        if est_tokens > 9000:  # Leave 1K buffer
            logger.warning(
                f"Qwen3 context approaching limit: ~{est_tokens} tokens, truncating"
            )
            context = self._truncate_context(context, max_tokens=9000)
            est_tokens = self._estimate_tokens(context)

        logger.debug(
            f"Qwen3 context V4: critical={len(critical)}, "
            f"reference={len(reference)}, "
            f"chars={len(context)}, est_tokens={est_tokens}"
        )

        return context

    def _get_example_sentences(self) -> List[str]:
        """
        Load example sentences from config file.

        WARNING: Example sentences cause HALLUCINATION in Qwen3-ASR!
        When audio is short/ambiguous, the model outputs example sentences
        verbatim instead of actual speech. This feature is DISABLED.

        Evidence (2025-01-31 debug log):
        - 1.3s audio → output entire example_sentences as transcription
        - Root cause: Context biasing too aggressive with complete sentences

        Returns:
            Empty list (feature disabled to prevent hallucination)
        """
        # DISABLED: Example sentences cause hallucination
        # See docstring for details. Do not re-enable without fixing.
        return []

    def _estimate_tokens(self, text: str) -> int:
        """
        Estimate token count for Qwen3-ASR context.

        Rough estimation:
        - Chinese characters: ~1.5 chars per token
        - English/ASCII: ~4 chars per token

        Args:
            text: The context string to estimate

        Returns:
            Estimated token count
        """
        if not text:
            return 0

        cn_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        en_chars = len(text) - cn_chars

        # Rough estimation formula
        return int(cn_chars / 1.5 + en_chars / 4)

    def _truncate_context(self, context: str, max_tokens: int = 9000) -> str:
        """
        Truncate context to fit within token limit.

        V4 format has simple structure: hotword line + domain context.
        Preserves lines in order, dropping from the end when over budget.

        Args:
            context: Full context string
            max_tokens: Maximum tokens allowed

        Returns:
            Truncated context string
        """
        lines = context.split("\n")
        result_lines = []

        for line in lines:
            result_lines.append(line)
            if self._estimate_tokens("\n".join(result_lines)) > max_tokens:
                result_lines.pop()
                break

        return "\n".join(result_lines)

    def reload(self) -> None:
        """Reload configuration from file."""
        if self.config.config_path:
            self.config.load_from_file(self.config.config_path)
            self._prompt_cache = None
            self._polisher = None
            self._local_polisher = None
