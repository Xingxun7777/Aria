"""ASR (Automatic Speech Recognition) engines."""

from .base import ASREngine, ASRResult, TranscriptType
from .funasr_engine import FunASREngine, FunASRConfig, check_funasr_installation
from .qwen3_engine import Qwen3ASREngine, Qwen3Config, check_qwen3_installation

# Note: FunASR and Qwen3 use lazy imports to avoid slow startup
# Call check_*_installation() to check availability

__all__ = [
    "ASREngine",
    "ASRResult",
    "TranscriptType",
    "FunASREngine",
    "FunASRConfig",
    "check_funasr_installation",
    "Qwen3ASREngine",
    "Qwen3Config",
    "check_qwen3_installation",
]
