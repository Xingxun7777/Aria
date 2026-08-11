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
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING, override

import numpy as np
from numpy.typing import NDArray

from .base import ASREngine, ASRResult, TranscriptType
from .acoustic_policy import (
    CONTEXT_ECHO_MIN_RUN,
    CTX_PEAK_ESCAPE_P95,
    CTX_PEAK_ESCAPE_P95_WHISPER,
    HALLUCINATION_SPEED_CLEAR_ENERGY,
    HALLUCINATION_SPEED_LIMIT_CLEAR,
    HALLUCINATION_SPEED_LIMIT_LOW,
    HALLUCINATION_SPEED_MIN_CHARS,
    LOW_SIGNAL_ENERGY_FOR_CTX,
    MODERATE_ENERGY,
    RMS_NORMALIZE_MIN_ENERGY,
    STRICT_ENERGY,
    WHISPER_POST_DSP_EARLY_ENERGY,
    WHISPER_POST_DSP_EARLY_MAX_DURATION_S,
    find_context_echo,
    should_skip_pre_screen,
)
from ..logging import get_system_logger
from ..utils.import_workarounds import prepare_qwen_asr_import

logger = get_system_logger()

# File-based debug logging (works with pythonw.exe)
_QWEN3_LOG = Path(__file__).parent.parent.parent / "DebugLog" / "qwen3_debug.log"


def _qwen3_log(msg: str) -> None:
    from ..debug import append_log_line

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    append_log_line(_QWEN3_LOG, f"[{ts}] {msg}")


# Lazy imports - qwen_asr is heavy
Qwen3ASRModel: Any | None = None
_qwen3_available: bool | None = None
_qwen3_unavailable_reason: str = ""


