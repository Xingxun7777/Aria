"""Promotion policy: correction events → auto-hotword pending-pool records.

Pure functions only — no I/O, no imports from core.hotword. The contract
with the existing hotword system is structural: `merge_into_pending_pool`
takes/returns the exact payload shape `SessionHotwordTracker` persists to
`data/auto_hotwords.json` ({version, saved_at, last_review_at, terms}),
so wiring code can load → transform → hand back to the tracker's own
atomic writer without this module ever touching that file.

Rules (design doc §4, after talax-dictation's 3-strike scheme):
  * a pattern is (normalized original → normalized corrected);
    normalization folds case / punctuation / whitespace so surface
    variants count as the same pattern;
  * weighted count >= min_count promotes (explicit user corrections
    weigh EXPLICIT_WEIGHT, passive signals weigh 1);
  * a reverse correction (corrected → original) is a hard
    counterexample and vetoes the pattern;
  * conflicting targets for the same original go to arbitration: the
    dominant target must hold >= DOMINANCE_NUM/DOMINANCE_DEN of the
    original's weighted mass, else nothing promotes (wait for evidence).
"""

from __future__ import annotations

import datetime as _dt
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from .correction_store import SOURCE_EXPLICIT, CorrectionEvent

# Mirrors SessionHotwordTracker.MIN_COUNT_FOR_REVIEW (=3). Duplicated on
# purpose: this module must not import core.hotword (zero-coupling until the
# wiring phase), and the merge boosts counts to this floor so promoted terms
# are immediately eligible for the existing LLM review batch.
PENDING_REVIEW_MIN_COUNT = 3

# Mirrors SessionHotwordTracker.MAX_TITLE_SAMPLES — evidence strings ride in
# the entry's `titles` list, which the existing reviewer prompt already shows
# to the LLM.
MAX_EVIDENCE_SAMPLES = 5

# One explicit (user-initiated) correction is worth this many passive
# observations — the user typed the fix by hand, intent is unambiguous.
EXPLICIT_WEIGHT = 3

# Conflict arbitration: dominant target must hold >= 2/3 of the weighted
# corrections recorded for that original.
DOMINANCE_NUM = 2
DOMINANCE_DEN = 3

POOL_SOURCE_TAG = "correction_learning"

_STATUS_PENDING = "pending"
_STATUS_APPROVED = "approved"
_STATUS_REJECTED = "rejected"


# ------------------------------------------------------------ normalization


