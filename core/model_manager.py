"""
Model Manager
=============
Handles FunASR model verification and first-run download.
"""

import os
import sys
import json
import hashlib
import urllib.request
import ssl
from pathlib import Path
from typing import Optional, Callable, List, Tuple
from dataclasses import dataclass


@dataclass
class ModelInfo:
    """Information about a required model."""

    name: str
    model_id: str  # ModelScope model ID
    size_mb: int
    description: str
    required: bool = True


# Required FunASR models
REQUIRED_MODELS = [
    ModelInfo(
        name="paraformer-zh",
        model_id="iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        size_mb=350,
        description="语音识别核心模型",
        required=True,
    ),
    ModelInfo(
        name="fsmn-vad",
        model_id="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        size_mb=50,
        description="语音活动检测",
        required=True,
    ),
    ModelInfo(
        name="ct-punc",
        model_id="iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
        size_mb=300,
        description="标点恢复",
        required=True,
    ),
]


def get_modelscope_cache_dir() -> Path:
    """Get the ModelScope cache directory."""
    # ModelScope uses this path by default
    cache_dir = os.environ.get("MODELSCOPE_CACHE")
    if cache_dir:
        return Path(cache_dir)

    # Default: ~/.cache/modelscope/hub/
    return Path.home() / ".cache" / "modelscope" / "hub"


def check_model_exists(model: ModelInfo) -> bool:
    """Check if a model exists in the cache."""
    cache_dir = get_modelscope_cache_dir()
    model_dir = cache_dir / model.model_id.replace("/", os.sep)

    # Check if directory exists and has some content
    if model_dir.exists():
        # Check for model.safetensors or pytorch_model.bin
        for pattern in [
            "*.safetensors",
            "*.bin",
            "*.pt",
            "model.json",
            "configuration.json",
        ]:
            if list(model_dir.rglob(pattern)):
                return True

    return False


def get_missing_models() -> List[ModelInfo]:
    """Get list of required models that are not yet downloaded."""
    missing = []
    for model in REQUIRED_MODELS:
        if model.required and not check_model_exists(model):
            missing.append(model)
    return missing


def get_total_download_size(models: List[ModelInfo]) -> int:
    """Get total download size in MB."""
    return sum(m.size_mb for m in models)


class ModelDownloader:
    """
    Downloads FunASR models from ModelScope.

    Uses FunASR's built-in download mechanism which handles:
    - Mirror selection (ModelScope China vs HuggingFace)
    - Caching
    - Progress reporting
    """

    def __init__(
        self,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ):
        """
        Initialize downloader.

        Args:
            progress_callback: Called with (model_name, downloaded_bytes, total_bytes)
            status_callback: Called with status message
        """
        self.progress_callback = progress_callback
        self.status_callback = status_callback
        self._cancelled = False

    def cancel(self):
        """Request download cancellation."""
        self._cancelled = True

    def _emit_status(self, msg: str):
        """Emit status message."""
        if self.status_callback:
            self.status_callback(msg)
        print(f"[ModelDownloader] {msg}")

    def _emit_progress(self, model: str, downloaded: int, total: int):
        """Emit progress update."""
        if self.progress_callback:
            self.progress_callback(model, downloaded, total)

    def download_model(self, model: ModelInfo) -> bool:
        """
        Download a single model using FunASR's mechanism.

        Returns True if successful.
        """
        if self._cancelled:
            return False

        self._emit_status(f"正在下载: {model.description} ({model.size_mb}MB)...")

        try:
            # FunASR's AutoModel handles downloading automatically
            # We just need to import and instantiate the model
            from funasr import AutoModel

            # This will trigger download if not cached
            # Set environment to use ModelScope (better for China)
            os.environ.setdefault("MODELSCOPE_DOWNLOAD_MODEL", "1")

            if model.name == "paraformer-zh":
                # Load paraformer model - this triggers download
                AutoModel(
                    model=model.model_id,
                    hub="ms",  # ModelScope
                    device="cpu",  # Use CPU for download only
                    disable_update=True,
                )
            elif model.name == "fsmn-vad":
                # VAD model is typically loaded with main model
                # Just verify it can be loaded
                AutoModel(
                    model=model.model_id,
                    hub="ms",
                    device="cpu",
                    disable_update=True,
                )
            elif model.name == "ct-punc":
                # Punctuation model
                AutoModel(
                    model=model.model_id,
                    hub="ms",
                    device="cpu",
                    disable_update=True,
                )

            self._emit_status(f"已完成: {model.description}")
            return True

        except Exception as e:
            self._emit_status(f"下载失败: {model.description} - {e}")
            return False

    def download_all_missing(self) -> Tuple[int, int]:
        """
        Download all missing required models.

        Returns:
            Tuple of (successful_count, failed_count)
        """
        missing = get_missing_models()
        if not missing:
            self._emit_status("所有模型已就绪")
            return (0, 0)

        total_size = get_total_download_size(missing)
        self._emit_status(f"需要下载 {len(missing)} 个模型 (约 {total_size}MB)")

        success = 0
        failed = 0

        for model in missing:
            if self._cancelled:
                self._emit_status("下载已取消")
                break

            if self.download_model(model):
                success += 1
            else:
                failed += 1

        return (success, failed)


def ensure_models_available(
    on_missing: Optional[Callable[[List[ModelInfo]], bool]] = None
) -> bool:
    """
    Ensure all required models are available.

    Args:
        on_missing: Callback when models are missing. Should return True to proceed
                   with download, False to abort. If None, will print message and
                   proceed automatically.

    Returns:
        True if all models are available (or were downloaded successfully)
    """
    missing = get_missing_models()

    if not missing:
        return True

    total_size = get_total_download_size(missing)

    if on_missing:
        proceed = on_missing(missing)
        if not proceed:
            return False
    else:
        print(f"[Model Manager] Missing {len(missing)} models ({total_size}MB)")
        print("[Model Manager] Will download on first use...")
        return True  # Let FunASR handle download lazily

    # Download missing models
    downloader = ModelDownloader()
    success, failed = downloader.download_all_missing()

    return failed == 0


# For testing
if __name__ == "__main__":
    print("=== Model Manager Test ===")
    print(f"Cache dir: {get_modelscope_cache_dir()}")
    print()

    for model in REQUIRED_MODELS:
        exists = check_model_exists(model)
        status = "OK" if exists else "MISSING"
        print(f"[{status}] {model.name}: {model.description} ({model.size_mb}MB)")

    print()
    missing = get_missing_models()
    if missing:
        print(f"Missing {len(missing)} models ({get_total_download_size(missing)}MB)")
    else:
        print("All models available!")
