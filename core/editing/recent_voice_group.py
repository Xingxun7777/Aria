"""Recent dictated-passage grouping and natural semantic edit commands.

The grouping policy is deliberately permissive about recording-session and
caret changes.  Automatic mutation safety is enforced separately by the
native range bookmark carried by each successfully inserted segment.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from enum import Enum
from collections.abc import Iterable
from typing import Any, Optional


class RecentVoiceCommandMode(str, Enum):
    REWRITE = "rewrite"
    ADVICE = "advice"


@dataclass(frozen=True)
class RecentVoiceCommand:
    mode: RecentVoiceCommandMode
    instruction: str
    raw_text: str


@dataclass(frozen=True)
class RecentVoiceCommandParseResult:
    recognized: bool
    command: Optional[RecentVoiceCommand] = None
    reason_code: str = "not_recent_voice_command"


@dataclass(frozen=True)
class RecentVoiceSegment:
    sequence: int
    text: str
    target: Any
    session_id: int
    voice_start: float
    voice_end: float
    inserted_at: float


@dataclass(frozen=True)
class RecentVoiceGroup:
    segments: tuple[RecentVoiceSegment, ...]
    text: str
    target: Any
    addressable: bool

    @property
    def first_sequence(self) -> int:
        return self.segments[0].sequence

    @property
    def last_sequence(self) -> int:
        return self.segments[-1].sequence


@dataclass(frozen=True)
class RecentVoiceGroupResult:
    status: str
    group: Optional[RecentVoiceGroup] = None


_DEFAULT_WAKEWORDS = ("小助手",)
_TRAILING_PUNCTUATION = "。.!！?？"
_REFERENCE_TERMS = (
    "这句话",
    "这句",
    "这段话",
    "这一段",
    "这段",
    "刚才那句话",
    "刚才这句话",
    "刚才说的话",
    "刚才说的",
    "刚才的话",
    "刚才那段",
    "刚才这段",
    "刚才那个里面的",
    "我刚才说的",
    "刚刚那句话",
    "刚刚这句话",
    "刚刚说的话",
    "刚刚说的",
    "刚刚的话",
    "刚刚那段话",
    "刚刚这段话",
    "刚刚那段",
    "刚刚这段",
    "上一句话",
    "上句话",
    "上一段话",
    "上一段",
    "前面那句话",
    "前面这句话",
    "前面说的话",
    "前面说的",
    "前面讲的话",
    "前面讲的",
    "前面那段话",
    "前面那段",
    "前面这段话",
    "前面这段",
    "前面的内容",
    "上面那句话",
    "上面说的话",
    "上面说的",
    "上面那段话",
    "上面那段",
    "上面的内容",
    "之前说的话",
    "之前说的",
)
_REFERENCE_STARTERS = (
    "刚才",
    "方才",
    "刚刚",
    "上一句",
    "上句",
    "前一句",
)
_REFERENCE_SAY_PARTS = ("说的", "讲的", "输入的")
_REFERENCE_DEMONSTRATIVES = ("那个", "那句", "那段", "那些", "内容", "话")
_REFERENCE_BARE_DEMONSTRATIVES = (
    "那个",
    "那句",
    "那段",
    "那些",
    "这句",
    "这段",
    "这句话",
    "这段话",
)
_REFERENCE_INSIDE_PARTS = ("里面", "里边", "里头", "里")
_ADVICE_TERMS = (
    "哪里不好",
    "哪儿不好",
    "有什么问题",
    "什么问题",
    "给点建议",
    "给些建议",
    "给我建议",
    "怎么改",
    "如何修改",
    "分析一下",
    "评价一下",
    "什么意思",
    "解释一下",
    "说明一下",
)
_REWRITE_TERMS = (
    "帮我修改",
    "帮我改一下",
    "帮我改",
    "帮我润色",
    "帮我优化",
    "帮我精简",
    "帮我简化",
    "帮我改写",
    "帮我重写",
    "帮我调整",
    "请修改",
    "请润色",
    "请优化",
    "请精简",
    "请简化",
    "请改写",
    "请重写",
    "请调整",
    "修改一下",
    "改一下",
    "改改",
    "润色一下",
    "润色一点",
    "优化一下",
    "优化一点",
    "精简一下",
    "精简一点",
    "简化一下",
    "简化一点",
    "改写一下",
    "重写一下",
    "调整一下",
    "写得更",
    "变得更",
)
_CONTINUATION_PREFIXES = (
    "而且",
    "并且",
    "然后",
    "所以",
    "但是",
    "不过",
    "另外",
    "包括",
    "比如",
    "例如",
    "也就是",
    "就是说",
    "因为",
    "或者",
    "以及",
    "还有",
    "同时",
    "那么",
    "对吧",
)

_REFERENCE_REPLACEMENT_PREFIXES = (
    "",
    "把",
    "将",
    "帮我",
    "帮我把",
    "帮我将",
    "请",
    "请把",
    "请将",
    "请帮我",
    "请帮我把",
    "请帮我将",
    "麻烦",
    "麻烦把",
    "麻烦将",
    "麻烦帮我",
    "麻烦帮我把",
    "麻烦帮我将",
)

_REFERENCE_SCOPED_AI_PREFIXES = (
    "帮我",
    "帮我把",
    "帮我将",
    "请帮我",
    "请帮我把",
    "请帮我将",
    "麻烦帮我",
    "麻烦帮我把",
    "麻烦帮我将",
    "帮忙",
    "请帮忙",
    "麻烦帮忙",
)

_PAIRED_QUOTES = {
    '"': '"',
    "'": "'",
    "“": "”",
    "‘": "’",
    "「": "」",
    "『": "』",
}


def _strip_paired_quotes(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and _PAIRED_QUOTES.get(text[0]) == text[-1]:
        return text[1:-1].strip()
    return text


def _consume_optional_parts(text: str, cursor: int) -> int:
    """Advance past optional say/demonstrative/inside/的 parts."""

    say = next(
        (
            item
            for item in sorted(_REFERENCE_SAY_PARTS, key=len, reverse=True)
            if text.startswith(item, cursor)
        ),
        None,
    )
    if say is not None:
        cursor += len(say)
    demonstrative = next(
        (
            item
            for item in sorted(_REFERENCE_DEMONSTRATIVES, key=len, reverse=True)
            if text.startswith(item, cursor)
        ),
        None,
    )
    if demonstrative is not None:
        cursor += len(demonstrative)
    inside = next(
        (
            item
            for item in sorted(_REFERENCE_INSIDE_PARTS, key=len, reverse=True)
            if text.startswith(item, cursor)
        ),
        None,
    )
    if inside is not None:
        cursor += len(inside)
    if cursor < len(text) and text[cursor] == "的":
        cursor += 1
    return cursor


def _match_recent_reference_prefix(value: str) -> Optional[str]:
    """Return the longest pure-reference prefix of ``value``, or None."""

    text = str(value or "")
    if not text:
        return None
    starter = next(
        (
            item
            for item in sorted(_REFERENCE_STARTERS, key=len, reverse=True)
            if text.startswith(item)
        ),
        None,
    )
    if starter is not None:
        cursor = _consume_optional_parts(text, len(starter))
        # A bare starter with no optional body ("刚才") is too ambiguous to
        # claim as a passage reference operand on its own.
        if cursor == len(starter):
            return None
        return text[:cursor]

    # Container-only forms such as ``那段里的`` / ``那个里面``.
    bare = next(
        (
            item
            for item in sorted(_REFERENCE_BARE_DEMONSTRATIVES, key=len, reverse=True)
            if text.startswith(item)
        ),
        None,
    )
    if bare is None:
        return None
    cursor = len(bare)
    inside = next(
        (
            item
            for item in sorted(_REFERENCE_INSIDE_PARTS, key=len, reverse=True)
            if text.startswith(item, cursor)
        ),
        None,
    )
    if inside is None:
        return None
    cursor += len(inside)
    if cursor < len(text) and text[cursor] == "的":
        cursor += 1
    return text[:cursor]


def _looks_like_recent_reference(operand: str) -> bool:
    """True when the whole operand is a pure recent-passage anaphor.

    Combinatorial forms such as ``刚才那个`` / ``刚刚说的那个里面`` must fully
    cover the operand.  Contentful phrases like ``刚才时间`` stay literal.
    """

    text = str(operand or "").strip().strip("，,：:、 ")
    if not text:
        return False
    if text in _REFERENCE_TERMS:
        return True
    matched = _match_recent_reference_prefix(text)
    return matched is not None and matched == text


def _find_recent_reference_span(value: str) -> Optional[tuple[int, int, str]]:
    """Locate the longest literal or combinatorial reference span in ``value``."""

    text = str(value or "")
    if not text:
        return None
    best: Optional[tuple[int, int, str]] = None

    def _consider(start: int, matched: str) -> None:
        nonlocal best
        if not matched:
            return
        end = start + len(matched)
        candidate = (start, end, matched)
        if best is None or len(matched) > len(best[2]) or (
            len(matched) == len(best[2]) and start < best[0]
        ):
            best = candidate

    for reference in sorted(_REFERENCE_TERMS, key=len, reverse=True):
        start = 0
        while True:
            position = text.find(reference, start)
            if position < 0:
                break
            _consider(position, reference)
            start = position + 1

    for anchor in (*_REFERENCE_STARTERS, *_REFERENCE_BARE_DEMONSTRATIVES):
        start = 0
        while True:
            position = text.find(anchor, start)
            if position < 0:
                break
            matched = _match_recent_reference_prefix(text[position:])
            if matched:
                _consider(position, matched)
            start = position + 1

    return best


def _strip_quoted_replacement_suffix(value: str) -> str:
    """Strip a harmless spoken tail only when a quoted target proves its end."""

    text = str(value or "").strip()
    if not text or text[0] not in _PAIRED_QUOTES:
        return _strip_paired_quotes(text)
    closing = _PAIRED_QUOTES[text[0]]
    end = text.find(closing, 1)
    if end < 0:
        return text
    tail = text[end + 1 :].strip("，,：:、 ")
    if tail not in ("", "的", "吧", "一下", "一下吧"):
        return text
    return text[1:end].strip()


def _iter_reference_hits(value: str):
    """Yield ``(position, reference)`` for literal and combinatorial matches."""

    text = str(value or "")
    seen: set[tuple[int, str]] = set()
    span = _find_recent_reference_span(text)
    ordered: list[tuple[int, str]] = []
    if span is not None:
        ordered.append((span[0], span[2]))
    for reference in sorted(_REFERENCE_TERMS, key=len, reverse=True):
        start = 0
        while True:
            position = text.find(reference, start)
            if position < 0:
                break
            ordered.append((position, reference))
            start = position + 1
    for anchor in (*_REFERENCE_STARTERS, *_REFERENCE_BARE_DEMONSTRATIVES):
        start = 0
        while True:
            position = text.find(anchor, start)
            if position < 0:
                break
            matched = _match_recent_reference_prefix(text[position:])
            if matched:
                ordered.append((position, matched))
            start = position + 1
    for position, reference in sorted(
        ordered, key=lambda item: (-len(item[1]), item[0])
    ):
        key = (position, reference)
        if key in seen:
            continue
        seen.add(key)
        yield position, reference


def _parse_referenced_source_target_request(body: str) -> Optional[tuple[str, str]]:
    """Recognize ``把刚才这句话“A”改成“B”的`` as an AI rewrite request.

    The operands are parsed only to prove this is request-shaped.  They are not
    used for a literal precondition: DeepSeek receives the full instruction and
    may resolve semantic wording differences in the recent passage.
    """

    value = str(body or "").strip()
    for position, reference in _iter_reference_hits(value):
        prefix = value[:position].strip("，,：:、 ")
        if prefix not in _REFERENCE_REPLACEMENT_PREFIXES:
            continue
        suffix = value[position + len(reference) :].lstrip("，,：:、 ")
        for locator in ("里面的", "中的", "里面", "中", "里"):
            if suffix.startswith(locator):
                suffix = suffix[len(locator) :].lstrip("，,：:、 ")
                break
        for optional_prefix in ("给我", "帮我"):
            if suffix.startswith(optional_prefix):
                suffix = suffix[len(optional_prefix) :].lstrip("，,：:、 ")
                break
        delimiters = ("修改成", "修改为", "改成", "改为")
        # ``上一句话修改为 B`` is a whole-passage rewrite.  Do not reinterpret
        # the shorter ``改为`` inside ``修改为`` as source text ``修``.
        if suffix.startswith(delimiters):
            return None
        for delimiter in delimiters:
            index = suffix.find(delimiter)
            if index <= 0:
                continue
            source = _strip_paired_quotes(suffix[:index])
            replacement = _strip_quoted_replacement_suffix(
                suffix[index + len(delimiter) :]
            )
            if (
                source
                and replacement
                and "\x00" not in source
                and "\x00" not in replacement
                and len(source) <= 256
                and len(replacement) <= 2000
                # Contentful left operands (e.g. ``刚才时间``) stay on the
                # deterministic editor; only pure anaphors claim this path.
                and not _looks_like_recent_reference(source)
            ):
                return source, replacement
    return None


def _is_referenced_replacement_request(body: str) -> bool:
    """Recognize natural whole-passage replacements without stealing A→B edits.

    Users commonly say either ``帮我把刚才那句话改成 B`` or, when ASR
    drops the coverb, ``帮我刚才那句话改成 B``.  The reference must be in a
    request-shaped position and immediately followed by a replacement verb, so
    prose such as ``帮我看看刚才那句话为什么改成这样了`` stays dictation.
    """

    value = str(body or "").strip()
    for position, reference in _iter_reference_hits(value):
        prefix = value[:position].strip("，,：:、 ")
        if prefix not in _REFERENCE_REPLACEMENT_PREFIXES:
            continue
        suffix = value[position + len(reference) :].lstrip("，,：:、 ")
        for optional_prefix in ("给我", "帮我"):
            if suffix.startswith(optional_prefix):
                suffix = suffix[len(optional_prefix) :].lstrip("，,：:、 ")
                break
        for delimiter in ("修改成", "修改为", "改成", "改为"):
            if not suffix.startswith(delimiter):
                continue
            replacement = suffix[len(delimiter) :].strip("，,：:、 ")
            if replacement:
                return True
    return False


def _is_reference_scoped_ai_request(body: str) -> bool:
    """Recognize a natural request whose object is the recent passage.

    ASR often drops the edit verb from colloquial speech such as
    ``帮我上一句话，那个天气挺好，感觉天气挺坏的``.  Requiring an explicit
    request prefix immediately before a known recent-passage reference keeps
    ordinary discussion of the previous sentence out of the rewrite path while
    still letting DeepSeek interpret the user's complete instruction.
    """

    value = str(body or "").strip()
    for position, reference in _iter_reference_hits(value):
        prefix = value[:position].strip("，,：:、 ")
        if prefix not in _REFERENCE_SCOPED_AI_PREFIXES:
            continue
        suffix = value[position + len(reference) :].strip("，,：:、 ")
        if suffix:
            return True
    return False


def parse_recent_voice_command(
    text: str,
    *,
    wakewords: Optional[Iterable[str]] = None,
) -> RecentVoiceCommandParseResult:
    """Parse a wakeword-scoped request about the latest dictated passage.

    Exact ``把 A 改成 B`` commands are intentionally left to the deterministic
    editor.  Everything recognized here is consumed even when no recent group
    is available, so the spoken command can never be dictated into the target.
    """

    raw = str(text or "").strip()
    candidates = wakewords if wakewords is not None else _DEFAULT_WAKEWORDS
    if isinstance(candidates, str):
        candidates = (candidates,)
    active_wakewords = sorted(
        {
            str(candidate or "").strip()
            for candidate in candidates
            if str(candidate or "").strip()
        },
        key=len,
        reverse=True,
    )
    matched_wakeword = next(
        (candidate for candidate in active_wakewords if raw.startswith(candidate)),
        None,
    )
    if matched_wakeword is None:
        return RecentVoiceCommandParseResult(False)
    body = raw[len(matched_wakeword) :].lstrip("，,：:、 ").strip()
    if body[-1:] in _TRAILING_PUNCTUATION:
        body = body[:-1].rstrip()
    if not body:
        return RecentVoiceCommandParseResult(False)

    has_reference = any(term in body for term in _REFERENCE_TERMS) or (
        _find_recent_reference_span(body) is not None
    )
    source_target = _parse_referenced_source_target_request(body)
    source_target_request = source_target is not None
    if source_target_request and source_target[0] == source_target[1]:
        return RecentVoiceCommandParseResult(
            True,
            reason_code="recent_voice_same_replacement",
        )
    referenced_replacement = source_target_request or _is_referenced_replacement_request(
        body
    )
    scoped_ai_request = _is_reference_scoped_ai_request(body)

    # Preserve the established exact-replacement command as a secondary,
    # deterministic workflow instead of stealing it as a semantic rewrite.
    # A left-hand operand such as ``刚才那句话`` is an anaphoric passage
    # reference, not literal source text, and belongs to this recent-voice path.
    if not referenced_replacement and body.startswith(("把", "将", "编辑")) and any(
        delimiter in body for delimiter in ("改成", "改为")
    ):
        return RecentVoiceCommandParseResult(False)

    advice = any(term in body for term in _ADVICE_TERMS)
    rewrite = (
        referenced_replacement
        or scoped_ai_request
        or any(term in body for term in _REWRITE_TERMS)
    )
    if not advice and not rewrite:
        return RecentVoiceCommandParseResult(False)
    # Edit terms are deliberately request-shaped (for example, "帮我优化" or
    # "优化一下"), rather than bare verbs.  A referenced sentence may itself
    # mention nouns such as "优化模式" and must not therefore become a command.
    # Without a reference, only an unmistakable first-person edit request is
    # allowed so bare "小助手润色" keeps its established selection semantics.
    if not has_reference and not body.startswith(
        (
            "帮我修改",
            "帮我润色",
            "帮我优化",
            "帮我改",
            "帮我精简",
            "帮我看看",
            "帮我分析",
            "帮我评价",
            "看看",
        )
    ):
        return RecentVoiceCommandParseResult(False)

    mode = (
        RecentVoiceCommandMode.ADVICE
        if advice
        else RecentVoiceCommandMode.REWRITE
    )
    return RecentVoiceCommandParseResult(
        True,
        RecentVoiceCommand(mode=mode, instruction=body, raw_text=raw),
        "recent_voice_command_ready",
    )


def _target_identity(target: Any) -> tuple[Any, ...]:
    if target is None:
        return ()
    profile = getattr(target, "profile", None)
    return (
        int(getattr(target, "hwnd", 0) or 0),
        int(getattr(target, "pid", 0) or 0),
        int(getattr(target, "focused_hwnd", 0) or 0),
        getattr(getattr(profile, "kind", None), "value", None),
        str(getattr(profile, "process_name", "") or "").lower(),
    )


def _range_identity(target: Any) -> Optional[tuple[type, int, int, int, Any]]:
    token = getattr(target, "adapter_token", None)
    if token is None or not all(hasattr(token, name) for name in ("hwnd", "start", "end")):
        return None
    return (
        type(token),
        int(token.hwnd),
        int(token.start),
        int(token.end),
        getattr(token, "story_type", None),
    )


def _ranges_are_contiguous(
    previous: RecentVoiceSegment, current: RecentVoiceSegment
) -> bool:
    left = _range_identity(previous.target)
    right = _range_identity(current.target)
    if left is None or right is None:
        return left is None and right is None
    return (
        left[0] is right[0]
        and left[1] == right[1]
        and left[4] == right[4]
        and left[3] == right[2]
    )


def _has_continuation_signal(previous_text: str, current_text: str) -> bool:
    previous = str(previous_text or "").rstrip()
    current = str(current_text or "").lstrip()
    if not previous or not current:
        return False
    if previous[-1] not in "。.!！?？；;":
        return True
    return current.startswith(_CONTINUATION_PREFIXES)


class RecentVoiceGroupTracker:
    """Keep a bounded in-memory history of successful Aria insertions."""

    def __init__(
        self,
        *,
        base_gap_s: float = 8.0,
        continuation_gap_s: float = 12.0,
        reference_ttl_s: float = 180.0,
        max_segments: int = 12,
        max_chars: int = 2000,
        max_span_s: float = 180.0,
        history_limit: int = 48,
    ) -> None:
        self.base_gap_s = float(base_gap_s)
        self.continuation_gap_s = float(continuation_gap_s)
        self.reference_ttl_s = float(reference_ttl_s)
        self.max_segments = int(max_segments)
        self.max_chars = int(max_chars)
        self.max_span_s = float(max_span_s)
        self.history_limit = max(self.max_segments + 1, int(history_limit))
        self._segments: list[RecentVoiceSegment] = []
        self._sequence = 0
        self._lock = threading.Lock()

    def record(
        self,
        text: str,
        target: Any,
        *,
        session_id: int,
        voice_start: float,
        voice_end: float,
        inserted_at: float,
    ) -> Optional[RecentVoiceSegment]:
        value = str(text or "")
        if not value:
            return None
        start = float(voice_start)
        end = max(start, float(voice_end))
        with self._lock:
            self._sequence += 1
            segment = RecentVoiceSegment(
                sequence=self._sequence,
                text=value,
                target=target,
                session_id=int(session_id),
                voice_start=start,
                voice_end=end,
                inserted_at=float(inserted_at),
            )
            self._segments.append(segment)
            if len(self._segments) > self.history_limit:
                self._segments = self._segments[-self.history_limit :]
            return segment

    def clear(self) -> None:
        with self._lock:
            self._segments.clear()

    def _combine_target(self, segments: list[RecentVoiceSegment]) -> tuple[Any, bool]:
        latest_target = segments[-1].target
        first_range = _range_identity(segments[0].target)
        last_range = _range_identity(segments[-1].target)
        if first_range is None or last_range is None:
            return latest_target, False
        token = getattr(latest_target, "adapter_token", None)
        try:
            replacements = {
                "start": first_range[2],
                "end": last_range[3],
            }
            first_token = getattr(segments[0].target, "adapter_token", None)
            if all(
                hasattr(token, name)
                and hasattr(first_token, name)
                for name in ("uia_verified", "anchor_chars", "anchor_sha256")
            ):
                replacements.update(
                    uia_verified=bool(first_token.uia_verified),
                    anchor_chars=int(first_token.anchor_chars),
                    anchor_sha256=str(first_token.anchor_sha256),
                )
            # Qt/Electron/custom composers carry a content-free chain of
            # whole-field fingerprints.  Joining the chain lets one request
            # safely undo several contiguous Aria pastes, one verified state
            # at a time, without storing the copied field text.
            if all(
                hasattr(getattr(item.target, "adapter_token", None), "state_chain")
                for item in segments
            ):
                generic_tokens = [
                    getattr(item.target, "adapter_token", None)
                    for item in segments
                ]
                if not all(len(item.state_chain) == 2 for item in generic_tokens):
                    return latest_target, False
                state_chain = [generic_tokens[0].state_chain[0]]
                for generic_token in generic_tokens:
                    if generic_token.state_chain[0] != state_chain[-1]:
                        return latest_target, False
                    state_chain.append(generic_token.state_chain[-1])
                replacements["state_chain"] = tuple(state_chain)
            combined_token = replace(
                token,
                **replacements,
            )
            return replace(latest_target, adapter_token=combined_token), True
        except (TypeError, ValueError):
            return latest_target, False

    def latest(self, *, now: float) -> RecentVoiceGroupResult:
        with self._lock:
            history = list(self._segments)
        if not history:
            return RecentVoiceGroupResult("empty")
        if float(now) - history[-1].inserted_at > self.reference_ttl_s:
            return RecentVoiceGroupResult("expired")

        chosen = [history[-1]]
        overflow = False
        for previous in reversed(history[:-1]):
            current = chosen[0]
            previous_identity = _target_identity(previous.target)
            current_identity = _target_identity(current.target)
            if (
                not previous_identity
                or not current_identity
                or previous_identity != current_identity
            ):
                break
            if not _ranges_are_contiguous(previous, current):
                break
            gap = max(0.0, current.voice_start - previous.voice_end)
            if gap > self.base_gap_s and not (
                gap <= self.continuation_gap_s
                and _has_continuation_signal(previous.text, current.text)
            ):
                break

            candidate = [previous, *chosen]
            candidate_chars = sum(len(item.text) for item in candidate)
            candidate_span = candidate[-1].voice_end - candidate[0].voice_start
            if (
                len(candidate) > self.max_segments
                or candidate_chars > self.max_chars
                or candidate_span > self.max_span_s
            ):
                overflow = True
                break
            chosen = candidate

        if overflow:
            return RecentVoiceGroupResult("too_large")
        target, addressable = self._combine_target(chosen)
        return RecentVoiceGroupResult(
            "ready",
            RecentVoiceGroup(
                segments=tuple(chosen),
                text="".join(item.text for item in chosen),
                target=target,
                addressable=addressable,
            ),
        )

    def replace_group(
        self,
        group: RecentVoiceGroup,
        replacement_text: str,
        replacement_target: Any,
        *,
        inserted_at: float,
    ) -> None:
        """Replace an accepted group with one new in-memory reference segment."""

        if not group.segments or not replacement_text:
            self.clear()
            return
        first = group.segments[0]
        last = group.segments[-1]
        with self._lock:
            sequences = {item.sequence for item in group.segments}
            self._segments = [
                item for item in self._segments if item.sequence not in sequences
            ]
            self._sequence += 1
            self._segments.append(
                RecentVoiceSegment(
                    sequence=self._sequence,
                    text=str(replacement_text),
                    target=replacement_target,
                    session_id=last.session_id,
                    voice_start=first.voice_start,
                    voice_end=last.voice_end,
                    inserted_at=float(inserted_at),
                )
            )
            self._segments.sort(key=lambda item: item.sequence)
            if len(self._segments) > self.history_limit:
                self._segments = self._segments[-self.history_limit :]


__all__ = [
    "RecentVoiceCommand",
    "RecentVoiceCommandMode",
    "RecentVoiceCommandParseResult",
    "RecentVoiceGroup",
    "RecentVoiceGroupResult",
    "RecentVoiceGroupTracker",
    "RecentVoiceSegment",
    "_looks_like_recent_reference",
    "parse_recent_voice_command",
]
