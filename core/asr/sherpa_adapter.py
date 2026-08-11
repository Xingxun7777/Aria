"""
sherpa_adapter.py — drop-in replacement for the qwen-asr PyTorch model object
that lives inside Aria's ``Qwen3ASREngine`` (``self._model``).

It mimics the *minimal* interface the engine wrapper actually depends on
(see core/asr/qwen3_engine.py transcribe()):

    model.transcribe(audio=(np.float32_array, sample_rate),
                     context=str | None,
                     language=str | None)  ->  [ _Result(text=str, language=str) ]

The wrapper only reads ``results[0].text`` and ``results[0].language``.

Backend: sherpa-onnx OfflineRecognizer (Qwen3-ASR int8), CPU. This object
replaces the multi-GB torch path with a ~950 MB int8 model and needs no torch
in the process.

Compatibility notes (ported from aria-rs, decisions dated 2026-06-02):
  * Per-utterance FREE-TEXT context IS supported — and is the primary biasing
    path. ``OfflineStream.set_option("hotwords", text)`` injects free text into
    the Qwen3-ASR SYSTEM prompt (sherpa formats it with
    Qwen3FormatHotwordsForPrompt: ASCII commas -> spaces, every other char
    kept). That is the SAME system-prompt slot Aria's native PyTorch path wrote
    to, so Aria's dynamic ``full_context`` (hotword list + ``recent_context``)
    is reproduced faithfully, per utterance, with NO recognizer rebuild. The
    per-stream option OVERRIDES construction hotwords (sherpa source:
    ``HasOption("hotwords") ? GetOption(...) : config``), so the recognizer is
    built with empty hotwords and all biasing flows per call.
    (Verified on raokouling.wav: context corrected 骨质疏松症 / 紫色柿子树 /
    灰黑灰化肥 that the no-context decode got wrong.)
  * Because sherpa splits the hotwords option on ASCII commas and joins with
    spaces, ASCII commas inside Aria's free-text context are defensively
    replaced with full-width commas before injection (semantics preserved,
    no token loss).
  * ``set_option("language", lang)`` forces the decode language the way the
    native path passed ``config.language`` ("Chinese"); skipped when ``lang``
    is falsy (qwen3 auto-detects).
  * ``set_hotwords()`` (construction-time rebuild) is retained for external
    callers but is no longer on the biasing path.
  * ``sherpa_onnx`` is imported lazily inside ``_build()`` so this module can
    be imported (and the rest of the app can run the torch path) on
    deployments where the sherpa_onnx wheel is not installed.
"""

from __future__ import annotations

import os
import glob
from dataclasses import dataclass

import numpy as np


@dataclass
class _Result:
    """Mimics qwen-asr's transcription object: only .text / .language are read."""

    text: str = ""
    language: str = ""


