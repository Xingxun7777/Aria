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
from ..utils.secrets import is_encrypted, protect_secret, reveal_secret
from .polish import AIPolisher, PolishConfig, sanitize_polish_prompt_template
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
    asr_engine_type: str = "qwen3"  # "qwen3", "qwen3_sherpa", "qwen3_llamacpp", "funasr"

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
    cli_destutter: bool = True  # CLI/终端里自动去口水（独立于全局风格）

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
            self.cli_destutter = data.get("cli_destutter", True)
            # v5.1 迁移：polish_style 是 3 档风格的权威键。老配置无此键时，
            # v5.0 前的 filter_filler_words 是死开关、不代表去口水意图，按
            # auto_structure 重算派生 bool，保证老用户升级后全局行为不变。
            _style = data.get("polish_style")
            if _style not in ("verbatim", "smooth", "structured"):
                _style = "structured" if self.auto_structure else "verbatim"
            self.filter_filler_words = _style in ("smooth", "structured")
            self.auto_structure = _style == "structured"

            # v1.2: Load screen context settings
            self.screen_context_enabled = data.get("screen_context_enabled", True)
            self.app_categories = data.get("app_categories", {})

            # Load quality mode (API) polish config if present
            polish_data = data.get("polish", {})
            if polish_data:
                config_kwargs = {
                    "enabled": polish_data.get("enabled", False),
                    "api_url": polish_data.get("api_url", "https://api.deepseek.com"),
                    # Keys are stored DPAPI-encrypted at rest; in-memory config
                    # always holds plaintext (reveal passes plaintext through).
                    "api_key": reveal_secret(polish_data.get("api_key", "")),
                    "model": polish_data.get("model", "deepseek-v4-flash"),
                    "timeout": polish_data.get("timeout", 20.0),
                    # 智能轮询配置（备用 API）
                    "api_url_backup": polish_data.get("api_url_backup", ""),
                    "api_key_backup": reveal_secret(
                        polish_data.get("api_key_backup", "")
                    ),
                    "model_backup": polish_data.get("model_backup", ""),
                    "slow_threshold_ms": polish_data.get("slow_threshold_ms", 3000.0),
                    "switch_after_slow_count": polish_data.get(
                        "switch_after_slow_count", 2
                    ),
                    # 拼音辅助同音字纠错（quality 路径专用，默认关）
                    "pinyin_hint": polish_data.get("pinyin_hint", False),
                    # PERF-1: 启动/深睡唤醒后预热 API 连接与 prompt 前缀缓存
                    "prewarm": polish_data.get("prewarm", True),
                    # PERF-2: 短文本（<10 有效字且无句首口水词）跳过云润色
                    "skip_short_text": polish_data.get("skip_short_text", True),
                    # v14 润色管线总开关（回滚闸）与会话历史注入开关。
                    # 缺省即默认值；保存侧合并式回写（R9），回滚设置可持久化。
                    "pipeline_v14": polish_data.get("polish_pipeline_v14", True),
                    "recent_context": polish_data.get(
                        "polish_recent_context", True
                    ),
                }
                # Allow optional prompt_template overrides from config.
                # v5.0: TWO independent templates — loose (default) and
                # structured. Older configs may only have prompt_template;
                # sanitize_polish_prompt_template() handles the fallback by
                # returning the matching new default for missing/legacy
                # values, so we always pass through both fields.
                if "prompt_template" in polish_data:
                    config_kwargs["prompt_template"] = sanitize_polish_prompt_template(
                        polish_data["prompt_template"], structured=False
                    )
                if "prompt_template_structured" in polish_data:
                    config_kwargs["prompt_template_structured"] = (
                        sanitize_polish_prompt_template(
                            polish_data["prompt_template_structured"],
                            structured=True,
                        )
                    )
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

            # One-time migration: encrypt any plaintext api_key still stored in
            # the file. Idempotent — once every key carries the dpapi:v1:
            # prefix nothing is rewritten, so the 2s mtime watcher sees at most
            # ONE extra reload right after migration.
            try:
                self._migrate_plaintext_keys(path, data)
            except Exception as e:
                logger.warning(f"API key encryption migration skipped: {e}")

        except FileNotFoundError:
            logger.warning(f"HotWord config not found: {path}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in hotword config: {e}")

    @staticmethod
    def _migrate_plaintext_keys(path: str, data: dict) -> None:
        """Encrypt plaintext api_key fields in the config file (in place).

        Covers polish.api_key / polish.api_key_backup / auto_hotword.api_key /
        asr_rescue.api_key. Uses the same atomic json.dump write path as
        save_to_file(). No-op when DPAPI is unavailable (protect_secret
        returns plaintext unchanged) or when everything is already encrypted.
        """
        changed = False
        for block_name, key_names in (
            ("polish", ("api_key", "api_key_backup")),
            ("auto_hotword", ("api_key",)),
            ("asr_rescue", ("api_key",)),
        ):
            block = data.get(block_name)
            if not isinstance(block, dict):
                continue
            for key_name in key_names:
                value = block.get(key_name)
                if not value or not isinstance(value, str) or is_encrypted(value):
                    continue
                encrypted = protect_secret(value)
                if encrypted != value:
                    block[key_name] = encrypted
                    changed = True

        if not changed:
            return

        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        logger.info("Migrated plaintext API keys to DPAPI-encrypted storage")

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
                "cli_destutter": self.cli_destutter,
                # v1.2: screen context settings
                "screen_context_enabled": self.screen_context_enabled,
                "app_categories": self.app_categories,
                # Note: prompt_words is auto-generated from hotwords, not saved
            }
        )

        # Save quality mode (API) polish config if present
        if self.polish_config:
            # R9: merge-update the existing polish block instead of replacing
            # it wholesale — a fixed-whitelist rebuild silently drops keys the
            # loader understands but this writer doesn't (the v14 rollback
            # switches, custom prompt templates, future compat keys), turning
            # the user's rollback setting back on at the next load.
            existing_polish = data.get("polish")
            polish_save = (
                dict(existing_polish) if isinstance(existing_polish, dict) else {}
            )
            polish_save.update(
                {
                    "enabled": self.polish_config.enabled,
                    "api_url": self.polish_config.api_url,
                    # At-rest encryption (in-memory config holds plaintext)
                    "api_key": protect_secret(self.polish_config.api_key),
                    "model": self.polish_config.model,
                    "timeout": self.polish_config.timeout,
                    # 拼音辅助同音字纠错开关——不写回会在下次保存时抹掉用户手动开启的值
                    "pinyin_hint": self.polish_config.pinyin_hint,
                    # PERF-1/PERF-2 开关——同理必须写回，否则保存会抹掉用户关闭的值
                    "prewarm": self.polish_config.prewarm,
                    "skip_short_text": self.polish_config.skip_short_text,
                    # v14 回滚双开关——显式持久化（R9；旧「只读不回写」策略作废，
                    # 整块替换会让用户的回滚设置在下次加载时静默失效）
                    "polish_pipeline_v14": self.polish_config.pipeline_v14,
                    "polish_recent_context": self.polish_config.recent_context,
                }
            )
            # 保存智能轮询配置（仅当有备用 API 时）；备用清空时移除旧键，
            # 保持与整块替换时代相同的清除语义。
            if self.polish_config.api_url_backup:
                polish_save["api_url_backup"] = self.polish_config.api_url_backup
                polish_save["api_key_backup"] = protect_secret(
                    self.polish_config.api_key_backup
                )
                polish_save["model_backup"] = self.polish_config.model_backup
                polish_save["slow_threshold_ms"] = self.polish_config.slow_threshold_ms
                polish_save["switch_after_slow_count"] = (
                    self.polish_config.switch_after_slow_count
                )
            else:
                for _backup_key in (
                    "api_url_backup",
                    "api_key_backup",
                    "model_backup",
                    "slow_threshold_ms",
                    "switch_after_slow_count",
                ):
                    polish_save.pop(_backup_key, None)
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

    def _effective_asr_weight(self, weight: object) -> float:
        """Return runtime-only ASR weight for the current polish mode.

        In quality mode, weight 0.3 can stay a gentle Polish-only hint because
        the remote LLM still has a chance to use it after ASR. In fast mode the
        remote Polish layer is not available, so 0.3 hints must become normal
        ASR references. 0.1 stays excluded/cautious.

        This never writes back to hotwords.json; it only changes ASR prompt /
        context generation at runtime.
        """
        try:
            raw_weight = float(weight)
        except (TypeError, ValueError):
            raw_weight = 0.3

        if self.config.polish_mode == "fast" and 0.3 <= raw_weight < 0.5:
            return 0.5
        return raw_weight

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
        - Only includes hotwords with effective ASR weight >= 0.5
          (fast mode promotes raw 0.3 hints; quality mode does not)
        """
        if not self.config.enable_initial_prompt:
            logger.info("initial_prompt disabled by config")
            return ""

        if self._prompt_cache is not None:
            return self._prompt_cache

        if not self.config.prompt_words:
            return ""

        # Load weights to filter low-priority hotwords. In fast mode,
        # _effective_asr_weight() promotes 0.3 hints to 0.5 references at
        # runtime only so they still help ASR when API Polish is unavailable.
        weights = self._load_weights()

        # Filter: only include words with effective ASR weight >= 0.5.
        # Quality mode keeps raw 0.3 words out of ASR; fast mode promotes them
        # to 0.5. Raw 0.1 remains excluded in all modes.
        MIN_WEIGHT_FOR_PROMPT = 0.5
        filtered_words = [
            word
            for word in self.config.prompt_words
            if self._effective_asr_weight(weights.get(word, 0.3))
            >= MIN_WEIGHT_FOR_PROMPT
        ]

        if not filtered_words:
            logger.info(
                "No hotwords with effective ASR weight >= 0.5, skipping initial_prompt"
            )
            return ""

        # Build optimized prompt format
        # Use comma-separated list instead of Chinese punctuation for better tokenization
        vocab_str = ", ".join(filtered_words)
        logger.debug(
            f"initial_prompt includes {len(filtered_words)}/{len(self.config.prompt_words)} hotwords "
            f"(effective ASR weight >= {MIN_WEIGHT_FOR_PROMPT}, mode={self.config.polish_mode})"
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

        # Check if using Qwen3 ASR (handles English well at ASR layer).
        # qwen3_sherpa (0.6B int8 via sherpa-onnx) and qwen3_llamacpp
        # (1.7B GGUF via llama-server) are the same model family, so the
        # English-hotword tiering policy applies identically.
        is_qwen3_mode = self.config.asr_engine_type in (
            "qwen3",
            "qwen3_sherpa",
            "qwen3_llamacpp",
        )

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

        # Qwen3 optimization: keep a small high-signal English reference set
        # for LLM polish. Qwen3 handles many English terms in ASR, but live
        # logs still show spaced acronym output such as "C R I" / "A P P".
        # Clearing this list entirely made the Polish prompt say
        # "【英文参考词汇】无", removing the only safe spelling source for
        # user-configured acronyms and mixed-case product names.
        if is_qwen3_mode:
            def _high_signal_english_reference(word: str) -> bool:
                token = (word or "").strip()
                if not token:
                    return False
                compact = token.replace(" ", "")
                return (
                    any(ch.isupper() for ch in token)
                    or any(ch.isdigit() for ch in token)
                    or any((not ch.isalnum()) and (not ch.isspace()) for ch in token)
                    or (compact.isupper() and 2 <= len(compact) <= 10)
                )

            kept_english = [
                word
                for word in english_reference
                if _high_signal_english_reference(word)
            ][:25]
            skipped_count = len(english_reference) - len(kept_english)
            # Note: cautious_english is NOT filtered — 0.1 words don't go through ASR,
            # so LLM is their only correction channel
            logger.debug(
                f"Qwen3 mode: keeping {len(kept_english)} high-signal "
                f"English hotwords for LLM polish, skipping {skipped_count}"
            )
            english_reference = kept_english

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
            w = self._effective_asr_weight(weights.get(word, 0.3))

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
                self.config.polish_config.cli_destutter = self.config.cli_destutter
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

        old_mode = self.config.polish_mode
        self.config.polish_mode = mode
        if old_mode != mode:
            # build_initial_prompt()/to_qwen3_context() are mode-sensitive:
            # fast mode promotes 0.3 hints to ASR references at runtime.
            self._prompt_cache = None

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

        V4 key insight:
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

        Fast-mode runtime adaptation:
        - Raw 0.3 hint words are treated as effective 0.5 references for ASR
          context, because fast mode cannot rely on remote Polish to rescue
          those hints later.
        - Raw 0.1 cautious words stay excluded.
        - The saved hotword weights are never mutated.

        Returns:
            Formatted context string for Qwen3-ASR
        """
        weights = self._load_weights()
        parts = []

        # Collect hotwords by tier (critical first for attention priority).
        critical = []
        reference = []
        for word in self.config.prompt_words:
            effective_weight = self._effective_asr_weight(weights.get(word, 0.3))
            if effective_weight >= 1.0:
                critical.append(word)
            elif effective_weight >= 0.5:
                reference.append(word)

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
            f"mode={self.config.polish_mode}, "
            f"chars={len(context)}, est_tokens={est_tokens}"
        )

        return context

    def _get_example_sentences(self) -> List[str]:
        """
        Load example sentences from config file.

        WARNING: Example sentences cause HALLUCINATION in Qwen3-ASR!
        When audio is short/ambiguous, the model outputs example sentences
        verbatim instead of actual speech. This feature is DISABLED.

        Symptom observed in early debug logs:
        - Short (< 2s) audio caused the model to regurgitate the entire
          example_sentences block verbatim as transcription
        - Root cause: Context biasing was too aggressive with complete sentences

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
