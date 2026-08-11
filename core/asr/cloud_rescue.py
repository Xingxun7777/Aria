"""
Cloud second-pass ASR via DashScope Qwen3-ASR-Flash (OpenAI-compatible mode).

Used only as a rescue path when the local engine times out / returns empty on
a committed segment.  Request shape per Alibaba Model Studio docs
(help.aliyun.com/zh/model-studio/qwen-asr-api-reference):

    POST {api_url}/chat/completions
    messages[0] (optional system): context text for recognition biasing
    messages[-1] (user): content=[{"type": "input_audio",
                                   "input_audio": {"data": "data:audio/wav;base64,..."}}]
    asr_options: non-standard top-level field (OpenAI SDKs pass it via
                 extra_body; we POST raw JSON with httpx so it just inlines).

Cost is logged through core.utils.cost_tracker with call_type="asr_rescue".
"""

from __future__ import annotations

import base64
import io
import logging
import struct
import time

logger = logging.getLogger(__name__)


def encode_wav_base64(audio, sample_rate: int = 16000) -> str:
    """float32 mono numpy array -> base64 of a 16-bit PCM WAV file."""
    import numpy as np

    arr = np.asarray(audio, dtype=np.float32)
    pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()

    buf = io.BytesIO()
    data_size = len(pcm)
    byte_rate = sample_rate * 2
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + data_size))
    buf.write(b"WAVEfmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16))
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(pcm)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def build_request_payload(
    *,
    audio_b64: str,
    model: str,
    context: str = "",
) -> dict:
    """Assemble the chat/completions JSON body (separate for unit testing)."""
    messages: list[dict] = []
    if context:
        # Qwen3-ASR-Flash reads the system message as recognition context
        # (hotword/entity biasing), not as a traditional system prompt.
        messages.append({"role": "system", "content": context[:2000]})
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "input_audio",
                    "input_audio": {"data": f"data:audio/wav;base64,{audio_b64}"},
                }
            ],
        }
    )
    return {
        "model": model,
        "messages": messages,
        "stream": False,
        "asr_options": {"enable_itn": False},
    }


def transcribe_via_dashscope(
    audio,
    *,
    api_key: str,
    model: str,
    timeout_s: float,
    api_url: str,
    context: str = "",
    sample_rate: int = 16000,
) -> tuple[str, str]:
    """One synchronous cloud transcription. Returns (text, error).

    Never raises: (text, "") on success, ("", reason) on any failure.
    """
    import httpx

    try:
        audio_b64 = encode_wav_base64(audio, sample_rate=sample_rate)
    except Exception as exc:
        return "", f"wav_encode: {exc}"

    payload = build_request_payload(
        audio_b64=audio_b64, model=model, context=context
    )
    url = api_url.rstrip("/") + "/chat/completions"

    # Scale the wall-clock budget with the audio: a 60s segment is ~2.6MB of
    # base64 to upload plus server-side decode, which a flat 15s cannot cover.
    try:
        audio_duration_s = len(audio) / float(sample_rate)
    except Exception:
        audio_duration_s = 0.0
    effective_timeout_s = max(float(timeout_s), 10.0 + 0.5 * audio_duration_s)
    started = time.time()

    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=effective_timeout_s,
        )
    except httpx.TimeoutException:
        return "", f"timeout after {effective_timeout_s:.0f}s"
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"

    latency_ms = (time.time() - started) * 1000
    if response.status_code != 200:
        body_preview = ""
        try:
            body_preview = response.text[:200]
        except Exception:
            pass
        return "", f"HTTP {response.status_code}: {body_preview}"

    try:
        data = response.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        return "", f"bad_response: {exc}"

    try:
        from ..utils.cost_tracker import safe_record

        safe_record(
            "asr_rescue",
            model,
            data,
            latency_ms=latency_ms,
            output_chars=len(text),
            extra={"audio_s": round(len(audio) / float(sample_rate), 2)},
        )
    except Exception:
        pass

    logger.info(
        f"Cloud ASR rescue ok: {len(text)} chars in {latency_ms:.0f}ms ({model})"
    )
    return text, ""
