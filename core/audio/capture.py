"""
Audio Capture Module with VAD Integration
=========================================
Real-time audio capture using WASAPI (Windows) with integrated VAD filtering.

Based on POC#4 validation + market report recommendations:
- Only send speech segments to ASR (not silence)
- Enable streaming mode for low-latency feedback
"""

import numpy as np
import threading
import queue
import time
from dataclasses import dataclass
from typing import Optional, Callable, List, Generator
from enum import Enum, auto

from .vad import VADProcessor, VADConfig
from ..logging import get_audio_logger

logger = get_audio_logger()


@dataclass
class AudioConfig:
    """Audio capture configuration."""

    sample_rate: int = 16000  # 16kHz for ASR
    channels: int = 1  # Mono
    dtype: str = "float32"  # Audio format
    chunk_duration_ms: int = 32  # Chunk size (matches VAD optimal)
    device_id: Optional[int] = None  # None = default device
    enable_vad: bool = True  # Enable VAD filtering
    vad_config: Optional[VADConfig] = None

    @property
    def chunk_size(self) -> int:
        """Samples per chunk."""
        return int(self.sample_rate * self.chunk_duration_ms / 1000)


class CaptureState(Enum):
    """Audio capture states."""

    IDLE = auto()
    RECORDING = auto()
    PAUSED = auto()
    STOPPING = auto()


