"""
sherpa_engine.py — SherpaQwen3Engine: a torch-free drop-in for Aria's Qwen3ASREngine.

It SUBCLASSES the original ``Qwen3ASREngine`` and overrides ONLY the model
lifecycle (load / unload / hotwords). The entire ``transcribe()`` path — RMS
normalization, the two near-silence pre-screens, the three hallucination/
leakage triggers (impossible speech-rate, hotword-context match, recent-context
regurgitation), retry-without-context and filler filtering — is INHERITED
UNCHANGED, so the quality guards Aria tuned over months are preserved
byte-for-byte.

``self._model`` becomes a ``SherpaQwen3Model`` (sherpa-onnx int8) instead of
the qwen-asr PyTorch model. Both expose
``model.transcribe(audio=(arr, sr), context, language) -> [obj.text/.language]``,
so the inherited wrapper drives either backend identically.

The engine runs with NO torch in the inference path — the torch load machinery
(``_load_with_fallback``, CUDA probing, dtype resolution) is simply never
called because ``load()`` is overridden.

Context: Aria's per-utterance free-text context (hotword list +
``recent_context``) is PRESERVED. The inherited ``transcribe()`` builds
``full_context`` and passes it to ``self._model.transcribe(context=...)``;
``SherpaQwen3Model`` injects it into the Qwen3-ASR system prompt via
``OfflineStream.set_option`` — the same slot the native PyTorch path used — so
the months-tuned biasing + leakage guards run unchanged, with NO recognizer
rebuild. Hotword updates are therefore just a base-class config refresh; the
next utterance picks them up automatically.
"""

from __future__ import annotations

import os

# In-package imports: SherpaQwen3Engine subclasses the base engine and uses the
# sherpa-onnx model shim, both siblings in this package. Neither import pulls
# torch or sherpa_onnx at module level (both are lazy inside their load paths),
# so this module is always importable — even on deployments without the
# sherpa_onnx wheel, where only load() fails with a clear message.
from .qwen3_engine import Qwen3ASREngine, Qwen3Config
from .sherpa_adapter import SherpaQwen3Model

DEFAULT_SHERPA_MODEL_SUBDIR = "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"


