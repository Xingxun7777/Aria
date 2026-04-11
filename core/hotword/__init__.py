"""
HotWord System
==============
Four-layer hotword correction for Aria.

Layer 1: ASR initial_prompt (zero latency) - bias ASR output
Layer 2: Regex replacement (zero latency) - exact pattern fixes
Layer 2.5: Pinyin fuzzy match (zero latency) - phonetic near-miss correction
Layer 3: LLM polish (optional, ~100ms) - AI refinement
"""

from .utils import CJK_PATTERN, is_cjk_word, is_english_word
from .manager import HotWordManager, HotWordConfig
from .processor import HotWordProcessor
from .fuzzy_matcher import PinyinFuzzyMatcher, FuzzyMatchConfig
from .polish import AIPolisher, PolishConfig, DEFAULT_POLISH_PROMPT
from .local_polish import LocalPolishEngine, LocalPolishConfig

__all__ = [
    "HotWordManager",
    "HotWordConfig",
    "HotWordProcessor",
    "PinyinFuzzyMatcher",
    "FuzzyMatchConfig",
    "AIPolisher",
    "PolishConfig",
    "DEFAULT_POLISH_PROMPT",
    "LocalPolishEngine",
    "LocalPolishConfig",
    # Shared utilities
    "CJK_PATTERN",
    "is_cjk_word",
    "is_english_word",
]
