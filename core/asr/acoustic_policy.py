"""
ASR acoustic gate policy — thresholds and pure skip predicates.

All energy thresholds are based on **pre-DSP raw mic signal** (before the
capture-mode HPF→Gate→AGC→Limiter chain and before the mic-gain slider).
Post-DSP levels are only used where a gate explicitly compares both.

Calibration history (keep in sync with field notes):
- 2026-07-03 calibration v2: CTX peak-escape floors 0.06→0.035 (standard),
  whisper 0.022 (LIVE per-segment p95 telemetry).
- 2026-07-04: whole-segment unbuffered noise gate CPU/GPU split
  (GPU never pre-filters; CPU keeps the raw-avg gate).
- 2026-07-19: PRE_SCREEN dead-gate revival 0.001→0.002 / 1.5s→3.0s + whisper
  exemption. Data: 15-day telemetry, 11,437 decoded sessions — old gate hit 0
  (VAD segments bottom out ≈1.9s incl. 1.5s tail silence, never ≤1.5s); new
  band intercepts ~20.7% empty-segment decodes (avg 5.4s each) at 0.06% true
  content loss; flagged real-speech look-alikes were whisper-mode, hence the
  exemption (mining/report.md section B, cluster_20260719).
- 2026-07-19 (slim trial P0): context-echo Trigger 0 (CONTEXT_ECHO_MIN_RUN=30,
  find_context_echo below) + audio-derived starve-gate fallback for hint-less
  transcribe calls. Field: sherpa 0.6B echoed the whole 309-char hotword
  context inline with real speech at pre-DSP 0.0225 — normal speech energy,
  where every energy-gated trigger is disarmed and the speed trigger's
  keep-original arm protected the echo (_scratch/cluster_20260719/slim_trial).

This module is a pure structural home for magic numbers previously scattered
across qwen3_engine.py and app.py. Values must stay byte-identical to the
pre-refactor literals.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Engine-side (qwen3) thresholds
# ---------------------------------------------------------------------------

# Starve hotword/recent context when pre-DSP mean is below this.
LOW_SIGNAL_ENERGY_FOR_CTX = 0.012

# Peak escape: silence-diluted real speech keeps context when p95 clears floor.
CTX_PEAK_ESCAPE_P95 = 0.035
CTX_PEAK_ESCAPE_P95_WHISPER = 0.022

# PRE-SCREEN: skip model on short near-silence (hint < floor AND duration).
# 2026-07-19 recalibration (see history above); whisper mode is exempt via
# should_skip_pre_screen (whisper energy is naturally low, false-block risk).
PRE_SCREEN_ENERGY = 0.002
PRE_SCREEN_MAX_DURATION_S = 3.0

# Whisper post-DSP early-return (RMS of audio entering the model).
WHISPER_POST_DSP_EARLY_ENERGY = 0.002
WHISPER_POST_DSP_EARLY_MAX_DURATION_S = 3.0

# RMS normalize: only boost when energy clears this floor.
RMS_NORMALIZE_MIN_ENERGY = 0.003

# Hallucination Trigger 1 (impossible speech rate) energy split.
HALLUCINATION_SPEED_CLEAR_ENERGY = 0.015
HALLUCINATION_SPEED_LIMIT_CLEAR = 35.0
HALLUCINATION_SPEED_LIMIT_LOW = 15.0
HALLUCINATION_SPEED_MIN_CHARS = 20

# Trigger 3 / leakage leakage energy tiers.
STRICT_ENERGY = 0.008
MODERATE_ENERGY = 0.015
# When CTX peak-escape fires, Trigger 3 ceiling widens to LOW_SIGNAL_ENERGY_FOR_CTX.
# recent_leak_ceiling = LOW_SIGNAL_ENERGY_FOR_CTX if escape else STRICT_ENERGY

# Trigger 0 (context echo): minimum NORMALIZED consecutive-char run of the
# injected context appearing verbatim inside the ASR output to call it an
# echo. Field echoes are 100-300+ normalized chars long (2026-07-19 slim
# trial: full 309-char hotword block reproduced inline, 6-10x this floor);
# real dictation would need 30 consecutive normalized chars in the exact
# context order to false-positive, and even then the retry-without-context
# path re-decodes the audio instead of dropping it.
CONTEXT_ECHO_MIN_RUN = 30

# ---------------------------------------------------------------------------
# App-side gate thresholds
# ---------------------------------------------------------------------------

# Buffered final tail skip (soft-split already has text).
BUFFERED_TAIL_MAX_DURATION_S = 2.0
BUFFERED_TAIL_PRE_DSP_MAX = 0.0050
BUFFERED_TAIL_POST_MULT = 1.6
BUFFERED_TAIL_POST_FLOOR = 0.0025

# Unbuffered low-energy final skip (CPU runtime only).
UNBUFFERED_FINAL_MAX_DURATION_S = 8.5
UNBUFFERED_FINAL_P95_ESCAPE = 0.06
UNBUFFERED_FINAL_RAW_GATE_MULT = 2.5
UNBUFFERED_FINAL_RAW_GATE_FLOOR = 0.0080
UNBUFFERED_FINAL_RAW_GATE_CEIL = 0.0120
# Noisy-mode speech escape. Live clean-CPU evidence (2026-08-09) showed real
# speech at raw avg 0.00698/0.00733, p95 0.03326/0.03687, vad_max 1.0 and
# voiced ratio 0.539/0.440 being discarded by the CPU-only raw gate before
# sherpa ever ran. A peaky waveform plus sustained Silero speech confidence
# separates that case from steady music/room noise without disabling the CPU
# protection for noisy mode wholesale.
UNBUFFERED_FINAL_NOISY_VAD_MAX_FLOOR = 0.70
UNBUFFERED_FINAL_NOISY_VOICED_FLOOR = 0.35
UNBUFFERED_FINAL_NOISY_P95_TO_AVG_RATIO = 3.5
UNBUFFERED_FINAL_NOISY_P95_FLOOR = 0.020

# Interim UI gate floor (stricter than final energy_threshold).
# 2026-07-20 recalibration 0.012→0.008: 9.4h llamacpp field data showed the
# 0.012 floor skipped interim previews 506 times — nearly half of live speech
# had no realtime caption feedback. Interim is UI-only, so a false preview is
# cheap (report_Q1.md §1 route 3 / §6 lever #2, cluster_20260720/quality).
INTERIM_ENERGY_FLOOR = 0.008

# Interim gate CPU/GPU split (2026-07-26 field incident): a quiet capture
# chain puts real speech at pre-DSP avg 0.0008-0.0025 (mic volume gain feeds
# only the model — every gate reads the raw signal), so the 0.008 floor
# skipped EVERY interim of the session ("Interim skipped: low energy" on
# 100% of chunks). That silently killed realtime captions AND the fast
# wakeword path (which fires from the interim callback), making voice
# commands wait for the final commit. Same philosophy as the 2026-07-04
# final-gate split above: the GPU runtime decodes and lets the model plus
# the post-ASR text guards (filler set, hallucination check, rate guard)
# judge; the CPU runtime keeps the strict raw gate to protect its budget.
# GPU keeps only a true-silence floor so a silently held hotkey does not
# burn a decode every second.
INTERIM_ENERGY_FLOOR_GPU = 0.0002

# Interim Silero corroboration (2026-07-29 field incident): dropping the GPU
# interim floor to 0.0002 alone let every ambient-noise chunk in the
# 0.0002-0.008 band reach the decoder once per second, and Qwen3 renders
# sustained faint noise as fluent encyclopedia lines ("《小王子》是法国作家…")
# straight into the caption bubble — the interim path has no VAD joint gate
# and the text filters (filler set / legacy regex / 15 cps) never match a
# 5-6 cps generative sentence. Silero separates the bands cleanly (joint-gate
# calibration: real speech vad_max >= 0.94, fabrication fodder <= 0.67), so
# GPU interim now additionally requires the in-flight segment's Silero peak
# to clear the same 0.70 ceiling the joint gate uses. Negative sentinel
# (no stats yet) never blocks — fail-open like the energy-gate exemption.
# The faint-music band (vad 0.7-0.9) still passes; the interim decode-
# confidence check (min-logprob axis, below) is the second layer for it.
INTERIM_VAD_MAX_FLOOR_GPU = 0.70


def should_skip_interim_low_vad(
    *,
    engine_is_cpu: bool,
    vad_prob_max: float,
    floor: float = INTERIM_VAD_MAX_FLOOR_GPU,
) -> bool:
    """Skip a GPU interim decode when Silero never scored the segment as speech.

    CPU interim keeps its strict energy gate and needs no VAD arm; sentinel
    (negative) vad_prob_max means "no stats", which never blocks.
    """
    if engine_is_cpu:
        return False
    return 0 <= float(vad_prob_max) < float(floor)


def interim_energy_gate(energy_threshold: float, engine_is_cpu: bool) -> float:
    """Effective interim energy gate for the active runtime.

    CPU: max(user energy threshold, INTERIM_ENERGY_FLOOR) — unchanged.
    GPU: INTERIM_ENERGY_FLOOR_GPU only (never pre-filter by energy).
    """
    if engine_is_cpu:
        return max(float(energy_threshold), INTERIM_ENERGY_FLOOR)
    return INTERIM_ENERGY_FLOOR_GPU

# App-level post-ASR hard discard (25 chars/s).
APP_LEAKAGE_CHARS_PER_SEC = 25
APP_LEAKAGE_MIN_CHARS = 30

# Whisper Check3 (token-overlap regurgitation suppressor).
WHISPER_CHECK3_PRE_DSP_MAX = 0.005
WHISPER_CHECK3_OVERLAP = 0.7
WHISPER_CHECK3_MIN_CHARS = 6

# Empty-final rescue / soft-split defer audio energy floor.
RESCUE_DEFER_ENERGY_FLOOR = 0.003

# ---------------------------------------------------------------------------
# Anti-hallucination gates (2026-07-20, cluster_20260720/hallucination H2)
# ---------------------------------------------------------------------------
#
# Field mechanism (H1 mid-term, checkpoint_H1.md): light ambient noise at
# pre-DSP avg ~0.0009-0.0015 squeaks past the lowered 0.0010 energy gate
# (AGC judges it silence and does not lift: post-DSP ~0.0011-0.0016), the
# decoder then invents fluent encyclopedia sentences ("刘备是蜀汉的开国皇帝").
# None of the existing defenses see it: PRE_SCREEN needs pre<0.002, the
# 25 chars/s leakage cap never fires (fabrications run 5-6 cps), and the
# echo/regurgitation triggers only match *context*, not LM world knowledge.

# VAD-probability joint gate: both energies near-silent AND Silero never
# confident in the whole segment => refuse to decode. Calibration (H1
# mid-term, 7/20 llamacpp field data): fabricated segments had vad_max
# 0.36/0.44/0.61/0.67 at pre 0.00086-0.00147 / post 0.0011-0.0016, while
# real speech — including rescued quiet speech at pre 0.00119 — had
# vad_max >= 0.94 (0.24 margin to the 0.70 ceiling). Values pending H1
# final-report confirmation; whisper capture mode is exempt (Silero prob is
# naturally low for breathy speech, and all field evidence is standard mode).
VAD_JOINT_GATE_ENERGY_MAX = 0.002
VAD_JOINT_GATE_VAD_MAX_BELOW = 0.70

# Energy-gate VAD exemption (2026-07-23 field incident): when the system mic
# capture level collapses (external app touching the endpoint volume, device
# re-route, session AGC), real speech arrives at pre-DSP avg ~0.0005-0.0007 —
# 25-35x below the same user's normal 0.018 — and the flat energy gate
# silently killed 15-47s utterances Silero scored as speech (voiced_ratio
# 0.858-0.951, vad_max 0.749-1.0; 340 suspected false kills in half a day,
# 48 of them >=10s or voiced>=0.7). The 7/20 fabrication band is acoustically
# disjoint on BOTH axes: vad_max <= 0.67 and, in the same half-day telemetry,
# every legitimate noise drop had voiced_ratio <= 0.34. Requiring vad_max
# above the joint-gate ceiling (0.70) AND voiced_ratio >= 0.50 AND a >=2s
# duration keeps light-noise hallucination fodder out (short clicks / hums
# never sustain high voiced ratios) while letting collapsed-mic speech reach
# the peak-normalize stage, which lifts it before the engine decodes.
ENERGY_GATE_VAD_EXEMPT_VAD_MAX_FLOOR = VAD_JOINT_GATE_VAD_MAX_BELOW
ENERGY_GATE_VAD_EXEMPT_VOICED_FLOOR = 0.50
ENERGY_GATE_VAD_EXEMPT_MIN_DURATION_S = 2.0

# Template-sentence blacklist: the classic subtitle-corpus hallucination zoo
# ("谢谢观看" family). Only an EXACT whole-segment match (echo-normalized)
# at near-silence is dropped, so a user genuinely dictating one of these at
# speech energy is untouched. 0.002 aligns with PRE_SCREEN_ENERGY, the
# established "near-silence" definition; real dictation p10 sits at 0.0035
# (report_Q1.md §1). H1's observed Qwen3 fabrications are generative, not
# templated — this list is cheap insurance against the classic zoo plus a
# config-extensible slot (vad.template_blacklist_extra) for phrases the H1
# final report may surface.
TEMPLATE_SENTENCE_PRE_DSP_MAX = 0.002
TEMPLATE_SENTENCES = (
    "谢谢观看",
    "感谢观看",
    "谢谢收看",
    "感谢收看",
    "谢谢大家观看",
    "感谢大家观看",
    "感谢您的观看",
    "谢谢您的观看",
    "谢谢聆听",
    "感谢聆听",
    "谢谢收听",
    "感谢收听",
    "请订阅",
    "请点赞订阅",
    "点赞订阅转发",
    "请不吝点赞订阅转发打赏支持明镜与点点栏目",
    "明镜与点点栏目",
    "字幕由Amara.org社区提供",
    "本字幕由Amara.org社区提供",
    "Thank you for watching",
    "Thanks for watching",
    "Please subscribe",
)

# Decode-confidence gate (llamacpp per-token logprob stats). Two axes:
#
# AVG axis (vad.conf_gate_enabled, OFF BY DEFAULT): the -1.0 floor follows
# the faster-whisper log_prob_threshold convention but field data now shows
# it false-kills — real speech "看，我这个。" decoded at avg=-1.28 (2026-07-28
# telemetry) — so it stays telemetry-only until someone recalibrates it.
#
# MIN axis (vad.conf_gate_min_enabled, ON BY DEFAULT since 2026-07-29): the
# 2026-07-20 comment predicted "a fluent fabrication's autoregressive
# momentum may keep avg_logprob high" — 2.5 days of field telemetry (417
# inserted utterances, 14 fabrications) confirmed it and exposed the sharper
# axis: a fabrication must commit to *something* from noise, and that
# entry-point token is wildly improbable. Every fabrication had
# min_logprob <= -3.54 (cluster -4.1..-4.7); every real utterance —
# dialect, fillers, low-avg mumbles included — stayed >= -2.51. The -3.0
# floor splits the bands with >= 0.5 margin on both sides; log replay:
# 14/14 fabrications dropped, 0/403 real utterances lost.
CONF_GATE_AVG_LOGPROB_FLOOR = -1.0
CONF_GATE_MIN_LOGPROB_FLOOR = -3.0
# Acoustic arm of the combined condition: low confidence alone must never
# drop text (accents / rare names decode with low logprob too) — the strict
# near-silence tier or a weak VAD segment must corroborate. All 14 field
# fabrications carried pre-DSP < 0.0069 (weak energy), including the four
# faint-music ones whose vad_max reached 0.70-0.86.
CONF_GATE_ACOUSTIC_ENERGY_MAX = STRICT_ENERGY


def should_skip_pre_screen(
    *,
    pre_dsp_energy_hint: float,
    duration_s: float,
    capture_mode: str,
) -> bool:
    """Skip the ASR model entirely for short pre-DSP near-silence.

    Hint-only: a negative hint (sentinel -1.0, caller had no pre-DSP path)
    never skips. Whisper capture mode is exempt — its energy is naturally
    below PRE_SCREEN_ENERGY, so blocking there would drop real speech
    (2026-07-19 calibration, mining/report.md section B).
    """
    if str(capture_mode or "standard").lower() == "whisper":
        return False
    # Duration is inclusive (<=) to match the mining calibration predicate
    # (du <= DUR in report.md section B trade-off table); energy stays strict.
    return (
        pre_dsp_energy_hint >= 0
        and pre_dsp_energy_hint < PRE_SCREEN_ENERGY
        and duration_s <= PRE_SCREEN_MAX_DURATION_S
    )


def should_skip_buffered_final_tail(
    *,
    kind: str,
    buffered_text: str,
    duration_s: float,
    pre_dsp_audio_level_avg: float,
    post_dsp_audio_level_avg: float,
    energy_gate: float,
    capture_mode: str,
) -> bool:
    """Skip ASR for a near-silent final tail when soft-split text exists.

    Byte-identical to the former AriaApp._should_skip_buffered_final_tail body.
    """
    if kind != "final" or not buffered_text:
        return False
    if str(capture_mode or "standard").lower() == "whisper":
        return False
    if duration_s <= 0.0 or duration_s > BUFFERED_TAIL_MAX_DURATION_S:
        return False

    post_limit = max(float(energy_gate) * BUFFERED_TAIL_POST_MULT, BUFFERED_TAIL_POST_FLOOR)
    return (
        float(pre_dsp_audio_level_avg) < BUFFERED_TAIL_PRE_DSP_MAX
        and float(post_dsp_audio_level_avg) < post_limit
    )


def should_skip_unbuffered_low_energy_final(
    *,
    kind: str,
    buffered_text: str,
    has_deferred_audio: bool,
    duration_s: float,
    pre_dsp_audio_level_avg: float,
    pre_dsp_audio_level_p95: float,
    energy_gate: float,
    capture_mode: str,
    engine_is_cpu: bool,
    vad_prob_max: float = -1.0,
    vad_voiced_ratio: float = -1.0,
) -> bool:
    """Skip standalone final noise before it can stall a CPU decode.

    Byte-identical to the former AriaApp._should_skip_unbuffered_low_energy_final
    body (CPU-only; GPU never pre-filters).
    """
    if kind != "final" or buffered_text or has_deferred_audio:
        return False
    if not engine_is_cpu:
        return False
    if str(capture_mode or "standard").lower() == "whisper":
        return False
    if duration_s <= 0.0 or duration_s > UNBUFFERED_FINAL_MAX_DURATION_S:
        return False

    if float(pre_dsp_audio_level_p95) >= UNBUFFERED_FINAL_P95_ESCAPE:
        return False

    raw_avg = float(pre_dsp_audio_level_avg)
    raw_p95 = float(pre_dsp_audio_level_p95)
    if (
        str(capture_mode or "standard").lower() == "noisy"
        and raw_avg > 0.0
        and float(vad_prob_max) >= UNBUFFERED_FINAL_NOISY_VAD_MAX_FLOOR
        and float(vad_voiced_ratio) >= UNBUFFERED_FINAL_NOISY_VOICED_FLOOR
        and raw_p95 >= UNBUFFERED_FINAL_NOISY_P95_FLOOR
        and raw_p95
        >= raw_avg * UNBUFFERED_FINAL_NOISY_P95_TO_AVG_RATIO
    ):
        return False

    raw_gate = min(
        max(float(energy_gate) * UNBUFFERED_FINAL_RAW_GATE_MULT, UNBUFFERED_FINAL_RAW_GATE_FLOOR),
        UNBUFFERED_FINAL_RAW_GATE_CEIL,
    )
    return raw_avg < raw_gate


# ---------------------------------------------------------------------------
# Context-echo detection (Trigger 0)
# ---------------------------------------------------------------------------

# Everything that transcription may legitimately re-punctuate/re-space must
# be normalized away before comparing output against the injected context:
# the sherpa adapter converts ASCII commas to full-width before injection,
# the decoder re-inserts its own spacing/punctuation around the echoed list,
# and single-char mutations occasionally break a run (鸣潮→鸩潮), so the
# match is "longest common run over normalized text", not raw substring.
_ECHO_NORM_RE = re.compile(
    r"[\s、，,。.!！?？()（）:：;；'\"“”‘’\-—_…·<>《》\[\]【】/\\|`~*+=]+"
)


def _normalize_for_echo(text: str) -> str:
    return _ECHO_NORM_RE.sub("", text or "").casefold()


def should_skip_low_vad_joint(
    *,
    kind: str,
    pre_dsp_audio_level_avg: float,
    post_dsp_audio_level_avg: float,
    vad_prob_max: float,
    capture_mode: str,
    energy_ceiling: float = VAD_JOINT_GATE_ENERGY_MAX,
    vad_max_ceiling: float = VAD_JOINT_GATE_VAD_MAX_BELOW,
) -> bool:
    """Refuse to decode a near-silent segment Silero was never confident in.

    Runs AFTER the energy gate passed, so it only ever fires inside the
    [energy_gate, energy_ceiling) band the 2026-07-20 gate lowering opened
    up (see constants block above for the field calibration). Hard guards:

    - vad_prob_max < 0 is the "no stats" sentinel (hotkey stop without an
      in-flight segment, legacy tuple formats) — never skip without evidence.
    - whisper capture mode is exempt: Silero probability is naturally low
      for breathy speech there, and the calibration data is standard mode.
    - only 'final' / 'soft_split' dictation segments; 'selection' is an
      explicit user flow with no field evidence.
    - BOTH pre- and post-DSP averages must sit under the ceiling: the
      fabrication band shows AGC declining to lift (post≈pre), and a lifted
      post-DSP level means the chain judged it speech-like — let it decode.
    """
    if kind not in ("final", "soft_split"):
        return False
    if str(capture_mode or "standard").lower() == "whisper":
        return False
    if vad_prob_max < 0 or vad_prob_max >= float(vad_max_ceiling):
        return False
    return (
        0 <= float(pre_dsp_audio_level_avg) < float(energy_ceiling)
        and 0 <= float(post_dsp_audio_level_avg) < float(energy_ceiling)
    )


def should_exempt_energy_gate_high_vad(
    *,
    kind: str,
    duration_s: float,
    vad_prob_max: float,
    vad_voiced_ratio: float,
    vad_max_floor: float = ENERGY_GATE_VAD_EXEMPT_VAD_MAX_FLOOR,
    voiced_ratio_floor: float = ENERGY_GATE_VAD_EXEMPT_VOICED_FLOOR,
    min_duration_s: float = ENERGY_GATE_VAD_EXEMPT_MIN_DURATION_S,
) -> bool:
    """Let a below-energy-gate segment decode anyway on strong VAD evidence.

    Rescues real speech recorded through a collapsed mic level (2026-07-23
    field data, constants block above) without reopening the 7/20 light-noise
    hallucination band: BOTH Silero axes must clear their floors AND the
    segment must be sustained. Negative VAD values are the "no stats"
    sentinel — no evidence, no exemption. Capture mode is irrelevant here:
    the exemption only ever *allows* a decode, and downstream post-ASR
    triggers still screen the result.
    """
    if kind not in ("final", "soft_split"):
        return False
    if duration_s < float(min_duration_s):
        return False
    return (
        float(vad_prob_max) >= float(vad_max_floor)
        and float(vad_voiced_ratio) >= float(voiced_ratio_floor)
    )


def find_template_sentence(
    text: str,
    *,
    pre_dsp_audio_level_avg: float,
    capture_mode: str,
    extra: tuple | list = (),
    pre_dsp_max: float = TEMPLATE_SENTENCE_PRE_DSP_MAX,
) -> str:
    """Return the canonical template the whole segment equals, "" if none.

    A hit requires ALL of: non-whisper capture mode, pre-DSP average inside
    [0, pre_dsp_max) (near-silence — the segment cannot plausibly contain a
    real spoken sentence), and the echo-normalized transcript comparing
    EQUAL to an echo-normalized blacklist entry (built-ins + ``extra`` from
    vad.template_blacklist_extra). Substring matches never count: real
    speech that merely contains "谢谢观看" keeps its text.
    """
    if not text:
        return ""
    if str(capture_mode or "standard").lower() == "whisper":
        return ""
    if not (0 <= float(pre_dsp_audio_level_avg) < float(pre_dsp_max)):
        return ""
    normalized = _normalize_for_echo(text)
    if not normalized:
        return ""
    for template in tuple(TEMPLATE_SENTENCES) + tuple(extra or ()):
        if template and normalized == _normalize_for_echo(str(template)):
            return str(template)
    return ""


def should_drop_low_confidence(
    *,
    avg_logprob: float | None,
    pre_dsp_audio_level_avg: float,
    vad_prob_max: float,
    floor: float = CONF_GATE_AVG_LOGPROB_FLOOR,
    acoustic_energy_max: float = CONF_GATE_ACOUSTIC_ENERGY_MAX,
    vad_max_ceiling: float = VAD_JOINT_GATE_VAD_MAX_BELOW,
    min_logprob: float | None = None,
    min_floor: float | None = None,
) -> bool:
    """Combined decode-confidence drop predicate.

    Two independent low-confidence signals share one acoustic corroboration:

    - AVG axis: mean token logprob below ``floor`` (pass avg_logprob=None to
      disarm — callers do so unless vad.conf_gate_enabled).
    - MIN axis: worst single token below ``min_floor`` (pass min_logprob=None
      or min_floor=None to disarm). Catches fluent fabrications whose
      autoregressive momentum keeps the average respectable but whose
      noise-to-first-token commit is wildly improbable (calibration in the
      CONF_GATE_MIN_LOGPROB_FLOOR block above).

    Low confidence ALONE never drops text — accents and rare proper names
    legitimately decode with low logprob. The acoustic side must corroborate:
    pre-DSP average under the strict near-silence tier, or a segment Silero
    never scored as confident speech (vad_prob_max in [0, ceiling); negative
    sentinel = no evidence, never corroborates).
    """
    low_avg = avg_logprob is not None and float(avg_logprob) < float(floor)
    low_min = (
        min_logprob is not None
        and min_floor is not None
        and float(min_logprob) < float(min_floor)
    )
    if not (low_avg or low_min):
        return False
    weak_energy = 0 <= float(pre_dsp_audio_level_avg) < float(acoustic_energy_max)
    weak_vad = 0 <= float(vad_prob_max) < float(vad_max_ceiling)
    return weak_energy or weak_vad


def find_context_echo(
    text: str,
    context: str,
    min_run: int = CONTEXT_ECHO_MIN_RUN,
) -> str:
    """Return the first normalized >=min_run char run of ``context`` found
    verbatim inside ``text`` ("" = no echo).

    Pure function; energy-independent by design. The 2026-07-19 slim-trial
    leak happened at clear-speech energy (pre-DSP 0.0225) where every
    energy-gated leakage trigger is disarmed: the 0.6B decoder emitted real
    speech PLUS the entire injected hotword context inline. A >=min_run
    normalized run shared with the injected context is model-regurgitation
    evidence at ANY energy — real dictation reproducing 30+ consecutive
    normalized context chars in context order does not occur naturally
    (users speak entries, not the serialized list).

    Uses a sliding window over the (shorter) normalized text with plain
    substring probes into the normalized context. Window slides one char at
    a time so an echo run anywhere in mixed "speech + echo + speech" output
    is found; contexts are <=10k chars and outputs <=1k, fast enough for the
    per-utterance call.
    """
    if not text or not context:
        return ""
    t = _normalize_for_echo(text)
    c = _normalize_for_echo(context)
    if len(t) < min_run or len(c) < min_run:
        return ""
    for i in range(0, len(t) - min_run + 1):
        window = t[i : i + min_run]
        if window in c:
            return window
    return ""
