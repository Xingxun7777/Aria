"""
Single AI Gateway
=================
Unified OpenAI-compatible chat/completions transport for Aria LLM call sites.

Wave G1 consumers: selection, Qt workers, auto_hotword reviewer, settings API test.
Wave G2 will migrate AIPolisher onto the same primitives.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Generator, Iterator, Optional

import httpx

from ..debug import DEBUG_DIR, append_log_line
from ..utils.secrets import reveal_secret

logger = logging.getLogger("aria.ai.gateway")

AI_GATEWAY_LOG = DEBUG_DIR / "ai_gateway.log"

_DETAIL_MAX = 200
_VERSIONED_API_TAIL = re.compile(r"/v\d+$")
_VERSIONED_API_SEGMENT = re.compile(r"/v\d+/")


class AIErrorCategory(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTH = "AUTH"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    CONNECT = "CONNECT"
    PROTOCOL = "PROTOCOL"
    EMPTY = "EMPTY"
    CANCELLED = "CANCELLED"


@dataclass
class AIRequestSpec:
    api_url: str
    api_key: str
    model: str
    messages: list[dict]
    timeout_s: float
    purpose: str
    trace_id: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    response_format: dict | None = None
    extra_payload: dict | None = None
    extra_headers: dict | None = None
    # G2 additive: cost / empty-content controls (defaults preserve G1 behavior).
    record_cost: bool = True
    cost_extra: dict | None = None
    allow_empty: bool = False


@dataclass
class AIResult:
    ok: bool
    text: str = ""
    error: AIErrorCategory | None = None
    status_code: int | None = None
    detail: str = ""
    elapsed_ms: int = 0
    # Internal bookkeeping for cost_tracker; never logged.
    _response_json: dict = field(default_factory=dict, repr=False, compare=False)


def targets_deepseek(model: str, api_url: str) -> bool:
    """True if the request is going to DeepSeek (safe to send `thinking` field).

    Other OpenAI-compatible endpoints may reject unknown request fields with
    HTTP 400, so we only attach the field when the target is DeepSeek or a
    DeepSeek model through a proxy (e.g. OpenRouter).
    """
    m = (model or "").lower()
    u = (api_url or "").lower()
    return "deepseek" in m or "deepseek.com" in u


def build_chat_completions_url(api_url: str) -> str:
    """Single URL join rule (absorbs settings.py /vN variants)."""
    base = (api_url or "").rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    if _VERSIONED_API_TAIL.search(base) or _VERSIONED_API_SEGMENT.search(base + "/"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _sanitize_detail(text: str, *, redacted_key: str = "") -> str:
    """Produce a short, non-sensitive error detail for UI/logs.

    Rules (in order):
    1. If body is JSON with ``error.message`` / ``error`` (str) / ``message``,
       keep only that field — API bodies often echo the user prompt elsewhere.
    2. Otherwise keep only the first line (drop everything after the first
       newline) so multi-line dumps cannot leak prompt fragments.
    3. Redact key-like tokens, then truncate to ≤200 chars.
    """
    raw = str(text or "")
    if not raw.strip():
        return ""

    extracted: str | None = None
    try:
        data = json.loads(raw.strip())
    except (ValueError, TypeError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if msg is not None and str(msg).strip():
                extracted = str(msg)
        elif isinstance(err, str) and err.strip():
            extracted = err
        elif data.get("message") is not None and str(data.get("message")).strip():
            extracted = str(data.get("message"))

    if extracted is not None:
        raw = extracted
    else:
        # First line only — discard remainder of multi-line bodies.
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
        raw = normalized.split("\n", 1)[0]

    raw = raw.strip()
    if not raw:
        return ""
    if redacted_key:
        raw = raw.replace(redacted_key, "<redacted>")
    raw = re.sub(r"(?i)(authorization\s*[:=]\s*)(\S+)", r"\1<redacted>", raw)
    raw = re.sub(r"(?i)(bearer\s+)(\S+)", r"\1<redacted>", raw)
    raw = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)(\S+)", r"\1<redacted>", raw)
    if len(raw) > _DETAIL_MAX:
        raw = raw[:_DETAIL_MAX]
    return raw


def _classify_http_status(status_code: int) -> AIErrorCategory:
    if status_code in (401, 403):
        return AIErrorCategory.AUTH
    if status_code == 429:
        return AIErrorCategory.RATE_LIMITED
    if 500 <= status_code <= 599:
        return AIErrorCategory.SERVER_ERROR
    return AIErrorCategory.PROTOCOL


def _classify_transport(exc: BaseException) -> AIErrorCategory:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return AIErrorCategory.CONNECT
    if isinstance(exc, httpx.TimeoutException):
        return AIErrorCategory.TIMEOUT
    return AIErrorCategory.CONNECT


def _input_chars(messages: list[dict]) -> int:
    total = 0
    for msg in messages or []:
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
    return total


def _extract_text(data: dict) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise KeyError("choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise KeyError("message")
    content = message.get("content")
    if content is None:
        return ""
    return str(content).strip()


def _record_cost(
    *,
    purpose: str,
    model: str,
    response_json: dict,
    latency_ms: float,
    input_chars: int,
    output_chars: int,
    extra: dict | None = None,
) -> None:
    try:
        from ..utils.cost_tracker import safe_record

        safe_record(
            call_type=purpose,
            model=model,
            response_json=response_json or {},
            latency_ms=latency_ms,
            input_chars=input_chars,
            output_chars=output_chars,
            extra=extra,
        )
    except Exception:
        pass


@contextlib.contextmanager
def _client_scope(
    client: Optional[httpx.Client], timeout_s: float
) -> Iterator[httpx.Client]:
    """Use a caller-owned client when provided; otherwise open a short-lived one."""
    if client is not None:
        yield client
        return
    with httpx.Client(timeout=timeout_s) as owned:
        yield owned


def _abort_requested(
    *,
    deadline_s: float | None,
    should_abort: Callable[[], bool] | None,
) -> bool:
    if should_abort is not None:
        try:
            if should_abort():
                return True
        except Exception:
            pass
    if deadline_s is not None and time.time() > float(deadline_s):
        return True
    return False


def _log_line(
    *,
    purpose: str,
    trace_id: str | None,
    status: str,
    elapsed_ms: int,
    status_code: int | None = None,
) -> None:
    code = "-" if status_code is None else str(status_code)
    trace = "-" if not trace_id else str(trace_id)
    purpose_s = purpose or "-"
    logger.info(
        "purpose=%s|trace=%s|status=%s|http=%s|elapsed=%sms",
        purpose_s,
        trace,
        status,
        code,
        elapsed_ms,
    )
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    append_log_line(
        AI_GATEWAY_LOG,
        f"[{ts}] purpose={purpose_s}|trace={trace}|status={status}|http={code}|elapsed={elapsed_ms}ms",
    )


def _validate_spec(spec: AIRequestSpec) -> AIResult | None:
    if not (spec.api_url or "").strip() or not (spec.api_key or "").strip() or not (
        spec.model or ""
    ).strip():
        return AIResult(
            ok=False,
            error=AIErrorCategory.NOT_CONFIGURED,
            detail="missing api_url, api_key, or model",
        )
    if not spec.messages:
        return AIResult(
            ok=False,
            error=AIErrorCategory.NOT_CONFIGURED,
            detail="missing messages",
        )
    return None


def _build_headers(spec: AIRequestSpec, plain_key: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {plain_key}",
    }
    if spec.extra_headers:
        for key, value in spec.extra_headers.items():
            if key and value is not None:
                headers[str(key)] = str(value)
    return headers


def _build_payload(spec: AIRequestSpec, *, stream: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": spec.model,
        "messages": spec.messages,
    }
    if spec.max_tokens is not None:
        payload["max_tokens"] = spec.max_tokens
    if spec.temperature is not None:
        payload["temperature"] = spec.temperature
    if spec.response_format is not None:
        payload["response_format"] = spec.response_format
    if stream:
        payload["stream"] = True
    if spec.extra_payload:
        for key, value in spec.extra_payload.items():
            if key in {"model", "messages", "Authorization"}:
                continue
            payload[key] = value
    # DeepSeek V4 defaults thinking=enabled; Aria chat/edit paths never need CoT.
    if targets_deepseek(spec.model, spec.api_url):
        payload["thinking"] = {"type": "disabled"}
    return payload


def chat(
    spec: AIRequestSpec,
    *,
    client: Optional[httpx.Client] = None,
) -> AIResult:
    """Non-streaming chat/completions call.

    Optional ``client`` (G2): reuse a caller-owned httpx client (e.g. AIPolisher
    connection pool / unit-test mocks). When omitted, opens a short-lived client.
    """
    started = time.time()
    bad = _validate_spec(spec)
    if bad is not None:
        bad.elapsed_ms = int((time.time() - started) * 1000)
        _log_line(
            purpose=spec.purpose,
            trace_id=spec.trace_id,
            status=bad.error.value if bad.error else "NOT_CONFIGURED",
            elapsed_ms=bad.elapsed_ms,
        )
        return bad

    plain_key = reveal_secret(spec.api_key) or ""
    if not plain_key.strip():
        result = AIResult(
            ok=False,
            error=AIErrorCategory.NOT_CONFIGURED,
            detail="api_key unavailable",
            elapsed_ms=int((time.time() - started) * 1000),
        )
        _log_line(
            purpose=spec.purpose,
            trace_id=spec.trace_id,
            status=result.error.value,
            elapsed_ms=result.elapsed_ms,
        )
        return result

    full_url = build_chat_completions_url(spec.api_url)
    headers = _build_headers(spec, plain_key)
    payload = _build_payload(spec, stream=False)
    in_chars = _input_chars(spec.messages)

    try:
        with _client_scope(client, spec.timeout_s) as http_client:
            response = http_client.post(full_url, headers=headers, json=payload)
        elapsed_ms = int((time.time() - started) * 1000)

        if response.status_code != 200:
            category = _classify_http_status(response.status_code)
            detail = _sanitize_detail(response.text, redacted_key=plain_key)
            result = AIResult(
                ok=False,
                error=category,
                status_code=response.status_code,
                detail=detail,
                elapsed_ms=elapsed_ms,
            )
            _log_line(
                purpose=spec.purpose,
                trace_id=spec.trace_id,
                status=category.value,
                elapsed_ms=elapsed_ms,
                status_code=response.status_code,
            )
            return result

        try:
            data = response.json()
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            result = AIResult(
                ok=False,
                error=AIErrorCategory.PROTOCOL,
                status_code=response.status_code,
                detail=_sanitize_detail(f"bad_response:{type(exc).__name__}"),
                elapsed_ms=elapsed_ms,
            )
            _log_line(
                purpose=spec.purpose,
                trace_id=spec.trace_id,
                status=AIErrorCategory.PROTOCOL.value,
                elapsed_ms=elapsed_ms,
                status_code=response.status_code,
            )
            return result

        try:
            text = _extract_text(data) if isinstance(data, dict) else ""
        except (ValueError, TypeError, KeyError) as exc:
            # ``allow_empty`` only relaxes the assistant content itself.  It
            # must never turn an arbitrary HTTP-200 JSON object into proof that
            # the configured model/key can complete an OpenAI-compatible chat
            # request (the guided DeepSeek setup persists the key after this).
            result = AIResult(
                ok=False,
                error=AIErrorCategory.PROTOCOL,
                status_code=response.status_code,
                detail=_sanitize_detail(f"bad_response:{type(exc).__name__}"),
                elapsed_ms=elapsed_ms,
            )
            _log_line(
                purpose=spec.purpose,
                trace_id=spec.trace_id,
                status=AIErrorCategory.PROTOCOL.value,
                elapsed_ms=elapsed_ms,
                status_code=response.status_code,
            )
            return result

        if not text:
            if not spec.allow_empty:
                result = AIResult(
                    ok=False,
                    error=AIErrorCategory.EMPTY,
                    status_code=response.status_code,
                    detail="empty content",
                    elapsed_ms=elapsed_ms,
                    _response_json=data if isinstance(data, dict) else {},
                )
                _log_line(
                    purpose=spec.purpose,
                    trace_id=spec.trace_id,
                    status=AIErrorCategory.EMPTY.value,
                    elapsed_ms=elapsed_ms,
                    status_code=response.status_code,
                )
                return result
            # allow_empty (e.g. polish prewarm max_tokens=1): treat as success.
            if spec.record_cost:
                _record_cost(
                    purpose=spec.purpose,
                    model=spec.model,
                    response_json=data if isinstance(data, dict) else {},
                    latency_ms=elapsed_ms,
                    input_chars=in_chars,
                    output_chars=0,
                    extra=spec.cost_extra,
                )
            result = AIResult(
                ok=True,
                text="",
                status_code=response.status_code,
                elapsed_ms=elapsed_ms,
                detail="empty_allowed",
                _response_json=data if isinstance(data, dict) else {},
            )
            _log_line(
                purpose=spec.purpose,
                trace_id=spec.trace_id,
                status="OK",
                elapsed_ms=elapsed_ms,
                status_code=response.status_code,
            )
            return result

        if spec.record_cost:
            _record_cost(
                purpose=spec.purpose,
                model=spec.model,
                response_json=data if isinstance(data, dict) else {},
                latency_ms=elapsed_ms,
                input_chars=in_chars,
                output_chars=len(text),
                extra=spec.cost_extra,
            )
        result = AIResult(
            ok=True,
            text=text,
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
            _response_json=data if isinstance(data, dict) else {},
        )
        _log_line(
            purpose=spec.purpose,
            trace_id=spec.trace_id,
            status="OK",
            elapsed_ms=elapsed_ms,
            status_code=response.status_code,
        )
        return result

    except Exception as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        category = _classify_transport(exc)
        result = AIResult(
            ok=False,
            error=category,
            detail=_sanitize_detail(f"{type(exc).__name__}"),
            elapsed_ms=elapsed_ms,
        )
        _log_line(
            purpose=spec.purpose,
            trace_id=spec.trace_id,
            status=category.value,
            elapsed_ms=elapsed_ms,
        )
        return result


def iter_stream(
    spec: AIRequestSpec,
    *,
    client: Optional[httpx.Client] = None,
    deadline_s: float | None = None,
    should_abort: Callable[[], bool] | None = None,
) -> Generator[str, None, AIResult]:
    """SSE streaming generator (G2).

    Yields *incremental* content deltas. The generator's return value (via
    ``StopIteration.value``) is the final ``AIResult``.

    Optional ``deadline_s`` / ``should_abort`` implement hard walls without
    changing default ``stream()`` behavior when left unset.
    """
    started = time.time()
    bad = _validate_spec(spec)
    if bad is not None:
        bad.elapsed_ms = int((time.time() - started) * 1000)
        _log_line(
            purpose=spec.purpose,
            trace_id=spec.trace_id,
            status=bad.error.value if bad.error else "NOT_CONFIGURED",
            elapsed_ms=bad.elapsed_ms,
        )
        return bad

    plain_key = reveal_secret(spec.api_key) or ""
    if not plain_key.strip():
        result = AIResult(
            ok=False,
            error=AIErrorCategory.NOT_CONFIGURED,
            detail="api_key unavailable",
            elapsed_ms=int((time.time() - started) * 1000),
        )
        _log_line(
            purpose=spec.purpose,
            trace_id=spec.trace_id,
            status=result.error.value,
            elapsed_ms=result.elapsed_ms,
        )
        return result

    full_url = build_chat_completions_url(spec.api_url)
    headers = _build_headers(spec, plain_key)
    payload = _build_payload(spec, stream=True)
    in_chars = _input_chars(spec.messages)
    full_content = ""
    stream_usage: dict = {}
    saw_done = False
    aborted_early = False
    truncated_by_deadline = False

    try:
        with _client_scope(client, spec.timeout_s) as http_client:
            with http_client.stream(
                "POST", full_url, headers=headers, json=payload
            ) as response:
                if response.status_code != 200:
                    body = response.read().decode("utf-8", errors="ignore")
                    elapsed_ms = int((time.time() - started) * 1000)
                    category = _classify_http_status(response.status_code)
                    result = AIResult(
                        ok=False,
                        error=category,
                        status_code=response.status_code,
                        detail=_sanitize_detail(body, redacted_key=plain_key),
                        elapsed_ms=elapsed_ms,
                    )
                    _log_line(
                        purpose=spec.purpose,
                        trace_id=spec.trace_id,
                        status=category.value,
                        elapsed_ms=elapsed_ms,
                        status_code=response.status_code,
                    )
                    return result

                for line in response.iter_lines():
                    if _abort_requested(
                        deadline_s=deadline_s, should_abort=should_abort
                    ):
                        if not full_content:
                            aborted_early = True
                        else:
                            truncated_by_deadline = True
                        break
                    if not line:
                        continue
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        saw_done = True
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict) and isinstance(data.get("usage"), dict):
                        stream_usage = data["usage"]
                    delta = (
                        data.get("choices", [{}])[0].get("delta", {})
                        if isinstance(data, dict)
                        else {}
                    )
                    content = delta.get("content", "") if isinstance(delta, dict) else ""
                    if content:
                        full_content += content
                        yield content

        elapsed_ms = int((time.time() - started) * 1000)
        if aborted_early and not full_content:
            result = AIResult(
                ok=False,
                error=AIErrorCategory.TIMEOUT,
                status_code=200,
                detail="deadline/abort before first content",
                elapsed_ms=elapsed_ms,
            )
            _log_line(
                purpose=spec.purpose,
                trace_id=spec.trace_id,
                status=AIErrorCategory.TIMEOUT.value,
                elapsed_ms=elapsed_ms,
                status_code=200,
            )
            return result

        if not full_content:
            category = (
                AIErrorCategory.EMPTY if saw_done else AIErrorCategory.CONNECT
            )
            result = AIResult(
                ok=False,
                error=category,
                status_code=200,
                detail="empty stream content" if saw_done else "stream interrupted",
                elapsed_ms=elapsed_ms,
            )
            _log_line(
                purpose=spec.purpose,
                trace_id=spec.trace_id,
                status=category.value,
                elapsed_ms=elapsed_ms,
                status_code=200,
            )
            return result

        if truncated_by_deadline:
            detail = "deadline_truncated"
        elif saw_done:
            detail = ""
        else:
            detail = "stream_incomplete"
        if spec.record_cost:
            _record_cost(
                purpose=spec.purpose,
                model=spec.model,
                response_json={"usage": stream_usage} if stream_usage else {},
                latency_ms=elapsed_ms,
                input_chars=in_chars,
                output_chars=len(full_content),
                extra=spec.cost_extra,
            )
        result = AIResult(
            ok=True,
            text=full_content,
            status_code=200,
            detail=detail,
            elapsed_ms=elapsed_ms,
            _response_json={"usage": stream_usage} if stream_usage else {},
        )
        _log_line(
            purpose=spec.purpose,
            trace_id=spec.trace_id,
            status="OK" if saw_done else "OK_PARTIAL",
            elapsed_ms=elapsed_ms,
            status_code=200,
        )
        return result

    except Exception as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        if full_content:
            # Preserve G1: mid-stream failure after tokens → CONNECT + partial text.
            result = AIResult(
                ok=False,
                text=full_content,
                error=AIErrorCategory.CONNECT,
                detail=_sanitize_detail(
                    f"stream_interrupted:{type(exc).__name__}"
                ),
                elapsed_ms=elapsed_ms,
            )
            _log_line(
                purpose=spec.purpose,
                trace_id=spec.trace_id,
                status=AIErrorCategory.CONNECT.value,
                elapsed_ms=elapsed_ms,
            )
            return result
        category = _classify_transport(exc)
        result = AIResult(
            ok=False,
            error=category,
            detail=_sanitize_detail(f"{type(exc).__name__}"),
            elapsed_ms=elapsed_ms,
        )
        _log_line(
            purpose=spec.purpose,
            trace_id=spec.trace_id,
            status=category.value,
            elapsed_ms=elapsed_ms,
        )
        return result


def stream(
    spec: AIRequestSpec,
    on_delta: Callable[[str], None],
    *,
    client: Optional[httpx.Client] = None,
    deadline_s: float | None = None,
    should_abort: Callable[[], bool] | None = None,
    on_chunk: Callable[[str], None] | None = None,
) -> AIResult:
    """SSE streaming chat/completions call.

    ``on_delta`` receives the cumulative assistant text after each content delta.
    G2 optional kwargs default to prior behavior when omitted.
    """
    cumulative = ""
    gen = iter_stream(
        spec,
        client=client,
        deadline_s=deadline_s,
        should_abort=should_abort,
    )
    try:
        while True:
            chunk = next(gen)
            cumulative += chunk
            if on_chunk is not None:
                try:
                    on_chunk(chunk)
                except Exception:
                    pass
            try:
                on_delta(cumulative)
            except Exception:
                pass
    except StopIteration as stop:
        result = stop.value
        if isinstance(result, AIResult):
            return result
        return AIResult(
            ok=bool(cumulative),
            text=cumulative,
            error=None if cumulative else AIErrorCategory.EMPTY,
            detail="stream_missing_result",
        )


# Friendly alias matching polish.py naming for G2 reuse.
def _targets_deepseek(model: str, api_url: str) -> bool:
    return targets_deepseek(model, api_url)
