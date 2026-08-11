"""
HotWord Processor
=================
Post-processing layer for regex-based text correction.
"""

import re
import unicodedata
from typing import Dict, Iterable, List, Tuple, Optional

from ..logging import get_system_logger
from ..learning.explicit_corrections import (
    ExplicitCorrectionProcessor,
    ExplicitCorrectionRule,
)

logger = get_system_logger()

_AMBIGUOUS_USER_REPLACEMENTS = {
    # Observed regression: in Blender/3D text "scale" is usually the correct
    # term, so a global scale->skill replacement is unsafe.
    "scale",
    # "骑士词/歧使词" can be ASR noise for "提示词", but only in prompt/LLM
    # contexts.  Handle it with the contextual built-in below, not globally.
    # Do NOT include "歧视词" here: it is a valid phrase when discussing
    # moderation/sensitive-word prompts, so deterministic replacement would be
    # too aggressive.
    "骑士词",
    "歧使词",
}

_PROMPT_TERM_CONTEXT_RE = re.compile(
    r"提示词|prompt|Prompt|大模型|模型|润色|词表|热词|ASR|OCR|LLM|生成|生图|图像|AI"
)
_PROMPT_TERM_MISHEAR_RE = re.compile(r"骑士词|歧使词")

_ACRONYM_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?<![A-Za-z])O\s+Z\s+R(?![A-Za-z])", re.IGNORECASE), "OZR"),
    (re.compile(r"(?<![A-Za-z])O\s+C\s+R(?![A-Za-z])", re.IGNORECASE), "OCR"),
    (re.compile(r"(?<![A-Za-z])O\s+C\s+二\s*层", re.IGNORECASE), "OCR层"),
    (re.compile(r"(?<![A-Za-z])S\s+D\s+F(?![A-Za-z])", re.IGNORECASE), "SDF"),
    (re.compile(r"(?<![A-Za-z])P\s+B\s+R(?![A-Za-z])", re.IGNORECASE), "PBR"),
    (re.compile(r"(?<![A-Za-z])N\s+P\s+R(?![A-Za-z])", re.IGNORECASE), "NPR"),
    (re.compile(r"(?<![A-Za-z])U\s+V(?![A-Za-z])", re.IGNORECASE), "UV"),
)

# Generic split-letter merge: runs of >=2 single ASCII uppercase letters
# separated by spaces (deterministic ASR artefact, e.g. "K K S" -> "KKS").
# Boundaries: uppercase only (lowercase "a b" untouched), never adjacent to
# other ASCII letters/digits (so "plan B" or "用 C 写" stay intact), and the
# pattern cannot cross punctuation because only spaces join the letters.
_SPLIT_LETTER_RUN_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Z](?: +[A-Z])+(?![A-Za-z0-9])"
)

_CLAUDE_HOTWORD_RE = re.compile(r"(?i)^(claude|claude\s+code)$")
_CLOUD_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])cloud(?![A-Za-z0-9])", re.IGNORECASE)
_CLOUD_SERVICE_AFTER_RE = re.compile(
    r"^\s*(?:"
    r"service|services|status|storage|sync|server|servers|provider|providers|"
    r"computing|backup|drive|function|functions|database|platform|infrastructure|"
    r"服务|云服务|状态|存储|同步|服务器|平台|计算|备份|数据库|网盘|云端"
    r")",
    re.IGNORECASE,
)
_CLOUD_VENDOR_BEFORE_RE = re.compile(
    r"(?:google|aws|azure|alibaba|aliyun|tencent|oracle|huawei)\s+$",
    re.IGNORECASE,
)
_CLOUD_ENGLISH_BEFORE_RE = re.compile(r"[A-Za-z]+\s+$")
_CLOUD_ENGLISH_AFTER_RE = re.compile(r"^\s*([A-Za-z]+)(?![A-Za-z])")
_CLAUDE_CODE_CASING_RE = re.compile(
    r"(?<![A-Za-z])Claude\s+code(?![A-Za-z])",
    re.IGNORECASE,
)


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

    def __init__(
        self,
        replacements: Optional[Dict[str, str]] = None,
        hotwords: Optional[Iterable[str]] = None,
        prompt_words: Optional[Iterable[str]] = None,
        explicit_corrections: Optional[Iterable[ExplicitCorrectionRule]] = None,
    ):
        self.replacements = replacements or {}
        self.hotwords = list(hotwords if hotwords is not None else prompt_words or [])
        self._compiled_patterns: List[Tuple[re.Pattern, str]] = []
        self._explicit_corrections = ExplicitCorrectionProcessor(
            explicit_corrections or ()
        )
        self._build_patterns()

    def _build_patterns(self) -> None:
        """Compile regex patterns from replacements."""
        self._compiled_patterns = []

        explicit_source_keys = self._explicit_corrections.source_keys()
        for wrong, correct in self.replacements.items():
            if wrong in _AMBIGUOUS_USER_REPLACEMENTS:
                continue
            # An explicit local correction is the user's latest decision for
            # this exact source.  Do not let the protected static config rewrite
            # it first and make the explicit rule unreachable.
            wrong_key = unicodedata.normalize("NFC", str(wrong)).casefold()
            if wrong_key in explicit_source_keys:
                continue
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

    def update_explicit_corrections(
        self, rules: Iterable[ExplicitCorrectionRule]
    ) -> None:
        """Atomically replace the local explicit-rule snapshot."""

        self._explicit_corrections.update_rules(rules)
        self._build_patterns()

    @property
    def explicit_correction_count(self) -> int:
        return len(self._explicit_corrections.rules)

    @property
    def total_rule_count(self) -> int:
        return len(self._compiled_patterns) + self.explicit_correction_count

    def update_prompt_words(self, prompt_words: Iterable[str]) -> None:
        """Update hotwords used by built-in contextual rules."""
        self.update_hotwords(prompt_words)

    def update_hotwords(self, hotwords: Iterable[str]) -> None:
        """Update active hotwords used by built-in contextual rules."""
        self.hotwords = list(hotwords or [])

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
        text, _ = _apply_builtin_safe_rules(
            text, hotwords=self.hotwords
        )

        # Apply all replacements
        for pattern, replacement in self._compiled_patterns:
            text = pattern.sub(replacement, text)

        # Explicit corrections run last and in one regex pass.  They therefore
        # override a same-source static rule and never chain A→B→C within one
        # utterance.  Do not put operands in debug metadata.
        text, _explicit_count = self._explicit_corrections.process(text)

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
        text, builtin_changes = _apply_builtin_safe_rules(
            text, hotwords=self.hotwords
        )
        changes.extend(builtin_changes)

        for pattern, replacement in self._compiled_patterns:
            matches = pattern.findall(text)
            if matches:
                for match in matches:
                    changes.append(f"'{match}' -> '{replacement}'")
                text = pattern.sub(replacement, text)

        text, explicit_count = self._explicit_corrections.process(text)
        changes.extend("explicit_correction_rule_applied" for _ in range(explicit_count))

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


