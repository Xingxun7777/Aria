"""Strict, content-only parsing for explicit voice edit commands."""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class VoiceEditCommand:
    """Replace one exact source string with replacement text."""

    source: str
    replacement: str
    syntax: str


@dataclass(frozen=True)
class VoiceEditParseResult:
    """Distinguish normal dictation from a rejected explicit command."""

    recognized: bool
    command: Optional[VoiceEditCommand] = None
    reason_code: str = "not_voice_edit"


@dataclass(frozen=True)
class VoiceEditChoiceCommand:
    action: str  # select | cancel
    occurrence: int = 0


@dataclass(frozen=True)
class VoiceEditChoiceParseResult:
    recognized: bool
    command: Optional[VoiceEditChoiceCommand] = None
    reason_code: str = "not_voice_edit_choice"


@dataclass(frozen=True)
class VoiceEditUndoCommand:
    action: str = "undo"


@dataclass(frozen=True)
class VoiceEditUndoParseResult:
    recognized: bool
    command: Optional[VoiceEditUndoCommand] = None
    reason_code: str = "not_voice_edit_undo"


_LEADING_WAKEWORD = "小助手"
_MAX_COMMAND_CHARS = 2400
_MAX_SOURCE_CHARS = 256
_MAX_REPLACEMENT_CHARS = 2000
_TRAILING_ASR_PUNCTUATION = "。.!！?？"
_PAIRED_QUOTES = {
    '"': '"',
    "'": "'",
    "“": "”",
    "‘": "’",
    "「": "」",
    "『": "』",
}
_CHOICE_RE = re.compile(r"^(?:替换|选择)第([0-9]{1,2}|[一二两三四五六七八九十]{1,3})(?:处|个)$")
_CHINESE_DIGITS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CHINESE_ORDINALS = {
    **_CHINESE_DIGITS,
    "十": 10,
    **{
        f"十{digit}": 10 + value
        for digit, value in _CHINESE_DIGITS.items()
        if digit != "两"
    },
    "二十": 20,
}


def _strip_optional_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and _PAIRED_QUOTES.get(text[0]) == text[-1]:
        return text[1:-1].strip()
    return text


def _strip_wakeword(text: str) -> tuple[str, bool]:
    value = text.strip()
    if not value.startswith(_LEADING_WAKEWORD):
        return value, False
    value = value[len(_LEADING_WAKEWORD) :].lstrip()
    return value.lstrip("，,：:、 "), True


def _candidate_splits(body: str, delimiters: tuple[str, ...]):
    candidates = []
    for delimiter in delimiters:
        start = 0
        while True:
            index = body.find(delimiter, start)
            if index < 0:
                break
            candidates.append((index, delimiter))
            start = index + 1
    return candidates


def _recognized_failure(reason_code: str) -> VoiceEditParseResult:
    return VoiceEditParseResult(recognized=True, reason_code=reason_code)


def _parse_ordinal(value: str) -> int:
    if value.isdigit():
        return int(value)
    return _CHINESE_ORDINALS.get(value, 0)


def parse_voice_edit_choice(text: str) -> VoiceEditChoiceParseResult:
    """Parse the second phase of a numbered ambiguous replacement."""

    value, has_wakeword = _strip_wakeword(str(text or ""))
    if not has_wakeword or not value:
        return VoiceEditChoiceParseResult(recognized=False)
    if value[-1:] in _TRAILING_ASR_PUNCTUATION:
        value = value[:-1].rstrip()
    if value == "取消替换":
        return VoiceEditChoiceParseResult(
            True, VoiceEditChoiceCommand(action="cancel"), "voice_edit_choice_ready"
        )
    if not value.startswith(("替换第", "选择第")):
        return VoiceEditChoiceParseResult(recognized=False)
    match = _CHOICE_RE.fullmatch(value)
    if match is None:
        return VoiceEditChoiceParseResult(
            True, reason_code="voice_edit_choice_invalid"
        )
    occurrence = _parse_ordinal(match.group(1))
    if occurrence < 1 or occurrence > 20:
        return VoiceEditChoiceParseResult(
            True, reason_code="voice_edit_choice_out_of_range"
        )
    return VoiceEditChoiceParseResult(
        True,
        VoiceEditChoiceCommand(action="select", occurrence=occurrence),
        "voice_edit_choice_ready",
    )