def default_sherpa_num_threads() -> int:
    """Core-count-aware ORT thread default: min(16, max(4, cpu_count // 2)).

    The old flat default of 8 left most of a big machine idle (2026-07-19
    slim trial: 32 logical cores, transcribe p50 ~2x the eval expectation)
    while oversubscribing small ones. Half the logical cores approximates
    the physical core count on typical SMT machines; the 4..16 clamp keeps
    small boxes usable and avoids ORT diminishing returns beyond 16 threads.
    An explicit ``num_threads`` in config always wins over this default.
    """
    cores = os.cpu_count() or 8
    return min(16, max(4, cores // 2))


def check_sherpa_installation() -> bool:
    """Return True when the sherpa_onnx wheel is importable (lazy check)."""
    try:
        import sherpa_onnx  # noqa: F401

        return True
    except Exception:
        return False


def resolve_sherpa_model_dir(model_dir: str | None = None) -> str:
    """Resolve the sherpa model dir portably (no hardcoded dev path).

    Order: explicit ``model_dir`` (relative paths resolve against the project
    root) -> ``ARIA_SHERPA_MODEL_DIR`` env override -> ``<root>/models/<subdir>``,
    where ``<root>`` is the project root containing this ``core`` package (dev
    tree or an unpacked release).

    Deliberately a function evaluated at engine-construction time, NOT an
    import-time module constant: env vars set after import (tests, launcher
    bootstrap) must still take effect.
    """
    from ..utils.paths import get_base_path

    root = str(get_base_path())
    explicit = (model_dir or "").strip()
    if explicit:
        if os.path.isabs(explicit):
            return explicit
        return os.path.join(root, explicit)
    env = os.environ.get("ARIA_SHERPA_MODEL_DIR", "").strip()
    if env:
        return env
    return os.path.join(root, "models", DEFAULT_SHERPA_MODEL_SUBDIR)


class SherpaQwen3Engine(Qwen3ASREngine):
    """Torch-free Qwen3-ASR engine backed by sherpa-onnx int8.

    Drop-in for ``Qwen3ASREngine``: same public interface, same transcribe
    guards, different (PyTorch-free) model backend.
    """

    def __init__(
        self,
        config: Qwen3Config | None = None,
        model_dir: str | None = None,
        provider: str = "cpu",
        num_threads: int | None = None,
    ) -> None:
        super().__init__(config)
        self._model_dir = resolve_sherpa_model_dir(model_dir)
        self._provider = str(provider or "cpu").strip().lower() or "cpu"
        # None/0 → core-aware default; explicit config value always wins.
        self._num_threads = (
            max(1, int(num_threads)) if num_threads else default_sherpa_num_threads()
        )
        # Keep config.device / actual_device truthful for every app-side device
        # check (CPU VAD policy, warmup gating, telemetry) even before load():
        # the sherpa provider IS the runtime device.
        self.config.device = self._provider
        self._actual_device = self._provider
        # Truthful model identity for telemetry/status readers that consult
        # config.model_name before load() resolves loaded_model_name.
        self.config.model_name = "sherpa-onnx-qwen3-asr-0.6B-int8"
        self._loaded_model_name = self.config.model_name

    @property
    def name(self) -> str:
        return "Qwen3-ASR-sherpa (0.6B-int8)"

    # --- overridden lifecycle (replaces the torch/CUDA machinery) -------------

    def load(self) -> None:
        if self._model is not None:
            return
        if not check_sherpa_installation():
            raise RuntimeError(
                "sherpa_onnx 未安装，无法使用 Qwen3-ASR 轻量引擎；"
                "请安装 sherpa-onnx==1.13.4 或在设置中切回 Qwen3-ASR"
            )
        if not os.path.isdir(self._model_dir):
            raise RuntimeError(f"sherpa Qwen3 model dir not found: {self._model_dir}")
        # Build with NO construction hotwords: biasing is per-utterance now. The
        # inherited transcribe() passes full_context (hotword list +
        # recent_context) to SherpaQwen3Model, which feeds it to set_option. The
        # per-stream option overrides config hotwords (sherpa source), so baking
        # them in would be redundant — and skipping it means set_hotwords never
        # rebuilds the recognizer.
        try:
            self._model = SherpaQwen3Model(
                self._model_dir,
                hotwords="",
                provider=self._provider,
                num_threads=self._num_threads,
                # Pass the decode budget through to sherpa. Without these the
                # recognizer silently used sherpa's defaults (max_total_len=512,
                # max_new_tokens=128): a long utterance + full hotword/recent
                # context overran 512 and the decoder collapsed to 1-2 chars
                # (the long-sentence word-swallow). 2048 + 1024 fits ~37s
                # speech + full context; verified on field audio
                # (37.4s: 1 char -> 160 chars).
                max_total_len=self.config.max_total_len,
                max_new_tokens=self.config.max_new_tokens,
            )
        except FileNotFoundError as exc:
            # Same failure class as a missing dir: dir exists but a model
            # file is absent (partial copy / wrong release). Surface both the
            # directory and the missing file so the user can fix the copy.
            raise RuntimeError(
                f"sherpa Qwen3 model dir is incomplete: {self._model_dir} ({exc})"
            ) from exc
        self._actual_device = self._provider
        self._device_reason = "sherpa-onnx int8"
        self._loaded_model_name = "sherpa-onnx-qwen3-asr-0.6B-int8"

    def unload(self) -> None:
        model = self._model
        self._model = None
        if model is not None:
            # Drop the native OfflineRecognizer reference explicitly so the
            # ORT session is freed even if something still holds the adapter.
            model._rec = None

    # --- hotwords: per-utterance now, so just refresh config (no rebuild) ------
    # Biasing flows through the inherited transcribe()'s full_context ->
    # set_option. super() refreshes config.hotwords + _context_string; the next
    # transcribe() folds them into full_context. No recognizer rebuild.

    def set_hotwords(self, hotwords) -> None:
        super().set_hotwords(hotwords)

    def set_hotwords_with_context(self, context_string: str) -> None:
        super().set_hotwords_with_context(context_string)
