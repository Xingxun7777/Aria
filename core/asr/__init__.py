"""ASR (Automatic Speech Recognition) engines."""

from .base import ASREngine, ASRResult
from .whisper_engine import WhisperEngine, WhisperConfig
from .funasr_engine import FunASREngine, FunASRConfig, check_funasr_installation

# FireRedASR - conditionally import (requires external repo)
try:
    from .fireredasr_engine import (
        FireRedASREngine,
        FireRedASRConfig,
        check_fireredasr_installation,
        FIREREDASR_AVAILABLE
    )
except ImportError:
    FIREREDASR_AVAILABLE = False
    FireRedASREngine = None
    FireRedASRConfig = None
    check_fireredasr_installation = None

__all__ = [
    'ASREngine', 'ASRResult',
    'WhisperEngine', 'WhisperConfig',
    'FunASREngine', 'FunASRConfig', 'check_funasr_installation',
    'FireRedASREngine', 'FireRedASRConfig', 'check_fireredasr_installation', 'FIREREDASR_AVAILABLE'
]
