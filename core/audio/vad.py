"""
Voice Activity Detection (VAD) Module
=====================================
Based on Silero-VAD for efficient speech detection.

Key benefits:
- Filter silence before sending to ASR (saves compute/cost)
- Reduce latency by only processing speech segments
- Enable "streaming chunks" mode for real-time feedback
"""

import os
import threading
import time
import numpy as np
from dataclasses import dataclass
from typing import Optional, List, Tuple, Callable, Dict, Any
from collections import deque

from ..logging import get_audio_logger

logger = get_audio_logger()


def _silero_onnx_path() -> str:
    """Filesystem path to the bundled torch-free onnx Silero-VAD model."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "assets",
        "silero_vad.onnx",
    )


def _torch_importable() -> bool:
    """True when the torch package is importable (without importing it)."""
    import sys

    if "torch" in sys.modules:
        return sys.modules["torch"] is not None
    try:
        import importlib.util

        return importlib.util.find_spec("torch") is not None
    except Exception:
        return False


def resolve_vad_backend(config_backend: Optional[str] = None) -> str:
    """Pick the Silero backend: 'torch' or 'onnx'.

    Priority: env ARIA_VAD_BACKEND=onnx|torch > config value > auto-detect
    (torch importable -> torch, else onnx). This only expresses PREFERENCE;
    _ensure_model still cascades torch -> onnx -> energy fallback on load
    failure, so a forced 'torch' on a torch-free box degrades gracefully.
    """
    env = os.environ.get("ARIA_VAD_BACKEND", "").strip().lower()
    if env in ("torch", "onnx"):
        return env
    cfg = (config_backend or "").strip().lower()
    if cfg in ("torch", "onnx"):
        return cfg
    return "torch" if _torch_importable() else "onnx"


# Optional second arg on speech_end / soft_split callbacks: segment VAD stats dict
# with keys avg/max/voiced_ratio (and optionally truncated=True after soft_split).
# Segments committed by the VAD state machine additionally carry endpoint
# telemetry (observation only, mining/report.md section C):
#   endpoint_reason        : "tail_silence" | "max_speech" | "soft_split" |
#                            "manual_stop" (hotkey stop via take_manual_stop_stats)
#   tail_silence_ms        : endpoint-accounting silence accumulated at commit
#                            (speech-prob drop — incl. tail-noise overrides — to commit)
#   last_voice_to_commit_ms: wall-clock ms from the last speech-labeled chunk
#                            to the commit instant (absent if no voice seen)
#   pause_histogram        : intra-segment silence gaps that ENDED with resumed
#                            speech, bucketed in 100ms steps ([count@0-100,
#                            count@100-200, ...], trailing zeros trimmed).
#                            The final tail is NOT counted here (see tail_silence_ms).
VadSegmentStats = Dict[str, Any]


@dataclass
class VADConfig:
    """VAD configuration parameters."""

    # Detection thresholds
    threshold: float = (
        0.3  # Speech probability threshold (0-1), lowered for better detection
    )
    min_speech_ms: int = (
        64  # Minimum speech duration to trigger (lowered for short utterances)
    )
    min_silence_ms: int = (
        1500  # Minimum silence to end speech segment (1.5s tolerates natural pauses)
    )
    max_speech_ms: int = (
        15000  # Maximum speech segment length before forced split (15 seconds)
    )

    # Soft-split: cut at natural pauses inside long utterances for pipeline parallelism.
    # Only triggers when BOTH conditions met: accumulated speech >= soft_split_min_speech_ms
    # AND silence >= soft_split_silence_ms (but still < min_silence_ms — so not a full EOS).
    # Designed so short utterances never see a split, and long utterances stream out segment by segment.
    soft_split_enabled: bool = True
    soft_split_silence_ms: int = 500
    soft_split_min_speech_ms: int = 5000

    # Audio parameters
    sample_rate: int = 16000  # Must be 16000 for Silero-VAD

    # Chunk processing
    chunk_size_ms: int = 32  # Process in 32ms chunks (Silero optimal)

    # Silero backend preference: None = auto (torch if importable, else the
    # bundled torch-free onnx model). Env ARIA_VAD_BACKEND overrides this.
    # Both backends produce equivalent probabilities (parity-tested), so
    # `threshold` and all endpoint guards apply identically either way.
    vad_backend: Optional[str] = None

    # Padding
    speech_pad_ms: int = 30  # Pad speech start/end

    # Endpoint guard for very sensitive modes.
    #
    # Silero can keep returning a tiny "speech" probability for room tone,
    # fan noise, keyboard resonance, or breathing.  In whisper mode the app
    # intentionally lowers the probability threshold so very quiet speech can
    # start recording, but that also means tail-end micro-noise may prevent
    # min_silence_ms from ever accumulating.  When enabled, already-started
    # speech whose raw RMS stays below this floor is treated as silence for
    # endpoint accounting only.  It is disabled by default and applied by
    # capture-mode presets.
    speech_end_micro_rms: float = 0.0
    speech_end_micro_min_speech_ms: int = 1200

    # Endpoint guard for sustained background noise.
    #
    # Air-conditioners, airplanes, fans, and GPU coil/fan noise often produce a
    # continuous energy floor.  Silero may keep classifying that as speech after
    # the user has stopped, so min_silence_ms never accumulates.  Human speech is
    # bursty: after a real utterance the tail usually becomes flatter and lower
    # than the speech peak.  When enabled, a stable tail window is counted as
    # silence for endpoint accounting only; it never blocks speech_start.
    speech_end_steady_noise_ms: int = 0
    speech_end_steady_noise_min_speech_ms: int = 1200
    speech_end_steady_noise_max_cv: float = 0.18
    speech_end_steady_noise_peak_ratio: float = 0.65
    speech_end_steady_noise_flat_ms: int = 1800
    speech_end_steady_noise_flat_max_cv: float = 0.10
    speech_end_steady_noise_min_rms: float = 0.0015

    # Endpoint guard for relative energy collapse.
    #
    # Whisper-mode tail junk (breath gasps, fabric/mouth noise) is erratic, so
    # the steady-noise CV gate never engages, and it often sits ABOVE the
    # absolute micro-RMS floor — yet it is always far below the chunk-level
    # RMS peak of the real speech that preceded it (field logs 2026-06-10:
    # tail junk ~0.003 vs speech peaks >= 0.01).  When enabled, a chunk whose
    # raw RMS collapses below `ratio x speech_peak_rms` (clamped by
    # `abs_ceiling` so a loud spike cannot raise the floor into real-whisper
    # territory) and whose VAD probability is not confidently speech is
    # counted as silence for endpoint accounting only.  Disabled by default;
    # applied by the whisper capture-mode preset.
    speech_end_rel_drop_ratio: float = 0.0  # 0 = disabled
    speech_end_rel_drop_min_speech_ms: int = 900
    speech_end_rel_drop_abs_ceiling: float = 0.0045
    speech_end_rel_drop_prob_ceiling: float = 0.70

    # Debounce for cancelling an active tail-endpoint override.  Historically a
    # SINGLE chunk that slipped past the endpoint guards zeroed the accumulated
    # silence — whisper junk produces such blips every second or two, so
    # min_silence_ms could never complete and the utterance stayed open
    # forever.  While an override is active, this many CONSECUTIVE speech
    # chunks are required to cancel it; isolated blips keep counting as
    # silence.  1 = legacy behavior (any blip cancels immediately).
    speech_end_override_cancel_chunks: int = 1

    @property
    def chunk_size_samples(self) -> int:
        """Samples per chunk."""
        return int(self.sample_rate * self.chunk_size_ms / 1000)

    @property
    def min_speech_samples(self) -> int:
        return int(self.sample_rate * self.min_speech_ms / 1000)

    @property
    def min_silence_samples(self) -> int:
        return int(self.sample_rate * self.min_silence_ms / 1000)

    @property
    def max_speech_samples(self) -> int:
        return int(self.sample_rate * self.max_speech_ms / 1000)

    @property
    def soft_split_silence_samples(self) -> int:
        return int(self.sample_rate * self.soft_split_silence_ms / 1000)

    @property
    def soft_split_min_speech_samples(self) -> int:
        return int(self.sample_rate * self.soft_split_min_speech_ms / 1000)

    @property
    def speech_end_micro_min_speech_samples(self) -> int:
        return int(self.sample_rate * self.speech_end_micro_min_speech_ms / 1000)

    @property
    def speech_end_steady_noise_samples(self) -> int:
        return int(self.sample_rate * self.speech_end_steady_noise_ms / 1000)

    @property
    def speech_end_steady_noise_min_speech_samples(self) -> int:
        return int(self.sample_rate * self.speech_end_steady_noise_min_speech_ms / 1000)

    @property
    def speech_end_rel_drop_min_speech_samples(self) -> int:
        return int(self.sample_rate * self.speech_end_rel_drop_min_speech_ms / 1000)


class VADProcessor:
    """
    Real-time Voice Activity Detection using Silero-VAD.

    Usage:
        vad = VADProcessor()

        # Process audio chunks in real-time
        for chunk in audio_stream:
            is_speech, probability = vad.process_chunk(chunk)
            if is_speech:
                # Send to ASR
                pass

        # Or use callback mode
        vad.set_callbacks(on_speech_start, on_speech_end, on_speech_chunk)
        vad.process_stream(audio_generator)
    """

    # Pause-histogram geometry: 100ms buckets, 64 buckets (covers 0-6.4s;
    # longer gaps clamp into the last bucket).  Intra-segment gaps are bounded
    # by min_silence_ms in practice, so 64 buckets is generous headroom.
    _PAUSE_HIST_BUCKETS = 64
    _PAUSE_BUCKET_MS = 100

    def __init__(self, config: Optional[VADConfig] = None):
        self.config = config or VADConfig()
        self._model = None
        self._is_speaking = False
        self._use_fallback = False  # True if silero_vad/torch failed to load
        # Torch-free onnx Silero-VAD (slim build): same model weights as the
        # torch path, run through onnxruntime CPUExecutionProvider.
        self._onnx_vad = None
        self._onnx_state = None
        self._onnx_context = None
        self._onnx_sr = None
        self._onnx_win = 512
        self._onnx_ctx_size = 64

        # Log VAD config for debugging (pythonw.exe safe)
        import sys

        soft_split_info = (
            f"soft_split={'ON' if self.config.soft_split_enabled else 'OFF'} "
            f"(>={self.config.soft_split_min_speech_ms}ms speech & "
            f">={self.config.soft_split_silence_ms}ms pause)"
        )
        if sys.stdout is not None:
            print(
                f"[VAD] Config: threshold={self.config.threshold}, "
                f"min_silence={self.config.min_silence_ms}ms, "
                f"max_speech={self.config.max_speech_ms}ms ({self.config.max_speech_samples} samples), "
                f"{soft_split_info}"
            )
        logger.info(
            f"VAD Config: threshold={self.config.threshold}, "
            f"min_silence={self.config.min_silence_ms}ms, max_speech={self.config.max_speech_ms}ms, "
            f"{soft_split_info}"
        )

        # State tracking
        self._speech_samples = 0
        self._silence_samples = 0
        self._speech_buffer: List[np.ndarray] = []
        self._speech_peak_rms = 0.0
        self._speech_peak_prob = 0.0
        # Per-segment Silero probability telemetry (observation only).
        # Accumulated alongside _speech_buffer; reset when a segment is emitted.
        # Soft-split resets with truncated=True rather than splitting the
        # parallel prob list by sample ratio (simpler, semantically honest).
        self._vad_prob_sum = 0.0
        self._vad_prob_count = 0
        self._vad_prob_max = 0.0
        self._vad_voiced_count = 0
        # Endpoint telemetry accumulators (observation only — they never feed
        # back into endpoint decisions).  The pause histogram is a fixed-size
        # integer-bucket list so the audio-callback hot path only does an
        # integer increment (zero allocations); it is packed once at commit.
        self._pause_hist: List[int] = [0] * self._PAUSE_HIST_BUCKETS
        self._pause_hist_nonzero = False
        self._pause_bucket_samples = max(
            1, int(self.config.sample_rate * self._PAUSE_BUCKET_MS / 1000)
        )
        self._last_voice_ts: Optional[float] = None
        self._tail_noise_override_active = False
        self._tail_override_speech_streak = 0
        self._tail_rms_history = deque(maxlen=96)  # ~3s at 32ms chunks
        self._tail_prob_history = deque(maxlen=96)  # aligned with RMS history
        self._buffer_lock = (
            threading.Lock()
        )  # Protect buffer access from multiple threads

        # Ring buffer for recent audio (for padding)
        # Need enough chunks to cover min_speech_ms + padding
        # min_speech_ms=250ms / chunk_size=32ms = ~8 chunks, use 12 for safety
        self._pre_buffer = deque(maxlen=12)  # ~384ms pre-speech buffer

        # Callbacks
        self._on_speech_start: Optional[Callable[[], None]] = None
        self._on_speech_end: Optional[Callable[..., None]] = None
        self._on_speech_chunk: Optional[Callable[[np.ndarray, float], None]] = None
        self._on_speech_soft_split: Optional[Callable[..., None]] = None

        # Load model (with fallback if torch fails)
        self._ensure_model()

    def _ensure_model(self) -> None:
        """Load a VAD model if not already loaded.

        Backend preference comes from resolve_vad_backend() (env var >
        config.vad_backend > auto-detect). The load chain then cascades:
        torch Silero (full build) -> onnx Silero (torch-free slim, same
        weights) -> energy-RMS fallback (last resort). A forced 'onnx'
        backend skips the torch attempt entirely so slim-build behavior can
        be exercised on a dev box that has torch installed.
        """
        if self._model is not None or self._onnx_vad is not None or self._use_fallback:
            return

        backend = resolve_vad_backend(self.config.vad_backend)

        if backend != "onnx":
            try:
                from silero_vad import load_silero_vad

                self._model = load_silero_vad()
                logger.info("Silero-VAD (torch) model loaded")
                return
            except OSError as e:
                # torch DLL loading failure (e.g., CUDA version mismatch)
                logger.warning(
                    f"Silero-VAD (torch) failed to load (torch error): {e}. "
                    "Trying torch-free onnx Silero-VAD."
                )
            except Exception as e:
                logger.warning(
                    f"Silero-VAD (torch) unavailable ({type(e).__name__}: {e}). "
                    "Trying torch-free onnx Silero-VAD."
                )

        if self._load_onnx_silero():
            return

        logger.warning("All Silero-VAD paths failed. Using energy-based fallback VAD.")
        self._use_fallback = True

    def _load_onnx_silero(self) -> bool:
        """Load the bundled torch-free onnx Silero-VAD. Returns True on success."""
        try:
            import onnxruntime as ort

            model_path = _silero_onnx_path()
            if not os.path.isfile(model_path):
                logger.warning(f"onnx Silero-VAD model missing at {model_path}")
                return False
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            self._onnx_vad = ort.InferenceSession(
                model_path,
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
            sr = int(self.config.sample_rate)
            self._onnx_sr = np.array(sr, dtype=np.int64)
            self._onnx_win = 512 if sr == 16000 else 256
            self._onnx_ctx_size = 64 if sr == 16000 else 32
            self._reset_onnx_state()
            logger.info("Silero-VAD (onnx, torch-free) loaded")
            return True
        except Exception as e:
            logger.warning(f"onnx Silero-VAD failed to load: {e}")
            self._onnx_vad = None
            return False

    def _reset_onnx_state(self) -> None:
        """Reset the onnx Silero-VAD recurrent state + context window."""
        self._onnx_state = np.zeros((2, 1, 128), dtype=np.float32)
        self._onnx_context = np.zeros((1, self._onnx_ctx_size), dtype=np.float32)

    def _onnx_silero_prob(self, audio: np.ndarray) -> float:
        """Speech probability for one chunk via onnx Silero-VAD (stateful).

        Mirrors silero_vad.OnnxWrapper: prepend the previous chunk's last
        ctx_size samples, run, then carry the new context + recurrent state.
        """
        win = self._onnx_win
        a = np.asarray(audio, dtype=np.float32).reshape(1, -1)
        n = a.shape[1]
        if n < win:
            a = np.pad(a, ((0, 0), (0, win - n)))
        elif n > win:
            a = a[:, :win]
        x = np.concatenate([self._onnx_context, a], axis=1)
        out, self._onnx_state = self._onnx_vad.run(
            None,
            {"input": x, "state": self._onnx_state, "sr": self._onnx_sr},
        )
        self._onnx_context = x[:, -self._onnx_ctx_size :].astype(np.float32)
        return float(out[0][0])

    def reset(self) -> None:
        """Reset VAD state (call between recordings).

        Thread Safety:
            Uses _buffer_lock to protect buffer operations.
        """
        self._is_speaking = False
        self._speech_samples = 0
        self._silence_samples = 0
        self._speech_peak_rms = 0.0
        self._speech_peak_prob = 0.0
        self._tail_noise_override_active = False
        self._tail_override_speech_streak = 0
        self._tail_rms_history.clear()
        self._tail_prob_history.clear()

        with self._buffer_lock:
            self._speech_buffer.clear()
            self._pre_buffer.clear()
        self._reset_vad_prob_accum()

        # Reset model state
        if self._model is not None:
            self._model.reset_states()
        if self._onnx_vad is not None:
            self._reset_onnx_state()

    def _reset_vad_prob_accum(self) -> None:
        """Clear per-segment probability + endpoint-telemetry accumulators."""
        self._vad_prob_sum = 0.0
        self._vad_prob_count = 0
        self._vad_prob_max = 0.0
        self._vad_voiced_count = 0
        # Guarded by the nonzero flag: this method runs on every not-speaking
        # silence chunk, so skip the O(buckets) clear when already clean.
        if self._pause_hist_nonzero:
            for i in range(len(self._pause_hist)):
                self._pause_hist[i] = 0
            self._pause_hist_nonzero = False
        self._last_voice_ts = None

    def _accumulate_vad_prob(self, prob: float) -> None:
        """Record one chunk probability for the in-flight speech segment."""
        p = float(prob)
        self._vad_prob_sum += p
        self._vad_prob_count += 1
        if p > self._vad_prob_max:
            self._vad_prob_max = p
        if p >= float(self.config.threshold):
            self._vad_voiced_count += 1

    def _take_vad_stats(
        self,
        *,
        truncated: bool = False,
        endpoint_reason: Optional[str] = None,
        tail_silence_samples: Optional[int] = None,
    ) -> VadSegmentStats:
        """Build segment stats and reset accumulators.

        Soft-split uses truncated=True: remaining audio after the cut starts a
        fresh accumulator (we do not attempt sample-proportional prob splits).

        endpoint_reason (when given) attaches the endpoint telemetry block:
        why this segment committed, how much endpoint-accounting silence had
        accumulated at commit, the wall-clock gap from the last voiced chunk
        to commit, and the packed intra-segment pause histogram.  Observation
        only — never feeds back into endpoint decisions.
        """
        if self._vad_prob_count <= 0:
            stats: VadSegmentStats = {
                "avg": -1.0,
                "max": -1.0,
                "voiced_ratio": -1.0,
            }
        else:
            stats = {
                "avg": self._vad_prob_sum / self._vad_prob_count,
                "max": self._vad_prob_max,
                "voiced_ratio": self._vad_voiced_count / self._vad_prob_count,
            }
        if truncated:
            stats["truncated"] = True
        if endpoint_reason is not None:
            stats["endpoint_reason"] = endpoint_reason
            if tail_silence_samples is not None:
                stats["tail_silence_ms"] = (
                    tail_silence_samples * 1000.0 / self.config.sample_rate
                )
            if self._last_voice_ts is not None:
                stats["last_voice_to_commit_ms"] = (
                    time.monotonic() - self._last_voice_ts
                ) * 1000.0
            # Pack: trim trailing zero buckets ([] = no intra-segment pauses).
            hist = self._pause_hist
            last = -1
            for i in range(len(hist)):
                if hist[i]:
                    last = i
            stats["pause_histogram"] = list(hist[: last + 1])
        self._reset_vad_prob_accum()
        return stats

    def peek_vad_prob_max(self) -> float:
        """Read the in-flight segment's Silero probability peak WITHOUT
        consuming the accumulators (interim caption gate, 2026-07-29).

        -1.0 sentinel when no chunk has been scored since the last commit —
        callers must treat it as "no evidence", never as low probability.
        """
        if self._vad_prob_count <= 0:
            return -1.0
        return float(self._vad_prob_max)

    def take_manual_stop_stats(self) -> Optional[VadSegmentStats]:
        """Segment stats for a hotkey/manual stop (endpoint_reason='manual_stop').

        Called by AudioCapture.stop() AFTER the stream is closed and BEFORE the
        VAD buffers are drained, so the in-flight segment accumulators are
        intact.  Returns None when nothing was in flight (no voiced chunk seen
        since the last commit/reset) so callers can keep passing vad_stats=None
        exactly as before.
        """
        if self._vad_prob_count <= 0 and self._last_voice_ts is None:
            return None
        return self._take_vad_stats(
            endpoint_reason="manual_stop",
            tail_silence_samples=self._silence_samples,
        )

    @staticmethod
    def _invoke_segment_callback(
        callback: Optional[Callable],
        audio: np.ndarray,
        vad_stats: Optional[VadSegmentStats],
    ) -> None:
        """Call speech_end/soft_split with optional stats; tolerate 1-arg callbacks."""
        if callback is None:
            return
        try:
            callback(audio, vad_stats)
        except TypeError:
            callback(audio)

    @staticmethod
    def _compute_rms(audio: np.ndarray) -> float:
        """Raw chunk RMS in normalized float32 range."""
        try:
            rms_audio = audio.astype(np.float32, copy=False)
            if rms_audio.size == 0:
                return 0.0
            if np.max(np.abs(rms_audio)) > 1.0:
                rms_audio = rms_audio / 32768.0
            return float(np.sqrt(np.mean(rms_audio**2)))
        except Exception:
            return 0.0

    def _is_steady_tail_noise(self, rms: float, prob: float) -> bool:
        """Return True when recent speech-labeled chunks look like steady noise.

        This is endpoint-only.  It is intentionally gated by already-started
        speech and minimum speech duration so it cannot stop a real user from
        starting dictation in a noisy room.
        """
        cfg = self.config
        if cfg.speech_end_steady_noise_ms <= 0:
            return False
        if not self._is_speaking:
            return False
        if (
            not self._tail_noise_override_active
            and self._speech_samples < cfg.speech_end_steady_noise_min_speech_samples
        ):
            return False
        if rms < float(cfg.speech_end_steady_noise_min_rms or 0.0):
            return False

        window_chunks = max(
            3,
            int(round(cfg.speech_end_steady_noise_ms / cfg.chunk_size_ms)),
        )
        if len(self._tail_rms_history) < window_chunks:
            return False

        recent = np.asarray(list(self._tail_rms_history)[-window_chunks:], dtype=float)
        mean = float(np.mean(recent))
        if mean <= 0:
            return False
        std = float(np.std(recent))
        cv = std / max(mean, 1e-8)
        peak = max(float(self._speech_peak_rms or 0.0), float(np.max(recent)))
        recent_prob = np.asarray(
            list(self._tail_prob_history)[-window_chunks:], dtype=float
        )
        prob_mean = float(np.mean(recent_prob)) if recent_prob.size else float(prob)
        prob_peak = max(
            float(self._speech_peak_prob or 0.0),
            float(np.max(recent_prob)) if recent_prob.size else float(prob),
        )

        # Normal post-speech case: the tail is flatter and lower than the
        # speech peak, e.g. airplane/fan continues after the user stops.
        lower_than_speech_peak = peak > 0 and mean <= peak * float(
            cfg.speech_end_steady_noise_peak_ratio
        )
        # Some noisy environments overlap the user's RMS level (close mic,
        # loud airplane/fan), but Silero's probability still drops after the
        # real phonemes stop.  Let that probability drop be a second safe
        # signal, while still requiring the raw energy tail to be stable.
        lower_than_speech_prob = prob_peak > 0 and prob_mean <= prob_peak * float(
            cfg.speech_end_steady_noise_peak_ratio
        )
        if cv <= float(cfg.speech_end_steady_noise_max_cv) and (
            lower_than_speech_peak or lower_than_speech_prob
        ):
            return True

        # Once the ratio gate has started an endpoint override, keep it stable
        # through a longer flat window.  Do not use flatness alone to START an
        # override: a monotone sustained vowel can also be flat, and false
        # cutting real speech is worse than waiting for max_speech_ms.
        flat_ms = max(0, int(cfg.speech_end_steady_noise_flat_ms or 0))
        if self._tail_noise_override_active and flat_ms > 0:
            flat_chunks = max(window_chunks, int(round(flat_ms / cfg.chunk_size_ms)))
            if len(self._tail_rms_history) >= flat_chunks:
                flat_recent = np.asarray(
                    list(self._tail_rms_history)[-flat_chunks:], dtype=float
                )
                flat_mean = float(np.mean(flat_recent))
                flat_cv = float(np.std(flat_recent)) / max(flat_mean, 1e-8)
                if flat_mean >= float(
                    cfg.speech_end_steady_noise_min_rms or 0.0
                ) and flat_cv <= float(cfg.speech_end_steady_noise_flat_max_cv):
                    return True

        return False

    def process_chunk(self, audio: np.ndarray) -> Tuple[bool, float]:
        """
        Process a single audio chunk.

        Args:
            audio: Audio samples (float32, mono, 16kHz)

        Returns:
            (is_speech, probability) tuple
        """
        self._ensure_model()

        # Ensure correct format
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        # Normalize if needed
        if np.max(np.abs(audio)) > 1.0:
            audio = audio / 32768.0

        if self._use_fallback:
            # Energy-based fallback VAD (last resort, no torch/onnx needed).
            # Checked BEFORE the onnx path: tests (and emergency kill-switches)
            # force this flag to get deterministic energy behavior even when an
            # onnx session was loaded at construction time.
            # RMS energy normalized to [0, 1] range
            rms = np.sqrt(np.mean(audio**2))
            # Map RMS to probability-like value (tuned for speech detection)
            # Typical speech RMS is 0.02-0.2, silence is <0.01
            prob = min(1.0, rms / 0.1)  # Scale so 0.1 RMS = 1.0 prob
            is_speech = prob >= self.config.threshold
            return is_speech, prob

        if self._onnx_vad is not None:
            # Torch-free Silero-VAD: same weights as the torch path, so the
            # same probability threshold and endpoint guards apply.
            prob = self._onnx_silero_prob(audio)
            is_speech = prob >= self.config.threshold
            return is_speech, prob

        # Silero-VAD model
        import torch

        tensor = torch.from_numpy(audio)
        prob = self._model(tensor, self.config.sample_rate).item()

        is_speech = prob >= self.config.threshold

        return is_speech, prob

    def process_chunk_with_state(
        self, audio: np.ndarray
    ) -> Tuple[str, Optional[np.ndarray]]:
        """
        Process chunk with state machine for speech segment detection.

        Returns:
            (event, audio_data) where event is:
            - "speech_start": Speech just started
            - "speech_continue": Speech continuing
            - "speech_end": Speech just ended, audio_data contains full segment
            - "silence": No speech detected

        Thread Safety:
            Uses _buffer_lock to protect buffer operations, synchronized with
            get_current_speech_buffer() and AudioCapture.stop().
        """
        is_speech, prob = self.process_chunk(audio)
        raw_rms = self._compute_rms(audio)

        if self._is_speaking and is_speech:
            self._tail_rms_history.append(raw_rms)
            self._tail_prob_history.append(prob)
            if not self._tail_noise_override_active:
                self._speech_peak_rms = max(self._speech_peak_rms, raw_rms)
                self._speech_peak_prob = max(self._speech_peak_prob, prob)

        # Tail micro-noise endpoint guard.
        #
        # Important: this runs only after speech has already started and after
        # a short amount of confirmed speech.  It never blocks speech_start, so
        # actual quiet users can still trigger whisper mode; it only lets the
        # state machine accumulate silence when the "speech" signal has fallen
        # to a tiny raw RMS floor.
        if (
            is_speech
            and self._is_speaking
            and self.config.speech_end_micro_rms > 0
            and self._speech_samples >= self.config.speech_end_micro_min_speech_samples
        ):
            if raw_rms < self.config.speech_end_micro_rms:
                self._tail_noise_override_active = True
                is_speech = False

        if (
            is_speech
            and self._is_speaking
            and self._is_steady_tail_noise(raw_rms, prob)
        ):
            self._tail_noise_override_active = True
            is_speech = False

        # Relative energy-collapse endpoint guard.
        #
        # Whisper-mode tail junk (breath gasps, fabric/mouth noise) is erratic
        # — the steady-noise CV gate never engages on it — and often above the
        # absolute micro-RMS floor, yet far below the chunk-level peak of the
        # real speech before it.  A chunk whose raw RMS collapses below
        # ratio x speech_peak (clamped by abs_ceiling so one loud spike cannot
        # push the floor into real-whisper territory) and whose probability is
        # not confidently speech counts as silence for endpoint accounting.
        if (
            is_speech
            and self._is_speaking
            and self.config.speech_end_rel_drop_ratio > 0
            and self._speech_peak_rms > 0
            and self._speech_samples
            >= self.config.speech_end_rel_drop_min_speech_samples
        ):
            rel_floor = min(
                self._speech_peak_rms * float(self.config.speech_end_rel_drop_ratio),
                float(self.config.speech_end_rel_drop_abs_ceiling),
            )
            if raw_rms <= rel_floor and prob <= float(
                self.config.speech_end_rel_drop_prob_ceiling
            ):
                self._tail_noise_override_active = True
                is_speech = False

        # Override-cancel debounce.  Historically ANY chunk that slipped past
        # the endpoint guards zeroed the accumulated silence below — whisper
        # junk produces such blips every second or two, so min_silence_ms could
        # never complete and the utterance stayed open until the user toggled
        # the hotkey.  While an override is active, require N CONSECUTIVE
        # speech chunks to cancel it; isolated blips keep counting as silence.
        # The audio itself is buffered either way, so a real resumption loses
        # nothing — its first chunks land in the segment as trailing audio.
        if is_speech and self._tail_noise_override_active:
            self._tail_override_speech_streak += 1
            cancel_after = max(
                1, int(self.config.speech_end_override_cancel_chunks or 1)
            )
            if self._tail_override_speech_streak < cancel_after:
                is_speech = False
        elif is_speech:
            self._tail_override_speech_streak = 0
        else:
            self._tail_override_speech_streak = 0

        # Store in pre-buffer (for capturing speech start)
        # Use lock to synchronize with readers (Timer thread, main thread)
        with self._buffer_lock:
            self._pre_buffer.append(audio.copy())

        if is_speech:
            # Endpoint telemetry: a silence gap that ends with resumed speech
            # is an intra-segment pause — bucket it (100ms steps) before the
            # counter resets.  The final tail (gap that ends in a commit) is
            # reported separately as tail_silence_ms, never counted here.
            if self._is_speaking and self._silence_samples > 0:
                bucket = self._silence_samples // self._pause_bucket_samples
                if bucket >= self._PAUSE_HIST_BUCKETS:
                    bucket = self._PAUSE_HIST_BUCKETS - 1
                self._pause_hist[bucket] += 1
                self._pause_hist_nonzero = True
            self._last_voice_ts = time.monotonic()
            self._silence_samples = 0
            self._tail_noise_override_active = False
            self._tail_override_speech_streak = 0

            if not self._is_speaking:
                # Accumulate consecutive speech chunks until threshold
                self._speech_samples += len(audio)
                self._speech_peak_rms = max(self._speech_peak_rms, raw_rms)
                self._speech_peak_prob = max(self._speech_peak_prob, prob)

                if self._speech_samples >= self.config.min_speech_samples:
                    self._is_speaking = True
                    self._tail_rms_history.clear()
                    self._tail_prob_history.clear()
                    self._tail_rms_history.append(raw_rms)
                    self._tail_prob_history.append(prob)

                    # Include pre-buffer for natural start
                    with self._buffer_lock:
                        for pre_chunk in self._pre_buffer:
                            self._speech_buffer.append(pre_chunk)
                    # Prob for the triggering chunk; pre-buffer chunks have no
                    # stored probs — segment stats cover speech-labeled frames
                    # from speech_start onward (honest partial coverage).
                    self._accumulate_vad_prob(prob)

                    if self._on_speech_start:
                        self._on_speech_start()

                    if self._on_speech_chunk:
                        self._on_speech_chunk(audio, prob)

                    return "speech_start", None
                else:
                    return "silence", None
            else:
                # Speech continuing
                self._speech_samples += len(audio)
                self._speech_peak_rms = max(self._speech_peak_rms, raw_rms)
                self._speech_peak_prob = max(self._speech_peak_prob, prob)
                with self._buffer_lock:
                    self._speech_buffer.append(audio.copy())
                self._accumulate_vad_prob(prob)

                if self._on_speech_chunk:
                    self._on_speech_chunk(audio, prob)

                # Check max speech length - force segment to prevent accumulation
                if self._speech_samples >= self.config.max_speech_samples:
                    # pythonw.exe safe logging
                    import sys

                    if sys.stdout is not None:
                        print(
                            f"[VAD] Max speech reached: {self._speech_samples} >= {self.config.max_speech_samples} samples, forcing split"
                        )
                    logger.info(
                        f"Max speech length reached ({self._speech_samples} samples), forcing segment"
                    )
                    self._is_speaking = False
                    self._silence_samples = 0

                    with self._buffer_lock:
                        full_audio = np.concatenate(self._speech_buffer)
                        self._speech_buffer.clear()

                    # Reset for next segment (keep speaking state)
                    self._speech_samples = 0
                    self._speech_peak_rms = 0.0
                    self._speech_peak_prob = 0.0
                    self._tail_noise_override_active = False
                    self._tail_override_speech_streak = 0
                    self._tail_rms_history.clear()
                    self._tail_prob_history.clear()
                    self._is_speaking = True  # Immediately re-enter speaking state
                    # Soft-split / max-speech cut: reset accumulators with
                    # truncated=True (remaining audio starts a fresh window).
                    # Forced cut lands on a speech chunk — tail silence is 0.
                    vad_stats = self._take_vad_stats(
                        truncated=True,
                        endpoint_reason="max_speech",
                        tail_silence_samples=0,
                    )

                    # The user is STILL talking — a 'speech_end' here would commit
                    # (polish + paste) a fragment cut at an arbitrary sample, splitting
                    # the sentence wherever the wall happened to land. When the
                    # soft-split pipeline is available, route the forced segment
                    # through it instead: the worker buffers the raw text and the one
                    # true EOS (min_silence_ms) commits everything in a single paste.
                    if self.config.soft_split_enabled and self._on_speech_soft_split:
                        if sys.stdout is not None:
                            print(
                                "[VAD] Max-speech segment routed to soft-split buffer "
                                "(speech continues)"
                            )
                        self._invoke_segment_callback(
                            self._on_speech_soft_split, full_audio, vad_stats
                        )
                        return "speech_soft_split", full_audio

                    self._invoke_segment_callback(
                        self._on_speech_end, full_audio, vad_stats
                    )

                    return "speech_end", full_audio

                return "speech_continue", None
        else:
            # Silence detected
            if self._is_speaking:
                self._silence_samples += len(audio)
                with self._buffer_lock:
                    self._speech_buffer.append(audio.copy())  # Include trailing silence
                # Trailing silence chunks still contribute a prob sample so
                # voiced_ratio reflects the full buffered segment.
                self._accumulate_vad_prob(prob)

                # Soft split: natural pause inside a long utterance.
                # Fires exactly once per qualifying pause — on the chunk that CROSSES the
                # soft-split silence threshold (pre-chunk silence was below, post-chunk is above).
                # Requires accumulated speech >= min threshold, and silence still below the
                # full-EOS threshold.
                #
                # Pre-wall pause hunting: once accumulated speech passes 70% of
                # max_speech, the pause requirement relaxes to 240ms so the cut
                # lands on a breath instead of mid-word at the hard wall. The
                # threshold is stable for the whole pause (speech_samples doesn't
                # grow during silence), so the crossing detection stays exact.
                soft_split_silence_threshold = self.config.soft_split_silence_samples
                if self._speech_samples >= int(self.config.max_speech_samples * 0.7):
                    urgent_silence_samples = int(self.config.sample_rate * 240 / 1000)
                    soft_split_silence_threshold = min(
                        soft_split_silence_threshold, urgent_silence_samples
                    )
                just_crossed = (
                    self._silence_samples >= soft_split_silence_threshold
                    and (self._silence_samples - len(audio))
                    < soft_split_silence_threshold
                )
                if (
                    self.config.soft_split_enabled
                    and just_crossed
                    and self._speech_samples
                    >= self.config.soft_split_min_speech_samples
                    and self._silence_samples < self.config.min_silence_samples
                ):
                    import sys

                    with self._buffer_lock:
                        full_audio = np.concatenate(self._speech_buffer)
                        self._speech_buffer.clear()

                    pause_ms = self._silence_samples / self.config.sample_rate * 1000
                    # Snapshot before the reset below: the pause that triggered
                    # this cut IS the segment's tail silence.
                    tail_silence_samples = self._silence_samples

                    # Keep _is_speaking=True so the min_silence_ms EOS path can still
                    # fire when the user stops talking after a soft split. Without this,
                    # subsequent silence chunks would be dropped on the floor and the
                    # buffered ASR text would wait forever for the NEXT utterance to
                    # drain it — causing the "sentence doesn't commit until I speak
                    # again" bug. The resulting trailing-silence speech_end segment is
                    # handled at the worker layer: the pre-ASR energy gate flips
                    # skip_asr=True for near-silence audio, and `kind='final' +
                    # _prior_buffered_text` routes straight to the commit path without
                    # a bogus ASR call or ghost segment.
                    self._speech_samples = 0
                    self._silence_samples = 0
                    self._speech_peak_rms = 0.0
                    self._speech_peak_prob = 0.0
                    self._tail_noise_override_active = False
                    self._tail_override_speech_streak = 0
                    self._tail_rms_history.clear()
                    self._tail_prob_history.clear()

                    # Soft-split: reset accumulators; remaining utterance starts
                    # fresh. truncated=True documents that post-cut probs are
                    # not carried over (we do not split the parallel list).
                    vad_stats = self._take_vad_stats(
                        truncated=True,
                        endpoint_reason="soft_split",
                        tail_silence_samples=tail_silence_samples,
                    )

                    if sys.stdout is not None:
                        print(
                            f"[VAD] Soft split at {pause_ms:.0f}ms pause "
                            f"(segment={len(full_audio) / self.config.sample_rate:.1f}s)"
                        )
                    logger.info(
                        f"Soft split fired (segment={len(full_audio)} samples, pause={pause_ms:.0f}ms)"
                    )

                    self._invoke_segment_callback(
                        self._on_speech_soft_split, full_audio, vad_stats
                    )

                    return "speech_soft_split", full_audio

                if self._silence_samples >= self.config.min_silence_samples:
                    # Speech ended
                    self._is_speaking = False
                    self._speech_peak_rms = 0.0
                    self._speech_peak_prob = 0.0
                    self._tail_noise_override_active = False
                    self._tail_override_speech_streak = 0
                    self._tail_rms_history.clear()
                    self._tail_prob_history.clear()

                    # Concatenate all speech audio
                    with self._buffer_lock:
                        full_audio = np.concatenate(self._speech_buffer)
                        self._speech_buffer.clear()

                    # _silence_samples is still intact here (only reset when
                    # speech resumes) — it IS the tail-silence wait that just
                    # completed (>= min_silence_ms by construction).
                    vad_stats = self._take_vad_stats(
                        truncated=False,
                        endpoint_reason="tail_silence",
                        tail_silence_samples=self._silence_samples,
                    )
                    self._invoke_segment_callback(
                        self._on_speech_end, full_audio, vad_stats
                    )

                    return "speech_end", full_audio
                else:
                    return "speech_continue", None
            else:
                self._speech_samples = 0
                self._speech_peak_rms = 0.0
                self._speech_peak_prob = 0.0
                self._tail_noise_override_active = False
                self._tail_override_speech_streak = 0
                self._tail_rms_history.clear()
                self._tail_prob_history.clear()
                self._reset_vad_prob_accum()
                return "silence", None

    def set_callbacks(
        self,
        on_speech_start: Optional[Callable[[], None]] = None,
        on_speech_end: Optional[Callable[..., None]] = None,
        on_speech_chunk: Optional[Callable[[np.ndarray, float], None]] = None,
        on_speech_soft_split: Optional[Callable[..., None]] = None,
    ) -> None:
        """Set callback functions for speech events.

        on_speech_end / on_speech_soft_split may accept (audio) or
        (audio, vad_stats=None) for backward compatibility.
        """
        self._on_speech_start = on_speech_start
        self._on_speech_end = on_speech_end
        self._on_speech_chunk = on_speech_chunk
        self._on_speech_soft_split = on_speech_soft_split

    @property
    def is_speaking(self) -> bool:
        """Check if currently detecting speech."""
        return self._is_speaking

    def get_current_speech_buffer(self) -> Optional[np.ndarray]:
        """
        获取当前已累积的语音缓冲（用于流式识别）。
        线程安全：返回缓冲区的快照副本。

        Returns:
            累积的语音音频数据，如果没有则返回 None
        """
        with self._buffer_lock:
            if not self._speech_buffer:
                return None
            # Return a copy to avoid race conditions
            return np.concatenate(self._speech_buffer)

    def get_speech_duration_ms(self) -> float:
        """
        获取当前语音段的持续时间（毫秒）。
        线程安全。

        Returns:
            语音持续时间（毫秒）
        """
        with self._buffer_lock:
            if not self._speech_buffer:
                return 0.0
            total_samples = sum(len(chunk) for chunk in self._speech_buffer)
            return total_samples / self.config.sample_rate * 1000

    def get_speech_timestamps(
        self, audio: np.ndarray, return_seconds: bool = True
    ) -> List[dict]:
        """
        Get speech timestamps from a complete audio buffer.

        This is for offline processing. For real-time, use process_chunk().

        Args:
            audio: Complete audio buffer
            return_seconds: Return timestamps in seconds (vs samples)

        Returns:
            List of {'start': float, 'end': float} dicts
        """
        # Offline silero path — needs the torch build. Guarded so the slim
        # (torch-free) build never crashes on import if this batch helper is
        # ever called; the realtime path uses process_chunk() instead.
        try:
            from silero_vad import get_speech_timestamps
            import torch
        except ImportError:
            logger.warning(
                "get_speech_timestamps needs silero_vad/torch (absent in slim "
                "build); returning no timestamps"
            )
            return []

        self._ensure_model()

        if self._model is None:
            # silero_vad is importable but the active backend is onnx (or the
            # energy fallback), so no torch-jit model was ever loaded. Batch
            # timestamps are torch-only; bail instead of AttributeError-ing.
            logger.warning(
                "get_speech_timestamps requires the torch Silero backend "
                "(current backend has no torch model); returning no timestamps"
            )
            return []

        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        tensor = torch.from_numpy(audio)

        timestamps = get_speech_timestamps(
            tensor,
            self._model,
            sampling_rate=self.config.sample_rate,
            threshold=self.config.threshold,
            min_speech_duration_ms=self.config.min_speech_ms,
            min_silence_duration_ms=self.config.min_silence_ms,
            return_seconds=return_seconds,
        )

        return timestamps


def create_vad(config: Optional[VADConfig] = None) -> VADProcessor:
    """Factory function to create VAD processor."""
    return VADProcessor(config)