class AudioCapture:
    """
    Real-time audio capture with VAD integration.

    Features:
    - WASAPI shared mode (Windows)
    - Integrated Silero-VAD for speech detection
    - Streaming mode with callbacks
    - Device monitoring and auto-recovery

    Usage:
        capture = AudioCapture()

        # Callback mode (recommended for real-time)
        capture.set_callbacks(
            on_speech_start=lambda: print("Speaking..."),
            on_speech_chunk=lambda chunk, prob: process(chunk),
            on_speech_end=lambda audio: send_to_asr(audio)
        )
        capture.start()
        ...
        capture.stop()

        # Or generator mode
        for event, data in capture.stream():
            if event == "speech_end":
                transcribe(data)
    """

    def __init__(self, config: Optional[AudioConfig] = None):
        self.config = config or AudioConfig()
        self._state = CaptureState.IDLE
        self._lock = threading.Lock()

        # VAD processor
        self._vad: Optional[VADProcessor] = None
        if self.config.enable_vad:
            vad_config = self.config.vad_config or VADConfig(
                sample_rate=self.config.sample_rate
            )
            self._vad = VADProcessor(vad_config)

        # Audio stream
        self._stream = None
        self._thread: Optional[threading.Thread] = None

        # Data queues
        self._chunk_queue: queue.Queue = queue.Queue(maxsize=100)
        self._speech_queue: queue.Queue = queue.Queue(maxsize=10)

        # Callbacks
        self._on_speech_start: Optional[Callable[[], None]] = None
        self._on_speech_end: Optional[Callable[[np.ndarray], None]] = None
        self._on_speech_chunk: Optional[Callable[[np.ndarray, float], None]] = None
        self._on_audio_level: Optional[Callable[[float], None]] = None
        self._latest_level: float = 0.0
        self._level_event = threading.Event()
        self._level_stop_event = threading.Event()
        self._level_thread: Optional[threading.Thread] = None
        self._level_dispatch_hz = 30.0

        # Statistics
        self._total_samples = 0
        self._speech_samples = 0

    def set_callbacks(
        self,
        on_speech_start: Optional[Callable[[], None]] = None,
        on_speech_end: Optional[Callable[[np.ndarray], None]] = None,
        on_speech_chunk: Optional[Callable[[np.ndarray, float], None]] = None,
        on_audio_level: Optional[Callable[[float], None]] = None,
    ) -> None:
        """Set callback functions for audio events."""
        self._on_speech_start = on_speech_start
        self._on_speech_end = on_speech_end
        self._on_speech_chunk = on_speech_chunk
        self._on_audio_level = on_audio_level

        # Also set VAD callbacks
        if self._vad:
            self._vad.set_callbacks(
                on_speech_start=on_speech_start,
                on_speech_end=on_speech_end,
                on_speech_chunk=on_speech_chunk,
            )

        if self._state == CaptureState.RECORDING:
            if self._on_audio_level:
                self._start_level_dispatcher()
            else:
                self._stop_level_dispatcher()

    def _start_level_dispatcher(self) -> None:
        """Dispatch audio level updates off the real-time PortAudio thread."""
        if not self._on_audio_level:
            return
        if self._level_thread and self._level_thread.is_alive():
            return
        self._level_stop_event.clear()
        self._level_event.clear()
        self._level_thread = threading.Thread(
            target=self._level_dispatch_loop,
            daemon=True,
            name="audio-level-dispatch",
        )
        self._level_thread.start()

    def _stop_level_dispatcher(self) -> None:
        self._level_stop_event.set()
        self._level_event.set()
        if self._level_thread and self._level_thread.is_alive():
            self._level_thread.join(timeout=1.0)
        self._level_thread = None
        self._level_event.clear()

    def _level_dispatch_loop(self) -> None:
        min_interval = 1.0 / self._level_dispatch_hz
        last_emit = 0.0

        while not self._level_stop_event.is_set():
            self._level_event.wait(0.5)
            if self._level_stop_event.is_set():
                break
            if not self._level_event.is_set():
                continue

            self._level_event.clear()
            callback = self._on_audio_level
            if not callback:
                continue

            now = time.perf_counter()
            if last_emit:
                remaining = min_interval - (now - last_emit)
                if remaining > 0 and self._level_stop_event.wait(remaining):
                    break

            try:
                callback(float(self._latest_level))
            except Exception as e:
                logger.debug(f"Audio level callback failed: {e}")

            last_emit = time.perf_counter()

    def _audio_callback(self, indata, frames, time_info, status):
        """Callback from sounddevice stream."""
        if status:
            logger.warning(f"Audio stream status: {status}")

        if self._state != CaptureState.RECORDING:
            return

        # Convert to numpy array
        audio = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
        audio = audio.astype(np.float32)

        self._total_samples += len(audio)

        # Report audio level outside the PortAudio real-time thread.
        level = float(np.max(np.abs(audio))) if audio.size else 0.0
        if self._on_audio_level:
            self._latest_level = level
            self._level_event.set()

        # Process through VAD if enabled
        if self._vad:
            event, speech_audio = self._vad.process_chunk_with_state(audio)

            if event == "speech_end" and speech_audio is not None:
                self._speech_samples += len(speech_audio)
                # Non-blocking put - drop if queue full (backpressure)
                try:
                    self._speech_queue.put_nowait(("speech_end", speech_audio))
                except queue.Full:
                    logger.warning("Speech queue full, dropping segment")

            elif event in ["speech_start", "speech_continue"]:
                try:
                    self._chunk_queue.put_nowait(("chunk", audio))
                except queue.Full:
                    pass  # Drop chunk silently if queue full

        else:
            # No VAD - pass all audio
            try:
                self._chunk_queue.put_nowait(("chunk", audio))
            except queue.Full:
                pass  # Drop chunk silently if queue full

    def start(self) -> bool:
        """Start audio capture."""
        with self._lock:
            if self._state != CaptureState.IDLE:
                logger.warning("Capture already started")
                return False

            try:
                import sounddevice as sd

                # Reset VAD state
                if self._vad:
                    self._vad.reset()

                # Clear queues
                while not self._chunk_queue.empty():
                    self._chunk_queue.get_nowait()
                while not self._speech_queue.empty():
                    self._speech_queue.get_nowait()

                # Reset stats
                self._total_samples = 0
                self._speech_samples = 0
                self._latest_level = 0.0

                if self._on_audio_level:
                    self._start_level_dispatcher()
                else:
                    self._stop_level_dispatcher()

                # Open stream
                self._stream = sd.InputStream(
                    device=self.config.device_id,
                    samplerate=self.config.sample_rate,
                    channels=self.config.channels,
                    dtype=self.config.dtype,
                    blocksize=self.config.chunk_size,
                    callback=self._audio_callback,
                )

                self._stream.start()
                self._state = CaptureState.RECORDING

                logger.info(f"Audio capture started (device={self.config.device_id})")
                return True

            except Exception as e:
                self._stop_level_dispatcher()
                logger.error(f"Failed to start capture: {e}")
                return False

    def stop(self) -> Optional[np.ndarray]:
        """
        Stop audio capture.

        Returns:
            Final speech segment if any (from VAD buffer), or None.
            NOTE: Only returns audio that was NOT yet processed by callbacks.
            Audio that already triggered on_speech_end callback is NOT returned
            to avoid duplicate processing.
        """
        final_speech = None

        with self._lock:
            if self._state == CaptureState.IDLE:
                self._stop_level_dispatcher()
                return None

            self._state = CaptureState.STOPPING

            # Close stream first to stop new callbacks
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None

            # Get any remaining speech from VAD (after stream stopped)
            # Check both _speech_buffer and _pre_buffer for short utterances
            # CRITICAL: Use VAD's _buffer_lock to prevent race condition with
            # Timer thread calling get_current_speech_buffer() concurrently
            if self._vad:
                with self._vad._buffer_lock:
                    # DEBUG: Log buffer states at stop time
                    logger.info(
                        f"Capture.stop(): _speech_buffer len={len(self._vad._speech_buffer)}, "
                        f"_pre_buffer len={len(self._vad._pre_buffer)}, "
                        f"_is_speaking={self._vad._is_speaking}, "
                        f"_speech_samples={self._vad._speech_samples}"
                    )

                    buffers_to_concat = []

                    # Primary buffer (speech already confirmed)
                    if self._vad._speech_buffer:
                        logger.info(
                            f"Capture: using _speech_buffer ({len(self._vad._speech_buffer)} chunks)"
                        )
                        buffers_to_concat.extend(self._vad._speech_buffer)
                        self._vad._speech_buffer.clear()
                    # Pre-buffer fallback: use ring buffer unconditionally when primary is empty
                    # This catches short utterances where VAD detected speech but _speech_samples
                    # was reset to 0 by silence before stop() was called (vad.py:220 issue)
                    elif self._vad._pre_buffer:
                        logger.info(
                            f"Capture: using pre_buffer fallback ({len(self._vad._pre_buffer)} chunks)"
                        )
                        buffers_to_concat.extend(list(self._vad._pre_buffer))
                    else:
                        logger.warning(
                            "Capture: BOTH buffers empty! No audio to return."
                        )

                    self._vad._pre_buffer.clear()
                    self._vad._speech_samples = 0

                    if buffers_to_concat:
                        final_speech = np.concatenate(buffers_to_concat)
                        self._vad._is_speaking = False
                        logger.info(
                            f"Capture: returning buffer with {len(final_speech)} samples"
                        )

            # NOTE: We do NOT collect from _speech_queue anymore.
            # Items in the queue already triggered on_speech_end callback,
            # so they've been sent to ASR. Collecting them again would cause duplicates.
            # Just clear the queue to avoid memory buildup.
            while not self._speech_queue.empty():
                try:
                    self._speech_queue.get_nowait()
                except queue.Empty:
                    break

            self._stop_level_dispatcher()

            self._state = CaptureState.IDLE

            # Log stats
            total_ms = self._total_samples / self.config.sample_rate * 1000
            speech_ms = self._speech_samples / self.config.sample_rate * 1000
            logger.info(
                f"Capture stopped. Total: {total_ms:.0f}ms, Speech: {speech_ms:.0f}ms"
            )

        return final_speech

    def stream(self) -> Generator[tuple, None, None]:
        """
        Generator that yields audio events.

        Yields:
            (event_type, data) tuples:
            - ("chunk", np.ndarray): Raw audio chunk
            - ("speech_end", np.ndarray): Complete speech segment
        """
        if not self.start():
            return

        try:
            while self._state == CaptureState.RECORDING:
                # Check speech queue first (higher priority)
                try:
                    event = self._speech_queue.get(timeout=0.01)
                    yield event
                except queue.Empty:
                    pass

                # Check chunk queue
                try:
                    event = self._chunk_queue.get(timeout=0.01)
                    yield event
                except queue.Empty:
                    pass

        finally:
            self.stop()

    def get_speech_segment(self, timeout: float = 30.0) -> Optional[np.ndarray]:
        """
        Wait for a complete speech segment.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            Speech audio as numpy array, or None if timeout
        """
        try:
            event_type, audio = self._speech_queue.get(timeout=timeout)
            if event_type == "speech_end":
                return audio
        except queue.Empty:
            pass

        return None

    @property
    def is_recording(self) -> bool:
        """Check if currently recording."""
        return self._state == CaptureState.RECORDING

    @property
    def is_speaking(self) -> bool:
        """Check if VAD currently detects speech."""
        return self._vad.is_speaking if self._vad else False

    @staticmethod
    def list_devices() -> List[dict]:
        """List available audio input devices."""
        import sounddevice as sd

        devices = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                devices.append(
                    {
                        "id": i,
                        "name": d["name"],
                        "channels": d["max_input_channels"],
                        "sample_rate": d["default_samplerate"],
                        "is_default": i == sd.default.device[0],
                    }
                )

        return devices
