"""
Voice Activity Detection (VAD) Module
=====================================
Based on Silero-VAD for efficient speech detection.

Key benefits (from 2025 market report):
- Filter silence before sending to ASR (saves compute/cost)
- Reduce latency by only processing speech segments
- Enable "streaming chunks" mode for real-time feedback
"""

import threading
import numpy as np
from dataclasses import dataclass
from typing import Optional, List, Tuple, Callable
from collections import deque

from ..logging import get_audio_logger

logger = get_audio_logger()


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

    # Audio parameters
    sample_rate: int = 16000  # Must be 16000 for Silero-VAD

    # Chunk processing
    chunk_size_ms: int = 32  # Process in 32ms chunks (Silero optimal)

    # Padding
    speech_pad_ms: int = 30  # Pad speech start/end

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

    def __init__(self, config: Optional[VADConfig] = None):
        self.config = config or VADConfig()
        self._model = None
        self._is_speaking = False
        self._use_fallback = False  # True if silero_vad/torch failed to load

        # Log VAD config for debugging (pythonw.exe safe)
        import sys

        if sys.stdout is not None:
            print(
                f"[VAD] Config: threshold={self.config.threshold}, "
                f"min_silence={self.config.min_silence_ms}ms, "
                f"max_speech={self.config.max_speech_ms}ms ({self.config.max_speech_samples} samples)"
            )
        logger.info(
            f"VAD Config: threshold={self.config.threshold}, "
            f"min_silence={self.config.min_silence_ms}ms, max_speech={self.config.max_speech_ms}ms"
        )

        # State tracking
        self._speech_samples = 0
        self._silence_samples = 0
        self._speech_buffer: List[np.ndarray] = []
        self._buffer_lock = (
            threading.Lock()
        )  # Protect buffer access from multiple threads

        # Ring buffer for recent audio (for padding)
        # Need enough chunks to cover min_speech_ms + padding
        # min_speech_ms=250ms / chunk_size=32ms = ~8 chunks, use 12 for safety
        self._pre_buffer = deque(maxlen=12)  # ~384ms pre-speech buffer

        # Callbacks
        self._on_speech_start: Optional[Callable[[], None]] = None
        self._on_speech_end: Optional[Callable[[np.ndarray], None]] = None
        self._on_speech_chunk: Optional[Callable[[np.ndarray, float], None]] = None

        # Load model (with fallback if torch fails)
        self._ensure_model()

    def _ensure_model(self) -> None:
        """Load Silero-VAD model if not already loaded."""
        if self._model is not None or self._use_fallback:
            return

        try:
            from silero_vad import load_silero_vad

            self._model = load_silero_vad()
            logger.info("Silero-VAD model loaded")
        except ImportError:
            logger.warning("Silero-VAD not installed. Using energy-based fallback VAD.")
            self._use_fallback = True
        except OSError as e:
            # torch DLL loading failure (e.g., CUDA version mismatch)
            logger.warning(
                f"Silero-VAD failed to load (torch error): {e}. "
                "Using energy-based fallback VAD."
            )
            self._use_fallback = True
        except Exception as e:
            logger.warning(
                f"Silero-VAD failed to load: {e}. Using energy-based fallback VAD."
            )
            self._use_fallback = True

    def reset(self) -> None:
        """Reset VAD state (call between recordings).

        Thread Safety:
            Uses _buffer_lock to protect buffer operations.
        """
        self._is_speaking = False
        self._speech_samples = 0
        self._silence_samples = 0

        with self._buffer_lock:
            self._speech_buffer.clear()
            self._pre_buffer.clear()

        # Reset model state
        if self._model is not None:
            self._model.reset_states()

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
            # Energy-based fallback VAD (no torch needed)
            # RMS energy normalized to [0, 1] range
            rms = np.sqrt(np.mean(audio**2))
            # Map RMS to probability-like value (tuned for speech detection)
            # Typical speech RMS is 0.02-0.2, silence is <0.01
            prob = min(1.0, rms / 0.1)  # Scale so 0.1 RMS = 1.0 prob
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

        # Store in pre-buffer (for capturing speech start)
        # Use lock to synchronize with readers (Timer thread, main thread)
        with self._buffer_lock:
            self._pre_buffer.append(audio.copy())

        if is_speech:
            self._silence_samples = 0

            if not self._is_speaking:
                # Accumulate consecutive speech chunks until threshold
                self._speech_samples += len(audio)

                if self._speech_samples >= self.config.min_speech_samples:
                    self._is_speaking = True

                    # Include pre-buffer for natural start
                    with self._buffer_lock:
                        for pre_chunk in self._pre_buffer:
                            self._speech_buffer.append(pre_chunk)

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
                with self._buffer_lock:
                    self._speech_buffer.append(audio.copy())

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
                    self._is_speaking = True  # Immediately re-enter speaking state

                    if self._on_speech_end:
                        self._on_speech_end(full_audio)

                    return "speech_end", full_audio

                return "speech_continue", None
        else:
            # Silence detected
            if self._is_speaking:
                self._silence_samples += len(audio)
                with self._buffer_lock:
                    self._speech_buffer.append(audio.copy())  # Include trailing silence

                if self._silence_samples >= self.config.min_silence_samples:
                    # Speech ended
                    self._is_speaking = False

                    # Concatenate all speech audio
                    with self._buffer_lock:
                        full_audio = np.concatenate(self._speech_buffer)
                        self._speech_buffer.clear()

                    if self._on_speech_end:
                        self._on_speech_end(full_audio)

                    return "speech_end", full_audio
                else:
                    return "speech_continue", None
            else:
                self._speech_samples = 0
                return "silence", None

    def set_callbacks(
        self,
        on_speech_start: Optional[Callable[[], None]] = None,
        on_speech_end: Optional[Callable[[np.ndarray], None]] = None,
        on_speech_chunk: Optional[Callable[[np.ndarray, float], None]] = None,
    ) -> None:
        """Set callback functions for speech events."""
        self._on_speech_start = on_speech_start
        self._on_speech_end = on_speech_end
        self._on_speech_chunk = on_speech_chunk

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
        from silero_vad import get_speech_timestamps
        import torch

        self._ensure_model()

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