def _probe_cuda_in_subprocess(timeout_s: float = 12.0) -> tuple[bool, str]:
    """Probe CUDA in a child process so a driver/PyTorch crash can't kill Aria."""
    code = (
        "import torch\n"
        "print('torch=' + getattr(torch, '__version__', 'unknown'), flush=True)\n"
        "if not torch.cuda.is_available():\n"
        "    print('CUDA_UNAVAILABLE', flush=True)\n"
        "    raise SystemExit(2)\n"
        "x = torch.zeros(1, device='cuda')\n"
        "torch.cuda.synchronize()\n"
        "print('CUDA_OK ' + torch.cuda.get_device_name(0), flush=True)\n"
    )
    env = os.environ.copy()
    env["ARIA_CUDA_PROBE_CHILD"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return False, f"CUDA probe timed out after {timeout_s:.0f}s: {output[-300:]}"
    except Exception as exc:
        return False, f"CUDA probe failed: {type(exc).__name__}: {exc}"

    output = (completed.stdout or "").strip()
    if completed.returncode == 0 and "CUDA_OK" in output:
        return True, output.splitlines()[-1]
    return False, f"CUDA probe exited {completed.returncode}: {output[-500:]}"


def check_cuda_available() -> tuple[bool, str]:
    """
    Check if CUDA is available and compatible with current GPU.

    Returns:
        (is_available, reason) tuple
    """
    ok, reason = _probe_cuda_in_subprocess()
    if ok:
        return True, "CUDA OK"
    return False, reason


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


def _process_memory_summary() -> str:
    """Compact process memory summary for qwen3_debug.log."""
    try:
        import psutil

        mem = psutil.Process(os.getpid()).memory_info()
        return (
            f"private={mem.private / 1024**3:.2f}GB, " f"rss={mem.rss / 1024**3:.2f}GB"
        )
    except Exception:
        return "private/rss=unknown"


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
    global Qwen3ASRModel, _qwen3_available, _qwen3_unavailable_reason
    if _qwen3_available is not None:
        return _qwen3_available
    try:
        prepare_qwen_asr_import()
        from qwen_asr import Qwen3ASRModel as _Qwen3ASRModel  # type: ignore[reportMissingTypeStubs]

        Qwen3ASRModel = _Qwen3ASRModel
        _qwen3_available = True
        _qwen3_unavailable_reason = ""
    except Exception as exc:
        _qwen3_available = False
        _qwen3_unavailable_reason = str(exc)
        logger.warning(f"qwen_asr unavailable: {exc}")
        _qwen3_log(f"[IMPORT] qwen_asr unavailable: {exc}")
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
    sample_rate: int = 16000  # Qwen3-ASR expects 16 kHz mono audio
    torch_dtype: str = (
        "bfloat16"  # bfloat16 recommended; auto-fallback to float16/32 if unsupported
    )
    max_new_tokens: int = (
        1024  # Support long continuous speech; 256 was too short (~80 Chinese chars)
    )
    # sherpa Qwen3-ASR caps prompt(context+hotwords) + audio KV at this total
    # budget. sherpa's own default is 512, which a long utterance + full context
    # overruns -> the decoder collapses to 1-2 chars (the long-sentence
    # word-swallow). The 0.6B ONNX accepts several thousand; 2048 fits ~37s of
    # speech + full context with margin. SherpaQwen3Engine.load() passes this
    # to SherpaQwen3Model explicitly; the native torch path ignores it.
    max_total_len: int = 2048
    max_inference_batch_size: int = (
        32  # Prevents OOM on long audio; official example uses 32
    )
    low_cpu_mem_usage: bool = (
        True  # Ask Transformers to avoid extra CPU copies while loading
    )

    @staticmethod
    def _as_int(value: Any, default: int, *, min_value: int, max_value: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return max(min_value, min(max_value, parsed))

    @staticmethod
    def _as_bool(value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"1", "true", "yes", "on"}:
                return True
            if v in {"0", "false", "no", "off"}:
                return False
        return default

    @classmethod
    def from_mapping(
        cls, data: dict[str, Any] | None, *, model_name_default: str = "auto"
    ) -> "Qwen3Config":
        """Build config from hotwords.json, preserving safe defaults."""
        data = data or {}
        cfg = cls(
            model_name=str(
                data.get("model_name", model_name_default) or model_name_default
            ),
            device=str(data.get("device", "cuda") or "cuda"),
            language=str(data.get("language", "Chinese") or "Chinese"),
        )
        if "torch_dtype" in data:
            cfg.torch_dtype = str(data.get("torch_dtype") or cfg.torch_dtype)
        cpu_runtime = cfg.device.strip().lower() == "cpu"
        if cpu_runtime:
            # CPU mode is a reliability path for GPU-heavy image generation, not
            # the long-form quality path.  Loading/running 1.7B on CPU or keeping
            # a large token/batch budget produces the observed “no load but stuck”
            # behavior because Qwen can spend tens of seconds decoding while the
            # app waits on its internal engine lock.  Normalize every CPU request
            # (settings UI, popup menu, config hot-reload, GPU fallback) to the
            # lightweight 0.6B/float32 profile.
            if "1.7B" in cfg.model_name:
                cfg.model_name = cfg.model_name.replace("1.7B", "0.6B")
            cfg.torch_dtype = "float32"
        token_default = 512 if cpu_runtime else cfg.max_new_tokens
        token_max = 512 if cpu_runtime else 4096
        cfg.max_new_tokens = cls._as_int(
            data.get("max_new_tokens"),
            token_default,
            min_value=64,
            max_value=token_max,
        )
        cfg.max_total_len = cls._as_int(
            data.get("max_total_len"),
            cfg.max_total_len,
            min_value=512,
            max_value=8192,
        )
        batch_default = 8 if cpu_runtime else cfg.max_inference_batch_size
        batch_max = 8 if cpu_runtime else 32
        cfg.max_inference_batch_size = cls._as_int(
            data.get("max_inference_batch_size"),
            batch_default,
            min_value=1,
            max_value=batch_max,
        )
        cfg.low_cpu_mem_usage = cls._as_bool(
            data.get("low_cpu_mem_usage"), cfg.low_cpu_mem_usage
        )
        return cfg


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
        # Context strings are plain str assignments (atomic in CPython), so
        # setters don't need to acquire self._lock. The only thread holding
        # that lock is the transcribe() call — making setters wait behind
        # a running transcribe() used to cause 5+ second stalls on final
        # segments when an interim transcription was in flight.
        self._context_string: str = " ".join(self.config.hotwords).strip()
        self._recent_context: str = ""  # Recent ASR outputs for continuity
        # Pre-DSP acoustic hint: caller (AriaApp) reports the RAW mic energy
        # before DSP boost. Without this, leakage detection reads post-DSP
        # energy and whisper-mode boosted noise looks like normal speech,
        # putting the detector into the most permissive STANDARD tier.
        # -1.0 = no hint set; consumed (reset to -1.0) at the end of each
        # transcribe() so a stale hint can't silently re-apply.
        self._pre_dsp_energy_hint: float = -1.0
        # Pre-DSP p95 amplitude hint (same lifecycle as the energy hint).
        # The whole-segment MEAN gets diluted by leading/trailing silence,
        # so the context-starve gate needs the peak tier to tell "quiet
        # real speech" (loud p95) from sustained noise (flat p95 ≈ avg).
        self._pre_dsp_p95_hint: float = -1.0
        # Capture-mode hint set by caller (AriaApp). Used by the post-DSP
        # whisper-mode early-return below to avoid spending 3+ seconds
        # transcribing tail-end near-silence that VAD's loose whisper
        # threshold (0.05) leaked through. "" = unset → no early return.
        self._capture_mode_hint: str = ""
        # Track actual device used (may differ from config if fallback occurred)
        self._actual_device: str = self.config.device
        self._device_reason: str = ""
        self._loaded_model_name: str = self.config.model_name

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

    @property
    def loaded_model_name(self) -> str:
        """Get the model that actually loaded after auto/fallback resolution."""
        return self._loaded_model_name

    def set_context(self, context_string: str) -> None:
        """Set context string for Qwen3-ASR text biasing. Lock-free (atomic str assign)."""
        self._context_string = (context_string or "").strip()
        logger.debug(f"Qwen3 context updated: {len(self._context_string)} chars")

    def set_initial_prompt(self, prompt: str) -> None:
        """Set initial prompt (alias for set_context for compatibility)."""
        self.set_context(prompt)

    def set_recent_context(self, recent_text: str) -> None:
        """Set recent ASR outputs as additional context for continuity. Lock-free."""
        self._recent_context = (recent_text or "").strip()

    def set_capture_mode_hint(self, mode: str) -> None:
        """
        Report the current capture mode (standard/noisy/whisper) for the
        next transcribe() call. Whisper mode's loose VAD threshold (0.05)
        leaks tail-end near-silence into ASR; the post-DSP early-return
        inside transcribe() uses this hint to skip those without affecting
        standard/noisy mode behavior.

        Set-and-stays: caller refreshes whenever capture_mode changes (or
        before each transcribe). Unset ("") = no early return, fall back
        to running the model.
        """
        self._capture_mode_hint = (mode or "").strip()

    def set_acoustic_hint(
        self, pre_dsp_energy: float, pre_dsp_p95: float = -1.0
    ) -> None:
        """
        Deprecated: prefer passing pre_dsp_energy / pre_dsp_p95 as keyword
        args to transcribe(). Kept for backward compatibility with older
        callers and tests; forwards to instance fields with the same
        consume-on-fallback semantics.

        Report the raw mic energy (pre-DSP) for the next transcribe() call.

        AriaApp computes this BEFORE the capture-mode DSP runs. Leakage
        detection uses this to pick its threshold tier — without it, whisper
        mode's 30dB AGC boost makes noise look like normal speech and
        downgrades detection to the most permissive tier.

        pre_dsp_p95 is the 95th-percentile absolute amplitude of the same
        pre-DSP segment. The context-starve gate uses it to rescue real
        speech whose whole-segment MEAN was diluted below the near-silence
        threshold by leading/trailing silence — sustained noise stays flat
        (p95 ≈ avg) and keeps starving. -1.0 = no p95 available (older
        callers/tests): gate falls back to mean-only behavior.

        Both instance hints are consumed (cleared to -1.0) inside
        transcribe() on every call — including calls that pass explicit
        kwargs (which take priority and ignore instance state entirely) —
        so a stale value can't leak into a later hint-less call.
        """
        try:
            self._pre_dsp_energy_hint = float(pre_dsp_energy)
        except (TypeError, ValueError):
            self._pre_dsp_energy_hint = -1.0
        try:
            self._pre_dsp_p95_hint = float(pre_dsp_p95)
        except (TypeError, ValueError):
            self._pre_dsp_p95_hint = -1.0

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

    @staticmethod
    def _resolve_local_model(model_name: str) -> str:
        """
        Check if a bundled local model exists and return its path.

        For portable distributions, models are bundled under:
            <base>/models/Qwen3-ASR-1.7B/  (or 0.6B)

        If the local directory contains model files, use it directly
        instead of downloading from HuggingFace Hub.

        Args:
            model_name: HuggingFace model ID (e.g., "Qwen/Qwen3-ASR-1.7B")

        Returns:
            Local path if bundled model exists, otherwise original model_name
        """
        if "/" not in model_name:
            # Already a local path
            return model_name

        from ..utils.paths import get_models_path

        # "Qwen/Qwen3-ASR-1.7B" → "Qwen3-ASR-1.7B"
        local_name = model_name.split("/")[-1]
        local_path = get_models_path(local_name)

        # Verify it has actual model files (not just an empty directory)
        if local_path.is_dir() and any(local_path.glob("*.safetensors")):
            _qwen3_log(f"[MODEL] Using bundled local model: {local_path}")
            logger.info(f"Using bundled local model: {local_path}")
            return str(local_path)

        return model_name

    @override
    def load(self) -> None:
        """Load the Qwen3-ASR model with automatic device/model/dtype selection."""
        check_qwen3_installation()
        if not _qwen3_available:
            detail = _qwen3_unavailable_reason or "unknown reason"
            raise RuntimeError(f"qwen_asr unavailable: {detail}")
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

        if (
            self._actual_device == "cpu"
            and "Auto-fallback" in (self._device_reason or "")
            and "1.7B" in model_name
        ):
            fallback_model = model_name.replace("1.7B", "0.6B")
            _qwen3_log(
                "[MODEL] CUDA probe failed; using lightweight CPU fallback: "
                f"{model_name} -> {fallback_model}"
            )
            logger.warning(
                "CUDA probe failed; using lightweight Qwen3-ASR CPU fallback: "
                f"{model_name} -> {fallback_model}"
            )
            model_name = fallback_model

        # Check for bundled local model (portable distribution)
        model_name = self._resolve_local_model(model_name)

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
        _qwen3_log(f"[MEM] Before load: {_process_memory_summary()}")

        # Resolve dtype with automatic fallback for compatibility
        requested_dtype = self.config.torch_dtype
        torch_dtype = self._resolve_torch_dtype(requested_dtype)

        if self._actual_device == "cpu" and torch_dtype != torch.float32:
            # CPU: half/bfloat16 are not reliably supported by the Qwen stack
            # and can produce mojibake/hallucinated text.  Always use float32.
            torch_dtype = torch.float32
            _qwen3_log(f"[DTYPE] CPU mode: {requested_dtype} → float32 fallback")
            logger.info(f"CPU mode: using float32 instead of {requested_dtype}")
        elif requested_dtype == "bfloat16":
            if not torch.cuda.is_bf16_supported():
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

                # Local path → skip network calls to avoid hangs in China
                is_local = os.path.isabs(try_model) or os.path.isdir(try_model)
                extra_kwargs = {}
                if is_local:
                    extra_kwargs["local_files_only"] = True
                    _qwen3_log(f"[LOAD] Local model detected, skipping network check")
                if self.config.low_cpu_mem_usage:
                    extra_kwargs["low_cpu_mem_usage"] = True
                    extra_kwargs["use_safetensors"] = True
                    _qwen3_log("[LOAD] low_cpu_mem_usage enabled")

                self._model = Qwen3ASRModel.from_pretrained(
                    try_model,
                    device_map=try_device,
                    torch_dtype=try_dtype,
                    max_new_tokens=self.config.max_new_tokens,
                    max_inference_batch_size=self.config.max_inference_batch_size,
                    **extra_kwargs,
                )

                # Success!
                self._actual_device = try_device
                self._loaded_model_name = try_model
                load_time = time.time() - start_time
                logger.info(
                    f"Qwen3 ASR model loaded in {load_time:.2f}s: "
                    f"{try_model} on {try_device}"
                )
                _qwen3_log(
                    f"[MEM] After load: {_process_memory_summary()} "
                    f"(elapsed={load_time:.1f}s)"
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
    def transcribe(
        self,
        audio: Any,
        *,
        pre_dsp_energy: float = -1.0,
        pre_dsp_p95: float = -1.0,
        capture_mode: str | None = None,
    ) -> ASRResult:
        """
        Transcribe audio using Qwen3-ASR.

        Args:
            audio: Audio samples as numpy array (float32, 16kHz mono)
            pre_dsp_energy: Pre-DSP mean abs amplitude. Prefer this over
                set_acoustic_hint(). All-or-nothing with other hint kwargs:
                if any hint kwarg is provided, missing fields use sentinels
                (-1.0 / "") and instance hint fields are never mixed in.
            pre_dsp_p95: Pre-DSP 95th-percentile abs amplitude (same rule).
            capture_mode: Capture mode hint. Non-None (with any other hint
                kwarg) selects the kwargs path; else instance fallback.

        Returns:
            ASRResult with transcribed text
        """
        if self._model is None:
            logger.error("Qwen3-ASR model is None! Model not loaded.")
            raise RuntimeError("Model not loaded. Call load() first.")

        with self._lock:
            start_time = time.time()
            _hints_from_args = False

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

                # Resolve acoustic / capture hints: all-or-nothing.
                # If any hint kwarg is provided (energy>=0, p95>=0, or
                # capture_mode is not None), the whole group comes from
                # kwargs only — missing fields use sentinels (-1.0 / "")
                # and instance hint fields are never mixed in. Otherwise
                # fall back to the deprecated setter instance fields.
                try:
                    _arg_energy = float(pre_dsp_energy)
                except (TypeError, ValueError):
                    _arg_energy = -1.0
                try:
                    _arg_p95 = float(pre_dsp_p95)
                except (TypeError, ValueError):
                    _arg_p95 = -1.0
                _hints_from_args = (
                    _arg_energy >= 0 or _arg_p95 >= 0 or capture_mode is not None
                )
                if _hints_from_args:
                    pre_screen_hint = _arg_energy if _arg_energy >= 0 else -1.0
                    pre_screen_p95_hint = _arg_p95 if _arg_p95 >= 0 else -1.0
                    if capture_mode is not None:
                        effective_capture_mode = (capture_mode or "").strip()
                    else:
                        effective_capture_mode = ""
                else:
                    pre_screen_hint = self._pre_dsp_energy_hint
                    pre_screen_p95_hint = self._pre_dsp_p95_hint
                    effective_capture_mode = self._capture_mode_hint

                # === Pre-screen short near-silence segments ===
                # Noise bursts (cough, fan blip, keyboard tick) that slip
                # past VAD take 5-10 seconds in the model because Qwen3's
                # decoder regurgitates recent_context (152+ chars) as output.
                # If the pre-DSP hint says near-silence AND the segment is
                # short (thresholds in acoustic_policy, 2026-07-19
                # recalibration), skip the model entirely. Whisper capture
                # mode is exempt inside the predicate — quiet real speech
                # lives below the energy floor there.
                #
                # Hint-only: if the caller didn't provide a pre-DSP hint
                # (hint == -1), fall back to running the model. This keeps
                # callers without the hint path working (e.g. unit tests).
                audio_duration_s = len(audio_float) / 16000
                if should_skip_pre_screen(
                    pre_dsp_energy_hint=pre_screen_hint,
                    duration_s=audio_duration_s,
                    capture_mode=effective_capture_mode,
                ):
                    _qwen3_log(
                        f"[PRE-SCREEN] Skipping near-silence segment: "
                        f"pre_dsp={pre_screen_hint:.5f}, "
                        f"duration={audio_duration_s:.2f}s "
                        f"(saved transcribe call)"
                    )
                    # Consume-always: clear instance energy/p95 hints so a
                    # stale setter value cannot leak into a later bare call.
                    # Do not touch _capture_mode_hint (set-and-stays).
                    self._pre_dsp_energy_hint = -1.0
                    self._pre_dsp_p95_hint = -1.0
                    return ASRResult(
                        text="",
                        type=TranscriptType.FINAL,
                        language=self.config.language,
                        confidence=0.0,
                    )

                # Compute energy BEFORE normalization (for leakage detection)
                audio_energy = float(np.sqrt(np.mean(audio_float**2)))
                # Mean-abs / p95 of the same un-normalized audio: starve-gate
                # fallback for hint-less callers (selection-command path,
                # deep-sleep warmup, external callers). Comparable to the
                # pre-DSP MAE/p95 hint semantics — the app DSP chain only
                # ever BOOSTS near-silence (AGC), so these read >= the true
                # mic level and can only under-starve relative to a real
                # hint; they can never starve real speech that a hint would
                # have kept. Captured here because RMS normalization below
                # rebinds audio_float to the boosted array.
                audio_mae_pre_normalize = float(np.abs(audio_float).mean())
                audio_p95_pre_normalize = float(
                    np.percentile(np.abs(audio_float), 95)
                )

                # === Whisper-mode post-DSP near-silence early-return ===
                # Whisper VAD threshold (0.05) leaks tail-end silence into
                # transcribe — caller's pre-DSP hint can be 0.002-0.003 (above
                # the < 0.001 gate above) yet post-DSP energy stays at noise
                # floor. The model still spends 3+ seconds on these before
                # regurgitating a filtered "嗯。". Skip directly.
                #
                # Threshold note: audio_energy here is RMS. DSP log reports
                # MAE (np.abs.mean), and RMS ≈ MAE × 1.25 for typical noise.
                # So RMS 0.002 corresponds to DSP-log post ≈ 0.0016. Real
                # whisper speech post-DSP RMS ≥ 0.005 (AGC target 0.10), so
                # 0.002 still has 2.5x safety margin. Duration < 3s prevents
                # wrongly killing rare long quiet utterances.
                #
                # Whisper mode only — standard/noisy VAD is strict enough
                # that this leak doesn't happen there.
                if (
                    effective_capture_mode == "whisper"
                    and audio_energy < WHISPER_POST_DSP_EARLY_ENERGY
                    and audio_duration_s < WHISPER_POST_DSP_EARLY_MAX_DURATION_S
                ):
                    _qwen3_log(
                        f"[PRE-SCREEN] Skipping whisper tail near-silence: "
                        f"post_dsp={audio_energy:.5f}, "
                        f"duration={audio_duration_s:.2f}s "
                        f"(saved transcribe call)"
                    )
                    self._pre_dsp_energy_hint = -1.0
                    self._pre_dsp_p95_hint = -1.0
                    return ASRResult(
                        text="",
                        type=TranscriptType.FINAL,
                        language=self.config.language,
                        confidence=0.0,
                    )

                # RMS normalization — boost quiet speech to consistent level
                # Quiet speech has lower SNR → worse ASR accuracy
                target_rms = 0.05  # ~-26 dBFS, typical speech level
                if audio_energy > RMS_NORMALIZE_MIN_ENERGY:  # Only boost real speech, not noise
                    gain = target_rms / audio_energy
                    gain = min(
                        gain, 5.0
                    )  # Max 14dB boost (was 10x/20dB, too aggressive)
                    if gain > 1.1:
                        audio_float = audio_float * gain
                        audio_float = np.clip(audio_float, -1.0, 1.0)

                # Qwen3-ASR expects tuple (audio_array, sample_rate)
                audio_tuple = (audio_float, self.config.sample_rate)

                # Build hotword context (also used for leakage detection).
                # OCR-derived screen keywords are NOT injected here — that
                # used to bias the ASR too hard (a short spoken token could
                # get coerced into a long unrelated context string when it
                # was the only similar token in context). The Polish LLM layer
                # uses the OCR text instead, where nudging is softer and
                # doesn't rewrite user intent.
                hotword_context = self._context_string
                if not hotword_context and self.config.hotwords:
                    hotword_context = " ".join(self.config.hotwords).strip()

                # Recent ASR context appended last (natural text, no label needed)
                recent_ctx = self._recent_context

                # === Starve regurgitation on weak/non-speech signal ===
                # When pre-DSP energy is low, audio contains either nothing,
                # sustained environmental noise (fan/airplane/traffic), or
                # extremely quiet speech. In all three cases recent_ctx is the
                # WORST thing we can give the decoder — it's the exact buffer
                # Qwen3 parrots back when it has no strong acoustic anchor.
                #
                # 2026 field logs showed Qwen3 can also copy the hotword/prompt
                # list itself ("用户常提到的专有名词...") when mic energy is
                # near-silence.  In that acoustic tier, recognition quality
                # matters more than domain-term biasing: pass NO context and let
                # the model anchor only to audio.
                # Field logs on 2026-06-07 showed airplane-like sustained noise
                # at pre-DSP 0.0089-0.0104 still carrying context and repeatedly
                # regurgitating the previous sentence. LOW_SIGNAL_ENERGY_FOR_CTX
                # (0.012) is below the observed normal-speech tier (user speech
                # in the same log was ~0.026-0.038) but catches those noise
                # bursts. Peak-escape floors live in acoustic_policy
                # (CTX_PEAK_ESCAPE_P95 / _WHISPER); see that module's docstring
                # for the 2026-07-03 calibration v2 notes.
                # Starve-gate inputs: the caller's pre-DSP hints when given,
                # else the audio's own pre-normalization MAE/p95. Hint-less
                # calls used to skip this gate entirely (guard required
                # hint >= 0), which let the selection-command path inject the
                # full hotword context on dead-silence segments — the exact
                # surface where the sherpa 0.6B echoes the context back
                # (upstream #3509; 2026-07-19 slim trial P0).
                if pre_screen_hint >= 0:
                    starve_gate_energy = pre_screen_hint
                    starve_gate_p95 = pre_screen_p95_hint
                    starve_gate_src = "pre_dsp_hint"
                else:
                    starve_gate_energy = audio_mae_pre_normalize
                    starve_gate_p95 = audio_p95_pre_normalize
                    starve_gate_src = "audio_fallback"
                near_silence_context_guard = (
                    starve_gate_energy < LOW_SIGNAL_ENERGY_FOR_CTX
                )
                # Tracks whether the peak escape kept context alive in the
                # sub-0.012 band. When it did, recent_ctx is now supplied on a
                # low-energy segment, so the recent-regurgitation backstop
                # (Trigger 3) must widen to cover this band — otherwise a
                # sustained-typing p95 spike could pass the escape and let the
                # decoder parrot recent_context with no guard between 0.008
                # and 0.012 (Trigger 3's old ceiling).
                ctx_peak_escape_fired = False
                if near_silence_context_guard and starve_gate_p95 >= 0:
                    _peak_floor = (
                        CTX_PEAK_ESCAPE_P95_WHISPER
                        if effective_capture_mode == "whisper"
                        else CTX_PEAK_ESCAPE_P95
                    )
                    if starve_gate_p95 >= _peak_floor:
                        near_silence_context_guard = False
                        ctx_peak_escape_fired = True
                        _qwen3_log(
                            f"[CTX-PEAK-ESCAPE] avg={starve_gate_energy:.5f} "
                            f"< {LOW_SIGNAL_ENERGY_FOR_CTX} but "
                            f"p95={starve_gate_p95:.5f} >= {_peak_floor} "
                            f"({effective_capture_mode or 'standard'}, "
                            f"{starve_gate_src}): "
                            f"silence-diluted real speech, keeping context"
                        )
                hotword_context_used = hotword_context
                if near_silence_context_guard and hotword_context_used:
                    _qwen3_log(
                        f"[CTX-STARVE] energy={starve_gate_energy:.5f}, "
                        f"p95={starve_gate_p95:.5f} ({starve_gate_src}), "
                        f"dropping hotword_ctx ({len(hotword_context_used)} chars) "
                        f"in near-silence"
                    )
                    hotword_context_used = ""

                if near_silence_context_guard and recent_ctx:
                    _qwen3_log(
                        f"[CTX-STARVE] energy={starve_gate_energy:.5f}, "
                        f"p95={starve_gate_p95:.5f} ({starve_gate_src}), "
                        f"dropping recent_ctx ({len(recent_ctx)} chars) "
                        f"to prevent regurgitation"
                    )
                    recent_ctx_used = ""
                else:
                    recent_ctx_used = recent_ctx

                if hotword_context_used and recent_ctx_used:
                    full_context = hotword_context_used + "\n" + recent_ctx_used
                else:
                    full_context = hotword_context_used or recent_ctx_used

                # Enhanced debug logging with context preview
                context_preview = (
                    full_context[:300] + "..."
                    if len(full_context) > 300
                    else full_context
                )
                _qwen3_log(
                    f">>> transcribe() - audio shape={audio_float.shape}\n"
                    f"    context_chars={len(full_context)}"
                    f" (hotword={len(hotword_context_used)}/{len(hotword_context)}, "
                    f"recent_passed={len(recent_ctx_used)}/{len(recent_ctx)})\n"
                    f"    context_preview:\n{context_preview}"
                )

                results = self._model.transcribe(
                    audio=audio_tuple,
                    context=full_context if full_context else None,
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
                # Multiple triggers feed the same retry-without-context path.
                text = text.strip()
                # Prefer the pre-DSP hint over post-DSP energy. In whisper
                # mode the AGC lifts noise by ~30dB, which makes post-DSP
                # energy useless for "is this near-silence?" classification.
                # Hint is consumed (cleared below) so a stale value can't
                # silently apply to the next call.
                #
                # Reuse the value captured at the TOP of this call
                # (pre_screen_hint) instead of re-reading the attribute:
                # setters are lock-free, so another thread (interim timer)
                # can overwrite the attribute mid-call, and re-reading here
                # would make the starve gate and the leakage-trigger tiers
                # disagree about the segment's energy within ONE transcribe.
                hint_energy = pre_screen_hint
                # Consume-always: clear instance energy/p95 after use so a
                # stale setter value cannot leak into a later bare call.
                # Do not touch _capture_mode_hint (set-and-stays).
                self._pre_dsp_energy_hint = -1.0
                self._pre_dsp_p95_hint = -1.0
                if hint_energy >= 0:
                    audio_energy = hint_energy
                else:
                    # Same pre-normalization MAE the starve gate used, so the
                    # gate and the leakage-trigger tiers agree on ONE energy
                    # for hint-less calls (the boosted post-normalization MAE
                    # previously read ~5x high and disarmed the strict tiers).
                    audio_energy = audio_mae_pre_normalize
                audio_duration_s = len(audio_float) / 16000

                leakage_reason = None
                # Trigger 1 (speed) is a weak proxy that misfires on dense real
                # speech; track when it alone fired so the retry can protect a
                # coherent original instead of swapping in a degenerate retry.
                leakage_is_speed_only = False
                if text:
                    # Trigger 0: context echo — the output contains a long
                    # verbatim run of the hotword/domain context injected on
                    # THIS call. Deliberately NOT energy-gated: the 2026-07-19
                    # slim-trial P0 leak rode on clear speech (pre-DSP 0.0225,
                    # 8.8s: real sentence + the entire 309-char context block
                    # inline), where Trigger 2/3 are disarmed by design and
                    # Trigger 1's keep-original arm protected the echo. A
                    # >=CONTEXT_ECHO_MIN_RUN normalized-char run is decoder
                    # regurgitation evidence at any energy; it must never be
                    # classified speed-only, so it is checked FIRST and routes
                    # to the plain retry arms (meaningful retry replaces the
                    # output, empty retry discards it).
                    if hotword_context_used:
                        _echo_run = find_context_echo(text, hotword_context_used)
                        if _echo_run:
                            leakage_reason = (
                                "context echo "
                                f"(>={CONTEXT_ECHO_MIN_RUN} normalized chars: "
                                f"'{_echo_run}')"
                            )

                    # Trigger 1: impossible speech rate catches recent_context
                    # regurgitation (model outputs prior ASR text verbatim).
                    # Energy-gated: at clear-speech energy (>=0.015) dense Chinese
                    # run-on legitimately reaches 25-35 chars/s, so only flag a
                    # genuinely impossible rate; at low energy real dense speech is
                    # implausible, so keep the strict bound.
                    # len>20 guards against short-text false positives.
                    if (
                        not leakage_reason
                        and audio_duration_s > 0
                        and len(text) > HALLUCINATION_SPEED_MIN_CHARS
                    ):
                        chars_per_sec = len(text) / audio_duration_s
                        speed_limit = (
                            HALLUCINATION_SPEED_LIMIT_CLEAR
                            if audio_energy >= HALLUCINATION_SPEED_CLEAR_ENERGY
                            else HALLUCINATION_SPEED_LIMIT_LOW
                        )
                        if chars_per_sec > speed_limit:
                            leakage_reason = (
                                f"impossible speech rate "
                                f"{chars_per_sec:.1f} chars/s "
                                f"({len(text)} chars / {audio_duration_s:.2f}s)"
                            )
                            leakage_is_speed_only = True

                    # Trigger 2: output matches hotword_context.
                    # Energy-gated (< 0.015): at clear-speech energy the model has
                    # a strong acoustic anchor, and a sentence that is mostly
                    # hotwords is usually the user DICTATING those domain terms —
                    # flagging it here sent real speech through the no-context
                    # retry, which loses exactly the rare terms biasing exists to
                    # protect (worst on the 0.6B CPU profile).  True hotword
                    # parroting happens on weak/noise audio, which stays covered.
                    if (
                        not leakage_reason
                        and hotword_context
                        and 0 <= audio_energy < MODERATE_ENERGY
                    ):
                        if self._is_context_leakage(
                            text, hotword_context, audio_energy
                        ):
                            leakage_reason = "matches hotword context"

                    # Trigger 3 (whisper-mode regurgitation guard):
                    # Real speech naturally shares vocabulary with recent topic
                    # at NORMAL energy, so we can't blanket-check recent_context.
                    # But at near-silence (pre-DSP energy < STRICT tier) the
                    # only thing reaching the model is residual noise post-DSP-
                    # boost, and matching recent_context tightly = the model
                    # parroted prior output. Limit to the strictest tier so
                    # legitimate quiet speech that resembles prior content
                    # (e.g. user repeating themselves at normal volume) is
                    # never killed.
                    # When the peak escape kept recent_ctx alive in the
                    # STRICT..LOW_SIGNAL band, widen this backstop to the same
                    # ceiling so a sustained-noise p95 spike that slipped
                    # context back in can still be caught parroting it.
                    recent_leak_ceiling = (
                        LOW_SIGNAL_ENERGY_FOR_CTX
                        if ctx_peak_escape_fired
                        else STRICT_ENERGY
                    )
                    if (
                        not leakage_reason
                        and recent_ctx
                        and 0 <= audio_energy < recent_leak_ceiling
                    ):
                        if self._is_context_leakage(text, recent_ctx, audio_energy):
                            leakage_reason = (
                                "regurgitates recent_context at near-silence"
                            )

                if leakage_reason:
                    _qwen3_log(
                        f"[HALLUCINATION] Suspected ({leakage_reason}): '{text[:80]}...'"
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
                        # Distinguish "retry ran and heard nothing" (strong
                        # evidence the original was context echo) from "retry
                        # crashed" (no evidence either way — keep original).
                        retry_attempt_failed = False
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
                            retry_attempt_failed = True

                        # Filter filler tokens from retry (common noise artifacts)
                        _FILLER_TOKENS = {"嗯", "啊", "呃", "哦", "额", "噢", "唔"}
                        retry_text_core = retry_text.strip().strip(
                            "。！？!?，,、. \t\r\n"
                        )
                        if retry_text and retry_text_core in _FILLER_TOKENS:
                            _qwen3_log(f"[RETRY] Filtered filler token: '{retry_text}'")
                            retry_text = ""

                        low_signal_speed_only = (
                            leakage_is_speed_only
                            and 0 <= audio_energy < MODERATE_ENERGY
                            and len(text) >= 30
                        )

                        # Low-energy speed-only hallucinations are the airplane/
                        # fan-noise failure mode: the retry usually returns a
                        # filler/empty string, while the original is a long prior
                        # sentence. Do NOT keep the original in this acoustic
                        # tier; prefer a meaningful retry, otherwise discard.
                        if low_signal_speed_only:
                            if retry_text and len(retry_text) >= 2:
                                _qwen3_log(
                                    f"[RETRY] Low-signal speed flag recovered text: "
                                    f"'{retry_text[:80]}'"
                                )
                                logger.info(
                                    "Qwen3 ASR: Low-signal speed retry recovered text"
                                )
                                text = retry_text
                                if retry_results and len(retry_results) > 0:
                                    detected_language = (
                                        getattr(
                                            retry_results[0],
                                            "language",
                                            detected_language,
                                        )
                                        or detected_language
                                    )
                            else:
                                _qwen3_log(
                                    "[HALLUCINATION] Confirmed "
                                    "(low-signal speed-only, retry empty/trivial), "
                                    "discarding"
                                )
                                logger.warning(
                                    "Qwen3 ASR: Low-signal speed hallucination "
                                    "confirmed, returning empty"
                                )
                                text = ""
                        # Retry-protection: when only the weak speed heuristic
                        # flagged this, a coherent original that the retry comes
                        # back far shorter on means the retry lost context biasing
                        # and degenerated — keep the original real speech instead
                        # of swapping in the shorter (often garbage) retry.  The
                        # protection requires the retry to have actually HEARD
                        # something (>=2 chars) or to have crashed outright; a
                        # clean retry that heard nothing is handled below as a
                        # confirmed context echo.
                        elif (
                            leakage_is_speed_only
                            and len(text) >= 30
                            and (len(retry_text) >= 2 or retry_attempt_failed)
                            and len(retry_text) < len(text) * 0.5
                        ):
                            _qwen3_log(
                                f"[RETRY] Kept original ({len(text)} chars) over "
                                f"shorter retry ({len(retry_text)} chars) — "
                                f"speed-only flag"
                            )
                            logger.info(
                                "Qwen3 ASR: Kept original over degenerate retry"
                            )
                        elif retry_text and len(retry_text) >= 2:
                            _qwen3_log(
                                f"[RETRY] Recovered real speech: '{retry_text[:80]}'"
                            )
                            logger.info(f"Qwen3 ASR: Leakage retry recovered text")
                            text = retry_text
                            # Update detected language from retry result
                            if retry_results and len(retry_results) > 0:
                                detected_language = (
                                    getattr(
                                        retry_results[0],
                                        "language",
                                        detected_language,
                                    )
                                    or detected_language
                                )
                        elif leakage_is_speed_only and len(text) >= 30:
                            # The context-free retry ran cleanly and heard
                            # NOTHING.  Real speech never decodes to empty on
                            # audible audio (an unbiased decode returns unbiased
                            # words, not silence), so a long "sentence" that only
                            # appears WITH context is the context itself echoed
                            # back — drop it instead of inserting old text.
                            # (Field case 2026-07-04 04:38: 1.38s of mic taps
                            # decoded to a 76-char sentence spoken 8 min earlier;
                            # the old keep-original arm let the echo through.)
                            _qwen3_log(
                                "[HALLUCINATION] Confirmed (speed-only flag, "
                                f"retry heard nothing) — discarding {len(text)} "
                                "chars of context echo"
                            )
                            logger.warning(
                                "Qwen3 ASR: speed-only flag with empty retry — "
                                "discarding as context echo"
                            )
                            text = ""
                        else:
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
                # Consume-always on the error path too (energy/p95 only).
                self._pre_dsp_energy_hint = -1.0
                self._pre_dsp_p95_hint = -1.0
                return ASRResult(
                    text="",
                    type=TranscriptType.FINAL,
                    language=self.config.language,
                    confidence=0.0,
                )

    def trim_runtime_cache(self, reason: str = "manual") -> None:
        """Release non-model CUDA/Python caches without unloading the ASR model."""
        try:
            import gc

            before = _process_memory_summary()
            cuda_active = (
                str(getattr(self, "actual_device", "") or "").lower().startswith("cuda")
            )
            torch = None
            if cuda_active:
                import torch

                try:
                    torch.cuda.synchronize()
                except Exception:
                    pass
                torch.cuda.empty_cache()
                try:
                    torch._C._cuda_clearCublasWorkspaces()
                except AttributeError:
                    pass
            gc.collect()
            if cuda_active and torch is not None:
                torch.cuda.empty_cache()
            _qwen3_log(
                f"[MEM] Runtime cache trim ({reason}): "
                f"{before} -> {_process_memory_summary()}"
            )
        except Exception as exc:
            _qwen3_log(f"[MEM] Runtime cache trim failed ({reason}): {exc}")

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
        # Audio that reaches here already passed energy_threshold gate (configurable, default 0.003) in app.py.
        # Tiers: STRICT (< STRICT_ENERGY) → MODERATE (< MODERATE_ENERGY) → STANDARD
        has_energy_info = audio_energy >= 0
        is_strict = has_energy_info and audio_energy < STRICT_ENERGY
        is_moderate = (
            has_energy_info and STRICT_ENERGY <= audio_energy < MODERATE_ENERGY
        )

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
        # Catches mutated leakage like "编辑AI工具，编辑TTS，FunASR，PyTorch..."
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
        """Unload the model to free memory (with proper VRAM cleanup)."""
        with self._lock:
            if self._model is not None:
                import gc

                try:
                    import torch
                    from pathlib import Path
                    import datetime

                    log_path = (
                        Path(__file__).parent.parent.parent
                        / "DebugLog"
                        / "pipeline_debug.log"
                    )

                    def _ulog(msg):
                        from ..debug import append_log_line

                        ts = datetime.datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S.%f"
                        )[:-3]
                        line = f"[{ts}] [UNLOAD] {msg}"
                        print(line)
                        append_log_line(log_path, line)

                    def _vram_mb():
                        if torch.cuda.is_available():
                            return torch.cuda.memory_allocated() / 1024**2
                        return 0

                    v0 = _vram_mb()
                    _ulog(f"Start: {v0:.0f} MB allocated (PyTorch)")

                    # Qwen3ASRModel is a wrapper, not nn.Module.
                    # Internal: .model = nn.Module (GPU), .processor = tokenizer
                    inner_model = getattr(self._model, "model", None)
                    _ulog(
                        f"inner_model={type(inner_model).__name__}, "
                        f"is_nn_Module={isinstance(inner_model, torch.nn.Module) if inner_model else False}"
                    )

                    # Step 1: Move inner nn.Module to CPU
                    if inner_model is not None and hasattr(inner_model, "to"):
                        try:
                            inner_model.cpu()
                            v1 = _vram_mb()
                            _ulog(f"After .cpu(): {v1:.0f} MB (freed {v0 - v1:.0f} MB)")
                        except Exception as e:
                            _ulog(f".cpu() FAILED: {e}")
                        # Break wrapper → inner references
                        try:
                            self._model.model = None
                            self._model.processor = None
                            self._model.forced_aligner = None
                        except Exception:
                            pass
                        del inner_model
                    else:
                        attrs = [a for a in dir(self._model) if not a.startswith("_")]
                        _ulog(f"WARNING: inner model not found! Attrs: {attrs}")

                    # Step 2: Delete wrapper
                    del self._model
                    self._model = None

                    # Step 3: GC
                    for i in range(3):
                        collected = gc.collect()
                        if i == 0:
                            _ulog(f"gc pass 1: collected {collected} objects")

                    v2 = _vram_mb()
                    _ulog(f"After gc+del: {v2:.0f} MB")

                    # Step 4: Aggressively release CUDA memory
                    if torch.cuda.is_available():
                        torch.cuda.synchronize()
                        torch.cuda.empty_cache()

                        # Free hidden cuBLAS/cuDNN workspace memory
                        # (empty_cache doesn't touch these, can be 100-500MB)
                        try:
                            torch._C._cuda_clearCublasWorkspaces()
                        except AttributeError:
                            pass

                        gc.collect()
                        torch.cuda.empty_cache()
                        torch.cuda.synchronize()

                    # Log final state
                    try:
                        free_mem, total = torch.cuda.mem_get_info(0)
                        used_nvidia = (total - free_mem) / 1024**2
                        alloc = torch.cuda.memory_allocated() / 1024**2
                        reserved = torch.cuda.memory_reserved() / 1024**2
                        _ulog(
                            f"DONE: PyTorch alloc={alloc:.0f} MB, "
                            f"reserved={reserved:.0f} MB, "
                            f"nvidia-smi={used_nvidia:.0f} MB, "
                            f"freed={v0 - alloc:.0f} MB tensors"
                        )
                    except Exception:
                        pass
                except Exception as e:
                    # Fallback: basic cleanup
                    self._model = None
                    gc.collect()
                    logger.warning(f"Qwen3 unload partial: {e}")

    @staticmethod
    def is_available() -> bool:
        """Check if qwen_asr is installed."""
        check_qwen3_installation()
        return bool(_qwen3_available)
