"""
HotWord Processor
=================
Post-processing layer for regex-based text correction.
"""

import re
from typing import Dict, List, Tuple, Optional

from ..logging import get_system_logger

logger = get_system_logger()


class HotWordProcessor:
    """
    Regex-based text post-processor for hotword correction.

    Layer 2 of the hotword system:
    - Zero latency (regex is fast)
    - Deterministic corrections
    - Handles common ASR mistakes

    Processing order:
    1. Case-insensitive exact matches
    2. Fuzzy pattern matches
    3. Cleanup (extra spaces, punctuation)
    """

    def __init__(self, replacements: Optional[Dict[str, str]] = None):
        self.replacements = replacements or {}
        self._compiled_patterns: List[Tuple[re.Pattern, str]] = []
        self._build_patterns()

    def _build_patterns(self) -> None:
        """Compile regex patterns from replacements."""
        self._compiled_patterns = []

        for wrong, correct in self.replacements.items():
            # Case-insensitive exact word match
            # Note: In Python 3, Chinese characters are word chars (\w),
            # so \b doesn't work between Chinese and English.
            # Use lookaround for ASCII letters instead.
            if wrong.isascii():
                # English words: use ASCII-only lookaround boundaries
                # This works correctly with mixed Chinese-English text
                # e.g., "说cloud" will match "cloud"
                pattern = re.compile(
                    rf'(?<![a-zA-Z]){re.escape(wrong)}(?![a-zA-Z])',
                    re.IGNORECASE
                )
            else:
                # Chinese/mixed: direct match (no word boundaries in Chinese)
                pattern = re.compile(re.escape(wrong))

            self._compiled_patterns.append((pattern, correct))

        logger.debug(f"Compiled {len(self._compiled_patterns)} replacement patterns")

    def update_replacements(self, replacements: Dict[str, str]) -> None:
        """Update replacement rules and rebuild patterns."""
        self.replacements.update(replacements)
        self._build_patterns()

    def process(self, text: str) -> str:
        """
        Apply all corrections to text.

        Args:
            text: Raw ASR output

        Returns:
            Corrected text
        """
        if not text:
            return text

        original = text

        # Apply all replacements
        for pattern, replacement in self._compiled_patterns:
            text = pattern.sub(replacement, text)

        # Cleanup: normalize whitespace
        text = re.sub(r'\s+', ' ', text).strip()

        # Log if changes were made
        if text != original:
            logger.debug(f"Corrected: '{original}' -> '{text}'")

        return text

    def process_with_info(self, text: str) -> Tuple[str, List[str]]:
        """
        Apply corrections and return info about changes.

        Returns:
            (corrected_text, list_of_changes)
        """
        if not text:
            return text, []

        changes = []

        for pattern, replacement in self._compiled_patterns:
            matches = pattern.findall(text)
            if matches:
                for match in matches:
                    changes.append(f"'{match}' -> '{replacement}'")
                text = pattern.sub(replacement, text)

        # Cleanup
        text = re.sub(r'\s+', ' ', text).strip()

        return text, changes

    def add_replacement(self, wrong: str, correct: str) -> None:
        """Add a single replacement rule."""
        self.replacements[wrong] = correct
        self._build_patterns()

    def remove_replacement(self, wrong: str) -> None:
        """Remove a replacement rule."""
        if wrong in self.replacements:
            del self.replacements[wrong]
            self._build_patterns()
