"""
Whisper ASR Engine (faster-whisper)
===================================
Local speech recognition using faster-whisper (CTranslate2).

4-5x faster than openai-whisper with same accuracy.
Supports large-v3-turbo for additional 5x speedup.
"""

# Fix OpenMP conflict between PyTorch and faster-whisper
import os
os.environ['KMP_DUPLICATE_LIB_OK'] = 'TRUE'

import numpy as np
import time
from dataclasses import dataclass
from typing import Optional, Generator
import threading

from .base import ASREngine, ASRResult, TranscriptType
from ..logging import get_asr_logger

logger = get_asr_logger()


@dataclass
class WhisperConfig:
    """Whisper engine configuration."""
    model_name: str = "large-v3-turbo"  # Default to turbo for speed
    device: str = "cuda"                 # cpu, cuda
    language: Optional[str] = "zh"       # None = auto-detect
    compute_type: str = "float16"        # float16, int8, int8_float16

    # HotWord support - vocabulary hints for decoder
    initial_prompt: Optional[str] = None

    # Beam size (faster-whisper default is 5, openai-whisper is 1)
    beam_size: int = 5

    @property
    def sample_rate(self) -> int:
        """Whisper requires 16kHz."""
        return 16000


class WhisperEngine(ASREngine):
    """
    Local Whisper ASR engine using faster-whisper.

    Features:
    - 4-5x faster than openai-whisper
    - Runs completely offline (privacy)
    - Zero marginal cost
    - HotWord support via initial_prompt

    Recommended models:
    - large-v3-turbo: Best speed/accuracy (default)
    - large-v3: Highest accuracy, slower
    - medium: Good balance for weaker GPUs
    - small: Fast, lower accuracy
    """

    def __init__(self, config: Optional[WhisperConfig] = None):
        self.config = config or WhisperConfig()
        self._model = None
        self._lock = threading.Lock()
        self._load_time = 0.0

    @property
    def is_loaded(self) -> bool:
        """Check if model is loaded."""
        return self._model is not None

    def load(self) -> None:
        """Load Whisper model."""
        if self._model is not None:
            return

        with self._lock:
            if self._model is not None:
                return

            try:
                from faster_whisper import WhisperModel

                logger.info(f"Loading faster-whisper model: {self.config.model_name}")
                start = time.time()

                self._model = WhisperModel(
                    self.config.model_name,
                    device=self.config.device,
                    compute_type=self.config.compute_type
                )

                self._load_time = time.time() - start
                logger.info(f"faster-whisper model loaded in {self._load_time:.1f}s")

            except ImportError:
                raise ImportError(
                    "faster-whisper not installed. Run: pip install faster-whisper"
                )

    def unload(self) -> None:
        """Unload model to free memory."""
        with self._lock:
            if self._model is not None:
                del self._model
                self._model = None
                logger.info("Whisper model unloaded")

    def set_initial_prompt(self, prompt: str) -> None:
        """Update the initial_prompt for hotword support."""
        self.config.initial_prompt = prompt
        log_prompt = prompt[:50] + "..." if len(prompt) > 50 else prompt
        logger.info(f"Set initial_prompt: {log_prompt}")

    def transcribe(self, audio: np.ndarray) -> ASRResult:
        """
        Transcribe audio buffer.

        Args:
            audio: Audio samples (float32, mono, 16kHz)

        Returns:
            ASRResult with transcription
        """
        self.load()

        start_time = time.time()

        # Ensure float32
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        try:
            # Build transcribe kwargs
            transcribe_kwargs = {
                'language': self.config.language,
                'task': 'transcribe',
                'beam_size': self.config.beam_size,
                'vad_filter': True,  # Filter out silence
                # Disable conditioning on previous text to prevent hallucination loops
                # When True (default), ASR errors can propagate to subsequent segments
                'condition_on_previous_text': False,
            }

            # Add initial_prompt if set (HotWord support)
            if self.config.initial_prompt:
                transcribe_kwargs['initial_prompt'] = self.config.initial_prompt

            # Transcribe - faster-whisper accepts numpy array directly
            segments, info = self._model.transcribe(audio, **transcribe_kwargs)

            # Collect all segment texts
            texts = []
            for segment in segments:
                texts.append(segment.text)

            full_text = "".join(texts).strip()

            end_time = time.time()
            transcribe_time = (end_time - start_time) * 1000
            audio_duration = len(audio) / self.config.sample_rate

            logger.info(f"Transcribed {audio_duration:.1f}s audio in {transcribe_time:.0f}ms "
                       f"({audio_duration / (transcribe_time/1000):.1f}x realtime)")

            return ASRResult(
                text=full_text,
                type=TranscriptType.FINAL,
                confidence=1.0,
                start_time=0.0,
                end_time=audio_duration,
                language=info.language if info else "zh",
                language_confidence=info.language_probability if info else 1.0
            )

        except Exception as e:
            logger.error(f"Transcription error: {e}")
            raise

    def transcribe_stream(
        self,
        audio_generator: Generator[np.ndarray, None, None]
    ) -> Generator[ASRResult, None, None]:
        """
        Streaming transcription with interim results.
        """
        self.load()

        # Accumulate audio
        buffer = []
        chunk_samples = int(5.0 * self.config.sample_rate)  # 5s chunks

        for chunk in audio_generator:
            buffer.append(chunk)
            buffer_samples = sum(len(c) for c in buffer)

            if buffer_samples >= chunk_samples:
                audio = np.concatenate(buffer)
                result = self.transcribe(audio)

                if result.text:
                    yield ASRResult(
                        text=result.text,
                        type=TranscriptType.INTERIM,
                        confidence=0.8,
                        language=result.language
                    )

                # Keep last 0.5s for continuity
                overlap_samples = int(0.5 * self.config.sample_rate)
                if len(audio) > overlap_samples:
                    buffer = [audio[-overlap_samples:]]
                else:
                    buffer = []

        # Process remaining
        if buffer:
            audio = np.concatenate(buffer)
            if len(audio) > self.config.sample_rate * 0.1:
                result = self.transcribe(audio)
                yield ASRResult(
                    text=result.text,
                    type=TranscriptType.FINAL,
                    confidence=1.0
                )
