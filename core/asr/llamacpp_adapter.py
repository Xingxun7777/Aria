"""
llamacpp_adapter.py — drop-in replacement for the qwen-asr PyTorch model object
that lives inside Aria's ``Qwen3ASREngine`` (``self._model``), backed by a
local llama.cpp ``llama-server`` (Qwen3-ASR GGUF, CUDA).

It mimics the *minimal* interface the engine wrapper actually depends on
(see core/asr/qwen3_engine.py transcribe(), same contract as
core/asr/sherpa_adapter.py SherpaQwen3Model):

    model.transcribe(audio=(np.float32_array, sample_rate),
                     context=str | None,
                     language=str | None)  ->  [ _Result(text=str, language=str) ]

The wrapper only reads ``results[0].text`` and ``results[0].language``.

Request shape (validated by the 2026-07-19 llama.cpp eval,
_scratch/cluster_20260719/slim_merge/eval_llamacpp_report.md §5):

    POST http://127.0.0.1:<port>/v1/chat/completions
    messages = [
        {"role": "system", "content": <context>}          # optional biasing slot
        {"role": "user", "content": [input_audio(b64 wav)]}
        {"role": "assistant", "content": "language Chinese<asr_text>"}  # prefill
    ]
    temperature 0; response content parsed after the "<asr_text>" marker.

Decoding is pinned to strict greedy PER REQUEST (2026-07-20 quality pass):
temperature=0 + top_k=1 + repeat/presence/frequency penalties explicitly
disabled. Qwen3-ASR's official evaluation uses greedy search, and since
llama.cpp PR #9897 ``temp=0`` alone only guarantees argmax when every
pre-temperature sampler in the chain (penalties first!) is inert. Today the
server defaults match these pins exactly (verified via /props: repeat 1.0,
presence/frequency 0.0), so this is a no-op — but GGUF files can embed
sampling defaults that llama-server adopts (PR #21509), and a repeat
penalty is poison for verbatim ASR ("好好好", repeated digits). Pinning
per request makes the contract survive server/model-metadata drift.

Design notes:
  * Context injection MUST go through the chat ``system`` message — that is
    Qwen3-ASR's native biasing slot, the same one the torch path and sherpa's
    hotwords option write to. Do NOT switch to /v1/audio/transcriptions: that
    endpoint folds the prompt into the *user* turn
    (convert_transcriptions_to_chatcmpl), where biasing does not work.
  * The assistant prefill forces both the output language and the
    "language X<asr_text>TEXT" response format (verified server-side in the
    eval: prefilling English changes the output head accordingly).
  * WAV base64 encoding is REUSED from core/asr/cloud_rescue.py (same
    float32 -> 16-bit PCM WAV pipeline the cloud rescue path ships).
  * Timeout scales with audio duration like cloud_rescue's
    ``max(timeout, 10 + 0.5*s)`` — the local server needs a smaller base:
    ``max(base, 2 + 0.5*s)`` with base default 8s.
  * Any request failure (server crashed, timeout, HTTP error, bad JSON)
    returns an EMPTY result instead of raising: the app's consecutive-failure
    chain then triggers the engine self-heal reload, which rebuilds this
    engine and thereby RESTARTS llama-server — free crash recovery, by
    design (see llamacpp_engine.py).
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass

import numpy as np

from .cloud_rescue import encode_wav_base64

logger = logging.getLogger(__name__)


def logprobs_enabled_default() -> bool:
    """Whether requests ask llama-server for per-token logprobs.

    Config bridge: app.py's engine factory exports the qwen3_llamacpp
    ``logprobs`` config key as ARIA_LLAMACPP_LOGPROBS (the engine wrapper's
    ctor signature is frozen, so the adapter reads the environment instead).
    Default ON: the bundled server build supports it (llama-server-impl.dll
    carries top_logprobs/n_probs handling), the per-token cost is trivial
    next to the decode, and a 400 from an unsupported build sticky-disables
    it at runtime (see transcribe()).
    """
    return os.environ.get("ARIA_LLAMACPP_LOGPROBS", "1").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )

# Model-native response head: "language Chinese<asr_text>正文". The prefill
# already contains everything up to and including the marker, so the returned
# content normally starts right at the transcript; keep the regex as a guard
# for servers that echo the full head back.
_ASR_TEXT_RE = re.compile(r"<asr_text>(.*)$", re.S)
_LANGUAGE_HEAD_RE = re.compile(r"^\s*language\s+(\w+)")


@dataclass
class _Result:
    """Mimics qwen-asr's transcription object: only .text / .language are read.

    avg_logprob is adapter-side extra telemetry (mean per-token logprob of
    the decode, None when logprobs were unavailable); the engine wrapper
    ignores it, the app reads it off ``model.last_confidence`` instead.
    """

    text: str = ""
    language: str = ""
    avg_logprob: float | None = None


def extract_confidence(data: dict) -> dict | None:
    """Pull per-token logprob stats out of a chat completion response.

    Returns {"avg_logprob", "min_logprob", "tokens"} or None when the
    response carries no usable logprobs block (server built without it, or
    logprobs disabled). Defensive: any malformed shape returns None rather
    than raising into the transcribe path.
    """
    try:
        content = (data["choices"][0].get("logprobs") or {}).get("content")
        if not isinstance(content, list) or not content:
            return None
        values = []
        for entry in content:
            lp = entry.get("logprob") if isinstance(entry, dict) else None
            if isinstance(lp, (int, float)):
                values.append(float(lp))
        if not values:
            return None
        return {
            "avg_logprob": sum(values) / len(values),
            "min_logprob": min(values),
            "tokens": len(values),
        }
    except Exception:
        return None


def parse_asr_content(raw: str, prefill_language: str = "") -> tuple[str, str]:
    """Split a chat completion content into (transcript, language).

    With the assistant prefill in place the server returns only the
    continuation (the transcript). Without prefill (or with servers that
    echo the head) the content is "language X<asr_text>TEXT".
    """
    raw = raw or ""
    m = _ASR_TEXT_RE.search(raw)
    if m:
        text = m.group(1)
        lang_m = _LANGUAGE_HEAD_RE.match(raw)
        language = lang_m.group(1) if lang_m else (prefill_language or "")
    else:
        text = raw
        language = prefill_language or ""
    return text.strip(), language


def build_chat_payload(
    *,
    audio_b64: str,
    context: str | None,
    language: str | None,
    max_tokens: int,
    logprobs: bool = False,
) -> dict:
    """Assemble the /v1/chat/completions JSON body (separate for unit tests).

    ``logprobs=True`` adds the OpenAI-compat ``logprobs`` + ``top_logprobs``
    pair (top_logprobs=1 keeps the response payload minimal; llama.cpp maps
    the pair onto its n_probs machinery and returns PRE-sampling logprobs,
    i.e. real distribution confidence, since post_sampling_probs stays
    unset). Greedy decoding (top_k=1) is unaffected: n_probs only reports
    probabilities, it never changes token selection.
    """
    messages: list[dict] = []
    if context:
        # Qwen3-ASR reads the system message as recognition context
        # (hotword/entity biasing) — the same slot the torch path used.
        messages.append({"role": "system", "content": str(context)})
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "input_audio",
                    "input_audio": {"data": audio_b64, "format": "wav"},
                }
            ],
        }
    )
    if language:
        # Assistant prefill: forces the decode language AND the native
        # "language X<asr_text>" response format.
        messages.append(
            {"role": "assistant", "content": f"language {language}<asr_text>"}
        )
    payload = {
        # Strict greedy decode, pinned per request (see module docstring):
        # top_k=1 keeps only the argmax token regardless of the server's
        # sampler-chain defaults, and the penalty pins keep the logits
        # unmutated even if a future server/GGUF default enables them.
        "temperature": 0,
        "top_k": 1,
        "repeat_penalty": 1.0,
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "max_tokens": int(max_tokens),
        "messages": messages,
    }
    if logprobs:
        payload["logprobs"] = True
        payload["top_logprobs"] = 1
    return payload


class LlamaServerModel:
    """
    llama-server (llama.cpp CUDA, Qwen3-ASR GGUF) backend shaped like the
    qwen-asr model. The server process lifecycle is owned by
    ``LlamaCppQwen3Engine`` — this object only speaks HTTP to it.

    Args:
        port: local llama-server port (bound to 127.0.0.1).
        max_tokens: decoder budget per request (engine passes
            config.max_new_tokens through).
        timeout_base_s: minimum wall-clock budget per request; effective
            timeout = max(timeout_base_s, 2 + 0.5 * audio_seconds).
    """

    def __init__(
        self,
        port: int,
        *,
        max_tokens: int = 1024,
        timeout_base_s: float = 8.0,
        host: str = "127.0.0.1",
        logprobs: bool | None = None,
    ) -> None:
        self._url = f"http://{host}:{int(port)}/v1/chat/completions"
        self._max_tokens = max(64, int(max_tokens or 1024))
        self._timeout_base_s = max(1.0, float(timeout_base_s or 8.0))
        # Last request-level failure ("" = last request succeeded). transcribe
        # never raises, so callers that must distinguish "silence decoded to
        # empty" from "request failed" (e.g. the engine's warmup observability)
        # read this after the call.
        self.last_error: str = ""
        # Per-token logprob telemetry for the decode-confidence gate
        # (H2 defense, cluster_20260720/hallucination). None = default from
        # env/config bridge. Sticky at runtime: an HTTP 400 while logprobs
        # are in the payload flips this off for the server's lifetime so an
        # older build can never break the main transcription path.
        self._logprobs_enabled: bool = (
            logprobs_enabled_default() if logprobs is None else bool(logprobs)
        )
        # Stats of the LAST completed request ({"avg_logprob", "min_logprob",
        # "tokens"}) or None when unavailable. Read by the app worker right
        # after transcribe(); engine-internal retries overwrite it, so the
        # caller must snapshot it before triggering any retry.
        self.last_confidence: dict | None = None

    def effective_timeout_s(self, audio_duration_s: float) -> float:
        """Wall-clock budget scaled with the audio (cloud_rescue pattern,
        smaller base: the server is local, no upload latency)."""
        return max(self._timeout_base_s, 2.0 + 0.5 * float(audio_duration_s))

    def transcribe(self, audio, context=None, language=None):
        """
        Mimics qwen-asr's model.transcribe(). Returns a list with one _Result.

        ``context`` (Aria's per-utterance ``full_context`` = hotword list +
        ``recent_context``) goes into the chat SYSTEM message — Qwen3-ASR's
        native biasing slot. ``context=None`` (e.g. the engine's
        retry-without-context path) sends no system message.

        Never raises on request failure: returns an empty-text result and
        lets the app's failure chain handle recovery (see module docstring).
        """
        import httpx

        # Accept (array, sample_rate) tuple (what the wrapper passes) or a bare array.
        if isinstance(audio, tuple):
            samples, sr = audio
        else:
            samples, sr = audio, 16000
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim > 1:
            samples = samples[:, 0]
        self.last_confidence = None
        if samples.size == 0:
            self.last_error = ""
            return [_Result(text="", language=language or "")]

        try:
            audio_b64 = encode_wav_base64(samples, sample_rate=int(sr))
        except Exception as exc:
            self.last_error = f"wav_encode: {exc}"
            logger.warning(f"llamacpp ASR wav encode failed: {exc}")
            return [_Result(text="", language=language or "")]

        audio_duration_s = samples.size / float(sr)
        timeout_s = self.effective_timeout_s(audio_duration_s)

        started = time.time()
        # Two attempts at most: when the first response is a 400 WITH
        # logprobs in the payload, assume the server build rejects the
        # parameter, sticky-disable it and retry once without. Every other
        # failure keeps the single-attempt semantics.
        for attempt in (1, 2):
            with_logprobs = self._logprobs_enabled
            payload = build_chat_payload(
                audio_b64=audio_b64,
                context=str(context) if context else None,
                language=str(language) if language else None,
                max_tokens=self._max_tokens,
                logprobs=with_logprobs,
            )
            try:
                response = httpx.post(self._url, json=payload, timeout=timeout_s)
            except httpx.TimeoutException:
                self.last_error = f"timeout after {timeout_s:.0f}s"
                logger.warning(
                    f"llamacpp ASR request timed out after {timeout_s:.0f}s "
                    f"({audio_duration_s:.1f}s audio)"
                )
                return [_Result(text="", language=language or "")]
            except Exception as exc:
                # ConnectionError here usually means the server died; the app's
                # consecutive-failure self-heal will rebuild the engine (= restart
                # the server). Do not raise.
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    f"llamacpp ASR request failed: {type(exc).__name__}: {exc}"
                )
                return [_Result(text="", language=language or "")]

            if response.status_code == 400 and with_logprobs and attempt == 1:
                self._logprobs_enabled = False
                logger.warning(
                    "llamacpp ASR HTTP 400 with logprobs requested — "
                    "disabling logprobs for this server and retrying once"
                )
                continue

            if response.status_code != 200:
                preview = ""
                try:
                    preview = response.text[:200]
                except Exception:
                    pass
                self.last_error = f"HTTP {response.status_code}: {preview}"
                logger.warning(
                    f"llamacpp ASR HTTP {response.status_code}: {preview}"
                )
                return [_Result(text="", language=language or "")]

            try:
                data = response.json()
                raw = data["choices"][0]["message"]["content"] or ""
            except Exception as exc:
                self.last_error = f"bad_response: {exc}"
                logger.warning(f"llamacpp ASR bad response: {exc}")
                return [_Result(text="", language=language or "")]

            self.last_error = ""
            if with_logprobs:
                self.last_confidence = extract_confidence(data)
            text, detected_language = parse_asr_content(raw, str(language or ""))
            avg_logprob = (
                self.last_confidence.get("avg_logprob")
                if self.last_confidence
                else None
            )
            logger.debug(
                f"llamacpp ASR ok: {len(text)} chars in "
                f"{(time.time() - started) * 1000:.0f}ms"
            )
            return [_Result(
                text=text,
                language=detected_language,
                avg_logprob=avg_logprob,
            )]

        # Unreachable (attempt 2 always returns above); keep a safe default.
        self.last_error = "logprobs retry fell through"
        return [_Result(text="", language=language or "")]
