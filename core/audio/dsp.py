"""Audio DSP for far-field capture.

Three independent, vectorized building blocks. All functions accept and return
np.float32 mono audio at 16kHz (Aria's standard ASR sample rate). Each is a
pure function — no global state — so they can be composed in any order or
A/B-tested via tools/audio_lab.py.

Order in the production pipeline (when wired into capture._audio_callback):
    raw → HPF → AGC → soft limiter → VAD → ASR

Why this order:
- HPF first: kill low-frequency rumble (HVAC, fan, mic handling) BEFORE the
  AGC measures envelope. Otherwise rumble inflates RMS and AGC under-gains
  the actual speech.
- AGC second: lift weak far-field speech to a consistent target level so VAD
  and ASR see the same dynamics whether the user is 0.3m or 2m away.
- Limiter last: catch any AGC overshoot + sudden loud transients (cough,
  door slam) without clipping. Tanh soft knee, no hard digital clipping.

CPU cost: under 5ms for 10s @ 16kHz on a modern CPU. Scales linearly.
"""

from __future__ import annotations

import math

import numpy as np


# ─────────────────────────────────────────────────────────────────────
# 0. Pure-NumPy replacements for the former SciPy dependencies
# ─────────────────────────────────────────────────────────────────────


def _butter2_highpass_sos(cutoff_hz: float, sample_rate: float) -> np.ndarray:
    """Second-order Butterworth high-pass SOS, pure NumPy.

    Reproduces ``scipy.signal.butter(2, cutoff_hz, btype='highpass',
    fs=sample_rate, output='sos')`` to machine precision via the bilinear
    transform of the normalized analog prototype  H(s) = s^2 / (s^2 + sqrt(2)*s + 1).

    The digital biquad coefficients (a0-normalized) are:
        K  = tan(pi * cutoff_hz / sample_rate)        # pre-warped cutoff
        a0 = 1 + sqrt(2)*K + K^2
        b  = [ 1, -2,  1] / a0
        a  = [ a0, 2(K^2-1), 1 - sqrt(2)*K + K^2 ] / a0
    """
    K = math.tan(math.pi * cutoff_hz / sample_rate)
    sqrt2 = math.sqrt(2.0)
    a0 = 1.0 + sqrt2 * K + K * K
    b0 = 1.0 / a0
    b1 = -2.0 / a0
    b2 = 1.0 / a0
    a1 = (2.0 * (K * K - 1.0)) / a0
    a2 = (1.0 - sqrt2 * K + K * K) / a0
    return np.array([[b0, b1, b2, 1.0, a1, a2]], dtype=np.float64)


# Precomputed SOS for the two fixed (order=2, fs=16000) designs the presets use.
# Hard-coded from the exact scipy.signal.butter output (see module docstring).
# Keyed by (order, cutoff_hz, sample_rate). Used as a fast path / source of truth;
# _butter2_highpass_sos reproduces these identically for any other order-2 combo.
_FIXED_SOS: dict[tuple[int, float, int], np.ndarray] = {
    (2, 80.0, 16000): np.array(
        [
            [
                0.9780304792065595,
                -1.956060958413119,
                0.9780304792065595,
                1.0,
                -1.9555782403150352,
                0.9565436765112031,
            ]
        ],
        dtype=np.float64,
    ),
    (2, 100.0, 16000): np.array(
        [
            [
                0.9726138984998436,
                -1.9452277969996872,
                0.9726138984998436,
                1.0,
                -1.9444776577670935,
                0.9459779362322813,
            ]
        ],
        dtype=np.float64,
    ),
}


