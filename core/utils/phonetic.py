"""
Phonetic Matching Utilities
===========================
Provides pinyin-based fuzzy matching for Chinese text.
Used for wakeword recognition with ASR variant tolerance.
"""

import threading
from functools import lru_cache
from typing import List, Optional, Tuple

from pypinyin import lazy_pinyin, Style


class PinyinMatcher:
    """
    Pinyin-based Chinese text matcher.

    Converts Chinese characters to pinyin and compares phonetically,
    allowing tolerance for ASR transcription variants like:
    - 瑶瑶 -> yao yao
    - 摇摇 -> yao yao
    - 妖妖 -> yao yao

    All above would match the target "瑶瑶".
    """

    def __init__(self, similarity_threshold: float = 0.85):
        """
        Initialize matcher.

        Args:
            similarity_threshold: Minimum similarity score (0-1) for a match.
                                  Default 0.85 allows minor variations.
        """
        self.similarity_threshold = similarity_threshold

    @lru_cache(maxsize=1024)
    def to_pinyin(self, text: str, with_tone: bool = False) -> Tuple[str, ...]:
        """
        Convert Chinese text to pinyin tuple.

        Args:
            text: Chinese text to convert
            with_tone: If True, include tone marks (e.g., yáo). Default False.

        Returns:
            Tuple of pinyin strings (immutable for caching)

        Example:
            >>> matcher.to_pinyin("瑶瑶")
            ('yao', 'yao')
        """
        if not text:
            return ()

        style = Style.TONE if with_tone else Style.NORMAL
        return tuple(lazy_pinyin(text, style=style))

    def pinyin_equal(self, text1: str, text2: str) -> bool:
        """
        Check if two texts have identical pinyin (tone-insensitive).

        Args:
            text1: First text
            text2: Second text

        Returns:
            True if pinyin sequences are identical

        Example:
            >>> matcher.pinyin_equal("瑶瑶", "摇摇")
            True
            >>> matcher.pinyin_equal("瑶瑶", "妖妖")
            True
            >>> matcher.pinyin_equal("瑶瑶", "好好")
            False
        """
        return self.to_pinyin(text1) == self.to_pinyin(text2)

    def pinyin_similarity(self, text1: str, text2: str) -> float:
        """
        Calculate pinyin similarity between two texts.

        Uses simple character-level comparison on joined pinyin.

        Args:
            text1: First text
            text2: Second text

        Returns:
            Similarity score from 0.0 to 1.0
        """
        py1 = "".join(self.to_pinyin(text1))
        py2 = "".join(self.to_pinyin(text2))

        if not py1 or not py2:
            return 0.0

        if py1 == py2:
            return 1.0

        # Simple Levenshtein-based similarity
        distance = self._levenshtein_distance(py1, py2)
        max_len = max(len(py1), len(py2))
        return 1.0 - (distance / max_len)

    def matches(self, text: str, target: str) -> bool:
        """
        Check if text matches target phonetically.

        First tries exact pinyin match, then falls back to similarity.

        Args:
            text: Text to check (e.g., ASR output)
            target: Target to match against (e.g., configured wakeword)

        Returns:
            True if text matches target phonetically
        """
        # Fast path: exact pinyin match
        if self.pinyin_equal(text, target):
            return True

        # Slow path: similarity check
        return self.pinyin_similarity(text, target) >= self.similarity_threshold

    def find_match_at_start(
        self, text: str, target: str, max_extra_chars: int = 2
    ) -> Optional[Tuple[str, str]]:
        """
        Find if text starts with a phonetic match of target.

        Useful for detecting wakeword at the beginning of a sentence.

        Args:
            text: Full text to search in
            target: Target pattern to find
            max_extra_chars: Allow this many extra characters for matching
                            (handles cases like "瑶瑶，" vs "瑶瑶")

        Returns:
            Tuple of (matched_text, remaining_text) if found, None otherwise

        Example:
            >>> matcher.find_match_at_start("摇摇，开启自动发送", "瑶瑶")
            ("摇摇", "开启自动发送")
        """
        if not text or not target:
            return None

        target_len = len(target)
        target_pinyin = self.to_pinyin(target)

        # Try different lengths around target length
        for offset in range(max_extra_chars + 1):
            for length in [target_len + offset, target_len - offset]:
                if length <= 0 or length > len(text):
                    continue

                candidate = text[:length]
                candidate_pinyin = self.to_pinyin(candidate)

                # Check pinyin match (ignoring length difference)
                if candidate_pinyin == target_pinyin:
                    # Strip punctuation and whitespace (note: \s is literal in lstrip, use actual whitespace)
                    remaining = text[length:].lstrip("，,。.：: \t\n\r")
                    return (candidate, remaining)

        return None

    def extract_wakeword(
        self, text: str, wakeword: str
    ) -> Optional[Tuple[str, str, str]]:
        """
        Extract wakeword and command from text.

        Args:
            text: Full transcribed text
            wakeword: Target wakeword (e.g., "瑶瑶")

        Returns:
            Tuple of (detected_wakeword, command, original_text) if found,
            None otherwise

        Example:
            >>> matcher.extract_wakeword("遥遥开启自动发送", "瑶瑶")
            ("遥遥", "开启自动发送", "遥遥开启自动发送")
        """
        if not text or not wakeword:
            return None

        text = text.strip()
        result = self.find_match_at_start(text, wakeword)

        if result:
            detected, remaining = result
            return (detected, remaining, text)

        return None

    @staticmethod
    def _levenshtein_distance(s1: str, s2: str) -> int:
        """Calculate Levenshtein distance between two strings."""
        if len(s1) < len(s2):
            s1, s2 = s2, s1

        if len(s2) == 0:
            return len(s1)

        prev_row = list(range(len(s2) + 1))

        for i, c1 in enumerate(s1):
            curr_row = [i + 1]
            for j, c2 in enumerate(s2):
                # Cost is 0 if characters match, 1 otherwise
                insertions = prev_row[j + 1] + 1
                deletions = curr_row[j] + 1
                substitutions = prev_row[j] + (c1 != c2)
                curr_row.append(min(insertions, deletions, substitutions))
            prev_row = curr_row

        return prev_row[-1]

    def clear_cache(self) -> None:
        """Clear the pinyin conversion cache."""
        self.to_pinyin.cache_clear()


# Module-level singleton for convenience (thread-safe)
_default_matcher: Optional[PinyinMatcher] = None
_matcher_lock = threading.Lock()


def get_matcher() -> PinyinMatcher:
    """Get the default PinyinMatcher instance (thread-safe)."""
    global _default_matcher
    if _default_matcher is None:
        with _matcher_lock:
            # Double-checked locking pattern
            if _default_matcher is None:
                _default_matcher = PinyinMatcher()
    return _default_matcher
