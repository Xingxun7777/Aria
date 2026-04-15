"""
Shared utilities for hotword system.
====================================
Single source of truth for CJK detection and language classification.
"""

import re

# Shared CJK detection pattern - single source of truth
# Covers: Chinese (CJK Unified), Japanese (Hiragana, Katakana), Korean (Hangul)
CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]")


def is_cjk_word(word: str) -> bool:
    """
    Check if a word contains any CJK (Chinese/Japanese/Korean) characters.

    Returns:
        True if the word contains CJK characters, False if pure ASCII/Latin
    """
    return bool(CJK_PATTERN.search(word))


def is_english_word(word: str) -> bool:
    """
    Check if a word is primarily English (no CJK characters).

    English hotwords need special handling because:
    - FunASR's hotword system doesn't work well for English
    - They rely on the polish layer for correction

    Returns:
        True if the word contains no CJK characters
    """
    return not CJK_PATTERN.search(word)