def _sosfilt(sos: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Cascade of biquad sections, Direct Form II transposed, zero ICs.

    Equivalent to ``scipy.signal.sosfilt(sos, x)`` (bit-exact for the float64
    path) for 1-D input. Each row of ``sos`` is [b0, b1, b2, a0, a1, a2] with
    a0 == 1 (scipy normalizes SOS this way). The recurrence per section is:

        y[n]  = b0*x[n] + z1
        z1    = b1*x[n] - a1*y[n] + z2
        z2    = b2*x[n] - a2*y[n]

    An IIR biquad is inherently sequential (each output feeds the next state),
    so this is an O(N) loop per section — the same cost class as the AGC/gate
    envelope loops in this module. Runs on a few-second segment, not realtime.
    """
    sos = np.asarray(sos, dtype=np.float64)
    y = np.asarray(x, dtype=np.float64).copy()
    for b0, b1, b2, _a0, a1, a2 in sos:
        z1 = 0.0
        z2 = 0.0
        out = np.empty_like(y)
        for i in range(y.shape[0]):
            xn = y[i]
            yn = b0 * xn + z1
            z1 = b1 * xn - a1 * yn + z2
            z2 = b2 * xn - a2 * yn
            out[i] = yn
        y = out
    return y


def _uniform_filter1d(arr: np.ndarray, size: int, mode: str = "reflect") -> np.ndarray:
    """Moving average — pure-NumPy drop-in for scipy.ndimage.uniform_filter1d.

    Reproduces SciPy's default behaviour (``mode='reflect'``, ``origin=0``)
    exactly, computed as a sliding-window mean via cumulative sum:

      - Window length ``size``. For even ``size`` SciPy biases the window one
        sample to the LEFT (origin=0): left = size//2, right = size-1-left.
      - Edge handling:
          mode='reflect' (SciPy default) = half-sample symmetric reflection,
              i.e. (d c b a | a b c d | d c b a). NumPy calls this 'symmetric'.
          mode='nearest' = clamp to edge value. NumPy calls this 'edge'.

    Verified bit-tight against scipy.ndimage.uniform_filter1d across odd/even
    sizes and both modes. Output dtype is float64.
    """
    a = np.asarray(arr, dtype=np.float64)
    if size <= 1:
        return a.copy()
    left = size // 2  # origin=0 convention
    right = size - 1 - left
    np_mode = {"reflect": "symmetric", "nearest": "edge"}.get(mode)
    if np_mode is None:
        raise ValueError(f"unsupported mode {mode!r}; supported: 'reflect', 'nearest'")
    padded = np.pad(a, (left, right), mode=np_mode)
    csum = np.concatenate(([0.0], np.cumsum(padded)))
    n = a.shape[0]
    window_sum = csum[size : size + n] - csum[0:n]
    return window_sum / size


# Public re-export under the historical SciPy name so any caller that imported
# ``uniform_filter1d`` from this module continues to work without SciPy.
uniform_filter1d = _uniform_filter1d


# ─────────────────────────────────────────────────────────────────────
# 1. High-pass filter
# ─────────────────────────────────────────────────────────────────────


def apply_highpass(
    audio: np.ndarray,
    sample_rate: int = 16000,
    cutoff_hz: float = 80.0,
    order: int = 2,
) -> np.ndarray:
    """Butterworth high-pass filter.

    cutoff_hz=80 is the safe default — lower than male fundamental (~85Hz for
    deep voices) so it doesn't touch speech, but high enough to crush:
      - HVAC rumble (40-60Hz)
      - Desk vibration (50-80Hz)
      - Mic body handling thumps (30-100Hz peak, mostly <80Hz)
      - Fan whoosh (broadband, but the worst part is <100Hz)

    Order 2 = 12dB/oct rolloff. Cheap, no audible phase artifacts on speech.
    """
    if cutoff_hz <= 0 or cutoff_hz >= sample_rate / 2:
        return audio.astype(np.float32, copy=False)
    key = (int(order), float(cutoff_hz), int(sample_rate))
    sos = _FIXED_SOS.get(key)
    if sos is None:
        if int(order) != 2:
            raise ValueError(
                f"apply_highpass supports order=2 only in the NumPy-only "
                f"build; got order={order}"
            )
        sos = _butter2_highpass_sos(float(cutoff_hz), float(sample_rate))
    out = _sosfilt(sos, audio).astype(np.float32, copy=False)
    return out


# ─────────────────────────────────────────────────────────────────────
# 2. Automatic Gain Control
# ─────────────────────────────────────────────────────────────────────


def apply_agc(
    audio: np.ndarray,
    sample_rate: int = 16000,
    target_rms: float = 0.10,
    max_gain_db: float = 25.0,
    window_ms: float = 30.0,
    attack_ms: float = 5.0,
    release_ms: float = 300.0,
    noise_floor: float = 0.001,
    silence_floor_ratio: float = 0.25,
) -> np.ndarray:
    """Asymmetric envelope-follower AGC.

    The purpose is far-field rescue: when the user is 2m from the mic and the
    waveform peaks at 0.05-0.10, lift it to the same operating range as a
    near-field 0.3-0.5 signal so VAD and the ASR encoder see consistent
    energy.

    Why not symmetric smoothing (the previous design): a centered moving
    average over the gain envelope smears the silence→speech boundary, so
    the first 30-50ms of word onset is under-amplified. Listeners report
    this as "the first character gets eaten" — clearly audible on plosives
    and unvoiced consonants whose energy is concentrated in the very onset.

    Asymmetric envelope follower fixes that, but the convention here uses
    user-mental-model naming, NOT classic compressor naming:
      - attack_ms = how fast gain RISES toward a higher target. This fires
        on silence→speech onset and on softer-syllable transitions.  Fast
        attack (default 5ms) = the first character is at full gain by the
        time the listener can perceive it.
      - release_ms = how fast gain FALLS toward a lower target. This fires
        on speech→silence transitions and on loud-syllable peaks. Default
        50ms is fast enough to avoid silence-pumping yet slow enough to
        let louder syllables ride above the AGC's nominal target without
        being immediately squashed.

    (Note: this is the OPPOSITE of the compressor convention where attack
    means gain-dropping. For AGC where the user-visible failure mode is
    "first character eaten", naming attack as the onset-response path is
    less surprising.)

    Args:
        target_rms: desired RMS level (0.10 ≈ -20dBFS, comfortable for ASR).
        max_gain_db: hard cap on lift, prevents pumping mostly-silence into
            full-amplitude noise. 25dB ≈ 17.8x linear, enough to bring a 2m
            signal to near-field level without amplifying the room tone past
            usability.
        window_ms: RMS measurement window. 30ms is one-syllable scale —
            short enough to catch onsets, long enough to be stable.
        attack_ms: time constant for gain RISING (target > state). 5ms is
            the parameter that fixes the first-character-eaten problem.
        release_ms: time constant for gain FALLING (target < state). 50ms
            is fast enough that inter-word silence is not pumped, slow
            enough that loud syllables aren't instantly choked.
        noise_floor: below this RMS the target gain is held at 1.0. Stops
            the AGC from creeping up to max_gain on pure silence (which
            would multiply room tone by 17x and destroy SNR).

    Cost: vectorized RMS pass + one Python-level envelope loop. The loop is
    O(N) with cheap arithmetic — ~10ms for 6s @ 16kHz on consumer CPU,
    fine for the audio_lab test path. If we wire this into the realtime
    callback later we'll port the loop to numba/C.
    """
    if audio.size == 0:
        return audio.astype(np.float32, copy=False)

    a = audio.astype(np.float32, copy=False)
    win = max(1, int(window_ms * sample_rate / 1000.0))
    max_gain_linear = float(10.0 ** (max_gain_db / 20.0))

    # Rolling RMS — MAX of forward-causal and backward-causal windows.
    #
    # Why both directions: a pure forward-only window underestimates signal
    # level at the silence→speech boundary (the 30ms window contains mostly
    # silence + a few ms of speech), which makes the AGC briefly think the
    # signal is much weaker than it is and ramp gain up — that's the 2x
    # overshoot. Backward-causal alone has the symmetric problem at
    # speech→silence boundaries. Taking the MAX of both means we always pick
    # whichever side has the actual signal, so RMS reflects what's
    # *acoustically present near sample i*, not just what's already happened.
    #
    # This is effectively a free-latency look-ahead: in batch processing
    # (lab + ASR-segment) we have the whole signal anyway. For real-time
    # streaming a future port would either drop backward-RMS or buffer one
    # window-length of look-ahead.
    sq = a * a
    csum = np.empty(sq.size + 1, dtype=np.float64)
    csum[0] = 0.0
    np.cumsum(sq, out=csum[1:], dtype=np.float64)
    n = sq.size
    idx = np.arange(n)
    # Forward-causal: window [i - win + 1, i]
    fwd_lo = np.maximum(0, idx - win + 1)
    fwd_counts = (idx + 1 - fwd_lo).astype(np.float64)
    fwd_power = ((csum[idx + 1] - csum[fwd_lo]) / fwd_counts).astype(np.float32)
    # Backward-causal: window [i, i + win - 1]  (looks at the future)
    bwd_hi = np.minimum(n, idx + win)
    bwd_counts = (bwd_hi - idx).astype(np.float64)
    bwd_power = ((csum[bwd_hi] - csum[idx]) / bwd_counts).astype(np.float32)
    local_power = np.maximum(fwd_power, bwd_power)
    local_rms = np.sqrt(np.maximum(local_power, 1e-12))

    # Lift-only AGC with adaptive silence floor.
    #
    # Lift-only: gain is clamped to [1.0, max_gain_linear]. When the user
    # is close to the mic and rms already exceeds target_rms,
    # target_rms / local_rms < 1.0, which would COMPRESS the near-field
    # signal back to the quiet target — wrong. Near-field passes through
    # at gain=1.0; only weak far-field signals get lifted. The limiter
    # handles ceiling protection.
    #
    # Adaptive silence floor: anything below 25% of target_rms is treated
    # as silence and gets gain=1.0, NOT max_gain. Without this, ambient
    # room tone (typically 0.001-0.005 amp) sits just above the absolute
    # noise_floor parameter and gets amplified up to max_gain — then when
    # speech onset arrives, the envelope follower's slow release leaks
    # this high gain into the first 30-100ms of speech, causing the
    # 2x-overshoot we were debugging. Tying the silence threshold to
    # target_rms instead of an absolute number means the rule scales
    # correctly across presets (whisper's higher target → higher floor).
    # silence_floor_ratio caller-tunable (preset-specific). standard preset
    # uses 0.12 instead of 0.25 because near-field weak consonants (s/sh/x/h)
    # sit between 0.12 and 0.25 of target_rms; with the default 0.25 they
    # were being treated as silence and AGC refused to lift them, so the
    # gate's earlier attenuation never got recovered.
    silence_floor = max(
        float(noise_floor), float(target_rms) * float(silence_floor_ratio)
    )
    target_gain = target_rms / np.maximum(local_rms, 1e-9)
    target_gain = np.minimum(max_gain_linear, np.maximum(1.0, target_gain))
    raw_gain = np.where(
        local_rms < silence_floor,
        1.0,
        target_gain,
    ).astype(np.float32)

    # Asymmetric envelope follower. Single-pole IIR with two coefficients:
    #   state ← α·state + (1-α)·target
    # where α is chosen so the time-constant matches attack_ms or release_ms
    # depending on whether the target is below or above the current state.
    #
    # When target<state ⇒ gain is dropping ⇒ "attack" path (signal got
    # louder, we want gain to come down fast).
    # When target>state ⇒ gain is rising  ⇒ "release" path (signal got
    # quieter, we want gain to come up slowly).
    attack_alpha = float(np.exp(-1.0 / max(1.0, attack_ms * 1e-3 * sample_rate)))
    release_alpha = float(np.exp(-1.0 / max(1.0, release_ms * 1e-3 * sample_rate)))

    smoothed = np.empty_like(raw_gain)
    state = float(raw_gain[0])
    # Per-sample loop. Could be JIT'd with numba, but for the lab tool
    # 160k iterations of float arithmetic is fine (~10-20ms).
    for i in range(raw_gain.shape[0]):
        target = float(raw_gain[i])
        if target > state:
            # Gain rising — silence→speech onset, softer syllable, etc.
            # Use attack_ms (fast) so the first character is fully amplified
            # by the time the user perceives it.
            state = attack_alpha * state + (1.0 - attack_alpha) * target
        else:
            # Gain falling — speech→silence return, loud transient, etc.
            # Use release_ms (slower) to avoid choking loud syllables.
            state = release_alpha * state + (1.0 - release_alpha) * target
        smoothed[i] = state

    out = a * smoothed
    np.clip(out, -1.0, 1.0, out=out)
    return out.astype(np.float32, copy=False)


# ─────────────────────────────────────────────────────────────────────
# 3. Soft limiter
# ─────────────────────────────────────────────────────────────────────


def apply_limiter(
    audio: np.ndarray,
    threshold: float = 0.95,
) -> np.ndarray:
    """Tanh soft-knee limiter.

    Goal: anything below `threshold` passes unchanged; above `threshold` it
    asymptotically approaches 1.0 via tanh, so loud transients (door slam,
    cough close to mic, AGC overshoot) are smoothly compressed instead of
    clipped. Hard digital clipping creates aliasing — a tanh knee is the
    cheapest perceptually-clean alternative.

    Why a separate stage and not folded into AGC: AGC's job is constant-RMS;
    the limiter's job is peak protection on top of that. Two single-purpose
    blocks beat one knob-soup block.
    """
    if audio.size == 0:
        return audio.astype(np.float32, copy=False)
    if threshold <= 0 or threshold >= 1.0:
        return audio.astype(np.float32, copy=False)

    a = audio.astype(np.float32, copy=False)
    abs_a = np.abs(a)
    over = abs_a > threshold

    if not over.any():
        return a.copy()

    out = a.copy()
    sign = np.sign(a[over])
    excess = abs_a[over] - threshold
    headroom = 1.0 - threshold
    # tanh saturation: maps [0, ∞) → [0, headroom).
    compressed = headroom * np.tanh(excess / max(headroom, 1e-6))
    out[over] = sign * (threshold + compressed)
    return out


def normalize_input_gain(gain: float, default: float = 1.0) -> float:
    """Clamp user-facing microphone input gain to a safe realtime range."""
    try:
        value = float(gain)
    except (TypeError, ValueError):
        value = float(default)
    if not np.isfinite(value):
        value = float(default)
    return float(max(0.5, min(2.0, value)))


def apply_input_gain(
    audio: np.ndarray,
    gain: float = 1.0,
    limiter_threshold: float | None = 0.96,
) -> np.ndarray:
    """Apply user microphone gain with peak protection.

    This is intentionally simple: the slider is a software receive-volume
    trim, not a Windows device-volume mutation.  Production applies it inside
    the DSP chain after noise gate + AGC and before the final limiter, so VAD
    and raw-energy hallucination guards remain based on the untouched mic
    signal.
    """
    if audio.size == 0:
        return audio.astype(np.float32, copy=False)

    safe_gain = normalize_input_gain(gain)
    a = audio.astype(np.float32, copy=False)
    if abs(safe_gain - 1.0) < 1e-6:
        return a

    out = (a * safe_gain).astype(np.float32, copy=False)
    if limiter_threshold is not None:
        out = apply_limiter(out, threshold=float(limiter_threshold))
        np.clip(out, -1.0, 1.0, out=out)
    return out.astype(np.float32, copy=False)


# ─────────────────────────────────────────────────────────────────────
# 4. Noise floor estimation + adaptive noise gate
# ─────────────────────────────────────────────────────────────────────


def _causal_rolling_rms(
    audio: np.ndarray, sample_rate: int, window_ms: float
) -> np.ndarray:
    """Internal helper — vectorized causal moving RMS via cumsum trick."""
    win = max(1, int(window_ms * sample_rate / 1000.0))
    sq = audio.astype(np.float32, copy=False) ** 2
    csum = np.empty(sq.size + 1, dtype=np.float64)
    csum[0] = 0.0
    np.cumsum(sq, out=csum[1:], dtype=np.float64)
    idx = np.arange(sq.size)
    lo = np.maximum(0, idx - win + 1)
    counts = (idx + 1 - lo).astype(np.float64)
    rms = np.sqrt(np.maximum((csum[idx + 1] - csum[lo]) / counts, 1e-12))
    return rms.astype(np.float32, copy=False)


def estimate_noise_floor(
    audio: np.ndarray,
    sample_rate: int = 16000,
    window_ms: float = 30.0,
    percentile: float = 15.0,
) -> float:
    """Estimate steady-state noise floor (RMS amplitude, 0..1).

    Method: rolling causal RMS → 15th percentile. Rationale:
      - The bottom percentile is dominated by inter-word silence and
        pre-roll, exactly what we want to call "noise".
      - Mean / median get pulled up by speech segments.
      - Min is too sensitive to a single quiet sample.
      - Minimum-statistics (Martin 2001) needs minutes of audio to
        converge; for a 5-15s lab take, percentile is the cheap robust
        proxy.

    This is the foundation for the user's intuition "say speech is at
    0.6, noise is at 0.2, set threshold at 0.5". The lab UI displays this
    plus speech_ceiling so the threshold gap is visible.
    """
    if audio.size == 0:
        return 0.0
    rms = _causal_rolling_rms(audio, sample_rate, window_ms)
    return float(np.percentile(rms, percentile))


def estimate_speech_ceiling(
    audio: np.ndarray,
    sample_rate: int = 16000,
    window_ms: float = 30.0,
    percentile: float = 90.0,
) -> float:
    """Symmetric counterpart to estimate_noise_floor — high percentile of
    rolling RMS, used as the "speech-loud" reference for the lab readout."""
    if audio.size == 0:
        return 0.0
    rms = _causal_rolling_rms(audio, sample_rate, window_ms)
    return float(np.percentile(rms, percentile))


def apply_noise_gate(
    audio: np.ndarray,
    sample_rate: int = 16000,
    threshold: float | None = None,
    threshold_above_floor_db: float = 6.0,
    window_ms: float = 30.0,
    attack_ms: float = 5.0,
    release_ms: float = 80.0,
    floor_db: float = -30.0,
    floor_percentile: float = 15.0,
    knee_db: float = 6.0,
) -> np.ndarray:
    """Soft adaptive noise gate.

    This is the user's "find the threshold between speech and noise" idea
    with two production-grade refinements over a hard binary gate:

      1. ADAPTIVE — threshold computed per-recording from the noise floor
         (15th percentile RMS) plus a margin in dB. 6 dB above the floor
         is a safe default for steady noise; crank to 10-12 dB in a noisy
         environment so steady noise never opens the gate.
      2. SOFT KNEE — a hard binary gate causes audible "chopping" on
         marginal speech; a tanh-shaped soft gate fades smoothly from
         "pass" to "attenuate to floor_db" over the knee region (default
         6 dB wide).

    The gate's per-sample multiplier is then driven through the same
    asymmetric envelope follower as the AGC: fast attack on word onset
    (no eaten first character) + slower release on word end (no chopped
    syllable tails).

    Args:
        threshold: explicit RMS gate threshold (0..1). If None, derived
            from this recording's own noise floor.
        threshold_above_floor_db: how many dB above the estimated noise
            floor to place the gate. Adjust per environment.
        floor_db: how much to attenuate sub-threshold audio. -30 dB is
            "barely audible but non-zero" — preserves recording texture
            without obviously gating, and leaves enough signal for an
            ASR-side VAD to still see the noise level.
        knee_db: width of the soft transition zone. Larger = smoother,
            smaller = more aggressive.
    """
    if audio.size == 0:
        return audio.astype(np.float32, copy=False)

    a = audio.astype(np.float32, copy=False)
    rms = _causal_rolling_rms(a, sample_rate, window_ms)

    if threshold is None:
        nf = float(np.percentile(rms, floor_percentile))
        margin_linear = 10.0 ** (threshold_above_floor_db / 20.0)
        thr = nf * margin_linear
    else:
        thr = float(threshold)
    thr = max(thr, 1e-6)

    # Soft tanh gate centered on the threshold in dB. Above threshold the
    # multiplier saturates to 1.0 (open); below, it saturates to floor_db.
    eps = 1e-9
    rms_db = 20.0 * np.log10(np.maximum(rms, eps))
    thr_db = 20.0 * np.log10(thr)
    delta = rms_db - thr_db
    open_amount = 0.5 * (1.0 + np.tanh(delta / max(knee_db / 2.0, 1.0)))
    floor_linear = 10.0 ** (floor_db / 20.0)
    raw_mult = (floor_linear + (1.0 - floor_linear) * open_amount).astype(np.float32)

    # Asymmetric envelope follower — same convention as AGC.
    attack_alpha = float(np.exp(-1.0 / max(1.0, attack_ms * 1e-3 * sample_rate)))
    release_alpha = float(np.exp(-1.0 / max(1.0, release_ms * 1e-3 * sample_rate)))
    smoothed = np.empty_like(raw_mult)
    state = float(raw_mult[0])
    for i in range(raw_mult.shape[0]):
        target = float(raw_mult[i])
        if target > state:
            state = attack_alpha * state + (1.0 - attack_alpha) * target
        else:
            state = release_alpha * state + (1.0 - release_alpha) * target
        smoothed[i] = state

    return (a * smoothed).astype(np.float32, copy=False)


# ─────────────────────────────────────────────────────────────────────
# Convenience presets — what tools/audio_lab.py uses for A/B comparison
# ─────────────────────────────────────────────────────────────────────


# Mode presets. Each preset captures a sane parameter set for a typical
# recording environment; users pick a preset rather than tuning 8 knobs.
# The lab UI exposes these as buttons; the production capture pipeline
# (when wired) will select one based on the user's chosen 收音模式 in
# the floating-ball menu.
# Each preset can override two upstream gating parameters that live OUTSIDE
# the DSP chain:
#   - vad_threshold_override : Silero VAD probability threshold. None = use
#     user's configured value. Whisper mode lowers this to 0.05 because
#     Silero's probability output for very-quiet speech is well below the
#     default 0.15, and if VAD doesn't fire there's no audio for the DSP
#     chain to lift.
#   - energy_gate_override : pre-ASR amplitude floor (after DSP). None = use
#     user's configured value. Whisper mode lowers this to 0.0010 so
#     barely-audible signals aren't dropped before transcription.
# When None, the user's settings.py value applies.

MODE_PRESETS: dict[str, dict] = {
    # standard = the everyday default. Tuned to be near-transparent for
    # near-field users while still providing enough lift for moderate
    # far-field. With AGC now lift-only (gain ≥ 1.0), this preset has
    # essentially no effect on someone speaking 30cm from the mic — only
    # users 1m+ away or speaking softly will see active gain.
    "standard": {
        "label": "正常模式",
        "description": "日常默认；近场说话几乎透明，但温和救助弱辅音 / 字头字尾",
        "use_hpf": True,
        "use_agc": True,
        "use_gate": True,
        "use_limiter": True,
        "hpf_cutoff_hz": 80.0,
        "agc_target_rms": 0.08,
        "agc_max_gain_db": 18.0,
        "agc_attack_ms": 5.0,
        "agc_release_ms": 80.0,
        # silence_floor_ratio 0.12 (vs default 0.25): 弱辅音 RMS 通常在
        # target_rms 的 0.12-0.25 之间，默认 0.25 会把它们钉成 gain=1.0，
        # 跟 gate 配合就会"字头字尾被吞"。降到 0.12 让 AGC 还能给这段
        # 信号 lift。
        "agc_silence_floor_ratio": 0.12,
        "gate_threshold_above_floor_db": 3.0,
        "gate_floor_db": -24.0,
        "gate_knee_db": 8.0,
        "limiter_threshold": 0.95,
        "input_gain_default": 1.0,
        # 轻度上游联动：standard 自己拉低 VAD/energy 门限，但不到 whisper 程度。
        # 实测从 whisper 切回 standard 时如果 base 是 vad=0.15/energy=0.003，
        # 近场正常说话的弱辅音段会被两道闸拦截 → 整段漏听。
        "vad_threshold_override": 0.10,
        # 2026-07-20 回滚 0.0010→0.0015（H1 反事实实锤）：下调期间
        # [0.0010,0.0015) 带内放行的段里 3 段是纯噪声编造句且全部上屏
        # （~0.5 段/h），而想救的 15 场安静真话 post-DSP 全部 <0.0010、
        # 新闸下依然被拦——降闸收益实测为 0，只放进垃圾并空耗 GPU 解码。
        # 安静真话的正确救法走 OS 增益 / 异步复核路线，不动这道闸
        # （checkpoint_H1.md，cluster_20260720/hallucination）。
        "energy_gate_override": 0.0015,
        "endpoint_micro_rms": None,
        "endpoint_micro_min_speech_ms": 1200,
        "endpoint_steady_noise_ms": 900,
        "endpoint_steady_noise_min_speech_ms": 1200,
        "endpoint_steady_noise_max_cv": 0.18,
        "endpoint_steady_noise_peak_ratio": 0.65,
        "endpoint_steady_noise_flat_ms": 1800,
        "endpoint_steady_noise_flat_max_cv": 0.10,
        "endpoint_steady_noise_min_rms": 0.0015,
        "max_speech_ms_override": None,
    },
    # noisy = strong-environment-noise mode. Same temperate AGC ceiling
    # so we don't pump up the room tone, but a much higher gate margin
    # (10 dB above estimated floor instead of 3 dB) so steady noise rarely
    # opens the gate; only clear speech does. HPF also bumped to 100 Hz to
    # crush low-frequency fan/HVAC rumble harder. The closed floor remains
    # finite (-30 dB): continuous music occupies the estimated noise floor,
    # and speech over that bed may never exceed it by 10 dB. A -40 dB floor
    # pushed the whole segment below Aria's 0.003 pre-ASR energy gate before
    # Qwen3 could inspect it. -30 dB still attenuates closed material 31.6x
    # (more aggressively than standard's -24 dB) while preserving enough
    # waveform for the downstream ASR/noise guards.
    "noisy": {
        "label": "嘈杂模式",
        "description": "空调/风扇/键盘/背景音乐等明显噪声；激进 gate 拒噪但保留语音安全底线",
        "use_hpf": True,
        "use_agc": True,
        "use_gate": True,
        "use_limiter": True,
        "hpf_cutoff_hz": 100.0,
        "agc_target_rms": 0.06,
        "agc_max_gain_db": 12.0,
        "agc_attack_ms": 8.0,
        "agc_release_ms": 100.0,
        "gate_threshold_above_floor_db": 10.0,
        "gate_floor_db": -30.0,
        "gate_knee_db": 4.0,
        "limiter_threshold": 0.95,
        # Noisy mode used to fall from the user's standard 1.5x gain to 1.0x
        # on first selection. That 33% drop could push speech-over-music under
        # the pre-ASR energy gate. Keep the stronger first-use headroom; the
        # closed gate still leaves steady music below the rejection floor.
        "input_gain_default": 1.5,
        # 默认 0.25 — noisy 模式不需要救弱辅音（环境本来就太吵），
        # 维持原 silence_floor 行为防止 AGC 抬起稳态噪声。
        "agc_silence_floor_ratio": 0.25,
        # No upstream overrides — noisy mode trusts the user's VAD config.
        "vad_threshold_override": None,
        "energy_gate_override": None,
        "endpoint_micro_rms": None,
        "endpoint_micro_min_speech_ms": 1200,
        "endpoint_steady_noise_ms": 800,
        "endpoint_steady_noise_min_speech_ms": 1000,
        "endpoint_steady_noise_max_cv": 0.20,
        "endpoint_steady_noise_peak_ratio": 0.75,
        "endpoint_steady_noise_flat_ms": 1500,
        "endpoint_steady_noise_flat_max_cv": 0.12,
        "endpoint_steady_noise_min_rms": 0.0015,
        "max_speech_ms_override": None,
    },
    # whisper = quiet-environment + soft-voice rescue. Used when the user
    # is speaking very quietly (late-night, sleeping family, etc.) AND
    # the room is quiet enough that aggressive AGC won't re-amplify noise.
    # Tuned aggressively across the board:
    #   - VAD threshold dropped to 0.05 (from default 0.15) so Silero
    #     fires on very-quiet speech; without this the rest of the chain
    #     never sees the audio.
    #   - energy gate dropped to 0.0005 (from default 0.003) so barely-
    #     audible captures aren't pre-skipped.
    #   - AGC: target 0.15 + 30dB max_gain + 3ms attack — lift hard, fast.
    #   - Gate: only 2 dB above noise floor (basically always open) and
    #     -20dB floor (when "closed", still passes audible signal so the
    #     ASR's own VAD can work on it).
    "whisper": {
        "label": "轻语模式",
        "description": "夜间 / 安静环境 + 极小声说话；灵敏收音，但尾部微噪声会自动断句",
        "use_hpf": True,
        "use_agc": True,
        "use_gate": True,
        "use_limiter": True,
        "hpf_cutoff_hz": 80.0,
        "agc_target_rms": 0.15,
        "agc_max_gain_db": 30.0,
        "agc_attack_ms": 3.0,
        "agc_release_ms": 60.0,
        "gate_threshold_above_floor_db": 2.0,
        "gate_floor_db": -20.0,
        "gate_knee_db": 8.0,
        "limiter_threshold": 0.95,
        "input_gain_default": 1.5,
        # whisper 已经 target_rms=0.15 + max_gain=30dB 极度激进，
        # silence_floor 维持 0.25 防止 AGC 把房间静默也抬起来变噪声泵。
        "agc_silence_floor_ratio": 0.25,
        # Whisper-specific upstream sensitivity.
        "vad_threshold_override": 0.05,
        "energy_gate_override": 0.0010,
        # Endpoint guard: once speech has already started, tail noise below
        # this raw RMS is counted as silence so a fan/breath/key resonance
        # cannot keep the utterance open forever.  Real whisper speech in our
        # field logs stays safely above ~0.0015 pre-DSP.
        "endpoint_micro_rms": 0.0012,
        "endpoint_micro_min_speech_ms": 1200,
        "endpoint_steady_noise_ms": 900,
        "endpoint_steady_noise_min_speech_ms": 1200,
        "endpoint_steady_noise_max_cv": 0.18,
        "endpoint_steady_noise_peak_ratio": 0.65,
        "endpoint_steady_noise_flat_ms": 1800,
        "endpoint_steady_noise_flat_max_cv": 0.10,
        "endpoint_steady_noise_min_rms": 0.0012,
        # Relative energy-collapse endpoint (field logs 2026-06-10 00:40):
        # breath/room junk at raw RMS ~0.003 sat ABOVE the micro floor and was
        # too erratic for the steady-noise CV gate, holding utterances open
        # 16s+ until the user toggled the hotkey.  Real whisper speech peaks
        # were >= 0.01 in the same logs, so a chunk far below the utterance's
        # own speech peak is junk, not speech.  abs_ceiling keeps one loud
        # spike from raising the floor into real-whisper territory, and the
        # prob ceiling protects quiet-but-confident speech.
        "endpoint_rel_drop_ratio": 0.35,
        "endpoint_rel_drop_min_speech_ms": 900,
        "endpoint_rel_drop_abs_ceiling": 0.0045,
        "endpoint_rel_drop_prob_ceiling": 0.70,
        # Whisper junk blips past the guards every second or two; a single
        # blip must not zero the accumulated silence (that is what made
        # whisper never end).  3 consecutive speech chunks (~96ms) cancel.
        "endpoint_override_cancel_chunks": 3,
        # User configs from older builds may have max_speech_ms=120000; the
        # runtime clamps that to 60000, which is still too long when whisper
        # VAD is held open by micro-noise.  Whisper should stream/commit more
        # often rather than waiting a full minute.
        "max_speech_ms_override": 20000,
    },
}


def process_chain(
    audio: np.ndarray,
    sample_rate: int = 16000,
    use_hpf: bool = False,
    use_gate: bool = False,
    use_agc: bool = False,
    use_limiter: bool = False,
    hpf_cutoff_hz: float = 80.0,
    agc_target_rms: float = 0.10,
    agc_max_gain_db: float = 25.0,
    agc_attack_ms: float = 5.0,
    agc_release_ms: float = 50.0,
    agc_silence_floor_ratio: float = 0.25,
    gate_threshold_above_floor_db: float = 6.0,
    gate_floor_db: float = -30.0,
    gate_knee_db: float = 6.0,
    input_gain: float = 1.0,
    limiter_threshold: float = 0.95,
) -> np.ndarray:
    """Run the full HPF→Gate→AGC→Limiter chain with a-la-carte toggles.

    Order rationale:
      1. HPF kills low-frequency rumble before any measurement.
      2. Gate kills steady noise BEFORE AGC sees it, otherwise the AGC's
         noise_floor estimate gets confused by what should already be silenced.
      3. AGC lifts weak speech to target RMS.
      4. Limiter catches AGC overshoot + sudden transients.

    This is the reference compose order for the production pipeline — keep
    audio_lab.py and capture.py both calling through here so what the user
    auditioned in the lab matches what runs in production.
    """
    out = audio
    if use_hpf:
        out = apply_highpass(out, sample_rate=sample_rate, cutoff_hz=hpf_cutoff_hz)
    if use_gate:
        out = apply_noise_gate(
            out,
            sample_rate=sample_rate,
            threshold_above_floor_db=gate_threshold_above_floor_db,
            floor_db=gate_floor_db,
            knee_db=gate_knee_db,
        )
    if use_agc:
        out = apply_agc(
            out,
            sample_rate=sample_rate,
            target_rms=agc_target_rms,
            max_gain_db=agc_max_gain_db,
            attack_ms=agc_attack_ms,
            release_ms=agc_release_ms,
            silence_floor_ratio=agc_silence_floor_ratio,
        )
    # User-facing "麦克风音量" is a post-AGC compensation gain.  Placing it
    # here makes the slider audible even when AGC has already normalized weak
    # speech, while keeping VAD/raw-energy/noise-floor logic untouched.
    out = apply_input_gain(
        out,
        input_gain,
        limiter_threshold=None if use_limiter else limiter_threshold,
    )
    if use_limiter:
        out = apply_limiter(out, threshold=limiter_threshold)
    return out.astype(np.float32, copy=False)


def process_with_preset(
    audio: np.ndarray,
    preset_name: str,
    sample_rate: int = 16000,
    input_gain: float = 1.0,
) -> np.ndarray:
    """Apply a named preset (standard / quiet_farfield / noisy_environment)."""
    if preset_name not in MODE_PRESETS:
        raise ValueError(
            f"unknown preset {preset_name!r}; valid: {list(MODE_PRESETS.keys())}"
        )
    p = MODE_PRESETS[preset_name]
    return process_chain(
        audio,
        sample_rate=sample_rate,
        use_hpf=p["use_hpf"],
        use_gate=p["use_gate"],
        use_agc=p["use_agc"],
        use_limiter=p["use_limiter"],
        hpf_cutoff_hz=p["hpf_cutoff_hz"],
        agc_target_rms=p["agc_target_rms"],
        agc_max_gain_db=p["agc_max_gain_db"],
        agc_attack_ms=p["agc_attack_ms"],
        agc_release_ms=p["agc_release_ms"],
        agc_silence_floor_ratio=p.get("agc_silence_floor_ratio", 0.25),
        gate_threshold_above_floor_db=p["gate_threshold_above_floor_db"],
        gate_floor_db=p["gate_floor_db"],
        gate_knee_db=p["gate_knee_db"],
        input_gain=input_gain,
        limiter_threshold=p["limiter_threshold"],
    )


# ─────────────────────────────────────────────────────────────────────
# Diagnostic helpers — exposed for the lab UI
# ─────────────────────────────────────────────────────────────────────


def signal_stats(audio: np.ndarray) -> dict:
    """Quick descriptive stats for the lab UI's level readout."""
    if audio.size == 0:
        return {"peak": 0.0, "rms": 0.0, "rms_db": -120.0, "peak_db": -120.0}
    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    eps = 1e-9
    return {
        "peak": peak,
        "rms": rms,
        "rms_db": 20.0 * np.log10(max(rms, eps)),
        "peak_db": 20.0 * np.log10(max(peak, eps)),
    }