def parse_voice_edit_undo(text: str) -> VoiceEditUndoParseResult:
    """Parse an exact request to compensate the last confirmed Aria edit."""

    value, has_wakeword = _strip_wakeword(str(text or ""))
    if not has_wakeword or not value:
        return VoiceEditUndoParseResult(recognized=False)
    if value[-1:] in _TRAILING_ASR_PUNCTUATION:
        value = value[:-1].rstrip()
    phrases = (
        "撤销上一次编辑",
        "撤回上一次编辑",
        "撤销上次编辑",
        "撤回上次编辑",
        "撤销刚才的编辑",
        "撤回刚才的编辑",
    )
    if value in phrases:
        return VoiceEditUndoParseResult(
            True, VoiceEditUndoCommand(), "voice_edit_undo_ready"
        )
    if value.startswith(phrases):
        return VoiceEditUndoParseResult(
            True, reason_code="voice_edit_undo_invalid"
        )
    return VoiceEditUndoParseResult(recognized=False)


def parse_voice_edit(text: str) -> VoiceEditParseResult:
    """Parse one explicit Chinese voice-edit utterance without fuzzy matching.

    Supported forms after the required wakeword are ``把 A 改成 B``,
    ``将 A 改为 B`` and ``编辑 A 为 B``. Once an
    utterance starts with one of these explicit forms it is consumed even when
    malformed, so a broken edit command is never dictated into the target.
    """

    raw = str(text or "")
    value, has_wakeword = _strip_wakeword(raw)
    if not has_wakeword:
        return VoiceEditParseResult(recognized=False)
    if not value:
        return VoiceEditParseResult(recognized=False)
    if len(value) > _MAX_COMMAND_CHARS:
        if value.startswith(("把", "将", "编辑", "纠正")):
            return _recognized_failure("voice_edit_command_too_long")
        return VoiceEditParseResult(recognized=False)

    # ASR normally appends one sentence-final punctuation mark. It is command
    # framing rather than replacement content unless the user explicitly put
    # the replacement in paired quotes.
    if value[-1] in _TRAILING_ASR_PUNCTUATION:
        value = value[:-1].rstrip()

    syntax = ""
    body = ""
    delimiters: tuple[str, ...] = ()
    if value.startswith(("把", "将")):
        syntax = "replace"
        body = value[1:].strip()
        delimiters = ("改成", "改为")
    elif value.startswith("编辑"):
        syntax = "edit"
        body = value[2:].strip()
        delimiters = ("改成", "改为", "为")
    elif value.startswith("纠正"):
        return _recognized_failure("voice_correction_not_enabled")
    else:
        return VoiceEditParseResult(recognized=False)

    if "\x00" in value:
        return _recognized_failure("voice_edit_invalid_character")

    candidates = _candidate_splits(body, delimiters)
    if not candidates:
        return _recognized_failure("voice_edit_syntax_invalid")

    # Different delimiter strings can start at the same index ("改为" and the
    # final "为"). Prefer the longest delimiter there, but reject genuinely
    # different split points rather than guessing what text the user meant.
    filtered_candidates = []
    for index, delimiter in candidates:
        contained_in_longer = any(
            len(other) > len(delimiter)
            and other_index <= index
            and index + len(delimiter) <= other_index + len(other)
            for other_index, other in candidates
        )
        if not contained_in_longer:
            filtered_candidates.append((index, delimiter))

    by_index = {}
    for index, delimiter in filtered_candidates:
        previous = by_index.get(index, "")
        if len(delimiter) > len(previous):
            by_index[index] = delimiter
    if len(by_index) != 1:
        return _recognized_failure("voice_edit_syntax_ambiguous")

    index, delimiter = next(iter(by_index.items()))
    source = _strip_optional_quotes(body[:index])
    replacement = _strip_optional_quotes(body[index + len(delimiter) :])
    if not source or not replacement:
        return _recognized_failure("voice_edit_empty_operand")
    if "\x00" in source or "\x00" in replacement:
        return _recognized_failure("voice_edit_invalid_character")
    if len(source) > _MAX_SOURCE_CHARS:
        return _recognized_failure("voice_edit_source_too_long")
    if len(replacement) > _MAX_REPLACEMENT_CHARS:
        return _recognized_failure("voice_edit_replacement_too_long")
    if source == replacement:
        return _recognized_failure("voice_edit_no_change")

    return VoiceEditParseResult(
        recognized=True,
        command=VoiceEditCommand(
            source=source,
            replacement=replacement,
            syntax=syntax,
        ),
        reason_code="voice_edit_ready",
    )
