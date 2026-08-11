"""
ASR final-segment rescue policy — thresholds and pure decision predicates.

Companion to acoustic_policy.py: this module owns the *when* of the timeout
rescue chain (self-heal engine reload, cloud second-pass transcription, late
result insertion) as side-effect-free functions so every trigger condition is
unit-testable without an AriaApp instance.

Field context (2026-07-19 investigation): 900+ historical "ASR timeout after
30s" events silently dropped dictation; 92%+ happened while the GPU looked
idle, pointing at long-lived-process CUDA context degradation rather than
external pressure.  The rescue chain therefore assumes the *primary engine
object* may be poisoned and a fresh instance can recover.
"""

from __future__ import annotations

from dataclasses import dataclass

# Minimum raw-audio energy for an empty result to count as a *failure* rather
# than genuine silence.  Matches the deferred-audio rescue floor in
# acoustic_policy so both rescue paths agree on "there was real speech here".
from .acoustic_policy import RESCUE_DEFER_ENERGY_FLOOR

# Segment kinds that commit text (interim previews never trigger rescue).
FINAL_KINDS = ("final", "soft_split", "selection")

# Segment kinds eligible for the cloud second pass.  soft_split failures are
# deliberately excluded: their audio is already deferred and re-attached to
# the session's final retry (deferred_audio chain), so a per-soft-split cloud
# call would transcribe the same speech twice.  "selection" is also excluded:
# selection processing never flows through the ASR worker's failure hook, so
# listing it here would be dead code (wiring it up is future work).
CLOUD_RESCUE_KINDS = ("final",)

# Consecutive final-segment failures before the primary engine is rebuilt.
RELOAD_AFTER_CONSECUTIVE_FAILURES = 2

# Minimum spacing between self-heal reloads (debounce; reloading is expensive
# and a reload that did not help should not loop).
RELOAD_COOLDOWN_S = 600.0

# When a self-heal reload FAILS (e.g. new engine load error), the full
# cooldown would leave the user stuck with a poisoned engine for 10 minutes.
# The failed attempt only keeps this short retry window instead.
RELOAD_FAILED_RETRY_S = 60.0

# Cloud rescue text longer than this is never auto-inserted (runaway /
# hallucination guard for the second-pass engine).
CLOUD_TEXT_MAX_CHARS = 500

# Filler-only outputs from the cloud pass are dropped outright (same set as
# the worker's post-ASR noise filter, minus meaningful words).
CLOUD_FILLER_ONLY = {
    "嗯", "啊", "哦", "呃", "额", "噢", "唔", "嘶", "哼", "啧",
    "嗯嗯", "啊啊", "哦哦", "呃呃", "嗯哼", "嗯啊", "嘶嘶",
}

# A rescued transcription older than this is never auto-inserted — the user
# has almost certainly moved on (switched window / kept talking).
LATE_INSERT_MAX_AGE_S = 20.0

# Per-session deferred soft_split audio bucket capacity (mirrors the worker's
# dispatch limit).  A chunk that does NOT fit is truly lost and must be
# routed as a real failure, not "deferred".
DEFER_BUCKET_MAX = 4

DEFAULT_CLOUD_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_CLOUD_MODEL = "qwen3-asr-flash"
DEFAULT_CLOUD_TIMEOUT_S = 15.0
DEFAULT_CLOUD_MAX_AUDIO_S = 60.0


