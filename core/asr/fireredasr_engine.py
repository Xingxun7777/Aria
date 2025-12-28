"""
FireRedASR Engine
=================
Industrial-grade Chinese/English ASR using FireRedTeam's FireRedASR.

FireRedASR achieves SOTA on Mandarin benchmarks:
- FireRedASR-AED (1.1B params): 3.18% avg CER
- FireRedASR-LLM (8.3B params): 3.05% avg CER

Key differences from other engines:
- Uses file paths instead of numpy arrays (requires temp file handling)
- No native hotword support (use post-processing layers)
- Batch API (we use batch_size=1 for real-time)

GitHub: https://github.com/FireRedTeam/FireRedASR
Paper: https://arxiv.org/abs/2501.14350
"""

import os
import sys
import tempfile
import wave
import numpy as np
import time
import uuid
import threading
from dataclasses import dataclass
from typing import Optional, List, Generator

from .base import ASREngine, ASRResult, TranscriptType
from ..logging import get_system_logger

logger = get_system_logger()


# Lazy import - FireRedASR is heavy (imports torch), only import when actually used
# Path detection: environment variable > sibling directory > None (disabled)
def _detect_fireredasr_path() -> Optional[str]:
    """Detect FireRedASR installation path."""
    # 1. Environment variable takes priority
    env_path = os.environ.get("FIREREDASR_PATH")
    if env_path and os.path.isdir(env_path):
        return env_path

    # 2. Check sibling directory (for development: G:\AIBOX\FireRedASR)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Go up: core/asr -> core -> voicetype-v1.1-dev -> AIBOX
    aibox_dir = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
    )
    sibling_path = os.path.join(aibox_dir, "FireRedASR")
    if os.path.isdir(sibling_path):
        return sibling_path

    # 3. Not found - FireRedASR is optional
    return None


FIREREDASR_PATH = _detect_fireredasr_path()
FireRedAsr = None
FIREREDASR_AVAILABLE = None  # Will be set on first check


def check_fireredasr_installation() -> bool:
    """Check if FireRedASR is available (lazy check)."""
    global FireRedAsr, FIREREDASR_AVAILABLE
    if FIREREDASR_AVAILABLE is not None:
        return FIREREDASR_AVAILABLE

    # FireRedASR path not detected - not available
    if FIREREDASR_PATH is None:
        FIREREDASR_AVAILABLE = False
        logger.info("FireRedASR not detected (optional feature)")
        return FIREREDASR_AVAILABLE

    # Add FireRedASR to path if available
    if FIREREDASR_PATH not in sys.path:
        sys.path.insert(0, FIREREDASR_PATH)

    try:
        from fireredasr.models.fireredasr import FireRedAsr as _FireRedAsr

        FireRedAsr = _FireRedAsr
        FIREREDASR_AVAILABLE = True
        logger.info(f"FireRedASR found at: {FIREREDASR_PATH}")
    except ImportError:
        FIREREDASR_AVAILABLE = False
        logger.warning(
            f"FireRedASR import failed from {FIREREDASR_PATH}. "
            "Run: pip install -r requirements.txt in FireRedASR directory"
        )
    return FIREREDASR_AVAILABLE


def _get_default_model_path() -> str:
    """Get default model path based on detected FireRedASR location."""
    if FIREREDASR_PATH:
        return os.path.join(FIREREDASR_PATH, "pretrained_models", "FireRedASR-AED-L")
    return ""  # No default when FireRedASR not installed


