"""
Pinyin Annotation Helper
========================
Builds a compact pinyin line for the raw ASR transcript, attached to the
quality-polish prompt when `polish.pinyin_hint` is enabled. Research shows
LLM homophone correction (的/地/得, near-homophone names) improves markedly
when the prompt carries phonetic annotations.

Pure function — no config or network dependencies — so a future local_polish
path can reuse it as-is.
"""

import re
from typing import Optional

from pypinyin import lazy_pinyin, Style

# Annotate at most this many source characters. Longer dictation would blow
# up the prompt budget for diminishing returns (homophone errors are fixed
# from local context, not the far tail).
PINYIN_SOURCE_CHAR_CAP = 200

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def build_pinyin_annotation(
    text: Optional[str], max_chars: int = PINYIN_SOURCE_CHAR_CAP
) -> str:
    """Return a single-line TONE3 pinyin annotation for `text`, or ''.

    - Returns '' when the text contains no CJK characters (pure English /
      digits carry no homophone signal worth annotating).
    - Uses Style.TONE3 (digit tones, e.g. wo3 xi3 huan1); neutral-tone
      syllables have no digit. Non-Chinese segments pass through verbatim.
    - Texts longer than `max_chars` are annotated only up to the cap and the
      line ends with an explicit truncation marker.
    """
    if not text or not _CJK_RE.search(text):
        return ""

    truncated = len(text) > max_chars
    source = text[:max_chars]
    try:
        tokens = lazy_pinyin(source, style=Style.TONE3)
    except Exception:
        return ""

    line = " ".join(t.strip() for t in tokens if t and t.strip())
    if not line:
        return ""
    if truncated:
        line += f" …（原文超长，拼音仅标注前{max_chars}字）"
    return line
