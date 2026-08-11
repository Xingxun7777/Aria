"""
ASR History Entry Composition
=============================
Pure helper that composes the unified-history ASR record fields from
per-segment pipeline artifacts.

Soft-split background: a long utterance is transcribed as several
soft-split segments whose raw texts accumulate in
``AriaApp._session_raw_segments``; the 'final' worker iteration joins them
with the tail segment's text before polish/paste. The debug session only
logs the tail segment's raw ASR text and audio duration, so the history
record must re-join the buffered chain here (BACKLOG DATA-1).
"""

from typing import NamedTuple


class AsrHistoryEntry(NamedTuple):
    """Fields for one ASR record in the unified history store."""

    input_text: str
    output_text: str
    duration_s: float


def compose_asr_history_entry(
    buffered_raw_text: str,
    tail_raw_text: str,
    final_text: str,
    tail_duration_s: float = 0.0,
    buffered_duration_s: float = 0.0,
) -> AsrHistoryEntry:
    """
    Compose history fields covering the WHOLE utterance chain.

    Args:
        buffered_raw_text: Joined raw text of all prior soft-split segments
            ("" when the utterance had no soft splits).
        tail_raw_text: Raw ASR text of the final segment only.
        final_text: Committed text (full chain, post hotword/polish).
        tail_duration_s: Audio seconds of the final segment (already
            includes deferred no-text chunks retried within it).
        buffered_duration_s: Accumulated audio seconds of the soft-split
            segments that produced ``buffered_raw_text``.

    Returns:
        AsrHistoryEntry where ``input_text`` is the full-chain raw
        transcript when known (else the final text), ``output_text`` is the
        final text only when it DIFFERS from a known raw transcript ("" when
        the raw is unknown or identical — a duplicated pair would look like
        a downstream rewrite to correction learning), and ``duration_s``
        sums the whole chain.
    """
    full_raw_text = f"{buffered_raw_text or ''}{tail_raw_text or ''}"
    final_text = final_text or ""
    input_text = full_raw_text or final_text
    output_text = (
        final_text if full_raw_text and final_text != full_raw_text else ""
    )
    try:
        duration_s = float(tail_duration_s or 0.0) + float(buffered_duration_s or 0.0)
    except (TypeError, ValueError):
        duration_s = 0.0
    return AsrHistoryEntry(
        input_text=input_text,
        output_text=output_text,
        duration_s=round(duration_s, 2),
    )
