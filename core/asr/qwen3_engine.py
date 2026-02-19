# pyright: reportImplicitOverride=false
"""
Qwen3-ASR Engine
================
ASR engine using Alibaba's Qwen3-ASR models.

Features:
- Qwen3-ASR-1.7B: 52 languages/dialects, SOTA for Chinese/English
- Qwen3-ASR-0.6B: Lightweight version, 2000x throughput
- Context/hotword support via text biasing
- Non-autoregressive (no hallucination loops)

Models:
- Qwen/Qwen3-ASR-1.7B: Full model (~3.4GB), best accuracy
- Qwen/Qwen3-ASR-0.6B: Small model (~1.2GB), faster inference
"""

import datetime
import re
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING, override

import numpy as np
from numpy.typing import NDArray

from .base import ASREngine, ASRResult, TranscriptType
from ..logging import get_system_logger

logger = get_system_logger()

# File-based debug logging (works with pythonw.exe)
_QWEN3_LOG = Path(__file__).parent.parent.parent / "DebugLog" / "qwen3_debug.log"


def _qwen3_log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    try:
        _ = _QWEN3_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_QWEN3_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


# Lazy imports - qwen_asr is heavy
Qwen3ASRModel: Any | None = None
_qwen3_available: bool | None = None


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

    cuda_ok, reason = check_cuda_available()

    if cuda_ok:
        return "cuda", "CUDA available"
    else:
        logger.warning(f"CUDA unavailable ({reason}), falling back to CPU")
        _qwen3_log(f"[DEVICE] CUDA fallback to CPU: {reason}")
        return "cpu", f"Auto-fallback: {reason}"