@dataclass
class FireRedASRConfig:
    """Configuration for FireRedASR engine."""

    # Model selection
    model_type: str = "aed"  # "aed" (1.1B, up to 60s) or "llm" (8.3B, up to 30s)
    model_path: str = ""  # Set in __post_init__ based on detected path

    # Device configuration
    use_gpu: bool = True

    # Decoding parameters
    beam_size: int = 2  # 1=fastest, 3=default, 5+=diminishing returns
    nbest: int = 1
    decode_max_len: int = 0  # 0 = auto

    # AED-specific parameters
    softmax_smoothing: float = 1.0
    aed_length_penalty: float = 0.0
    eos_penalty: float = 1.0

    # LLM-specific parameters
    repetition_penalty: float = 1.0
    llm_length_penalty: float = 0.0
    temperature: float = 1.0

    # Audio constraints
    max_audio_length_s: float = 60.0  # AED: 60s, LLM: 30s

    # Temp file management
    cleanup_temp_files: bool = True
    temp_dir: Optional[str] = None  # None = system temp

    def __post_init__(self):
        """Set default model_path if not provided."""
        if not self.model_path:
            self.model_path = _get_default_model_path()

    @property
    def sample_rate(self) -> int:
        return 16000

    # Compatibility properties for debug logging (matching WhisperConfig/FunASRConfig interface)
    @property
    def model_name(self) -> str:
        return f"FireRedASR-{self.model_type.upper()}"

    @property
    def device(self) -> str:
        return "cuda" if self.use_gpu else "cpu"

    @property
    def language(self) -> str:
        return "zh"  # FireRedASR supports zh/en, auto-detects

    @property
    def compute_type(self) -> str:
        return "float16" if self.use_gpu else "float32"