class SherpaQwen3Model:
    """
    Off-the-shelf sherpa-onnx Qwen3-ASR int8 backend, shaped like the qwen-asr model.

    Args:
        model_dir: folder containing conv_frontend.onnx, encoder.int8.onnx,
                   decoder.int8.onnx and tokenizer/ (the extracted
                   sherpa-onnx-qwen3-asr-*-int8 release).
        hotwords:  initial comma-separated static hotword string (may be "").
        provider:  "cpu" (default) or "cuda". CPU is fast enough for short
                   utterances (0.2-0.9 s measured) so cpu avoids the CUDA DLL
                   supply chain.
        num_threads: CPU threads for ORT.
        max_total_len / max_new_tokens / temperature / top_p / seed: passed
                   through to the Qwen3-ASR decoder (defaults match the sherpa
                   model).
    """

    def __init__(
        self,
        model_dir: str,
        hotwords: str = "",
        provider: str = "cpu",
        num_threads: int = 8,
        max_total_len=None,
        max_new_tokens=None,
        temperature=None,
        top_p=None,
        seed=None,
    ) -> None:
        self._dir = model_dir
        self._conv = self._one(model_dir, "conv_frontend*.onnx")
        self._enc = self._one(model_dir, "encoder*.onnx")
        self._dec = self._one(model_dir, "decoder*.onnx")
        self._tok = (
            os.path.join(model_dir, "tokenizer")
            if os.path.isdir(os.path.join(model_dir, "tokenizer"))
            else model_dir
        )
        self._provider = provider
        self._num_threads = num_threads
        # Only forward optional decoder params when explicitly set — leaving
        # them unset matches sherpa's own defaults (the known-good call).
        # Passing temperature=0.0 explicitly was observed to truncate long
        # generations (raokouling 150->8 chars), so default to None = don't pass.
        self._opt = {
            k: v
            for k, v in (
                ("max_total_len", max_total_len),
                ("max_new_tokens", max_new_tokens),
                ("temperature", temperature),
                ("top_p", top_p),
                ("seed", seed),
            )
            if v is not None
        }
        self._hotwords = (hotwords or "").strip()
        self._rec = None
        self._build()

    @staticmethod
    def _one(d: str, pattern: str) -> str:
        hits = sorted(glob.glob(os.path.join(d, pattern)))
        if not hits:
            raise FileNotFoundError(f"{pattern} not found in {d}")
        return hits[0]

    def _build(self) -> None:
        # Lazy import: the mainline app must keep running the torch qwen3 path
        # on deployments without the sherpa_onnx wheel; only actually building
        # a sherpa recognizer requires the package.
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError(
                "sherpa_onnx 未安装，无法使用 Qwen3-ASR 轻量引擎 "
                "(pip install sherpa-onnx==1.13.4)"
            ) from exc

        self._rec = sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
            conv_frontend=self._conv,
            encoder=self._enc,
            decoder=self._dec,
            tokenizer=self._tok,
            num_threads=self._num_threads,
            provider=self._provider,
            hotwords=self._hotwords,
            **self._opt,
        )
        # Per-utterance context needs OfflineStream.set_option (sherpa qwen3
        # >= 1.12). Probe once so transcribe() degrades gracefully (no context)
        # on an older API instead of raising AttributeError mid-utterance.
        self._supports_set_option = hasattr(self._rec.create_stream(), "set_option")

    def set_hotwords(self, hotwords: str) -> None:
        """Rebuild the recognizer with a new static hotword string. ~3 s; call rarely."""
        new = (hotwords or "").strip()
        if new == self._hotwords:
            return
        self._hotwords = new
        self._build()

    def transcribe(self, audio, context=None, language=None):
        """
        Mimics qwen-asr's model.transcribe(). Returns a list with one _Result.

        ``context`` (Aria's per-utterance ``full_context`` = hotword list +
        ``recent_context``) is injected into the Qwen3-ASR system prompt via
        ``OfflineStream.set_option("hotwords", ...)`` — the same biasing slot
        the native PyTorch path used. ``context=None`` (e.g. the engine's
        retry-without-context path) decodes with no biasing. See module
        docstring.
        """
        # Accept (array, sample_rate) tuple (what the wrapper passes) or a bare array.
        if isinstance(audio, tuple):
            samples, sr = audio
        else:
            samples, sr = audio, 16000
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim > 1:
            samples = samples[:, 0]
        if samples.size == 0:
            return [_Result(text="", language=language or "")]

        stream = self._rec.create_stream()
        # Per-utterance biasing: free-text context -> qwen3 system prompt. The
        # per-stream option overrides the (empty) construction hotwords, so
        # this carries Aria's full dynamic context with no recognizer rebuild.
        if context and self._supports_set_option:
            # sherpa splits the option value on ASCII commas (then joins with
            # spaces); full-width commas keep Aria's free text intact.
            stream.set_option("hotwords", str(context).replace(",", "，"))
        # Force the decode language the way the native path passed
        # config.language; falsy language = let qwen3 auto-detect.
        if language and self._supports_set_option:
            stream.set_option("language", language)
        stream.accept_waveform(int(sr), samples)
        self._rec.decode_stream(stream)
        text = (stream.result.text or "").strip()
        return [_Result(text=text, language=language or "")]
