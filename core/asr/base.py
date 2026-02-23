"""
ASR Engine Base Classes
=======================
Abstract interfaces for speech recognition engines.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List, Generator
from enum import Enum, auto


class TranscriptType(Enum):
    """Type of transcription result."""

    INTERIM = auto()  # Temporary, may change
    FINAL = auto()  # Confirmed, won't change


@dataclass
class ASRResult:
    """
    ASR transcription result.

    Supports both interim (unstable) and final (stable) results
    for optimistic UI display.
    """

    text: str
    type: TranscriptType = TranscriptType.FINAL

    # Confidence and timing
    confidence: float = 1.0
    start_time: float = 0.0
    end_time: float = 0.0

    # Language detection
    language: str = "en"
    language_confidence: float = 1.0

    # Word-level details (if available)
    words: List[dict] = field(default_factory=list)

    @property
    def is_interim(self) -> bool:
        """Check if this is an interim (unstable) result."""
        return self.type == TranscriptType.INTERIM

    @property
    def is_final(self) -> bool:
        """Check if this is a final (stable) result."""
        return self.type == TranscriptType.FINAL

    @property
    def duration(self) -> float:
        """Duration in seconds."""
        return self.end_time - self.start_time


class ASREngine(ABC):
    """
    Abstract base class for ASR engines.

    Implementations:
    - Qwen3ASREngine: Qwen3-ASR (local, default)
    - FunASREngine: FunASR Paraformer (local)
    """

    @abstractmethod
    def transcribe(self, audio: bytes) -> ASRResult:
        """
        Transcribe audio buffer.

        Args:
            audio: Audio data (16kHz, mono, float32)

        Returns:
            ASRResult with transcription
        """
        pass

    @abstractmethod
    def transcribe_stream(
        self, audio_generator: Generator[bytes, None, None]
    ) -> Generator[ASRResult, None, None]:
        """
        Streaming transcription.

        Yields interim results as audio is processed,
        followed by final result.

        Args:
            audio_generator: Generator yielding audio chunks

        Yields:
            ASRResult objects (interim and final)
        """
        pass

    @property
    @abstractmethod
    def is_loaded(self) -> bool:
        """Check if model is loaded and ready."""
        pass

    @abstractmethod
    def load(self) -> None:
        """Load the ASR model."""
        pass

    @abstractmethod
    def unload(self) -> None:
        """Unload the ASR model to free memory."""
        pass
