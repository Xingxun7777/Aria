"""Per-utterance loudness normalization applied right before ASR.

Position in the pipeline (see app.py ``_asr_worker``):

    raw mic -> VAD (capture.py, raw stream, untouched)
        -> [worker] pre-DSP stats -> capture-mode DSP chain (dsp.py)
        -> energy gates (raw/post-DSP thresholds, untouched)
        -> THIS MODULE (opt-in: audio.asr_gain_normalize)
        -> engine.transcribe() for every engine (torch / sherpa / llamacpp)

Why a separate stage instead of extending ``dsp.apply_agc``:
- AGC is a time-varying envelope follower tuned for far-field rescue. It runs
  inside the capture-mode chain BEFORE the energy gates, so its output level
  feeds gate thresholds; changing its target would shift gate semantics.
- This stage is ONE scalar gain per utterance (no dynamics, no pumping, no
  waveform shaping), applied AFTER the gates — gate/VAD behavior is
  bit-identical whether the feature is on or off.

Interaction with the engine-side RMS normalize
(``qwen3_engine.transcribe``: target_rms=0.05, lift-only, applies only when
its computed gain > 1.1): after peak normalization to -3dBFS, speech RMS
lands well above 0.05, so the engine stage becomes a no-op instead of
double-boosting. Segments this module skips (near-silence, see
``MIN_RMS_FOR_GAIN``) keep the engine stage's original behavior.

All functions are pure NumPy, float32 in/out. A scalar float32 multiply with
gain <= 10x cannot overflow, and gain = target_peak/peak guarantees the
output peak lands exactly on target (< 1.0), so no clipping by construction;
a final clip stays as cheap insurance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..asr.acoustic_policy import RMS_NORMALIZE_MIN_ENERGY

DEFAULT_TARGET_PEAK_DBFS = -3.0
DEFAULT_MAX_GAIN_DB = 20.0

# Below this RMS the segment is noise/near-silence by the engine's own
# definition (acoustic_policy.RMS_NORMALIZE_MIN_ENERGY = 0.003): boosting it
# would raise the noise floor into a "noise wall" AND defeat the engine's
# whisper-mode near-silence early-return (post-DSP RMS < 0.002 check in
# qwen3_engine.transcribe) by lifting leaked silence above that threshold.
MIN_RMS_FOR_GAIN = RMS_NORMALIZE_MIN_ENERGY

# Skip micro-gains: below ~0.5dB the level change is inaudible to the model
# and not worth an array copy.
_MIN_APPLY_GAIN_DB = 0.5

# Config clamps (template values live in config/hotwords.template.json).
_TARGET_PEAK_DBFS_MIN = -20.0
_TARGET_PEAK_DBFS_MAX = -0.5
_MAX_GAIN_DB_MIN = 0.0
_MAX_GAIN_DB_MAX = 40.0


def _as_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(lo, min(hi, parsed))


@dataclass(frozen=True)
class AsrGainConfig:
    """Parsed ``audio.asr_gain_*`` block (see hotwords.template.json)."""

    enabled: bool = False
    target_peak_dbfs: float = DEFAULT_TARGET_PEAK_DBFS
    max_gain_db: float = DEFAULT_MAX_GAIN_DB

    @classmethod
    def from_mapping(cls, audio_cfg: dict | None) -> "AsrGainConfig":
        """Build from the hotwords.json ``audio`` block, clamped to safe ranges.

        Keys (flat, inside the audio block):
        - asr_gain_normalize: bool, master switch (default False)
        - target_peak_dbfs: float, peak normalization target (default -3.0)
        - max_gain_db: float, hard cap on applied boost (default 20.0)
        """
        audio_cfg = audio_cfg or {}
        return cls(
            enabled=bool(audio_cfg.get("asr_gain_normalize", False)),
            target_peak_dbfs=_as_float(
                audio_cfg.get("target_peak_dbfs"),
                DEFAULT_TARGET_PEAK_DBFS,
                _TARGET_PEAK_DBFS_MIN,
                _TARGET_PEAK_DBFS_MAX,
            ),
            max_gain_db=_as_float(
                audio_cfg.get("max_gain_db"),
                DEFAULT_MAX_GAIN_DB,
                _MAX_GAIN_DB_MIN,
                _MAX_GAIN_DB_MAX,
            ),
        )


def apply_peak_normalize(
    audio: np.ndarray,
    *,
    target_peak_dbfs: float = DEFAULT_TARGET_PEAK_DBFS,
    max_gain_db: float = DEFAULT_MAX_GAIN_DB,
    min_rms: float = MIN_RMS_FOR_GAIN,
) -> tuple[np.ndarray, float]:
    """Per-utterance scalar peak normalization.

    Rules (in decision order):
    1. Empty / all-zero / non-finite-peak segments pass through untouched.
    2. RMS below ``min_rms`` passes through untouched — near-silence must
       never be amplified into a noise wall (see MIN_RMS_FOR_GAIN).
    3. Quiet segments (peak below target) are boosted by a single scalar
       gain up to the target peak, clamped at ``max_gain_db``. Gains under
       ~0.5dB are skipped as no-ops.
    4. Segments already at/above target — including clipped ones (peak at
       1.0) — pass through untouched: attenuating a clipped waveform does
       not repair it, and "loud enough" needs no help. The clamp in rule 3
       means clipped audio is also never boosted further.
    5. Out-of-range audio (peak > 1.0, possible only on paths that skipped
       the DSP limiter) is pulled back down to the target peak.

    Returns:
        (audio, applied_gain_db) — audio is float32; applied_gain_db is 0.0
        whenever the segment passed through untouched.
    """
    a = np.asarray(audio)
    if a.dtype != np.float32:
        a = a.astype(np.float32)
    if a.size == 0:
        return a, 0.0

    peak = float(np.max(np.abs(a)))
    if not math.isfinite(peak) or peak <= 0.0:
        return a, 0.0

    # float64 accumulator: float32 squares of a long segment would lose
    # precision (and can't represent the sum tightly) — same pattern as
    # dsp.py's cumsum-based RMS helpers.
    rms = float(np.sqrt(np.mean(np.square(a, dtype=np.float64))))
    if rms < float(min_rms):
        return a, 0.0

    # Never target at/above full-scale even if the caller passed a bad value.
    target_peak = min(10.0 ** (float(target_peak_dbfs) / 20.0), 0.999)
    gain = target_peak / peak

    if gain >= 1.0:
        gain = min(gain, 10.0 ** (float(max_gain_db) / 20.0))
        gain_db = 20.0 * math.log10(gain)
        if gain_db < _MIN_APPLY_GAIN_DB:
            return a, 0.0
    elif peak <= 1.0:
        # Already at/above target but within legal range: leave it alone.
        return a, 0.0
    else:
        # peak > 1.0: pull back into range (gain = target_peak / peak < 1).
        gain_db = 20.0 * math.log10(gain)

    out = a * np.float32(gain)
    np.clip(out, -1.0, 1.0, out=out)
    return out, float(gain_db)