@dataclass(frozen=True)
class RescueConfig:
    """asr_rescue config block from hotwords.json."""

    enabled: bool = True
    cloud_enabled: bool = False
    api_key: str = ""
    api_url: str = DEFAULT_CLOUD_API_URL
    model: str = DEFAULT_CLOUD_MODEL
    timeout_s: float = DEFAULT_CLOUD_TIMEOUT_S
    max_audio_s: float = DEFAULT_CLOUD_MAX_AUDIO_S
    beep: bool = True

    @staticmethod
    def _as_bool(value: object, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"1", "true", "yes", "on"}:
                return True
            if v in {"0", "false", "no", "off"}:
                return False
        return default

    @staticmethod
    def _as_float(
        value: object, default: float, *, min_value: float, max_value: float
    ) -> float:
        try:
            parsed = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
        return max(min_value, min(max_value, parsed))

    @classmethod
    def from_mapping(cls, data: dict | None) -> "RescueConfig":
        from ..utils.secrets import reveal_secret

        data = data or {}
        return cls(
            enabled=cls._as_bool(data.get("enabled"), True),
            cloud_enabled=cls._as_bool(data.get("cloud_enabled"), False),
            api_key=reveal_secret(str(data.get("api_key") or "").strip()),
            api_url=str(data.get("api_url") or DEFAULT_CLOUD_API_URL).strip()
            or DEFAULT_CLOUD_API_URL,
            model=str(data.get("model") or DEFAULT_CLOUD_MODEL).strip()
            or DEFAULT_CLOUD_MODEL,
            timeout_s=cls._as_float(
                data.get("timeout_s"),
                DEFAULT_CLOUD_TIMEOUT_S,
                min_value=3.0,
                max_value=120.0,
            ),
            max_audio_s=cls._as_float(
                data.get("max_audio_s"),
                DEFAULT_CLOUD_MAX_AUDIO_S,
                min_value=1.0,
                max_value=300.0,
            ),
            beep=cls._as_bool(data.get("beep"), True),
        )


def classify_final_failure(
    *,
    kind: str,
    skip_asr: bool,
    text: str,
    timed_out: bool,
    pre_dsp_audio_level_avg: float,
    energy_gate: float,
    engine_error: bool = False,
) -> str:
    """Classify one worker segment outcome.

    Returns "timeout", "error" (engine raised), "empty", or "" (no failure).
    Empty text only counts as a failure when the raw mic energy says real
    speech was present — otherwise "heard nothing" is the correct result,
    not a fault.
    """
    if kind not in FINAL_KINDS or skip_asr:
        return ""
    if engine_error:
        return "error"
    if timed_out:
        return "timeout"
    if text:
        return ""
    energy_floor = max(float(energy_gate), RESCUE_DEFER_ENERGY_FLOOR)
    if float(pre_dsp_audio_level_avg) >= energy_floor:
        return "empty"
    return ""


def will_defer_soft_split_audio(
    *,
    skip_asr: bool,
    audio_samples: int,
    pre_dsp_audio_level_avg: float,
    energy_gate: float,
    asr_time_ms: float | None,
    engine_label: str,
) -> bool:
    """Mirror of the worker's soft_split deferred-audio predicate.

    A deferred soft_split is NOT a loss: its audio rides the session's final
    retry, so the failure hook must not count it toward the self-heal streak.
    Must stay logic-identical to the `should_defer_audio` expression in
    AriaApp._asr_worker's soft_split dispatch.
    """
    return (
        not skip_asr
        and int(audio_samples) >= 1600
        and float(pre_dsp_audio_level_avg)
        >= max(float(energy_gate), RESCUE_DEFER_ENERGY_FLOOR)
        and (
            asr_time_ms is None
            or float(asr_time_ms) >= 3000
            or "timeout" in str(engine_label)
            or str(engine_label).startswith("primary_long_segment")
        )
    )


def resolve_failure_action(
    *,
    failure_kind: str,
    kind: str,
    has_prior_buffered_text: bool,
    will_defer_audio: bool,
    defer_bucket_full: bool = False,
) -> str:
    """Route one classified failure. Returns:

    - "count":    real loss — count streak, run rescue chain, notify.
    - "count_bucket_full": soft_split that WOULD defer but the session's
                  deferred-audio bucket is full — the chunk is dropped, so
                  it is a real loss (counts like "count", distinct telemetry).
    - "deferred": soft_split whose audio rides the final retry — telemetry only.
    - "partial":  final tail failed but buffered soft-split text still commits
                  (user gets output) — telemetry only, no rescue, no toast.
    - "":         not a failure.
    """
    if not failure_kind:
        return ""
    if kind == "soft_split" and will_defer_audio:
        if defer_bucket_full:
            return "count_bucket_full"
        return "deferred"
    if kind == "final" and has_prior_buffered_text:
        return "partial"
    return "count"


