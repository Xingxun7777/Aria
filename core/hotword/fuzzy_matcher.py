"""
Fuzzy Pinyin Matcher (Layer 2.5)
================================
Phonetically-aware correction for Chinese ASR output.

Uses pinyin similarity to catch near-misses that exact regex can't handle.
Example: "接待" (jiē dài) -> "迭代" (dié dài) when "迭代" is a hotword.
"""

import re
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass, field

try:
    from pypinyin import pinyin, Style
    PYPINYIN_AVAILABLE = True
except ImportError:
    PYPINYIN_AVAILABLE = False

from ..logging import get_system_logger

logger = get_system_logger()


@dataclass
class FuzzyMatchConfig:
    """Configuration for fuzzy pinyin matching."""
    enabled: bool = True
    # Minimum similarity threshold (0.0-1.0) for a match
    threshold: float = 0.7
    # Only match words with at least this many characters
    min_word_length: int = 2
    # Maximum edit distance for pinyin comparison
    max_pinyin_distance: int = 2


class PinyinFuzzyMatcher:
    """
    Pinyin-based fuzzy matcher for Chinese text correction.

    Layer 2.5 of the hotword system:
    - Runs after regex (Layer 2) but before polish (Layer 3)
    - Catches phonetically similar errors that exact regex misses
    - Zero external API calls, fast local processing
    """

    def __init__(self, hotwords: List[str], config: Optional[FuzzyMatchConfig] = None):
        """
        Initialize fuzzy matcher with hotword list.

        Args:
            hotwords: List of correct hotwords to match against
            config: Fuzzy matching configuration
        """
        self.config = config or FuzzyMatchConfig()
        self.hotwords = hotwords
        self._hotword_pinyin: Dict[str, List[str]] = {}
        self._chinese_hotwords: List[str] = []

        if not PYPINYIN_AVAILABLE:
            logger.warning("pypinyin not installed, fuzzy matching disabled. Run: pip install pypinyin")
            self.config.enabled = False
            return

        self._build_pinyin_index()

    def _build_pinyin_index(self) -> None:
        """Build pinyin index for all hotwords."""
        for word in self.hotwords:
            # Only index Chinese words (or mixed with Chinese)
            if self._contains_chinese(word):
                py = self._get_pinyin(word)
                if py:
                    self._hotword_pinyin[word] = py
                    self._chinese_hotwords.append(word)

        logger.debug(f"Built pinyin index for {len(self._chinese_hotwords)} Chinese hotwords")

    def _contains_chinese(self, text: str) -> bool:
        """Check if text contains Chinese characters."""
        return bool(re.search(r'[\u4e00-\u9fff]', text))

    def _get_pinyin(self, text: str) -> List[str]:
        """Get pinyin representation of Chinese text."""
        if not PYPINYIN_AVAILABLE:
            return []

        # Get pinyin without tones for more flexible matching
        py_list = pinyin(text, style=Style.NORMAL, errors='ignore')
        return [p[0] for p in py_list if p[0]]

    def _pinyin_similarity(self, py1: List[str], py2: List[str]) -> float:
        """
        Calculate similarity between two pinyin sequences.

        Uses a combination of:
        1. Exact syllable matches
        2. Initial consonant matches (声母)
        3. Edit distance penalty
        """
        if not py1 or not py2:
            return 0.0

        if len(py1) != len(py2):
            # Different number of syllables - lower base score
            len_diff = abs(len(py1) - len(py2))
            if len_diff > 1:
                return 0.0
            base_penalty = 0.3
        else:
            base_penalty = 0.0

        # Compare syllables
        matches = 0
        partial_matches = 0

        min_len = min(len(py1), len(py2))
        for i in range(min_len):
            s1, s2 = py1[i], py2[i]
            if s1 == s2:
                matches += 1
            elif s1 and s2 and s1[0] == s2[0]:
                # Same initial consonant (声母相同)
                partial_matches += 0.5
            elif self._levenshtein_distance(s1, s2) <= 1:
                # Very similar syllables
                partial_matches += 0.3

        max_len = max(len(py1), len(py2))
        score = (matches + partial_matches) / max_len - base_penalty
        return max(0.0, min(1.0, score))

    def _levenshtein_distance(self, s1: str, s2: str) -> int:
        """Calculate Levenshtein edit distance between two strings."""
        if len(s1) < len(s2):
            s1, s2 = s2, s1

        if len(s2) == 0:
            return len(s1)

        previous_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row

        return previous_row[-1]

    def _extract_chinese_words(self, text: str) -> List[Tuple[str, int, int]]:
        """
        Extract Chinese word segments from text.

        Returns list of (word, start_pos, end_pos) tuples.
        Simple segmentation based on consecutive Chinese characters.
        """
        words = []
        pattern = re.compile(r'[\u4e00-\u9fff]+')

        for match in pattern.finditer(text):
            word = match.group()
            if len(word) >= self.config.min_word_length:
                words.append((word, match.start(), match.end()))

        return words

    def find_best_match(self, word: str) -> Optional[Tuple[str, float]]:
        """
        Find the best matching hotword for a given word.

        Args:
            word: Input word to match

        Returns:
            (matched_hotword, similarity_score) or None if no match
        """
        if not self.config.enabled or not self._contains_chinese(word):
            return None

        word_py = self._get_pinyin(word)
        if not word_py:
            return None

        best_match = None
        best_score = 0.0

        for hotword, hotword_py in self._hotword_pinyin.items():
            # Skip if it's an exact match (handled by regex)
            if word == hotword:
                continue

            # Skip if length difference is too big
            if abs(len(word) - len(hotword)) > 1:
                continue

            score = self._pinyin_similarity(word_py, hotword_py)

            if score > best_score and score >= self.config.threshold:
                best_score = score
                best_match = hotword

        if best_match:
            return (best_match, best_score)
        return None

    def process(self, text: str) -> str:
        """
        Process text and apply fuzzy corrections.

        Args:
            text: Input text from ASR

        Returns:
            Corrected text
        """
        if not self.config.enabled:
            return text

        # Extract Chinese word segments
        words = self._extract_chinese_words(text)

        if not words:
            return text

        # Find replacements (process in reverse to maintain positions)
        replacements = []
        for word, start, end in words:
            match = self.find_best_match(word)
            if match:
                hotword, score = match
                replacements.append((start, end, word, hotword, score))

        # Apply replacements in reverse order
        result = text
        for start, end, original, replacement, score in reversed(replacements):
            result = result[:start] + replacement + result[end:]
            logger.debug(f"Fuzzy match: '{original}' -> '{replacement}' (score: {score:.2f})")

        return result

    def process_with_info(self, text: str) -> Tuple[str, List[Dict]]:
        """
        Process text and return correction info.

        Returns:
            (corrected_text, list_of_corrections)
        """
        if not self.config.enabled:
            return text, []

        words = self._extract_chinese_words(text)
        if not words:
            return text, []

        corrections = []
        replacements = []

        for word, start, end in words:
            match = self.find_best_match(word)
            if match:
                hotword, score = match
                replacements.append((start, end, word, hotword, score))
                corrections.append({
                    "original": word,
                    "corrected": hotword,
                    "score": round(score, 2),
                    "type": "pinyin_fuzzy"
                })

        result = text
        for start, end, original, replacement, score in reversed(replacements):
            result = result[:start] + replacement + result[end:]

        return result, corrections

    def update_hotwords(self, hotwords: List[str]) -> None:
        """Update hotword list and rebuild index."""
        self.hotwords = hotwords
        self._hotword_pinyin.clear()
        self._chinese_hotwords.clear()

        if PYPINYIN_AVAILABLE and self.config.enabled:
            self._build_pinyin_index()