def normalize_text(s: str) -> str:
    """Fold case / punctuation / whitespace into one equivalence class.

    NFKC first (full-width → half-width, compatibility forms), then
    casefold, then strip every Unicode punctuation/symbol-ish separator and
    collapse whitespace. "SDF" / "sdf" / " s.d.f " all normalize alike.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).casefold()
    out: list[str] = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat.startswith("P") or cat.startswith("Z") or ch.isspace():
            continue
        out.append(ch)
    return "".join(out)


def pattern_key(event: CorrectionEvent) -> tuple[str, str]:
    """Identity of a correction pattern after normalization."""
    return (normalize_text(event.original), normalize_text(event.corrected))


def _has_cjk(s: str) -> bool:
    return any(
        0x4E00 <= ord(c) <= 0x9FFF or 0x3400 <= ord(c) <= 0x4DBF for c in s
    )


def _is_promotable_pattern(norm_original: str, norm_corrected: str) -> bool:
    """Structural gate before counting even starts.

    * empty sides never promote;
    * original == corrected after normalization means the user only fixed
      case/punctuation — formatting, not a hotword signal;
    * the corrected form must look like a term: contains CJK, or is an
      alphanumeric token of >= 2 chars (single letters are noise).
    """
    if not norm_original or not norm_corrected:
        return False
    if norm_original == norm_corrected:
        return False
    if _has_cjk(norm_corrected):
        return True
    return len(norm_corrected) >= 2


def _event_weight(event: CorrectionEvent) -> int:
    return EXPLICIT_WEIGHT if event.source == SOURCE_EXPLICIT else 1


# ---------------------------------------------------------------- promotion


@dataclass
class Promotion:
    """A correction pattern that cleared the promotion bar."""

    term: str  # dominant raw surface form of `corrected` (what enters the pool)
    original: str  # dominant raw surface form of `original`
    count: int  # weighted observation count
    first_ts: str = ""
    last_ts: str = ""
    evidence: list[str] = field(default_factory=list)  # review-evidence strings


def evaluate(
    events: Iterable[CorrectionEvent],
    *,
    min_count: int = PENDING_REVIEW_MIN_COUNT,
) -> list[Promotion]:
    """Pure promotion judgement over the whole event history.

    Deterministic: output order is (weighted count desc, term asc).
    """
    events = [e for e in events if e.original.strip() and e.corrected.strip()]

    # Group events by normalized pattern; track per-original target mass for
    # arbitration and the set of patterns for counterexample lookup.
    by_pattern: dict[tuple[str, str], list[CorrectionEvent]] = defaultdict(list)
    for event in events:
        key = pattern_key(event)
        by_pattern[key].append(event)

    pattern_weights: dict[tuple[str, str], int] = {
        key: sum(_event_weight(e) for e in evs) for key, evs in by_pattern.items()
    }
    mass_by_original: dict[str, int] = defaultdict(int)
    for (norm_orig, _norm_corr), weight in pattern_weights.items():
        mass_by_original[norm_orig] += weight

    promotions: list[Promotion] = []
    for (norm_orig, norm_corr), evs in by_pattern.items():
        if not _is_promotable_pattern(norm_orig, norm_corr):
            continue
        weight = pattern_weights[(norm_orig, norm_corr)]
        if weight < min_count:
            continue
        # Hard counterexample: the reverse correction was ever observed.
        if (norm_corr, norm_orig) in by_pattern:
            continue
        # Conflict arbitration across all targets of this original.
        total_mass = mass_by_original[norm_orig]
        if weight * DOMINANCE_DEN < total_mass * DOMINANCE_NUM:
            continue

        term = _dominant_surface(e.corrected for e in evs)
        original = _dominant_surface(e.original for e in evs)
        timestamps = sorted(e.ts for e in evs if e.ts)
        evidence: list[str] = []
        for e in evs:
            sample = f"修正学习: 「{e.original}」→「{e.corrected}」"
            if e.context:
                sample += f"（{e.context}）"
            if sample not in evidence:
                evidence.append(sample)
            if len(evidence) >= MAX_EVIDENCE_SAMPLES:
                break
        promotions.append(
            Promotion(
                term=term,
                original=original,
                count=weight,
                first_ts=timestamps[0] if timestamps else "",
                last_ts=timestamps[-1] if timestamps else "",
                evidence=evidence,
            )
        )

    promotions.sort(key=lambda p: (-p.count, p.term))
    return promotions


def _dominant_surface(raw_forms: Iterable[str]) -> str:
    """Most common raw spelling wins; ties break deterministically."""
    counter = Counter(s.strip() for s in raw_forms if s.strip())
    if not counter:
        return ""
    best = max(sorted(counter), key=lambda s: counter[s])
    return best


# ------------------------------------------------- pending-pool integration


def build_pool_entry(promotion: Promotion, now: str = "") -> dict:
    """Tracker-compatible `terms` entry for a promoted correction."""
    if not now:
        now = _dt.datetime.now().isoformat(timespec="seconds")
    return {
        "count": max(int(promotion.count), PENDING_REVIEW_MIN_COUNT),
        "first": promotion.first_ts or now,
        "last": promotion.last_ts or now,
        "status": _STATUS_PENDING,
        "titles": list(promotion.evidence[:MAX_EVIDENCE_SAMPLES]),
        "source": POOL_SOURCE_TAG,
        "correction_original": promotion.original,
    }


def merge_into_pending_pool(
    pool_payload: dict | None,
    promotions: Iterable[Promotion],
    now: str = "",
) -> tuple[dict, dict]:
    """Merge promotions into an auto_hotwords.json payload. Pure function.

    Contract with the existing tracker (session_tracker.py):
      * rejected is a permanent blacklist — never resurrected;
      * approved terms are already injected — left untouched;
      * existing pending terms get their count boosted to the review floor
        and correction evidence appended to `titles` (capped);
      * new terms enter as pending with source="correction_learning";
      * every field the tracker persists (version, last_review_at, other
        terms' entries) passes through unchanged.

    Returns (new_payload, report). Input payload is not mutated.
    """
    if not now:
        now = _dt.datetime.now().isoformat(timespec="seconds")

    payload = dict(pool_payload or {})
    terms_src = payload.get("terms")
    terms: dict[str, dict] = {}
    if isinstance(terms_src, dict):
        terms = {k: dict(v) for k, v in terms_src.items() if isinstance(v, dict)}
    payload["terms"] = terms

    report = {"added": 0, "boosted": 0, "skipped_rejected": 0, "skipped_approved": 0}

    for promotion in promotions:
        term = (promotion.term or "").strip()
        if not term:
            continue
        entry = terms.get(term)
        if entry is None:
            terms[term] = build_pool_entry(promotion, now=now)
            report["added"] += 1
            continue
        status = entry.get("status", _STATUS_PENDING)
        if status == _STATUS_REJECTED:
            report["skipped_rejected"] += 1
            continue
        if status == _STATUS_APPROVED:
            report["skipped_approved"] += 1
            continue
        # Existing pending term: make it review-eligible and attach evidence.
        entry["count"] = max(int(entry.get("count", 0)), PENDING_REVIEW_MIN_COUNT)
        entry["last"] = now
        titles = list(entry.get("titles", []))
        for sample in promotion.evidence:
            if sample not in titles:
                titles.append(sample)
        # Cap like the tracker does (keep the newest samples).
        if len(titles) > MAX_EVIDENCE_SAMPLES:
            titles = titles[-MAX_EVIDENCE_SAMPLES:]
        entry["titles"] = titles
        report["boosted"] += 1

    return payload, report
