"""ASR (Automatic Speech Recognition) engines."""

from .base import ASREngine, ASRResult, TranscriptType
from .whisper_engine import WhisperEngine, WhisperConfig
from .funasr_engine import FunASREngine, FunASRConfig, check_funasr_installation
from .fireredasr_engine import (
    FireRedASREngine,
    FireRedASRConfig,
    check_fireredasr_installation,
)

# Note: FunASR and FireRedASR use lazy imports to avoid slow startup
# Call check_funasr_installation() or check_fireredasr_installation() to check availability

__all__ = [
    "ASREngine",
    "ASRResult",
    "TranscriptType",
    "WhisperEngine",
    "WhisperConfig",
    "FunASREngine",
    "FunASRConfig",
    "check_funasr_installation",
    "FireRedASREngine",
    "FireRedASRConfig",
    "check_fireredasr_installation",
]
