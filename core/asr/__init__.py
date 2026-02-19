"""ASR (Automatic Speech Recognition) engines."""

from .base import ASREngine, ASRResult, TranscriptType
from .whisper_engine import WhisperEngine, WhisperConfig
from .funasr_engine import FunASREngine, FunASRConfig, check_funasr_installation
from .fireredasr_engine import (
    FireRedASREngine,
    FireRedASRConfig,
    check_fireredasr_installation,
)
from .qwen3_engine import Qwen3ASREngine, Qwen3Config, check_qwen3_installation

# Note: FunASR, FireRedASR, and Qwen3 use lazy imports to avoid slow startup
# Call check_*_installation() to check availability

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
    "Qwen3ASREngine",
    "Qwen3Config",
    "check_qwen3_installation",
]
