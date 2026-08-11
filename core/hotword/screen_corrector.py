"""
Screen-aware deterministic correction helpers.

This module is intentionally conservative.  The LLM polish prompt is still the
main "soft reasoning" layer, but screen-visible proper nouns have one failure
mode that is cheap to fix locally: ASR often chooses a common homophone for one
Chinese character while the screen/title already contains the exact spelling.

Screen evidence is treated as a continuous confidence signal, not a fixed
"high/medium/low" tier.  A correction must be supported by the combined score of
phonetic fit, literal overlap, screen evidence strength, and ASR-side name/term
context.  Risky cases (especially two-character low-overlap phrases) need more
combined evidence instead of being accepted or rejected solely by source type.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import product
import re
from typing import Dict, Iterable, List, Tuple

try:
    from pypinyin import Style, lazy_pinyin

    _PYPINYIN_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only on broken installs
    _PYPINYIN_AVAILABLE = False
    Style = None
    lazy_pinyin = None


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_STANDALONE_CJK_TERM_RE = re.compile(
    r"(?<![\u3400-\u4dbf\u4e00-\u9fff])"
    r"(?P<term>[\u3400-\u4dbf\u4e00-\u9fff]{3,8})"
    r"(?=(?:\s+|[>」』\"'）)\]】,，。；;、:：!?！？]|$))"
)
_LATIN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_.+-]{2,40}\b")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{1,40}")
_LATIN_SCAN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{1,40}")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_IME_CANDIDATE_RE = re.compile(
    r"(?P<prefix>[\u3400-\u4dbf\u4e00-\u9fff]{2,12})\s+"
    r"(?P<pinyin>[A-Za-züÜvV]{1,8})\s+"
    r"(?P<candidates>(?:[1-9]\s*[\u3400-\u4dbf\u4e00-\u9fff]\s*){1,9})"
)
_EXPLICIT_SCREEN_NAME_PATTERNS = (
    re.compile(
        r"(?:目标名|目标词)\s*[:：]\s*" r"(?P<name>[\u3400-\u4dbf\u4e00-\u9fff]{2,8})"
    ),
    re.compile(
        r"(?:唯一需要被识别成固定写法的(?:人名|名字|专名|名)|"
        r"固定写法的(?:人名|名字|专名|名)|"
        r"(?:人名|名字|角色名|专名))"
        r"(?:是|为)?(?:叫做|叫作|名叫)?"
        r"(?P<name>[\u3400-\u4dbf\u4e00-\u9fff]{2,8})"
    ),
    re.compile(
        r"(?:当前)?屏幕(?:的)?(?:写法|用法)"
        r"\s*(?:是|为|[:：])\s*"
        r"(?P<name>[\u3400-\u4dbf\u4e00-\u9fff]{2,8})"
    ),
)
_SOURCE_NAME_PATTERNS = (
    re.compile(
        r"(?:这个|那个|这位|这名|叫做|叫作|名叫|名字叫|名字是|人名叫|角色叫)"
        r"(?P<name>[\u3400-\u4dbf\u4e00-\u9fff]{2,8}?)"
        r"(?=(?:的)?(?:人设|设定|角色|名字|人名|女主角?|主角)|[，。,.、\s]|$)"
    ),
)

_CJK_STOP_TERMS = {
    "首页",
    "设置",
    "页面",
    "当前页面",
    "目标名",
    "候选",
    "噪声区",
    "窗口按钮",
    "行号",
    "屏幕",
    "上下文",
    "输入法",
    "链路",
    "测试文本",
    "评论",
    "评论区",
    "点赞",
    "投币",
    "收藏",
    "分享",
    "关注",
    "相关推荐",
    "热门视频",
    "直播",
    "番剧",
    "游戏中心",
    "会员购",
    "登录",
    "搜索",
    "文件",
    "编辑",
    "视图",
    "窗口",
}

_LATIN_STOP_LOWER = {
    "http",
    "https",
    "www",
    "com",
    "exe",
    "dll",
    "png",
    "jpg",
    "json",
}

_LATIN_PHONETIC_STOP_LOWER = _LATIN_STOP_LOWER | {
    "and",
    "the",
    "for",
    "with",
    "from",
    "this",
    "that",
    "then",
    "than",
    "true",
    "false",
}

# Screen-gated pronunciation hints for common short English UI/graphics words.
# These are not static hotwords: a replacement is considered only when the
# Latin term itself is visible in the current/recent screen context.
_LATIN_WORD_PHONETIC_HINTS = {
    "add": ("aide", "ad"),
    "edge": ("aiji", "eji", "aizhi"),
    "shadow": ("shadou", "shaduo"),
    "shader": ("sheide", "sheder"),
    "guide": ("gaide", "guide"),
    "map": ("maipu", "mapu", "map"),
    "mask": ("masike", "mask"),
    "proxy": ("puluokesi", "proxy"),
    "normal": ("nuomo", "normal"),
    "floor": ("fuluo", "floor"),
    "support": ("sapote", "support"),
}


@dataclass(frozen=True)
class ScreenCorrection:
    """A single local screen-based rewrite."""

    original: str
    corrected: str
    source: str
    score: float
    reason: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "original": self.original,
            "corrected": self.corrected,
            "source": self.source,
            "score": round(self.score, 3),
            "reason": self.reason,
        }


@dataclass
class _TermEvidence:
    text: str
    title_count: int = 0
    recent_count: int = 0
    body_count: int = 0
    lead_count: int = 0
    standalone_count: int = 0
    explicit_count: int = 0

    @property
    def total_count(self) -> int:
        return self.title_count + self.recent_count + self.body_count

    @property
    def source(self) -> str:
        if self.title_count:
            return "title"
        if self.explicit_count:
            return "explicit"
        if self.lead_count:
            return "lead"
        if self.recent_count:
            return "recent"
        if self.standalone_count:
            return "standalone"
        return "ocr"


def correct_text_with_screen(
    text: str, screen_text: str, max_corrections: int = 4
) -> Tuple[str, List[Dict[str, object]]]:
    """Apply high-confidence screen-visible term corrections.

    Args:
        text: ASR/polish input text.
        screen_text: Structured screen context from ScreenOCR.get_text_for_polish().
        max_corrections: Safety cap; prevents a noisy screen from rewriting too
            much of a single utterance.

    Returns:
        (corrected_text, corrections_as_dicts)
    """

    if not text or not screen_text or not screen_text.strip():
        return text, []

    cjk_terms, latin_terms = _extract_screen_terms(screen_text)
    if not cjk_terms and not latin_terms:
        return text, []

    corrected = text
    corrections: List[ScreenCorrection] = []

    if cjk_terms and _PYPINYIN_AVAILABLE:
        corrected, explicit_corrections = _correct_explicit_screen_name_spans(
            corrected, cjk_terms, max_corrections=max_corrections
        )
        corrections.extend(explicit_corrections)

    if cjk_terms and _PYPINYIN_AVAILABLE and len(corrections) < max_corrections:
        corrected, cjk_corrections = _correct_cjk_spans(
            corrected, cjk_terms, max_corrections=max_corrections - len(corrections)
        )
        corrections.extend(cjk_corrections)

    if cjk_terms and _PYPINYIN_AVAILABLE and len(corrections) < max_corrections:
        corrected, completion_corrections = _complete_cjk_prefix_spans(
            corrected,
            cjk_terms,
            max_corrections=max_corrections - len(corrections),
        )
        corrections.extend(completion_corrections)

    if len(corrections) < max_corrections and latin_terms:
        corrected, latin_fuzzy_corrections = _correct_latin_fuzzy_terms(
            corrected,
            latin_terms,
            max_corrections=max_corrections - len(corrections),
        )
        corrections.extend(latin_fuzzy_corrections)

    if len(corrections) < max_corrections and latin_terms and _PYPINYIN_AVAILABLE:
        corrected, cjk_to_latin_corrections = _correct_cjk_to_latin_terms(
            corrected,
            latin_terms,
            max_corrections=max_corrections - len(corrections),
        )
        corrections.extend(cjk_to_latin_corrections)

    if len(corrections) < max_corrections and latin_terms:
        corrected, latin_corrections = _correct_latin_case(
            corrected,
            latin_terms,
            max_corrections=max_corrections - len(corrections),
        )
        corrections.extend(latin_corrections)

    return corrected, [c.to_dict() for c in corrections]


def protect_text_after_polish(
    source_text: str,
    polished_text: str,
    screen_text: str = "",
    max_corrections: int = 4,
) -> Tuple[str, List[Dict[str, object]]]:
    """Re-apply screen-backed correction after the LLM call.

    The LLM sometimes normalizes a screen-visible rare term back to a common
    homophone during prose cleanup.  This post-pass uses the same dynamic screen
    evidence as the pre-pass, without trusting the ASR source itself as a
    hotword.  That distinction matters: if a rare name was visible on the
    previous/current screen, screen context should win; if it was not visible,
    this function does not invent or preserve anything.
    """

    if not polished_text:
        return polished_text, []

    if not screen_text or not screen_text.strip():
        return polished_text, []

    unsafe_name_changes = detect_unrelated_polish_name_changes(
        source_text, polished_text
    )
    if unsafe_name_changes:
        # The LLM sometimes over-trusts noisy screen history and rewrites a
        # coherent ASR-side name to a different screen-visible name whose
        # pronunciation is not close.  In that case the safest output is the
        # pre-LLM source/fallback text; deterministic screen correction below
        # can still handle true same-pinyin fixes on later attempts.
        return source_text, unsafe_name_changes

    corrected, screen_corrections = correct_text_with_screen(
        polished_text, screen_text, max_corrections=max_corrections
    )
    corrections = [
        ScreenCorrection(
            original=str(item.get("original", "")),
            corrected=str(item.get("corrected", "")),
            source=f"post_{item.get('source', 'screen')}",
            score=float(item.get("score", 0.0)),
            reason=str(item.get("reason", "screen post-correction")),
        )
        for item in screen_corrections
    ]

    return corrected, [c.to_dict() for c in corrections]


def detect_unrelated_polish_name_changes(
    source_text: str, polished_text: str
) -> List[Dict[str, object]]:
    """Reject LLM-only name rewrites that are not phonetic corrections.

    Screen context is allowed to fix same/near-pinyin proper nouns, but not to
    turn a coherent dictated name into an unrelated name simply because an old
    screen block contains that other name.  This guard is source-based and
    runtime-only; it does not add static hotwords.
    """

    if (
        not source_text
        or not polished_text
        or source_text == polished_text
        or not _PYPINYIN_AVAILABLE
    ):
        return []

    source_names = _extract_source_named_spans(source_text)
    if not source_names:
        return []

    corrections: List[ScreenCorrection] = []
    for source_name in source_names:
        if source_name in polished_text:
            continue

        candidate, py_score, overlap = _best_polished_name_equivalent(
            source_name, polished_text
        )
        if candidate and _is_phonetic_name_equivalent(
            py_score=py_score,
            overlap=overlap,
        ):
            continue

        reason = (
            "LLM changed ASR-side name to a non-phonetic screen candidate"
            if candidate
            else "LLM removed ASR-side name without phonetic equivalent"
        )
        if candidate:
            reason += f" (candidate={candidate}, pinyin={py_score:.2f}, overlap={overlap:.2f})"
        corrections.append(
            ScreenCorrection(
                original=candidate or "",
                corrected=source_name,
                source="source_name_guard",
                score=1.0,
                reason=reason,
            )
        )
        break

    return [c.to_dict() for c in corrections]


def _extract_screen_terms(
    screen_text: str,
) -> Tuple[Dict[str, _TermEvidence], Dict[str, _TermEvidence]]:
    title_lines, recent_lines, body_lines = _split_screen_sections(screen_text)
    cjk_terms: Dict[str, _TermEvidence] = {}
    latin_terms: Dict[str, _TermEvidence] = {}

    for line in title_lines:
        _add_terms_from_line(line, cjk_terms, latin_terms, source="title")
    for line in recent_lines:
        _add_terms_from_line(line, cjk_terms, latin_terms, source="recent")
    for line in body_lines:
        _add_terms_from_line(line, cjk_terms, latin_terms, source="ocr")

    return cjk_terms, latin_terms


def _split_screen_sections(screen_text: str) -> Tuple[List[str], List[str], List[str]]:
    title_lines: List[str] = []
    recent_lines: List[str] = []
    body_lines: List[str] = []
    saw_label = False
    section = "body"

    for raw_line in screen_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        title_match = re.match(r"^窗口标题\s*[:：]\s*(.+)$", line)
        if title_match and section != "recent":
            saw_label = True
            section = "title"
            title_lines.append(title_match.group(1).strip())
            continue

        if re.match(r"^近期屏幕上下文\s*(?:\([^)]*\))?\s*[:：]?\s*$", line):
            # Recent OCR memory is useful, but it is a weaker signal than the
            # current OS window title.  Keep it as its own evidence source
            # instead of flattening it into title/body buckets.
            saw_label = True
            section = "recent"
            continue

        if re.match(r"^页面内容\s*(?:\([^)]*\))?\s*[:：]?\s*$", line):
            saw_label = True
            section = "body"
            continue

        if section == "body":
            body_lines.append(line)
        elif section == "recent":
            recent_lines.append(line)
        elif saw_label:
            # Extra unlabeled lines before "页面内容" are still title-adjacent
            # current-window context.
            title_lines.append(line)
        else:
            body_lines.append(line)

    if not saw_label and body_lines:
        # Backward compatibility with old dumps/tests that passed:
        # "current page title\n..."
        first = body_lines.pop(0)
        if len(first) <= 120:
            title_lines.append(first)
        else:
            body_lines.insert(0, first)

    return title_lines, recent_lines, body_lines


def _add_terms_from_line(
    line: str,
    cjk_terms: Dict[str, _TermEvidence],
    latin_terms: Dict[str, _TermEvidence],
    source: str,
) -> None:
    segment = line
    _add_ime_candidate_terms(segment, cjk_terms, source)
    _add_explicit_cjk_name_terms(segment, cjk_terms, source)
    _add_line_leading_cjk_term(segment, cjk_terms, source)
    _add_standalone_cjk_terms(segment, cjk_terms, source)
    for seq in _CJK_RE.findall(segment):
        for term in _iter_cjk_terms(seq):
            if source in {"ocr", "recent"} and _looks_like_noisy_body_cjk_term(term):
                continue
            _add_evidence(cjk_terms, term, source)

    for term in _iter_latin_terms(segment):
        _add_evidence(latin_terms, term, source)


def _add_explicit_cjk_name_terms(
    segment: str,
    cjk_terms: Dict[str, _TermEvidence],
    source: str,
) -> None:
    """Boost names that the visible text itself presents as the intended spelling.

    This is runtime screen evidence from natural prose like "目标名: X" or
    "固定写法的人名是 X".  It is not an artificial correct/incorrect label and
    does not add a static hotword; the term must be present on the current
    screen/OCR material.
    """

    for pattern in _EXPLICIT_SCREEN_NAME_PATTERNS:
        for match in pattern.finditer(segment):
            name = (match.group("name") or "").strip()
            if not (2 <= len(name) <= 8):
                continue
            if name in _CJK_STOP_TERMS:
                continue
            if source in {"ocr", "recent"} and _looks_like_noisy_body_cjk_term(name):
                continue
            _add_evidence(cjk_terms, name, source, explicit=True)


def _add_line_leading_cjk_term(
    segment: str,
    cjk_terms: Dict[str, _TermEvidence],
    source: str,
) -> None:
    """Boost a standalone CJK term placed at the beginning of a screen line.

    In live tests the user often types the target name first, then dictates a
    sentence containing a near-homophone.  OCR sees a shape like
    ``目标名   dictated sentence ...``.  The leading standalone token should be
    stronger than another same-pinyin term embedded later in the noisy line, but
    this is still only screen evidence, not a static hotword.
    """

    if source == "title":
        return
    match = re.match(
        r"^[\s>「『\"'（(]*"
        r"(?P<term>[\u3400-\u4dbf\u4e00-\u9fff]{2,8})"
        r"(?=\s{2,}|[：:，,。；;、]|$)",
        segment,
    )
    if not match:
        return
    term = match.group("term")
    if term in _CJK_STOP_TERMS:
        return
    _add_evidence(cjk_terms, term, source, lead=True)


def _add_standalone_cjk_terms(
    segment: str,
    cjk_terms: Dict[str, _TermEvidence],
    source: str,
) -> None:
    """Boost CJK terms typed as standalone tokens inside a noisy OCR line.

    Terminal/chat OCR often flattens the whole viewport into one long line.  A
    user-typed test name can be visually standalone in the prompt area even when
    it is not at the start of that flattened line: ``... <correct-name> <wrong-homophone>是...``.
    This evidence is stronger than a previous dictated sentence that merely
    contains a wrong homophone, but it is still runtime screen evidence only.
    """

    if source == "title":
        return
    for match in _STANDALONE_CJK_TERM_RE.finditer(segment):
        term = match.group("term")
        if term in _CJK_STOP_TERMS:
            continue
        if source in {"ocr", "recent"} and _looks_like_noisy_body_cjk_term(term):
            continue
        _add_evidence(cjk_terms, term, source, standalone=True)


def _add_ime_candidate_terms(
    segment: str,
    cjk_terms: Dict[str, _TermEvidence],
    source: str,
) -> None:
    """Recover terms from visible Chinese IME composition/candidate UI.

    OCR of a freshly typed name can see the IME state instead of the final word,
    e.g. ``<prefix> ya 1娅2呀3芽``.  This is dynamic screen evidence, not a local
    hotword: we compose only the currently visible prefix plus the first matching
    candidate character.
    """

    for match in _IME_CANDIDATE_RE.finditer(segment):
        raw_prefix = match.group("prefix")
        raw_py = _normalize_pinyin_token(match.group("pinyin"))
        candidate_chars = re.findall(
            r"[1-9]\s*([\u3400-\u4dbf\u4e00-\u9fff])", match.group("candidates")
        )
        if not raw_prefix or not raw_py or not candidate_chars:
            continue

        first = candidate_chars[0]
        if not _char_matches_pinyin(first, raw_py):
            continue

        # OCR may glue the previous sentence char into the prefix ("字<name>").
        # Emit suffixes so the actual name suffix survives without trusting the
        # entire glued run.
        max_prefix_len = min(6, len(raw_prefix))
        for prefix_len in range(max_prefix_len, 1, -1):
            prefix = raw_prefix[-prefix_len:]
            if prefix in _CJK_STOP_TERMS:
                continue
            term = prefix + first
            if 3 <= len(term) <= 8:
                _add_evidence(cjk_terms, term, source, standalone=True)


def _normalize_pinyin_token(token: str) -> str:
    return (token or "").lower().replace("v", "ü")


def _char_matches_pinyin(ch: str, pinyin: str) -> bool:
    if not _PYPINYIN_AVAILABLE:
        return True
    try:
        py = _normalize_pinyin_token(lazy_pinyin(ch, style=Style.NORMAL)[0])
    except Exception:
        return True
    return py == pinyin


def _iter_latin_terms(segment: str) -> Iterable[str]:
    """Yield single and short compound Latin terms from one screen line."""

    token_matches = list(_LATIN_TOKEN_RE.finditer(segment))
    tokens: List[str] = []

    for match in token_matches:
        token = match.group(0).strip("_.+-")
        if len(token) < 3:
            continue
        if token.lower() in _LATIN_STOP_LOWER:
            continue
        tokens.append(token)
        yield token

        split = _split_latin_compound(token)
        if split and split != token:
            yield split

    # Adjacent two-word screen phrases are common in prose ("edge shadow").
    # Keep this short and local to avoid turning noisy OCR lines into long
    # phrase candidates.
    for left, right in zip(tokens, tokens[1:]):
        if left.lower() in _LATIN_PHONETIC_STOP_LOWER:
            continue
        if right.lower() in _LATIN_PHONETIC_STOP_LOWER:
            continue
        if len(left) + len(right) > 28:
            continue
        yield f"{left} {right}"


def _split_latin_compound(token: str) -> str:
    """Split CamelCase/PascalCase identifiers for prose correction."""

    cleaned = token.strip("_.+-")
    if not cleaned or " " in cleaned:
        return cleaned

    parts = _CAMEL_BOUNDARY_RE.split(cleaned)
    if len(parts) >= 2 and all(len(part) >= 2 for part in parts):
        return " ".join(parts)
    return cleaned


def _iter_cjk_terms(seq: str) -> Iterable[str]:
    if len(seq) < 2:
        return

    emitted = set()
    max_len = min(8, len(seq))
    if len(seq) <= 8:
        if seq not in _CJK_STOP_TERMS:
            emitted.add(seq)
            yield seq

    # OCR often glues labels together.  N-grams recover the
    # embedded proper noun without requiring a tokenizer.
    for size in range(2, max_len + 1):
        for start in range(0, len(seq) - size + 1):
            term = seq[start : start + size]
            if term in emitted or term in _CJK_STOP_TERMS:
                continue
            emitted.add(term)
            yield term


def _looks_like_noisy_body_cjk_term(term: str) -> bool:
    """Filter prose fragments from noisy OCR body/recent n-grams.

    OCR body lines often contain full Chinese sentences.  Blind n-grams from
    those sentences can look like same-pinyin candidates and rewrite unrelated
    chunks of the ASR text.  Body/recent terms containing common grammar
    particles are usually prose fragments, not standalone names.  Title and
    standalone runtime screen terms bypass this filter.
    """

    if len(term) <= 1:
        return True
    noisy_substrings = (
        "这个",
        "那个",
        "页面",
        "屏幕",
        "上下",
        "上下文",
        "传递",
        "语音",
        "输入法",
        "正确",
        "利用",
        "链路",
        "正常",
        "最终",
        "输出",
        "用法",
        "同音",
        "固定",
        "写法",
        "识别",
        "测试",
        "文本",
        "候选",
        "噪声",
        "窗口",
        "按钮",
        "复制",
        "粘贴",
        "行号",
        "业务",
        "语境",
        "普通",
        "句子",
        "根据",
        "理解",
        "上面",
        "下面",
        "如果",
        "应该",
        "正在",
        "用户",
        "的人",
        "人设",
        "还是",
        "不错",
        "觉得",
        "目前",
        "已经",
        "现在",
        "可以",
        "问题",
        "叫做",
        "叫作",
        "名叫",
    )
    if any(part in term for part in noisy_substrings):
        return True
    noisy_chars = set("的了是我你他她它不还都就很在有和跟把被啊呢吗吧个里页")
    return any(ch in noisy_chars for ch in term)


def _add_evidence(
    terms: Dict[str, _TermEvidence],
    term: str,
    source: str,
    lead: bool = False,
    standalone: bool = False,
    explicit: bool = False,
) -> None:
    if not term:
        return
    ev = terms.get(term)
    if not ev:
        ev = _TermEvidence(text=term)
        terms[term] = ev
    if source == "title":
        ev.title_count += 1
    elif source == "recent":
        ev.recent_count += 1
    else:
        ev.body_count += 1
    if lead:
        ev.lead_count += 1
    if standalone:
        ev.standalone_count += 1
    if explicit:
        ev.explicit_count += 1


def _correct_explicit_screen_name_spans(
    text: str,
    terms: Dict[str, _TermEvidence],
    max_corrections: int,
) -> Tuple[str, List[ScreenCorrection]]:
    """Use explicit screen-spelling intent to fix one dictated name.

    This handles live-test/product cases where the user says "屏幕写法/固定写法/
    不是同音字" and the screen contains a visibly intended name.  The gate is
    deliberately narrow: require an ASR-side name cue, require explicit runtime
    screen evidence, and require at least a suffix/phonetic anchor so old noisy
    context cannot freely replace unrelated names.
    """

    if max_corrections <= 0 or not _has_explicit_screen_spelling_intent(text):
        return text, []

    source_names = _extract_source_named_spans(text)
    if not source_names:
        return text, []

    candidates = [
        ev for ev in terms.values() if ev.explicit_count and 2 <= len(ev.text) <= 8
    ]
    if not candidates:
        return text, []

    replacements: List[Tuple[int, int, str, ScreenCorrection]] = []
    for source_name in source_names:
        match = re.search(re.escape(source_name), text)
        if not match:
            continue
        best = _best_explicit_screen_name_match(source_name, candidates)
        if not best:
            continue
        target, ev, score, reason = best
        correction = ScreenCorrection(
            original=source_name,
            corrected=target,
            source=ev.source,
            score=score,
            reason=reason,
        )
        replacements.append((match.start(), match.end(), target, correction))
        break

    if not replacements:
        return text, []

    result = text
    corrections = [r[3] for r in replacements[:max_corrections]]
    for start, end, target, _correction in reversed(replacements[:max_corrections]):
        result = result[:start] + target + result[end:]
    return result, corrections


def _has_explicit_screen_spelling_intent(text: str) -> bool:
    markers = (
        "固定写法",
        "屏幕写法",
        "屏幕的用法",
        "当前屏幕写法",
        "屏幕上下文",
        "不是同音字",
        "同音字",
    )
    return bool(text and any(marker in text for marker in markers))


def _best_explicit_screen_name_match(
    source_name: str,
    candidates: List[_TermEvidence],
) -> Tuple[str, _TermEvidence, float, str] | None:
    source_py = _to_pinyin(source_name)
    if not source_py:
        return None

    best = None
    for ev in candidates:
        target = ev.text
        if target == source_name or abs(len(target) - len(source_name)) > 1:
            continue
        target_py = _to_pinyin(target)
        if not target_py:
            continue

        full_py = _pinyin_similarity(source_py, target_py)
        suffix_n = min(2, len(source_py), len(target_py))
        suffix_py = (
            _pinyin_similarity(source_py[-suffix_n:], target_py[-suffix_n:])
            if suffix_n > 0
            else 0.0
        )
        overlap = _positional_overlap(source_name, target)
        evidence = _screen_evidence_strength(ev)

        # Need a real anchor.  The common live failure is a mangled prefix with
        # the suffix still recognisable (leading char misheard as a homophone,
        # tail chars intact).  Without such an
        # anchor, explicit screen prose remains LLM-only semantic context.
        if suffix_py < 0.92 and full_py < 0.70 and overlap < 0.25:
            continue

        confidence = (
            full_py * 0.24 + suffix_py * 0.38 + evidence * 0.30 + overlap * 0.08
        )
        threshold = 0.66
        if ev.explicit_count:
            threshold -= 0.08
        if ev.title_count or ev.lead_count:
            threshold -= 0.04
        if confidence < threshold:
            continue

        reason = (
            f"explicit_screen_name full_pinyin={full_py:.2f}, "
            f"suffix_pinyin={suffix_py:.2f}, overlap={overlap:.2f}, "
            f"evidence={evidence:.2f}, confidence={confidence:.2f}, "
            f"threshold={threshold:.2f}, explicit={ev.explicit_count}, "
            f"title={ev.title_count}, recent={ev.recent_count}, body={ev.body_count}"
        )
        if best is None or confidence > best[2]:
            best = (target, ev, confidence, reason)

    return best


def _correct_cjk_spans(
    text: str,
    terms: Dict[str, _TermEvidence],
    max_corrections: int,
) -> Tuple[str, List[ScreenCorrection]]:
    replacements: List[Tuple[int, int, str, ScreenCorrection]] = []
    exact_index, near_candidates, named_near_candidates = _build_cjk_index(terms)

    for match in _CJK_RE.finditer(text):
        run = match.group(0)
        run_start = match.start()
        i = 0
        while i < len(run) and len(replacements) < max_corrections:
            best = None
            best_len = 0
            max_len = min(8, len(run) - i)
            for size in range(max_len, 1, -1):
                if size <= 2 and len(run) > size:
                    # Avoid producing half-fixed names (tail rewritten while the
                    # leading char stays stale) by rewriting only a 2-char
                    # suffix inside a longer CJK run.
                    # Standalone two-character names are still handled when the
                    # whole run is exactly that span.
                    continue
                span = run[i : i + size]
                candidate = _best_cjk_match(
                    span,
                    exact_index,
                    near_candidates,
                    named_near_candidates,
                    full_text=text,
                    span_start=run_start + i,
                    span_end=run_start + i + size,
                )
                if candidate:
                    best = candidate
                    best_len = size
                    break
            if best:
                target, ev, score, reason = best
                start = run_start + i
                end = start + best_len
                correction = ScreenCorrection(
                    original=run[i : i + best_len],
                    corrected=target,
                    source=ev.source,
                    score=score,
                    reason=reason,
                )
                replacements.append((start, end, target, correction))
                i += best_len
            else:
                i += 1

    if not replacements:
        return text, []

    result = text
    corrections = [r[3] for r in replacements]
    for start, end, target, _correction in reversed(replacements):
        result = result[:start] + target + result[end:]
    return result, corrections


def _complete_cjk_prefix_spans(
    text: str,
    terms: Dict[str, _TermEvidence],
    max_corrections: int,
) -> Tuple[str, List[ScreenCorrection]]:
    """Complete a shorter polished span to a screen-visible longer term.

    This catches the common LLM failure after screen reasoning: the model fixes
    some characters but drops the final rare IME candidate, e.g. screen-visible
    ``<full-4char-name>`` but polished text becomes ``<3-char-prefix-only>``.
    """

    candidates = [
        ev
        for ev in terms.values()
        if 3 <= len(ev.text) <= 8
        and (
            ev.title_count
            or ev.lead_count
            or ev.standalone_count
            or ev.recent_count
            or ev.total_count >= 2
        )
    ]
    if not candidates:
        return text, []

    replacements: List[Tuple[int, int, str, ScreenCorrection]] = []
    for match in _CJK_RE.finditer(text):
        run = match.group(0)
        run_start = match.start()
        for i in range(len(run)):
            if len(replacements) >= max_corrections:
                break
            max_len = min(7, len(run) - i)
            for size in range(max_len, 1, -1):
                span = run[i : i + size]
                candidate = _best_cjk_prefix_completion(
                    span,
                    candidates,
                    full_text=text,
                    span_start=run_start + i,
                    span_end=run_start + i + size,
                )
                if not candidate:
                    continue
                target, ev, score, reason = candidate
                start = run_start + i
                end = start + size
                correction = ScreenCorrection(
                    original=span,
                    corrected=target,
                    source=f"{ev.source}_completion",
                    score=score,
                    reason=reason,
                )
                replacements.append((start, end, target, correction))
                break

    if not replacements:
        return text, []

    # Avoid overlapping completions.
    replacements.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    filtered: List[Tuple[int, int, str, ScreenCorrection]] = []
    last_end = -1
    for item in replacements:
        if item[0] < last_end:
            continue
        filtered.append(item)
        last_end = item[1]

    result = text
    corrections = [r[3] for r in filtered]
    for start, end, target, _correction in reversed(filtered):
        result = result[:start] + target + result[end:]
    return result, corrections


def _best_cjk_prefix_completion(
    span: str,
    candidates: List[_TermEvidence],
    full_text: str,
    span_start: int,
    span_end: int,
) -> Tuple[str, _TermEvidence, float, str] | None:
    if len(span) < 2:
        return None

    span_py = _to_pinyin(span)
    if not span_py:
        return None

    named_context = _has_named_entity_context(full_text, span_start, span_end)
    best = None
    for ev in candidates:
        target = ev.text
        if target == span or not (len(span) < len(target) <= len(span) + 2):
            continue
        if not (named_context or ev.title_count or ev.lead_count):
            # Prefix completion can otherwise extend ordinary prose fragments
            # copied from OCR ("测试" -> "测试文本", "屏幕上下" -> ...).  Keep it
            # for explicit name/term contexts and title/leading screen terms.
            continue
        if _looks_like_noisy_body_cjk_term(target) and not ev.title_count:
            # Prefix completion is powerful enough to create prose corruption
            # such as "页面" -> "页面只" from a noisy OCR sentence.  Keep it for
            # real screen-visible names/terms, but do not complete ordinary UI
            # or prose fragments unless a high-confidence title supplies them.
            continue
        if full_text.startswith(target, span_start):
            # The full screen term is already present at this position; do not
            # complete its prefix and duplicate the suffix.
            continue
        missing_suffix = target[len(span) :]
        following = full_text[span_end : span_end + len(missing_suffix)]
        if (
            following
            and _pinyin_similarity(_to_pinyin(following), _to_pinyin(missing_suffix))
            >= 0.96
        ):
            # The dictated/polished text already contains a suffix that sounds
            # like the target suffix.  Completing the prefix would duplicate it
            # (e.g. "<prefix><suffix>" -> "<prefix><suffix><suffix-tail>").
            continue

        target_prefix = target[: len(span)]
        target_prefix_py = _to_pinyin(target_prefix)
        py_score = _pinyin_similarity(span_py, target_prefix_py)
        if py_score < 0.96:
            continue

        literal_prefix = target.startswith(span)
        overlap = _positional_overlap(span, target_prefix)
        evidence = _screen_evidence_strength(ev)
        confidence = py_score * 0.48 + overlap * 0.26 + evidence * 0.34

        threshold = 0.78
        if named_context:
            threshold -= 0.08
        if literal_prefix:
            threshold -= 0.08
        if ev.title_count:
            threshold -= 0.06
        if evidence < 0.35 and not named_context:
            threshold += 0.08

        if confidence < threshold:
            continue

        reason = (
            f"prefix_completion pinyin={py_score:.2f}, overlap={overlap:.2f}, "
            f"evidence={evidence:.2f}, confidence={confidence:.2f}, "
            f"threshold={threshold:.2f}, title={ev.title_count}, "
            f"recent={ev.recent_count}, body={ev.body_count}, "
            f"name_ctx={int(named_context)}, "
            f"literal_prefix={int(literal_prefix)}"
        )
        if best is None or confidence > best[2]:
            best = (target, ev, confidence, reason)

    return best


def _correct_cjk_to_latin_terms(
    text: str,
    terms: Dict[str, _TermEvidence],
    max_corrections: int,
) -> Tuple[str, List[ScreenCorrection]]:
    """Correct Chinese ASR transliterations to screen-visible Latin terms.

    Example class of failures: a short dictated English UI/shader term may be
    decoded as ordinary Chinese words ("埃及石头") while the current screen
    repeatedly contains the Latin term.  This pass remains screen-gated and
    evidence-scored; it never invents a Latin term that is not on screen.
    """

    candidates = _build_latin_phonetic_candidates(terms)
    if not candidates:
        return text, []

    replacements: List[Tuple[int, int, str, ScreenCorrection]] = []
    for match in _CJK_RE.finditer(text):
        run = match.group(0)
        run_start = match.start()
        i = 0
        while i < len(run) and len(replacements) < max_corrections:
            best = None
            best_len = 0
            max_len = min(7, len(run) - i)
            for size in range(max_len, 1, -1):
                span = run[i : i + size]
                candidate = _best_cjk_to_latin_match(
                    span,
                    candidates,
                    full_text=text,
                    span_start=run_start + i,
                    span_end=run_start + i + size,
                )
                if candidate:
                    best = candidate
                    best_len = size
                    break
            if best:
                target, ev, score, reason = best
                start = run_start + i
                end = start + best_len
                correction = ScreenCorrection(
                    original=run[i : i + best_len],
                    corrected=target,
                    source=f"{ev.source}_latin_phonetic",
                    score=score,
                    reason=reason,
                )
                replacements.append((start, end, target, correction))
                i += best_len
            else:
                i += 1

    if not replacements:
        return text, []

    result = text
    corrections = [r[3] for r in replacements]
    for start, end, target, _correction in reversed(replacements):
        result = result[:start] + target + result[end:]
    return result, corrections


def _correct_latin_fuzzy_terms(
    text: str,
    terms: Dict[str, _TermEvidence],
    max_corrections: int,
) -> Tuple[str, List[ScreenCorrection]]:
    """Correct near-miss Latin ASR output to screen-visible Latin compounds."""

    candidates = _build_latin_phonetic_candidates(terms)
    if not candidates:
        return text, []

    replacements: List[Tuple[int, int, str, ScreenCorrection]] = []
    matches = list(_LATIN_SCAN_RE.finditer(text))
    used_ranges: List[Tuple[int, int]] = []

    for index, match in enumerate(matches):
        if len(replacements) >= max_corrections:
            break
        for token_count in (3, 2, 1):
            if index + token_count > len(matches):
                continue
            span_start = matches[index].start()
            span_end = matches[index + token_count - 1].end()
            if any(not (span_end <= s or span_start >= e) for s, e in used_ranges):
                continue
            raw_span = text[span_start:span_end]
            # Only handle compact space-separated phrases; do not cross
            # punctuation/sentence boundaries.
            if "\n" in raw_span or re.search(r"[，。！？；;,.!?]", raw_span):
                continue
            if not (
                _is_latin_span_standalone(text, span_start, span_end)
                or raw_span.lstrip()[:1].isupper()
                or _has_named_entity_context(text, span_start, span_end)
            ):
                # Avoid turning an ordinary lower-case verb phrase inside a
                # Chinese sentence ("我想 add shadow 一下") into a screen term.
                continue
            candidate = _best_latin_surface_match(raw_span, candidates)
            if not candidate:
                continue
            target, ev, score, reason = candidate
            if _normalize_latin_surface(raw_span) == _normalize_latin_surface(target):
                continue
            correction = ScreenCorrection(
                original=raw_span,
                corrected=target,
                source=f"{ev.source}_latin_fuzzy",
                score=score,
                reason=reason,
            )
            replacements.append((span_start, span_end, target, correction))
            used_ranges.append((span_start, span_end))
            break

    if not replacements:
        return text, []

    result = text
    corrections = [r[3] for r in replacements]
    for start, end, replacement, _correction in reversed(replacements):
        result = result[:start] + replacement + result[end:]
    return result, corrections


def _build_latin_phonetic_candidates(
    terms: Dict[str, _TermEvidence]
) -> List[Tuple[str, _TermEvidence, Tuple[str, ...], float]]:
    candidates: List[Tuple[str, _TermEvidence, Tuple[str, ...], float]] = []
    seen = set()

    for ev in terms.values():
        display = _display_latin_term(ev.text)
        norm = _normalize_latin_surface(display)
        if not norm or len(norm) < 6:
            continue
        if not _looks_like_latin_term(display):
            continue

        evidence = _screen_evidence_strength(ev)
        if evidence < 0.16:
            continue

        aliases = _latin_term_phonetic_aliases(display)
        if not aliases:
            continue

        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append((display, ev, aliases, evidence))

    return candidates


def _best_cjk_to_latin_match(
    span: str,
    candidates: List[Tuple[str, _TermEvidence, Tuple[str, ...], float]],
    full_text: str,
    span_start: int,
    span_end: int,
) -> Tuple[str, _TermEvidence, float, str] | None:
    span_py = "".join(_to_pinyin(span))
    if len(span_py) < 4:
        return None

    named_context = _has_named_entity_context(full_text, span_start, span_end)
    short_or_weird = _is_short_standalone_span(full_text, span_start, span_end)
    best = None

    for target, ev, aliases, evidence in candidates:
        phonetic = max(_latin_phonetic_similarity(span_py, alias) for alias in aliases)
        if phonetic < 0.70:
            continue

        # This path intentionally allows low literal overlap (Chinese vs Latin),
        # but still requires screen evidence plus either high phonetic fit or a
        # context shape that looks like a term/name test.
        confidence = phonetic * 0.68 + evidence * 0.32
        threshold = 0.62
        if named_context:
            threshold -= 0.05
        if short_or_weird:
            threshold -= 0.04
        if ev.title_count:
            threshold -= 0.04
        if confidence < threshold:
            continue

        reason = (
            f"cjk_to_latin phonetic={phonetic:.2f}, evidence={evidence:.2f}, "
            f"confidence={confidence:.2f}, threshold={threshold:.2f}, "
            f"title={ev.title_count}, recent={ev.recent_count}, "
            f"body={ev.body_count}, "
            f"name_ctx={int(named_context)}, short={int(short_or_weird)}"
        )
        if best is None or confidence > best[2]:
            best = (target, ev, confidence, reason)

    return best


def _best_latin_surface_match(
    raw_span: str,
    candidates: List[Tuple[str, _TermEvidence, Tuple[str, ...], float]],
) -> Tuple[str, _TermEvidence, float, str] | None:
    source_norm = _normalize_latin_surface(raw_span)
    if len(source_norm) < 5:
        return None

    best = None
    for target, ev, aliases, evidence in candidates:
        target_norm = _normalize_latin_surface(target)
        if source_norm == target_norm:
            continue

        surface = _surface_similarity(source_norm, target_norm)
        if surface < 0.70:
            continue

        # A fuzzy Latin rewrite is risky, so require either a shared tail word
        # (e.g. "... shadow") or strong screen evidence.
        shared_tail = (
            _last_latin_word(raw_span).lower() == _last_latin_word(target).lower()
        )
        if not shared_tail and evidence < 0.35:
            continue

        confidence = surface * 0.70 + evidence * 0.30
        first_word_confusable = _first_latin_word_confusable(raw_span, target)
        if shared_tail and first_word_confusable:
            threshold = 0.52
        else:
            threshold = 0.70 if shared_tail else 0.76
        if ev.title_count:
            threshold -= 0.04
        if confidence < threshold:
            continue

        reason = (
            f"latin_fuzzy surface={surface:.2f}, evidence={evidence:.2f}, "
            f"confidence={confidence:.2f}, threshold={threshold:.2f}, "
            f"shared_tail={int(shared_tail)}, "
            f"first_confusable={int(first_word_confusable)}, title={ev.title_count}, "
            f"recent={ev.recent_count}, body={ev.body_count}, "
            f"standalone={ev.standalone_count}"
        )
        if best is None or confidence > best[2]:
            best = (target, ev, confidence, reason)

    return best


def _build_cjk_index(terms: Dict[str, _TermEvidence]) -> Tuple[
    Dict[int, Dict[Tuple[str, ...], List[_TermEvidence]]],
    Dict[int, List[Tuple[_TermEvidence, Tuple[str, ...]]]],
    Dict[int, List[Tuple[_TermEvidence, Tuple[str, ...]]]],
]:
    """Precompute screen-term pinyin indexes for fast span lookup."""

    exact_index: Dict[int, Dict[Tuple[str, ...], List[_TermEvidence]]] = {}
    near_candidates: Dict[int, List[Tuple[_TermEvidence, Tuple[str, ...]]]] = {}
    named_near_candidates: Dict[int, List[Tuple[_TermEvidence, Tuple[str, ...]]]] = {}

    for ev in terms.values():
        py = _to_pinyin(ev.text)
        if not py:
            continue
        length = len(ev.text)
        exact_index.setdefault(length, {}).setdefault(py, []).append(ev)

        # Near-pinyin scan is intentionally bounded.  Terms with explicit,
        # current-title, recent-memory, or repeated OCR support can participate
        # in normal near matching; noisy body-only singletons are considered only
        # when the ASR span itself looks like a name/ID/term.
        if (
            ev.title_count > 0
            or ev.explicit_count > 0
            or ev.lead_count > 0
            or ev.standalone_count > 0
            or ev.recent_count > 0
            or ev.total_count >= 2
        ):
            near_candidates.setdefault(length, []).append((ev, py))
        else:
            # Body-only singletons are too noisy for broad near matching, but
            # they are useful when the ASR span itself is explicitly presented
            # as a name/ID.  Keep them separate so normal spans don't pay the
            # broad-scan cost or false-positive risk.
            named_near_candidates.setdefault(length, []).append((ev, py))

    return exact_index, near_candidates, named_near_candidates


def _best_cjk_match(
    span: str,
    exact_index: Dict[int, Dict[Tuple[str, ...], List[_TermEvidence]]],
    near_candidates: Dict[int, List[Tuple[_TermEvidence, Tuple[str, ...]]]],
    named_near_candidates: Dict[int, List[Tuple[_TermEvidence, Tuple[str, ...]]]],
    full_text: str = "",
    span_start: int = 0,
    span_end: int = 0,
) -> Tuple[str, _TermEvidence, float, str] | None:
    span_py = _to_pinyin(span)
    if not span_py:
        return None

    best: Tuple[str, _TermEvidence, float, str] | None = None
    length = len(span)
    named_context = _has_named_entity_context(full_text, span_start, span_end)

    candidates: List[Tuple[_TermEvidence, Tuple[str, ...], bool]] = []
    for ev in exact_index.get(length, {}).get(span_py, []):
        candidates.append((ev, span_py, True))

    if not candidates or not any(ev.text != span for ev, _py, _exact in candidates):
        seen_ev_ids = {id(ev) for ev, _py, _exact in candidates}
        near_pool = list(near_candidates.get(length, []))
        if named_context:
            near_pool.extend(named_near_candidates.get(length, []))
        for ev, target_py in near_pool:
            if id(ev) in seen_ev_ids:
                continue
            seen_ev_ids.add(id(ev))
            candidates.append((ev, target_py, False))

    span_screen_support = 0.0
    span_has_trusted_support = False
    for ev, _target_py, _exact_candidate in candidates:
        if ev.text != span:
            continue
        span_screen_support = max(span_screen_support, _screen_evidence_strength(ev))
        if (
            ev.title_count
            or ev.explicit_count
            or ev.lead_count
            or ev.standalone_count
            or ev.recent_count
        ):
            span_has_trusted_support = True

    for ev, target_py, exact_candidate in candidates:
        target = ev.text
        if target == span or len(target) != len(span):
            continue
        if _looks_like_noisy_body_cjk_term(target) and not ev.title_count:
            continue

        py_score = 1.0 if exact_candidate else _pinyin_similarity(span_py, target_py)
        overlap = _positional_overlap(span, target)
        exact_py = py_score >= 0.999
        if py_score < (0.999 if exact_candidate else 0.92):
            continue

        evidence = _screen_evidence_strength(ev)
        if span_screen_support > 0:
            candidate_trusted = bool(
                ev.title_count
                or ev.explicit_count
                or ev.lead_count
                or ev.standalone_count
                or ev.recent_count
            )
            # If the current span is itself visible on screen, do not flip it
            # to an equally weak body-only homophone from the same noisy OCR
            # block.  Still allow a stronger title/recent/standalone candidate to
            # override a weak OCR echo.
            if not candidate_trusted and evidence <= span_screen_support + 0.10:
                continue
            if span_has_trusted_support and evidence <= span_screen_support + 0.20:
                continue

        confidence = _correction_confidence(
            py_score=py_score,
            overlap=overlap,
            evidence=evidence,
            named_context=named_context,
        )
        threshold = _correction_threshold(
            length=length,
            overlap=overlap,
            py_score=py_score,
            evidence=evidence,
            ev=ev,
            named_context=named_context,
            exact_candidate=exact_candidate,
        )
        if confidence < threshold:
            continue

        reason = (
            f"pinyin={py_score:.2f}, overlap={overlap:.2f}, "
            f"evidence={evidence:.2f}, confidence={confidence:.2f}, "
            f"threshold={threshold:.2f}, title={ev.title_count}, "
            f"recent={ev.recent_count}, body={ev.body_count}, lead={ev.lead_count}, "
            f"standalone={ev.standalone_count}, "
            f"name_ctx={int(named_context)}"
        )
        if best is None or confidence > best[2]:
            best = (target, ev, confidence, reason)

    return best


def _screen_evidence_strength(ev: _TermEvidence) -> float:
    """Return a continuous 0..1-ish confidence contribution from screen evidence.

    Source type is only one signal.  Repetition, current title,
    recent memory, and standalone screen tokens can stack, but none of them alone is an unconditional pass.
    """

    strength = 0.0
    if ev.title_count:
        strength += 0.56 + min(ev.title_count - 1, 2) * 0.06
    if ev.explicit_count:
        strength += 0.34 + min(ev.explicit_count - 1, 2) * 0.04
    if ev.lead_count:
        strength += 0.32 + min(ev.lead_count - 1, 2) * 0.04
    if ev.standalone_count:
        strength += 0.26 + min(ev.standalone_count - 1, 2) * 0.04
    if ev.recent_count:
        strength += 0.24 + min(ev.recent_count - 1, 3) * 0.05
    if ev.body_count:
        strength += 0.12 + min(ev.body_count - 1, 4) * 0.04
    return min(strength, 1.0)


def _correction_confidence(
    py_score: float,
    overlap: float,
    evidence: float,
    named_context: bool,
) -> float:
    """Combine signals into a continuous confidence score."""

    context_bonus = 0.10 if named_context else 0.0
    return py_score * 0.48 + overlap * 0.30 + evidence * 0.30 + context_bonus


def _correction_threshold(
    length: int,
    overlap: float,
    py_score: float,
    evidence: float,
    ev: _TermEvidence,
    named_context: bool,
    exact_candidate: bool,
) -> float:
    """Dynamic threshold: higher risk requires stronger combined evidence."""

    threshold = 0.68

    if named_context:
        threshold -= 0.10
    if ev.title_count:
        threshold -= 0.04

    # Two-character spans are common inside ordinary phrases.  Do not ban them:
    # require more evidence unless the ASR itself says this is a name/term.
    if length <= 2:
        threshold += 0.12
        if not named_context:
            threshold += 0.16

    if overlap < 0.25:
        threshold += 0.06
    elif overlap < 0.5:
        threshold += 0.03

    if not exact_candidate or py_score < 0.999:
        threshold += 0.12

    # Very weak screen evidence can still be used in name context, but should
    # not rewrite unrelated daily text on its own.
    if evidence < 0.20 and not named_context:
        threshold += 0.12

    return max(0.50, min(threshold, 0.98))


def _has_named_entity_context(text: str, start: int, end: int) -> bool:
    """True if the ASR span is syntactically presented as a name/ID/term."""

    if not text:
        return False

    left = text[max(0, start - 12) : start]
    right = text[end : min(len(text), end + 12)]
    window = left + text[start:end] + right

    markers = (
        "名字",
        "人名",
        "角色名",
        "角色",
        "人设",
        "设定",
        "主角",
        "女主角",
        "女主",
        "声优",
        "专名",
        "目标词",
        "叫做",
        "叫作",
        "名叫",
        "称为",
        "叫",
        "问题",
        "逻辑",
        "系统",
        "功能",
        "参数",
        "材质",
        "阴影",
        "算法",
        "Shader",
        "shader",
        "ID",
        "id",
    )
    if any(marker in window for marker in markers):
        return True

    # Quoted spans are also often names/IDs in dictated tests.
    before = left[-2:]
    after = right[:2]
    return any(ch in before for ch in '“"《「') and any(ch in after for ch in '”"》」')


def _extract_source_named_spans(text: str) -> List[str]:
    """Extract explicit ASR-side name spans such as ``这个X的人设``.

    This intentionally handles only strong syntactic cues.  Broad Chinese
    segmentation would create false positives on ordinary prose and make the
    LLM guard too heavy-handed.
    """

    names: List[str] = []
    seen = set()
    for pattern in _SOURCE_NAME_PATTERNS:
        for match in pattern.finditer(text):
            name = (match.group("name") or "").strip()
            if not (2 <= len(name) <= 8):
                continue
            if name in _CJK_STOP_TERMS or name in seen:
                continue
            if _looks_like_noisy_body_cjk_term(name):
                continue
            seen.add(name)
            names.append(name)
    return names


def _best_polished_name_equivalent(
    source_name: str, polished_text: str
) -> Tuple[str, float, float] | Tuple[None, float, float]:
    """Find the most likely polished-side replacement for a source name."""

    best: Tuple[str, float, float] | Tuple[None, float, float] = (None, 0.0, 0.0)
    source_len = len(source_name)
    min_len = max(2, source_len - 1)
    max_len = min(8, source_len + 2)
    source_py = _to_pinyin(source_name)
    if not source_py:
        return best

    for match in _CJK_RE.finditer(polished_text):
        run = match.group(0)
        for size in range(min_len, min(max_len, len(run)) + 1):
            for start in range(0, len(run) - size + 1):
                candidate = run[start : start + size]
                if candidate == source_name or candidate in _CJK_STOP_TERMS:
                    continue
                overlap = _positional_overlap(source_name, candidate)
                # Need at least some literal anchor; otherwise every unrelated
                # Chinese phrase in the sentence becomes a possible candidate.
                if overlap < 0.25 and not (
                    source_len >= 3 and candidate[:2] == source_name[:2]
                ):
                    continue
                candidate_py = _to_pinyin(candidate)
                if not candidate_py:
                    continue
                py_score = _pinyin_similarity(source_py, candidate_py)
                score = py_score * 0.68 + overlap * 0.32
                best_score = best[1] * 0.68 + best[2] * 0.32
                if score > best_score:
                    best = (candidate, py_score, overlap)

    return best


def _is_phonetic_name_equivalent(py_score: float, overlap: float) -> bool:
    """True when a name change still looks like an ASR/OCR correction."""

    if py_score >= 0.92:
        return True
    if py_score >= 0.86 and overlap >= 0.50:
        return True
    return False


def _correct_latin_case(
    text: str,
    terms: Dict[str, _TermEvidence],
    max_corrections: int,
) -> Tuple[str, List[ScreenCorrection]]:
    canonical_by_lower: Dict[str, _TermEvidence] = {}
    for term, ev in terms.items():
        key = term.lower()
        current = canonical_by_lower.get(key)
        if current is None:
            canonical_by_lower[key] = ev
            continue
        if (ev.title_count, ev.total_count, len(ev.text)) > (
            current.title_count,
            current.total_count,
            len(current.text),
        ):
            canonical_by_lower[key] = ev

    replacements: List[Tuple[int, int, str, ScreenCorrection]] = []
    for match in _LATIN_RE.finditer(text):
        token = match.group(0)
        ev = canonical_by_lower.get(token.lower())
        if not ev or ev.text == token:
            continue
        correction = ScreenCorrection(
            original=token,
            corrected=ev.text,
            source=ev.source,
            score=1.0 + (0.2 if ev.title_count else 0.0),
            reason="case-insensitive screen token match",
        )
        replacements.append((match.start(), match.end(), ev.text, correction))
        if len(replacements) >= max_corrections:
            break

    if not replacements:
        return text, []

    result = text
    corrections = [r[3] for r in replacements]
    for start, end, replacement, _correction in reversed(replacements):
        result = result[:start] + replacement + result[end:]
    return result, corrections


def _display_latin_term(term: str) -> str:
    """Return the prose-friendly display form for a screen Latin term."""

    term = re.sub(r"\s+", " ", term.strip())
    if not term:
        return term
    split = _split_latin_compound(term)
    if split != term:
        return split
    if " " in term and term.islower():
        return " ".join(part.capitalize() for part in term.split())
    return term


def _looks_like_latin_term(term: str) -> bool:
    words = _latin_words(term)
    if not words:
        return False
    if len(words) >= 2:
        return all(word.lower() not in _LATIN_PHONETIC_STOP_LOWER for word in words)
    word = words[0]
    return (
        bool(_CAMEL_BOUNDARY_RE.search(word))
        or any(ch.isupper() for ch in word[1:])
        or word.lower() in _LATIN_WORD_PHONETIC_HINTS
    )


def _latin_words(term: str) -> List[str]:
    words: List[str] = []
    for token in _LATIN_TOKEN_RE.findall(term):
        split = _CAMEL_BOUNDARY_RE.split(token)
        words.extend(part for part in split if part)
    return words


def _last_latin_word(term: str) -> str:
    words = _latin_words(term)
    return words[-1] if words else ""


def _first_latin_word_confusable(source: str, target: str) -> bool:
    source_words = _latin_words(source)
    target_words = _latin_words(target)
    if not source_words or not target_words:
        return False
    left = source_words[0].lower()
    right = target_words[0].lower()
    if left == right:
        return True
    if not left or not right:
        return False
    if left[0] in "aeiou" and right[0] in "aeiou":
        return True
    return _surface_similarity(left, right) >= 0.50


def _normalize_latin_surface(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _surface_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return 1.0 - _levenshtein(left, right) / max(len(left), len(right))


def _is_short_standalone_span(text: str, start: int, end: int) -> bool:
    """True for short utterances like '埃及石头。' where a term rewrite is plausible."""

    stripped = re.sub(r"[，。！？；;,.!?\s]+", "", text or "")
    span = text[start:end]
    return bool(stripped) and stripped == span and len(span) <= 7


def _is_latin_span_standalone(text: str, start: int, end: int) -> bool:
    stripped = re.sub(r"[，。！？；;,.!?\s]+", "", text or "")
    span = re.sub(r"\s+", "", text[start:end])
    return bool(stripped) and stripped.lower() == span.lower()


@lru_cache(maxsize=4096)
def _latin_term_phonetic_aliases(term: str) -> Tuple[str, ...]:
    words = _latin_words(term)
    if not words:
        return ()

    per_word: List[Tuple[str, ...]] = []
    for word in words[:4]:
        aliases = _latin_word_phonetic_aliases(word)
        if not aliases:
            return ()
        per_word.append(aliases[:3])

    aliases = set()
    for combo in product(*per_word):
        joined = "".join(combo)
        if joined:
            aliases.add(joined)
        if len(aliases) >= 12:
            break
    return tuple(sorted(aliases, key=len))


def _latin_word_phonetic_aliases(word: str) -> Tuple[str, ...]:
    lower = re.sub(r"[^a-z0-9]+", "", word.lower())
    if not lower or lower in _LATIN_PHONETIC_STOP_LOWER:
        return ()

    hints = _LATIN_WORD_PHONETIC_HINTS.get(lower)
    fallback = _rough_english_to_pinyin_like(lower)
    if hints:
        values = list(hints)
        if fallback and fallback not in values:
            values.append(fallback)
        return tuple(values)
    return (fallback,) if fallback else ()


def _rough_english_to_pinyin_like(word: str) -> str:
    """Very small English->pinyin-ish approximation for screen-gated matching."""

    w = word.lower()
    replacements = (
        ("tion", "shen"),
        ("sion", "shen"),
        ("dge", "j"),
        ("tch", "ch"),
        ("sh", "sh"),
        ("ch", "ch"),
        ("ph", "f"),
        ("th", "s"),
        ("ck", "k"),
        ("qu", "ku"),
        ("ee", "i"),
        ("ea", "i"),
        ("oo", "u"),
        ("ow", "ou"),
        ("ou", "ao"),
        ("ai", "ei"),
        ("ay", "ei"),
        ("er", "e"),
    )
    for old, new in replacements:
        w = w.replace(old, new)
    if w.endswith("e") and len(w) > 3:
        w = w[:-1]
    w = re.sub(r"c(?=[eiy])", "s", w)
    w = re.sub(r"g(?=[eiy])", "j", w)
    w = w.replace("c", "k").replace("q", "k").replace("x", "ks")
    return re.sub(r"[^a-z0-9]+", "", w)


def _latin_phonetic_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return 1.0 - _levenshtein(left, right) / max(len(left), len(right))


@lru_cache(maxsize=16384)
def _to_pinyin(text: str) -> Tuple[str, ...]:
    if not _PYPINYIN_AVAILABLE or not text:
        return ()
    return tuple(lazy_pinyin(text, style=Style.NORMAL, errors="ignore"))


def _pinyin_similarity(left: Tuple[str, ...], right: Tuple[str, ...]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    if left == right:
        return 1.0
    joined_left = "".join(left)
    joined_right = "".join(right)
    if not joined_left or not joined_right:
        return 0.0
    distance = _levenshtein(joined_left, joined_right)
    return 1.0 - distance / max(len(joined_left), len(joined_right))


def _positional_overlap(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    same = sum(1 for a, b in zip(left, right) if a == b)
    return same / max(len(left), len(right))


def _levenshtein(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for i, c1 in enumerate(left, start=1):
        current = [i]
        for j, c2 in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (c1 != c2),
                )
            )
        previous = current
    return previous[-1]