def get_gpu_vram_gb() -> float:
    """
    Get available GPU VRAM in GB.

    Returns:
        VRAM in GB, or 0.0 if no GPU available
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return 0.0
        # Get total memory of first GPU
        props = torch.cuda.get_device_properties(0)
        vram_gb = props.total_memory / (1024**3)
        return vram_gb
    except Exception:
        return 0.0


def select_optimal_model(vram_gb: float) -> tuple[str, str]:
    """
    Select optimal Qwen3-ASR model based on available VRAM.

    Model requirements (float16):
    - Qwen3-ASR-1.7B: ~3.4GB VRAM
    - Qwen3-ASR-0.6B: ~1.2GB VRAM

    Args:
        vram_gb: Available GPU VRAM in GB

    Returns:
        (model_name, reason) tuple
    """
    if vram_gb >= 5.0:
        # Plenty of VRAM - use full model
        return "Qwen/Qwen3-ASR-1.7B", f"VRAM {vram_gb:.1f}GB >= 5GB, using full model"
    elif vram_gb >= 2.0:
        # Limited VRAM - use lightweight model
        return (
            "Qwen/Qwen3-ASR-0.6B",
            f"VRAM {vram_gb:.1f}GB < 5GB, using lightweight model",
        )
    else:
        # Very low VRAM or no GPU - still try 0.6B (will fallback to CPU if needed)
        return (
            "Qwen/Qwen3-ASR-0.6B",
            f"VRAM {vram_gb:.1f}GB < 2GB, using lightweight model",
        )


def check_qwen3_installation() -> bool:
    """Check if qwen_asr is available (lazy check)."""
    global Qwen3ASRModel, _qwen3_available
    if _qwen3_available is not None:
        return _qwen3_available
    try:
        from qwen_asr import Qwen3ASRModel as _Qwen3ASRModel  # type: ignore[reportMissingTypeStubs]

        Qwen3ASRModel = _Qwen3ASRModel
        _qwen3_available = True
    except ImportError:
        _qwen3_available = False
        logger.warning("qwen_asr not installed. Run: pip install qwen-asr")
    return _qwen3_available


@dataclass
class Qwen3Config:
    """Configuration for Qwen3 ASR engine."""

    model_name: str = (
        "auto"  # "auto" = detect VRAM and choose, or explicit "Qwen/Qwen3-ASR-1.7B" / "0.6B"
    )
    device: str = "cuda"  # "cuda" or "cpu"
    hotwords: list[str] = field(default_factory=list)
    language: str = "Chinese"  # Force language output
    torch_dtype: str = (
        "bfloat16"  # bfloat16 recommended; auto-fallback to float16/32 if unsupported
    )
    max_new_tokens: int = (
        256  # Optimized for short utterances; official example uses 256
    )
    max_inference_batch_size: int = (
        32  # Prevents OOM on long audio; official example uses 32
    )


class Qwen3ASREngine(ASREngine):
    """
    Qwen3-ASR speech recognition engine.

    Supports:
    - Qwen3-ASR-1.7B: Full model, 52 languages
    - Qwen3-ASR-0.6B: Lightweight, faster
    - Context biasing via hotwords (text-based, not score-based)
    """

    def __init__(self, config: Qwen3Config | None = None):
        self.config: Qwen3Config = config or Qwen3Config()
        self._model: Any | None = None
        self._lock: threading.Lock = threading.Lock()
        self._context_string: str = " ".join(self.config.hotwords).strip()
        # Track actual device used (may differ from config if fallback occurred)
        self._actual_device: str = self.config.device
        self._device_reason: str = ""

    @property
    def name(self) -> str:
        return f"Qwen3-ASR ({self.config.model_name.split('/')[-1]})"

    @property
    @override
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

    def set_context(self, context_string: str) -> None:
        """Set context string for Qwen3-ASR text biasing."""
        with self._lock:
            self._context_string = (context_string or "").strip()
        logger.debug(f"Qwen3 context updated: {len(self._context_string)} chars")

    def set_initial_prompt(self, prompt: str) -> None:
        """Set initial prompt (alias for set_context for compatibility)."""
        self.set_context(prompt)

    def _resolve_torch_dtype(self, dtype_str: str) -> Any:
        """Convert string dtype to torch.dtype."""
        import torch

        dtype_map = {
            "float16": torch.float16,
            "float32": torch.float32,
            "bfloat16": torch.bfloat16,
            "auto": "auto",
        }
        return dtype_map.get(dtype_str, torch.float16)

    @override
    def load(self) -> None:
        """Load the Qwen3-ASR model with automatic device/model/dtype selection."""
        check_qwen3_installation()
        if not _qwen3_available:
            raise RuntimeError("qwen_asr not installed. Run: pip install qwen-asr")
        if Qwen3ASRModel is None:
            raise RuntimeError("Qwen3ASRModel unavailable after import")

        if self._model is not None:
            logger.info("Model already loaded")
            return

        # Auto-detect optimal device with fallback
        self._actual_device, self._device_reason = get_optimal_device(
            self.config.device
        )

        if self._actual_device != self.config.device:
            _qwen3_log(
                f"[DEVICE] Requested: {self.config.device}, Using: {self._actual_device}"
            )
            logger.info(
                f"Device fallback: {self.config.device} -> {self._actual_device} ({self._device_reason})"
            )

        # Auto-select model based on VRAM if model_name is "auto"
        model_name = self.config.model_name
        if model_name == "auto":
            vram_gb = get_gpu_vram_gb() if self._actual_device == "cuda" else 0.0
            model_name, model_reason = select_optimal_model(vram_gb)
            _qwen3_log(f"[MODEL] Auto-selected: {model_name} ({model_reason})")
            logger.info(f"Auto-selected model: {model_name} ({model_reason})")

        # Try loading with graceful fallback on OOM
        self._load_with_fallback(model_name)

    def _load_with_fallback(self, model_name: str) -> None:
        """
        Load model with graceful fallback on OOM or other errors.

        Fallback chain:
        1. Try requested model on GPU
        2. If OOM and using 1.7B → try 0.6B on GPU
        3. If still fails → try on CPU with float32

        Args:
            model_name: Model to load (e.g., "Qwen/Qwen3-ASR-1.7B")
        """
        import torch

        start_time = time.time()

        # Resolve dtype with automatic fallback for compatibility
        requested_dtype = self.config.torch_dtype
        torch_dtype = self._resolve_torch_dtype(requested_dtype)

        if requested_dtype == "bfloat16":
            if self._actual_device == "cpu":
                # CPU: bfloat16 not reliably supported → float32
                torch_dtype = torch.float32
                _qwen3_log("[DTYPE] CPU mode: bfloat16 → float32 fallback")
                logger.info("CPU mode: using float32 instead of bfloat16")
            elif not torch.cuda.is_bf16_supported():
                # Old GPU (GTX 10xx, RTX 20xx, etc.): bfloat16 not supported → float16
                torch_dtype = torch.float16
                _qwen3_log("[DTYPE] GPU does not support bfloat16 → float16 fallback")
                logger.info(
                    "GPU does not support bfloat16 (older architecture), using float16"
                )

        # Fallback chain for loading
        fallback_chain = self._build_fallback_chain(model_name, torch_dtype)

        for attempt, (try_model, try_device, try_dtype) in enumerate(fallback_chain):
            try:
                logger.info(
                    f"Loading Qwen3-ASR: {try_model} on {try_device} ({try_dtype})"
                )
                _qwen3_log(
                    f"[LOAD] Attempt {attempt + 1}: model={try_model}, "
                    f"device={try_device}, dtype={try_dtype}, "
                    f"max_new_tokens={self.config.max_new_tokens}, "
                    f"max_inference_batch_size={self.config.max_inference_batch_size}"
                )

                self._model = Qwen3ASRModel.from_pretrained(
                    try_model,
                    device_map=try_device,
                    torch_dtype=try_dtype,
                    max_new_tokens=self.config.max_new_tokens,
                    max_inference_batch_size=self.config.max_inference_batch_size,
                )

                # Success!
                self._actual_device = try_device
                load_time = time.time() - start_time
                logger.info(
                    f"Qwen3 ASR model loaded in {load_time:.2f}s: "
                    f"{try_model} on {try_device}"
                )
                return

            except RuntimeError as e:
                error_msg = str(e).lower()
                is_oom = (
                    "out of memory" in error_msg or "cuda out of memory" in error_msg
                )

                if is_oom:
                    # Clear CUDA cache before retry
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                    _qwen3_log(
                        f"[OOM] Attempt {attempt + 1} failed: {try_model} on {try_device}"
                    )
                    logger.warning(
                        f"OOM loading {try_model} on {try_device}, trying fallback..."
                    )
                    continue
                else:
                    # Non-OOM error - still try fallback
                    _qwen3_log(f"[ERROR] Attempt {attempt + 1}: {e}")
                    logger.warning(
                        f"Failed to load {try_model}: {e}, trying fallback..."
                    )
                    continue

            except Exception as e:
                _qwen3_log(f"[ERROR] Attempt {attempt + 1}: {e}")
                logger.warning(f"Failed to load {try_model}: {e}, trying fallback...")
                continue

        # All attempts failed
        self._model = None
        raise RuntimeError(
            f"Failed to load Qwen3-ASR after {len(fallback_chain)} attempts. "
            "Try reducing max_inference_batch_size or using CPU mode."
        )

    def _build_fallback_chain(
        self, model_name: str, torch_dtype: Any
    ) -> list[tuple[str, str, Any]]:
        """
        Build fallback chain for model loading.

        Returns:
            List of (model_name, device, dtype) tuples to try in order
        """
        import torch

        chain = []

        # First: try requested configuration
        chain.append((model_name, self._actual_device, torch_dtype))

        # If on GPU with 1.7B, add 0.6B fallback
        if self._actual_device == "cuda" and "1.7B" in model_name:
            small_model = model_name.replace("1.7B", "0.6B")
            chain.append((small_model, "cuda", torch_dtype))

        # If on GPU, add CPU fallback with float32
        if self._actual_device == "cuda":
            # Try same model on CPU
            chain.append((model_name, "cpu", torch.float32))
            # If 1.7B, also try 0.6B on CPU
            if "1.7B" in model_name:
                small_model = model_name.replace("1.7B", "0.6B")
                chain.append((small_model, "cpu", torch.float32))

        return chain

    @override
    def transcribe_stream(
        self, audio_generator: Generator[bytes, None, None]
    ) -> Generator[ASRResult, None, None]:
        """
        Streaming transcription - not natively supported by Qwen3-ASR.

        Qwen3-ASR is non-autoregressive, so it processes complete
        audio segments rather than streaming. This implementation collects
        audio and yields a final result.
        """
        audio_chunks: list[np.ndarray] = []
        for chunk in audio_generator:
            if isinstance(chunk, (bytes, bytearray)):
                audio_chunks.append(np.frombuffer(chunk, dtype=np.float32))
            else:
                audio_chunks.append(np.asarray(chunk, dtype=np.float32))

        if not audio_chunks:
            return

        # Concatenate and transcribe
        full_audio = np.concatenate(audio_chunks)
        result = self.transcribe(full_audio)
        yield result

    @override
    def transcribe(self, audio: Any) -> ASRResult:
        """
        Transcribe audio using Qwen3-ASR.

        Args:
            audio: Audio samples as numpy array (float32, 16kHz mono)

        Returns:
            ASRResult with transcribed text
        """
        if self._model is None:
            logger.error("Qwen3-ASR model is None! Model not loaded.")
            raise RuntimeError("Model not loaded. Call load() first.")

        with self._lock:
            start_time = time.time()

            try:
                if isinstance(audio, (bytes, bytearray)):
                    audio_array = np.frombuffer(audio, dtype=np.float32)
                else:
                    audio_array = np.asarray(audio)

                if audio_array.dtype == np.int16:
                    audio_float = audio_array.astype(np.float32) / 32768.0
                elif audio_array.dtype == np.float32:
                    audio_float = audio_array
                else:
                    audio_float = audio_array.astype(np.float32)

                # Qwen3-ASR expects tuple (audio_array, sample_rate)
                audio_tuple = (audio_float, 16000)

                context_string = self._context_string
                if not context_string and self.config.hotwords:
                    context_string = " ".join(self.config.hotwords).strip()

                # Enhanced debug logging with context preview
                context_preview = (
                    context_string[:300] + "..."
                    if len(context_string) > 300
                    else context_string
                )
                _qwen3_log(
                    f">>> transcribe() - audio shape={audio_float.shape}\n"
                    f"    context_chars={len(context_string)}\n"
                    f"    context_preview:\n{context_preview}"
                )

                results = self._model.transcribe(
                    audio=audio_tuple,
                    context=context_string if context_string else None,
                    language=self.config.language,
                )

                # Extract text from result
                if results and len(results) > 0:
                    first = results[0]
                    text = getattr(first, "text", "") or ""  # Ensure string, not None

                    # Safety check: if text looks like object repr, it's a bug
                    # e.g., "ASRTranscription(language='', text='', ...)"
                    if (
                        "ASRTranscription(" in str(text)
                        or "language=" in str(text)[:30]
                    ):
                        _qwen3_log(
                            f"[BUG] Got object repr instead of text: {text[:100]}"
                        )
                        text = ""

                    detected_language = getattr(first, "language", self.config.language)
                    if not detected_language:
                        detected_language = self.config.language
                else:
                    text = ""
                    detected_language = self.config.language

                # === Hallucination Detection ===
                # Check for context leakage (model outputs context instead of speech)
                text = text.strip()
                # Compute audio energy for acoustic-aware leakage detection
                audio_energy = float(np.abs(audio_float).mean())

                if text and context_string:
                    if self._is_context_leakage(text, context_string, audio_energy):
                        _qwen3_log(
                            f"[HALLUCINATION] Context leakage suspected: '{text[:80]}...'"
                            f" (energy={audio_energy:.5f})"
                        )

                        # Retry-without-context: rerun ASR without context biasing.
                        # If we still get meaningful text, it's likely real speech.
                        # Time guard: skip retry if initial transcription already took too long.
                        elapsed = time.time() - start_time
                        if elapsed > 20:
                            _qwen3_log(
                                f"[RETRY] Skipping retry — initial took {elapsed:.1f}s (>20s limit)"
                            )
                            text = ""
                        else:
                            _qwen3_log("[RETRY] Retrying transcription without context...")
                            try:
                                retry_results = self._model.transcribe(
                                    audio=audio_tuple,
                                    context=None,
                                    language=self.config.language,
                                )
                                retry_text = ""
                                if retry_results and len(retry_results) > 0:
                                    retry_text = (
                                        getattr(retry_results[0], "text", "") or ""
                                    ).strip()
                            except Exception as retry_err:
                                _qwen3_log(f"[RETRY] Retry failed: {retry_err}")
                                logger.warning(
                                    f"Qwen3 ASR: Retry transcription failed: {retry_err}"
                                )
                                retry_text = ""
                                retry_results = None

                            # Filter filler tokens from retry (common noise artifacts)
                            _FILLER_TOKENS = {"嗯", "啊", "呃", "哦", "额", "噢", "唔"}
                            if retry_text and retry_text in _FILLER_TOKENS:
                                _qwen3_log(
                                    f"[RETRY] Filtered filler token: '{retry_text}'"
                                )
                                retry_text = ""

                            if retry_text and len(retry_text) >= 2:
                                # Non-trivial text recovered → likely real speech
                                _qwen3_log(
                                    f"[RETRY] Recovered real speech: '{retry_text[:80]}'"
                                )
                                logger.info(
                                    f"Qwen3 ASR: Leakage retry recovered text"
                                )
                                text = retry_text
                                # Update detected language from retry result
                                if retry_results and len(retry_results) > 0:
                                    detected_language = (
                                        getattr(retry_results[0], "language", detected_language)
                                        or detected_language
                                    )
                            else:
                                # Retry also empty/trivial → confirmed hallucination
                                _qwen3_log(
                                    f"[HALLUCINATION] Confirmed (retry empty/trivial), discarding"
                                )
                                logger.warning(
                                    f"Qwen3 ASR: Context leakage confirmed, returning empty"
                                )
                                text = ""

                transcribe_time = time.time() - start_time
                logger.debug(
                    f"Qwen3 ASR transcribed in {transcribe_time:.3f}s: {text[:50]}..."
                )

                return ASRResult(
                    text=text,
                    type=TranscriptType.FINAL,
                    language=detected_language,
                    confidence=1.0 if text else 0.0,
                )

            except Exception as e:
                logger.error(f"Qwen3 ASR transcription error: {e}")
                return ASRResult(
                    text="",
                    type=TranscriptType.FINAL,
                    language=self.config.language,
                    confidence=0.0,
                )

    def _is_context_leakage(
        self, text: str, context: str, audio_energy: float = -1.0
    ) -> bool:
        """
        Detect if ASR output is actually context leakage (hallucination).

        When there's no real speech, Qwen3-ASR sometimes outputs words from
        the context/initial_prompt instead of recognizing silence. This method
        detects such leakage patterns with acoustic-aware dual thresholds.

        Detection uses stricter thresholds when audio energy is low (likely noise),
        since low-energy audio is more prone to producing hallucinated context words.

        Detection cases (thresholds vary by energy level):
        - Case 1: Exact context match (always flagged)
        - Case 1b: Continuous substring of context (>= 8 chars at low energy, >= 15 standard)
        - Case 2: High token overlap >= 90% with 5+ tokens
        - Case 3: 100% token overlap (>= 1 token at low energy, >= 2 standard)
        - Case 4: Substring density check (>= 3 context words at low energy, >= 5 standard)

        Energy thresholds (mean absolute amplitude, float32 [-1,1]):
        - < 0.003: Pre-ASR gate skips ASR entirely (in app.py)
        - < 0.008: STRICT detection (highest sensitivity)
        - < 0.015: MODERATE detection
        - >= 0.015: STANDARD detection (lowest sensitivity)

        Args:
            text: ASR output text
            context: Context string provided to the model
            audio_energy: Mean absolute amplitude of audio (-1.0 if not provided)

        Returns:
            True if text is almost certainly context leakage
        """
        if not text or not context:
            return False

        # Normalize for comparison
        text_norm = text.lower().strip()
        context_norm = context.lower().strip()

        # Acoustic-aware 3-tier detection: lower energy → stricter thresholds.
        # Audio that reaches here already passed PRE_ASR_ENERGY_GATE (0.003) in app.py.
        # Tiers: STRICT (< 0.008) → MODERATE (< 0.015) → STANDARD (>= 0.015)
        STRICT_ENERGY = 0.008   # Near-silence: very likely noise/hallucination
        MODERATE_ENERGY = 0.015  # Quiet: possibly real but suspicious
        has_energy_info = audio_energy >= 0
        is_strict = has_energy_info and audio_energy < STRICT_ENERGY
        is_moderate = has_energy_info and STRICT_ENERGY <= audio_energy < MODERATE_ENERGY

        # Case 1: Output is EXACTLY the context or a continuous substring
        # (strongest indicator - almost certainly leakage)
        if text_norm == context_norm:
            _qwen3_log(f"[LEAKAGE] Exact context match")
            return True

        # Case 1b: Output is a continuous substring of context
        # Strict: >= 8 chars | Moderate: >= 12 chars | Standard: >= 15 chars
        substring_threshold = 8 if is_strict else (12 if is_moderate else 15)
        if len(text_norm) >= substring_threshold and text_norm in context_norm:
            _qwen3_log(
                f"[LEAKAGE] Output is substring of context: '{text[:50]}...' "
                f"(threshold={substring_threshold}, energy={audio_energy:.5f})"
            )
            return True

        # Tokenize both text and context for overlap analysis
        # Split on common delimiters: spaces, Chinese punctuation, commas
        def _tokenize(s: str) -> set:
            return set(
                t
                for t in re.split(r"[\s、，,。.!！?？()（）]+", s)
                if t and len(t) >= 1
            )

        text_tokens = _tokenize(text_norm)
        context_tokens = _tokenize(context_norm)

        if not text_tokens:
            return False

        overlap = text_tokens & context_tokens
        overlap_ratio = len(overlap) / len(text_tokens)

        # Case 2: High token overlap (>= 90%) with 5+ tokens
        # Catches mutated leakage like "编辑AI工具，编辑TTS，FunASR，Claude..."
        if len(text_tokens) >= 5 and overlap_ratio >= 0.9:
            _qwen3_log(
                f"[LEAKAGE] High token overlap: {len(overlap)}/{len(text_tokens)} "
                f"= {overlap_ratio:.1%} (energy={audio_energy:.5f})"
            )
            return True

        # Case 3: ALL tokens are context words (100% match)
        # Strict: >= 1 token | Moderate/Standard: >= 2 tokens
        min_tokens_for_full_overlap = 1 if is_strict else 2
        if len(text_tokens) >= min_tokens_for_full_overlap and overlap_ratio >= 1.0:
            _qwen3_log(
                f"[LEAKAGE] All tokens are context words: {overlap} "
                f"(min_tokens={min_tokens_for_full_overlap}, energy={audio_energy:.5f})"
            )
            return True

        # Case 4: Substring-based check for mutated leakage
        # When tokenization fails (e.g., "编辑AI工具" = one token but contains "AI工具"),
        # count how many distinct context words appear as substrings in the text.
        # Strict: >= 3 words/0.2 density | Moderate: >= 4/0.25 | Standard: >= 5/0.3
        found_context_words = set()
        for cw in context_tokens:
            if len(cw) >= 2 and cw in text_norm:
                found_context_words.add(cw)
        min_context_words = 3 if is_strict else (4 if is_moderate else 5)
        if len(found_context_words) >= min_context_words:
            # Calculate density: matched chars vs total text length
            matched_chars = sum(len(w) for w in found_context_words)
            density = matched_chars / max(len(text_norm), 1)
            density_threshold = 0.2 if is_strict else (0.25 if is_moderate else 0.3)
            if density >= density_threshold:
                _qwen3_log(
                    f"[LEAKAGE] Substring match: {len(found_context_words)} context words "
                    f"found (min={min_context_words}), density={density:.1%} "
                    f"(threshold={density_threshold}), energy={audio_energy:.5f}"
                )
                return True

        return False

    def set_hotwords(self, hotwords: list[str]) -> None:
        """Set hotwords for context biasing."""
        with self._lock:
            self.config.hotwords = hotwords
            self._context_string = " ".join(hotwords).strip()
        logger.info(f"Qwen3 ASR hotwords updated: {len(hotwords)} words")

    def set_hotwords_with_context(self, context_string: str) -> None:
        """Set raw context string directly (Qwen3 format)."""
        with self._lock:
            self._context_string = (context_string or "").strip()
            self.config.hotwords = (
                self._context_string.split() if self._context_string else []
            )
        logger.info(
            f"Qwen3 ASR context string updated: {len(self.config.hotwords)} words"
        )

    @override
    def unload(self) -> None:
        """Unload the model to free memory."""
        with self._lock:
            if self._model is not None:
                del self._model
                self._model = None

                logger.info("Qwen3 ASR model unloaded")

    @staticmethod
    def is_available() -> bool:
        """Check if qwen_asr is installed."""
        check_qwen3_installation()
        return bool(_qwen3_available)