def should_notify_failure(
    *,
    failure_kind: str,
    voiced_ratio: float,
    consecutive_failures: int,
) -> bool:
    """Only surface confirmed engine faults, never ambiguous empty segments.

    An empty decode may be a pause, a click, background sound, or speech the
    recognizer could not recover. VAD ratio and streak length do not make that
    distinction reliable enough for an intrusive red warning. The telemetry
    and self-heal chain still process those events silently; explicit timeout
    and engine-error failures remain visible.
    """
    del voiced_ratio, consecutive_failures
    return failure_kind in ("timeout", "error")


def sanitize_cloud_rescue_text(text: str) -> tuple[str, str]:
    """Cheap structural hygiene for cloud second-pass output.

    Returns (verdict, cleaned_text): verdict is "ok", "too_long" (send to
    history, not caret) or "junk" (drop: empty / punctuation-only /
    filler-only).  The semantic hallucination pass (pattern regexes) lives in
    AriaApp._is_hallucination — it is stateless, just already implemented and
    maintained there.
    """
    import re

    cleaned = (text or "").strip()
    if not cleaned:
        return "junk", ""
    stripped = re.sub(r"[，。！？、,\.!\?;；:：\s]+", "", cleaned)
    if not stripped:
        return "junk", cleaned
    # Collapse consecutive repeats before the set lookup so "嗯嗯嗯" and
    # "啊啊啊啊" match their single-char filler entry.  Real words that
    # collapse to a non-filler ("谢谢" -> "谢") are unaffected.
    collapsed = re.sub(r"(.)\1+", r"\1", stripped)
    if stripped in CLOUD_FILLER_ONLY or collapsed in CLOUD_FILLER_ONLY:
        return "junk", cleaned
    if len(cleaned) > CLOUD_TEXT_MAX_CHARS:
        return "too_long", cleaned
    return "ok", cleaned


def should_trigger_engine_reload(
    *,
    consecutive_failures: int,
    now: float,
    last_reload_at: float,
    threshold: int = RELOAD_AFTER_CONSECUTIVE_FAILURES,
    cooldown_s: float = RELOAD_COOLDOWN_S,
    backend_dead: bool = False,
) -> bool:
    """Self-heal gate: N consecutive failures outside the cooldown window.

    backend_dead bypasses the cooldown (not the streak threshold): when an
    external backend process (llama-server) has verifiably exited, a reload
    is the ONLY possible recovery and the anti-storm debounce would just
    leave the user without ASR for up to 10 minutes.
    """
    if consecutive_failures < max(1, int(threshold)):
        return False
    if backend_dead:
        return True
    if last_reload_at > 0 and (now - last_reload_at) < float(cooldown_s):
        return False
    return True


def should_attempt_cloud_rescue(
    *,
    config: RescueConfig,
    kind: str,
    audio_duration_s: float,
) -> bool:
    """Cloud second-pass gate.  Zero behavior without an opted-in key."""
    if not config.enabled or not config.cloud_enabled or not config.api_key:
        return False
    if kind not in CLOUD_RESCUE_KINDS:
        return False
    if audio_duration_s <= 0.0 or audio_duration_s > config.max_audio_s:
        return False
    return True


def should_insert_late_result(
    *,
    now: float,
    segment_done_at: float,
    last_success_insert_at: float,
    has_pending_work: bool,
    max_age_s: float = LATE_INSERT_MAX_AGE_S,
) -> bool:
    """Decide whether an async rescue result may still be typed at the caret.

    Insert only when the original segment ended recently, nothing else was
    successfully inserted since (would create out-of-order text), and no new
    segment is in flight.  Everything else goes to history.
    """
    if now - segment_done_at > float(max_age_s):
        return False
    if last_success_insert_at > segment_done_at:
        return False
    if has_pending_work:
        return False
    return True
