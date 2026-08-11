"""ASR (Automatic Speech Recognition) engines."""

from .base import ASREngine, ASRResult, TranscriptType
from .funasr_engine import FunASREngine, FunASRConfig, check_funasr_installation
from .qwen3_engine import Qwen3ASREngine, Qwen3Config, check_qwen3_installation
from .sherpa_engine import (
    SherpaQwen3Engine,
    check_sherpa_installation,
    resolve_sherpa_model_dir,
)
from .llamacpp_engine import (
    LlamaCppQwen3Engine,
    resolve_llamacpp_asset,
    resolve_llamacpp_path,
    default_mmproj_for,
)

# Note: FunASR, Qwen3 and sherpa use lazy imports to avoid slow startup
# Call check_*_installation() to check availability
# llamacpp has no wheel dependency: availability = server exe + GGUF on disk

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
    "SherpaQwen3Engine",
    "check_sherpa_installation",
    "resolve_sherpa_model_dir",
    "LlamaCppQwen3Engine",
    "resolve_llamacpp_asset",
    "resolve_llamacpp_path",
    "default_mmproj_for",
]