def _has_claude_hotword(prompt_words: Iterable[str]) -> bool:
    return any(
        _CLAUDE_HOTWORD_RE.match(str(word or "").strip()) for word in prompt_words
    )


def _replace_cloud_with_claude(text: str) -> Tuple[str, List[str]]:
    changes: List[str] = []

    def repl(match: re.Match) -> str:
        before = text[max(0, match.start() - 32) : match.start()]
        after = text[match.end() : match.end() + 32]
        if _CLOUD_VENDOR_BEFORE_RE.search(before):
            return match.group(0)
        if _CLOUD_SERVICE_AFTER_RE.match(after):
            return match.group(0)
        after_english = _CLOUD_ENGLISH_AFTER_RE.match(after)
        if after_english and after_english.group(1).lower() != "code":
            return match.group(0)
        if _CLOUD_ENGLISH_BEFORE_RE.search(before) and not (
            after_english and after_english.group(1).lower() == "code"
        ):
            return match.group(0)

        changes.append(f"'{match.group(0)}' -> 'Claude'")
        return "Claude"

    replaced = _CLOUD_TOKEN_RE.sub(repl, text)
    if "Claude Code" in text or _CLAUDE_CODE_CASING_RE.search(replaced):
        replaced = _CLAUDE_CODE_CASING_RE.sub("Claude Code", replaced)
    return replaced, changes


def _apply_builtin_safe_rules(
    text: str, hotwords: Optional[Iterable[str]] = None
) -> Tuple[str, List[str]]:
    """Apply high-confidence local rules before user-configured replacements.

    These rules are deliberately tiny and context-bound:
    - split-letter acronyms are deterministic ASR artefacts;
    - "歧视词/骑士词" only maps to "提示词" when the utterance itself is about
      prompts/models/hotwords, avoiding a global semantic replacement.
    """

    if not text:
        return text, []

    changes: List[str] = []

    for pattern, replacement in _ACRONYM_RULES:
        matches = pattern.findall(text)
        if matches:
            for match in matches:
                changes.append(f"'{match}' -> '{replacement}'")
            text = pattern.sub(replacement, text)

    def _merge_split_letters(match: re.Match) -> str:
        merged = match.group(0).replace(" ", "")
        changes.append(f"'{match.group(0)}' -> '{merged}'")
        return merged

    # After the curated acronym rules so mixed patterns like "O C 二层"
    # keep their specific rewrite ("OCR层") instead of the plain merge.
    text = _SPLIT_LETTER_RUN_RE.sub(_merge_split_letters, text)

    if _PROMPT_TERM_CONTEXT_RE.search(text):
        matches = _PROMPT_TERM_MISHEAR_RE.findall(text)
        if matches:
            for match in matches:
                changes.append(f"'{match}' -> '提示词'")
            text = _PROMPT_TERM_MISHEAR_RE.sub("提示词", text)

    if _has_claude_hotword(hotwords or []):
        text, cloud_changes = _replace_cloud_with_claude(text)
        changes.extend(cloud_changes)

    return text, changes
