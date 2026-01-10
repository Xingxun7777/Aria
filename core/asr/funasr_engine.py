"""
FunASR Engine (Paraformer/SenseVoice)
=====================================
Alternative ASR engine using Alibaba's FunASR models.

Advantages over Whisper:
- Paraformer-zh: Optimized for Chinese, non-autoregressive (no hallucination loops)
- SenseVoice: Multilingual with emotion detection
- Built-in VAD and punctuation restoration
- Native hotword support
- Generally faster than Whisper for Chinese

Models:
- paraformer-zh: Best for Chinese-only, ~700MB
- SenseVoiceSmall: Multilingual with emotion, ~500MB
"""

import os
import numpy as np
import time
import datetime
from dataclasses import dataclass, field
from typing import Optional, List
from pathlib import Path
import threading

from .base import ASREngine, ASRResult, TranscriptType
from ..logging import get_system_logger

logger = get_system_logger()

# File-based debug logging (works with pythonw.exe)
_FUNASR_LOG = Path(__file__).parent.parent.parent / "DebugLog" / "funasr_debug.log"


def _funasr_log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    try:
        _FUNASR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_FUNASR_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


# Lazy import - FunASR is heavy, only import when actually used
AutoModel = None
rich_transcription_postprocess = None
FUNASR_AVAILABLE = None  # Will be set on first check