class FireRedASREngine(ASREngine):
    """
    FireRedASR-based speech recognition engine.

    Achieves SOTA on Mandarin benchmarks with strong English support.

    Notes:
    - AED model (1.1B params): Up to 60s audio, faster
    - LLM model (8.3B params): Up to 30s audio, more accurate
    - Input: Requires file paths (adapter creates temp WAV files)
    - No native hotword support (use Layer 2/3 post-processing)
    """

    def __init__(self, config: Optional[FireRedASRConfig] = None):
        self.config = config or FireRedASRConfig()
        self._model = None
        self._lock = threading.Lock()

        # Setup temp directory
        self._temp_dir = self.config.temp_dir or tempfile.gettempdir()
        self._temp_prefix = "fireredasr_"

        # Hotwords storage (for reference, not used by model)
        self._hotwords: List[str] = []

    @property
    def name(self) -> str:
        return f"FireRedASR ({self.config.model_type.upper()})"

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Load FireRedASR model."""
        if not FIREREDASR_AVAILABLE:
            raise RuntimeError(
                "FireRedASR not installed. "
                "Clone https://github.com/FireRedTeam/FireRedASR to G:\\AIBOX\\FireRedASR "
                "and run: pip install -r requirements.txt"
            )

        if self._model is not None:
            logger.info("FireRedASR model already loaded")
            return

        logger.info(
            f"Loading FireRedASR model: {self.config.model_type} from {self.config.model_path}"
        )
        start_time = time.time()

        try:
            # Verify model path exists
            if not os.path.exists(self.config.model_path):
                raise FileNotFoundError(
                    f"Model path not found: {self.config.model_path}. "
                    f"Download from HuggingFace: FireRedTeam/FireRedASR-AED-L"
                )

            # PyTorch 2.6+ requires explicit allowlist for pickle loading
            # FireRedASR models use argparse.Namespace which isn't allowed by default
            import torch
            import argparse

            try:
                torch.serialization.add_safe_globals([argparse.Namespace])
            except AttributeError:
                # Older PyTorch versions don't have this function
                pass

            self._model = FireRedAsr.from_pretrained(
                self.config.model_type, self.config.model_path
            )

            load_time = time.time() - start_time
            logger.info(f"FireRedASR model loaded in {load_time:.2f}s")

        except Exception as e:
            logger.error(f"Failed to load FireRedASR model: {e}")
            raise

    def unload(self) -> None:
        """Unload model and cleanup."""
        with self._lock:
            if self._model is not None:
                del self._model
                self._model = None
                logger.info("FireRedASR model unloaded")

        # Cleanup any remaining temp files
        self._cleanup_temp_files()

    def _cleanup_temp_files(self) -> None:
        """Remove any remaining temporary WAV files."""
        try:
            for filename in os.listdir(self._temp_dir):
                if filename.startswith(self._temp_prefix) and filename.endswith(".wav"):
                    filepath = os.path.join(self._temp_dir, filename)
                    try:
                        os.unlink(filepath)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Failed to cleanup temp files: {e}")

    def _array_to_temp_wav(self, audio: np.ndarray, uttid: str) -> str:
        """
        Convert numpy array to temporary WAV file.

        Args:
            audio: float32 numpy array, 16kHz mono, normalized [-1, 1]
            uttid: Unique utterance ID for filename

        Returns:
            Path to temporary WAV file
        """
        temp_path = os.path.join(self._temp_dir, f"{self._temp_prefix}{uttid}.wav")

        # Convert to int16 for WAV file
        if audio.dtype == np.float32:
            # Clip to [-1, 1] range before conversion
            audio_clipped = np.clip(audio, -1.0, 1.0)
            audio_int16 = (audio_clipped * 32767).astype(np.int16)
        elif audio.dtype == np.int16:
            audio_int16 = audio
        else:
            # Convert other dtypes through float32
            audio_float = audio.astype(np.float32)
            audio_clipped = np.clip(audio_float, -1.0, 1.0)
            audio_int16 = (audio_clipped * 32767).astype(np.int16)

        # Write WAV file
        with wave.open(temp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # int16 = 2 bytes
            wf.setframerate(16000)
            wf.writeframes(audio_int16.tobytes())

        return temp_path

    def _get_decode_params(self) -> dict:
        """Build decode parameters based on model type."""
        params = {
            "use_gpu": 1 if self.config.use_gpu else 0,
            "beam_size": self.config.beam_size,
            "nbest": self.config.nbest,
            "decode_max_len": self.config.decode_max_len,
        }

        if self.config.model_type == "aed":
            params.update(
                {
                    "softmax_smoothing": self.config.softmax_smoothing,
                    "aed_length_penalty": self.config.aed_length_penalty,
                    "eos_penalty": self.config.eos_penalty,
                }
            )
        elif self.config.model_type == "llm":
            params.update(
                {
                    "decode_min_len": 0,
                    "repetition_penalty": self.config.repetition_penalty,
                    "llm_length_penalty": self.config.llm_length_penalty,
                    "temperature": self.config.temperature,
                }
            )

        return params

    def transcribe(self, audio: np.ndarray) -> ASRResult:
        """
        Transcribe audio using FireRedASR.

        Args:
            audio: Audio samples as numpy array (float32, 16kHz mono)

        Returns:
            ASRResult with transcribed text
        """
        if self._model is None:
            raise RuntimeError("Model not loaded. Call load() first.")

        with self._lock:
            # Calculate duration
            duration_s = len(audio) / 16000
            max_duration = self.config.max_audio_length_s

            # Warn if audio too long
            if duration_s > max_duration:
                logger.warning(
                    f"Audio too long ({duration_s:.1f}s > {max_duration}s). "
                    f"FireRedASR-{self.config.model_type.upper()} may produce poor results."
                )

            # Generate unique utterance ID
            uttid = f"vt_{uuid.uuid4().hex[:8]}"
            wav_path = None

            try:
                # Create temp WAV file
                wav_path = self._array_to_temp_wav(audio, uttid)

                # Prepare batch (single utterance)
                batch_uttid = [uttid]
                batch_wav_path = [wav_path]

                # Get decode parameters
                decode_params = self._get_decode_params()

                # Log for debugging
                logger.debug(
                    f"FireRedASR transcribing: {uttid}, duration={duration_s:.2f}s"
                )

                # Run inference
                start_time = time.time()
                results = self._model.transcribe(
                    batch_uttid, batch_wav_path, decode_params
                )
                transcribe_time = (time.time() - start_time) * 1000

                # Parse result
                # FireRedASR returns: [{"uttid": "...", "text": "...", ...}, ...]
                text = ""
                if results and len(results) > 0:
                    result = results[0]
                    if isinstance(result, dict):
                        text = result.get("text", "")
                    elif isinstance(result, str):
                        text = result
                    elif isinstance(result, (list, tuple)) and len(result) > 0:
                        # Some versions return nested structure
                        if isinstance(result[0], dict):
                            text = result[0].get("text", "")
                        else:
                            text = str(result[0])

                # Clean up text
                text = text.strip()

                logger.info(
                    f"FireRedASR transcribed in {transcribe_time:.0f}ms: {text[:50]}..."
                )
                print(f"[FireRedASR] {transcribe_time:.0f}ms: {text[:80]}")

                return ASRResult(
                    text=text,
                    type=TranscriptType.FINAL,
                    language="zh",  # FireRedASR auto-detects zh/en
                    confidence=1.0,  # FireRedASR doesn't provide confidence
                    start_time=0.0,
                    end_time=duration_s,
                )

            except Exception as e:
                logger.error(f"FireRedASR transcription error: {e}")
                import traceback

                traceback.print_exc()
                return ASRResult(
                    text="", type=TranscriptType.FINAL, language="zh", confidence=0.0
                )

            finally:
                # Cleanup temp file
                if (
                    self.config.cleanup_temp_files
                    and wav_path
                    and os.path.exists(wav_path)
                ):
                    try:
                        os.unlink(wav_path)
                    except Exception:
                        pass

    def transcribe_stream(
        self, audio_generator: Generator[np.ndarray, None, None]
    ) -> Generator[ASRResult, None, None]:
        """
        Streaming transcription - NOT natively supported by FireRedASR.

        FireRedASR is offline-only. This implementation collects audio
        and yields a single final result.
        """
        # Collect all audio
        audio_chunks = []
        for chunk in audio_generator:
            audio_chunks.append(chunk)

        if not audio_chunks:
            return

        # Concatenate and transcribe
        full_audio = np.concatenate(audio_chunks)
        result = self.transcribe(full_audio)
        yield result

    def set_hotwords(self, hotwords: List[str]) -> None:
        """
        Set hotwords - NOT supported by FireRedASR.

        FireRedASR does not support native hotword biasing.
        Hotwords must be handled at the post-processing layer
        (Layer 2 regex replacements, Layer 2.5 fuzzy matching, Layer 3 AI polish).
        """
        logger.warning(
            "FireRedASR does not support native hotwords. "
            "Relying on post-processing layers (regex, fuzzy matching, AI polish)."
        )
        self._hotwords = hotwords

    def set_initial_prompt(self, prompt: str) -> None:
        """
        Set initial prompt - NOT supported by FireRedASR.

        This method exists for API compatibility but has no effect.
        """
        logger.warning("FireRedASR does not support initial prompts.")

    @staticmethod
    def is_available() -> bool:
        """Check if FireRedASR is installed and available."""
        return FIREREDASR_AVAILABLE


def get_fireredasr_info() -> dict:
    """
    Get FireRedASR installation status info.

    Returns:
        Dict with installation info
    """
    default_model = _get_default_model_path()
    info = {
        "installed": FIREREDASR_AVAILABLE or False,
        "repo_path": FIREREDASR_PATH,
        "repo_exists": FIREREDASR_PATH is not None and os.path.exists(FIREREDASR_PATH),
        "models_available": [],
        "default_model_path": default_model,
    }

    if FIREREDASR_AVAILABLE:
        info["models_available"] = ["aed", "llm"]

        # Check if default model exists
        if default_model:
            info["default_model_exists"] = os.path.exists(default_model)
        else:
            info["default_model_exists"] = False

    return info