def check_cuda_available() -> tuple[bool, str]:
    """
    Check if CUDA is available and compatible with current GPU.

    Returns:
        (is_available, reason) tuple
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return False, "CUDA not available (no GPU or driver issue)"

        # Try a simple CUDA operation to verify kernel compatibility
        try:
            # This will fail if GPU architecture is not supported
            x = torch.zeros(1, device="cuda")
            del x
            torch.cuda.empty_cache()
            return True, "CUDA OK"
        except RuntimeError as e:
            error_msg = str(e).lower()
            if "no kernel image" in error_msg:
                return False, "GPU architecture not supported by PyTorch build"
            elif "out of memory" in error_msg:
                return False, "GPU out of memory"
            else:
                return False, f"CUDA runtime error: {e}"

    except ImportError:
        return False, "PyTorch not installed"
    except Exception as e:
        return False, f"CUDA check failed: {e}"


def get_optimal_device(preferred: str = "cuda") -> tuple[str, str]:
    """
    Get the optimal device for inference, with automatic fallback.

    Args:
        preferred: User's preferred device ("cuda" or "cpu")

    Returns:
        (actual_device, reason) tuple
    """
    if preferred == "cpu":
        return "cpu", "User selected CPU mode"

    # User wants CUDA, check if available
    cuda_ok, reason = check_cuda_available()

    if cuda_ok:
        return "cuda", "CUDA available"
    else:
        logger.warning(f"CUDA unavailable ({reason}), falling back to CPU")
        _funasr_log(f"[DEVICE] CUDA fallback to CPU: {reason}")
        return "cpu", f"Auto-fallback: {reason}"


def check_funasr_installation() -> bool:
    """Check if FunASR is available (lazy check)."""
    global AutoModel, rich_transcription_postprocess, FUNASR_AVAILABLE
    if FUNASR_AVAILABLE is not None:
        return FUNASR_AVAILABLE
    try:
        from funasr import AutoModel as _AutoModel
        from funasr.utils.postprocess_utils import (
            rich_transcription_postprocess as _rich_postprocess,
        )

        AutoModel = _AutoModel
        rich_transcription_postprocess = _rich_postprocess
        FUNASR_AVAILABLE = True
    except ImportError:
        FUNASR_AVAILABLE = False
        logger.warning("funasr not installed. Run: pip install funasr")
    return FUNASR_AVAILABLE


@dataclass
class FunASRConfig:
    """Configuration for FunASR engine."""

    # Model selection
    model_name: str = "paraformer-zh"  # or "iic/SenseVoiceSmall"
    device: str = "cuda"  # "cuda" or "cpu"

    # VAD settings (built-in)
    enable_vad: bool = True
    vad_model: str = "fsmn-vad"
    max_single_segment_time: int = 30000  # ms

    # Punctuation restoration (Paraformer only)
    enable_punc: bool = True
    punc_model: str = "ct-punc"

    # Hotword support
    hotwords: List[str] = field(default_factory=list)

    # Performance
    batch_size_s: int = 300  # Dynamic batch total audio duration

    # SenseVoice specific
    use_itn: bool = True  # Inverse text normalization
    language: str = "auto"  # "zh", "en", "auto"


class FunASREngine(ASREngine):
    """
    FunASR-based speech recognition engine.

    Supports:
    - Paraformer-zh: Chinese-optimized with VAD and punctuation
    - SenseVoiceSmall: Multilingual with emotion detection
    """

    def __init__(self, config: Optional[FunASRConfig] = None):
        self.config = config or FunASRConfig()
        self._model = None
        self._lock = threading.Lock()
        self._is_sensevoice = "sensevoice" in self.config.model_name.lower()
        # Track actual device used (may differ from config if fallback occurred)
        self._actual_device: str = self.config.device
        self._device_reason: str = ""

    @property
    def name(self) -> str:
        return f"FunASR ({self.config.model_name})"

    @property
    def is_loaded(self) -> bool:
        """Check if model is loaded."""
        return self._model is not None

    @property
    def actual_device(self) -> str:
        """Get the actual device being used (may be CPU if CUDA fallback occurred)."""
        return self._actual_device

    @property
    def device_info(self) -> str:
        """Get human-readable device info for UI display."""
        if self._actual_device == "cuda":
            return "GPU (CUDA)"
        elif self._device_reason:
            return f"CPU ({self._device_reason})"
        else:
            return "CPU"

    def load(self) -> None:
        """Load the FunASR model with automatic CUDA fallback."""
        # Ensure installation check is done first
        check_funasr_installation()
        if not FUNASR_AVAILABLE:
            raise RuntimeError("funasr not installed. Run: pip install funasr")

        if self._model is not None:
            logger.info("Model already loaded")
            return

        # Auto-detect optimal device with fallback
        self._actual_device, self._device_reason = get_optimal_device(
            self.config.device
        )

        if self._actual_device != self.config.device:
            _funasr_log(
                f"[DEVICE] Requested: {self.config.device}, Using: {self._actual_device}"
            )
            logger.info(
                f"Device fallback: {self.config.device} -> {self._actual_device} ({self._device_reason})"
            )

        logger.info(
            f"Loading FunASR model: {self.config.model_name} on {self._actual_device}"
        )
        start_time = time.time()

        try:
            # Use actual device (may be fallback from CUDA to CPU)
            device_str = (
                f"{self._actual_device}:0"
                if self._actual_device == "cuda"
                else self._actual_device
            )

            if self._is_sensevoice:
                # SenseVoice model
                self._model = AutoModel(
                    model=self.config.model_name,
                    trust_remote_code=True,
                    vad_model=self.config.vad_model if self.config.enable_vad else None,
                    vad_kwargs={
                        "max_single_segment_time": self.config.max_single_segment_time
                    },
                    device=device_str,
                )
            else:
                # Paraformer model
                device = device_str

                model_kwargs = {
                    "model": self.config.model_name,
                    "device": device,
                    "hub": "ms",  # Explicitly use ModelScope
                    "trust_remote_code": True,
                    "disable_update": True,  # Avoid update checks
                }

                if self.config.enable_vad:
                    model_kwargs["vad_model"] = self.config.vad_model
                    model_kwargs["vad_kwargs"] = {
                        "max_single_segment_time": self.config.max_single_segment_time
                    }

                if self.config.enable_punc:
                    model_kwargs["punc_model"] = self.config.punc_model

                self._model = AutoModel(**model_kwargs)

            load_time = time.time() - start_time
            logger.info(f"FunASR model loaded in {load_time:.2f}s")

        except Exception as e:
            # Ensure model is cleaned up on failure
            self._model = None
            logger.error(f"Failed to load FunASR model: {e}")
            raise

    def transcribe_stream(self, audio_generator):
        """
        Streaming transcription - not natively supported by Paraformer.

        FunASR's Paraformer is non-autoregressive, so it processes complete
        audio segments rather than streaming. This implementation collects
        audio and yields a final result.

        For true streaming, consider using paraformer-zh-streaming model.
        """
        from typing import Generator

        # Collect all audio from generator
        audio_chunks = []
        for chunk in audio_generator:
            audio_chunks.append(chunk)

        if not audio_chunks:
            return

        # Concatenate and transcribe
        full_audio = np.concatenate(audio_chunks)
        result = self.transcribe(full_audio)
        yield result

    def transcribe(self, audio: np.ndarray) -> ASRResult:
        """
        Transcribe audio using FunASR.

        Args:
            audio: Audio samples as numpy array (float32, 16kHz mono)

        Returns:
            ASRResult with transcribed text
        """
        if self._model is None:
            logger.error("FunASR model is None! Model not loaded.")
            raise RuntimeError("Model not loaded. Call load() first.")

        # Log model state for debugging
        logger.info(
            f"FunASR transcribe called. Model type: {type(self._model)}, config.model_name: {self.config.model_name}"
        )
        print(
            f"[ASR DEBUG] Model loaded: {self._model is not None}, Model name: {self.config.model_name}"
        )

        with self._lock:
            start_time = time.time()

            try:
                # FunASR expects float32 audio normalized to [-1, 1] or int16
                # Keep float32 as-is (most robust), convert int16 to float32 if needed
                if audio.dtype == np.int16:
                    audio_float = audio.astype(np.float32) / 32768.0
                elif audio.dtype == np.float32:
                    audio_float = audio
                else:
                    # Other dtypes: convert to float32
                    audio_float = audio.astype(np.float32)

                # Build generation kwargs
                gen_kwargs = {
                    "input": audio_float,
                    "batch_size_s": self.config.batch_size_s,
                }

                if self._is_sensevoice:
                    # SenseVoice specific
                    gen_kwargs["language"] = self.config.language
                    gen_kwargs["use_itn"] = self.config.use_itn
                    gen_kwargs["merge_vad"] = True
                    gen_kwargs["merge_length_s"] = 15
                else:
                    # Paraformer specific - hotword support
                    if self.config.hotwords:
                        # FunASR hotword format: newline-separated "word score" pairs
                        # e.g., "阿里巴巴 20\nClaude 10"
                        gen_kwargs["hotword"] = "\n".join(self.config.hotwords)

                # Run inference - add detailed logging for debugging
                audio_stats = {
                    "shape": audio_float.shape,
                    "dtype": str(audio_float.dtype),
                    "min": float(np.min(audio_float)),
                    "max": float(np.max(audio_float)),
                    "mean": float(np.mean(audio_float)),
                    "std": float(np.std(audio_float)),
                    "non_zero": int(np.count_nonzero(audio_float)),
                    "abs_max": float(np.abs(audio_float).max()),
                }
                logger.info(f"FunASR starting generate() - audio stats: {audio_stats}")
                print(
                    f"[ASR DEBUG] Audio stats: shape={audio_float.shape}, abs_max={audio_stats['abs_max']:.4f}, non_zero={audio_stats['non_zero']}"
                )

                _funasr_log(
                    f">>> generate() starting - audio shape={audio_float.shape}, abs_max={audio_stats['abs_max']:.4f}"
                )
                _funasr_log(f"gen_kwargs keys: {list(gen_kwargs.keys())}")

                try:
                    # FunASR tries to write to stdout which is None in pythonw.exe
                    # Temporarily redirect stdout/stderr to devnull
                    import sys
                    import io

                    old_stdout = sys.stdout
                    old_stderr = sys.stderr

                    # Create dummy streams if None (pythonw.exe)
                    if sys.stdout is None:
                        sys.stdout = io.StringIO()
                    if sys.stderr is None:
                        sys.stderr = io.StringIO()

                    try:
                        result = self._model.generate(**gen_kwargs)
                    finally:
                        # Restore original streams
                        sys.stdout = old_stdout
                        sys.stderr = old_stderr

                    _funasr_log(
                        f"generate() completed - result type: {type(result)}, len: {len(result) if result else 0}"
                    )
                except Exception as gen_error:
                    _funasr_log(f"!!! generate() EXCEPTION: {gen_error}")
                    raise

                logger.info(f"FunASR generate() returned: {result}")
                print(f"[ASR DEBUG] FunASR result: {result}")

                # Extract text from result
                if result and len(result) > 0:
                    if self._is_sensevoice:
                        # SenseVoice returns rich format
                        text = rich_transcription_postprocess(result[0]["text"])
                    else:
                        # Paraformer returns list of dicts
                        if isinstance(result[0], dict):
                            text = result[0].get("text", "")
                        else:
                            text = str(result[0])

                    # Remove spaces between Chinese characters (Paraformer adds them)
                    # Keep spaces around English/numbers
                    import re

                    # Remove space between two Chinese characters
                    text = re.sub(
                        r"([\u4e00-\u9fff])\s+([\u4e00-\u9fff])", r"\1\2", text
                    )
                    # May need multiple passes for consecutive spaces
                    text = re.sub(
                        r"([\u4e00-\u9fff])\s+([\u4e00-\u9fff])", r"\1\2", text
                    )
                else:
                    text = ""

                transcribe_time = time.time() - start_time
                logger.debug(
                    f"FunASR transcribed in {transcribe_time:.3f}s: {text[:50]}..."
                )

                return ASRResult(
                    text=text.strip(),
                    type=TranscriptType.FINAL,
                    language="zh",
                    confidence=1.0,  # FunASR doesn't provide confidence
                )

            except Exception as e:
                logger.error(f"FunASR transcription error: {e}")
                return ASRResult(
                    text="", type=TranscriptType.FINAL, language="zh", confidence=0.0
                )

    def set_hotwords(self, hotwords: List[str]) -> None:
        """
        Set hotwords for improved recognition (default score 50).

        Note: Only works with Paraformer, not SenseVoice.
        """
        # Convert to format with default score
        self.config.hotwords = [f"{word} 50" for word in hotwords]
        logger.info(
            f"FunASR hotwords updated: {len(hotwords)} words (default score=50)"
        )

    def set_hotwords_with_score(self, hotwords_with_score: List[tuple]) -> None:
        """
        Set hotwords with individual scores for FunASR.

        Based on tri-party analysis weight-to-score mapping:
        - weight 0.3 → score 20 (hint)
        - weight 0.5 → score 50 (standard)
        - weight 1.0 → score 80 (lock)

        Args:
            hotwords_with_score: List of (word, score) tuples
        """
        # FunASR format: "word score" per line
        self.config.hotwords = [
            f"{word} {score}" for word, score in hotwords_with_score
        ]
        logger.info(
            f"FunASR hotwords updated: {len(hotwords_with_score)} words with scores"
        )

    def unload(self) -> None:
        """Unload the model to free memory."""
        with self._lock:
            if self._model is not None:
                del self._model
                self._model = None
                logger.info("FunASR model unloaded")

    @staticmethod
    def is_available() -> bool:
        """Check if FunASR is installed."""
        return FUNASR_AVAILABLE


def get_funasr_info() -> dict:
    """
    Get FunASR installation status info.

    Returns:
        Dict with installation info
    """
    info = {"installed": FUNASR_AVAILABLE, "version": None, "models_available": []}

    if FUNASR_AVAILABLE:
        try:
            import funasr

            info["version"] = getattr(funasr, "__version__", "unknown")
            info["models_available"] = [
                "paraformer-zh",
                "paraformer-zh-streaming",
                "iic/SenseVoiceSmall",
            ]
        except Exception as e:
            info["error"] = str(e)

    return info
