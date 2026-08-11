"""LLM call cost tracker — token usage + estimated cost in CNY.

Records every LLM API call (Polish / Auto-hotword review / Selection commands
/ etc) into a per-day JSONL file under `data/cost/`. Cost estimation uses
DeepSeek pricing from api-docs.deepseek.com.

The tracker is the *source of truth* for "how much did Aria's LLM stack
actually cost today".  It deliberately does NOT use any third-party metering
service: every byte of usage stays on disk in plain JSONL so the user can
audit / replot / re-price retroactively.
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
from pathlib import Path
from typing import Optional


# DeepSeek pricing snapshot — see https://api-docs.deepseek.com/quick_start/pricing
# Values are USD per 1M tokens. Update this dict when DeepSeek revises prices.
_USD_TO_CNY = 7.2

_PRICING_USD_PER_M = {
    # v4-flash and the legacy aliases that currently route to it
    "deepseek-v4-flash": {"hit": 0.0028, "miss": 0.14, "output": 0.28},
    "deepseek-chat": {"hit": 0.0028, "miss": 0.14, "output": 0.28},
    "deepseek-reasoner": {"hit": 0.0028, "miss": 0.14, "output": 0.28},
    "deepseek-v4-pro": {"hit": 0.0145, "miss": 1.74, "output": 3.48},
}

_DEFAULT_PRICE = _PRICING_USD_PER_M["deepseek-v4-flash"]


def _resolve_price(model: str) -> dict:
    """Lookup pricing for a model name, defaulting to v4-flash if unknown.

    Match is case-insensitive and prefix-greedy: 'deepseek-v4-pro-thinking'
    matches 'deepseek-v4-pro'.
    """
    if not model:
        return _DEFAULT_PRICE
    m = model.strip().lower()
    if m in _PRICING_USD_PER_M:
        return _PRICING_USD_PER_M[m]
    # Prefix match (longest-first so v4-pro beats v4 when both registered)
    for key in sorted(_PRICING_USD_PER_M.keys(), key=len, reverse=True):
        if m.startswith(key):
            return _PRICING_USD_PER_M[key]
    return _DEFAULT_PRICE


class CostTracker:
    """Append-only daily JSONL cost log + on-demand aggregation.

    Singleton — get the live instance via `CostTracker.get_instance()`.
    """

    _instance: Optional["CostTracker"] = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls, base_dir: Optional[Path] = None) -> "CostTracker":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(base_dir=base_dir)
            return cls._instance

    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is None:
            try:
                from .paths import get_base_path

                base_dir = get_base_path() / "data" / "cost"
            except Exception:
                base_dir = Path("data") / "cost"
        self._base = Path(base_dir)
        try:
            self._base.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._write_lock = threading.Lock()
        self._cleanup_old()

    # ------------------------------------------------------------------ I/O

    def record(
        self,
        call_type: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        latency_ms: float = 0.0,
        input_chars: int = 0,
        output_chars: int = 0,
        extra: Optional[dict] = None,
    ) -> dict:
        """Persist one LLM call to today's JSONL log.

        Args:
            call_type: "polish" / "polish_stream" / "auto_hotword" /
                "translate_popup" / "summarize_popup" / "ask_ai" / etc.
            model: model identifier as sent in the API payload.
            prompt_tokens: from API `usage.prompt_tokens`.
            completion_tokens: from API `usage.completion_tokens`.
            cached_tokens: from DeepSeek's
                `usage.prompt_cache_hit_tokens` (0 when not provided).
            latency_ms: wall-clock end-to-end call duration.
            input_chars: char count of system+user message (for sanity).
            output_chars: char count of assistant output.
            extra: any call-specific debugging info to keep around.

        Returns:
            The persisted JSONL row as a dict, including computed cost.
        """
        prompt_tokens = max(0, int(prompt_tokens or 0))
        completion_tokens = max(0, int(completion_tokens or 0))
        cached_tokens = max(0, int(cached_tokens or 0))
        if cached_tokens > prompt_tokens:
            cached_tokens = prompt_tokens
        miss_tokens = prompt_tokens - cached_tokens

        price = _resolve_price(model)
        cost_usd = (
            cached_tokens / 1_000_000.0 * price["hit"]
            + miss_tokens / 1_000_000.0 * price["miss"]
            + completion_tokens / 1_000_000.0 * price["output"]
        )
        cost_cny = cost_usd * _USD_TO_CNY

        entry: dict = {
            "ts": _dt.datetime.now().isoformat(timespec="milliseconds"),
            "call_type": call_type,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_tokens": cached_tokens,
            "miss_tokens": miss_tokens,
            "latency_ms": round(latency_ms, 1),
            "input_chars": int(input_chars or 0),
            "output_chars": int(output_chars or 0),
            "cost_usd": round(cost_usd, 6),
            "cost_cny": round(cost_cny, 6),
        }
        if extra:
            try:
                # Defensive copy so callers can't mutate it after recording
                entry["extra"] = json.loads(json.dumps(extra, ensure_ascii=False))
            except Exception:
                entry["extra"] = {"_unserializable": str(extra)[:200]}

        today = _dt.date.today().isoformat()
        fp = self._base / f"cost_{today}.jsonl"
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._write_lock:
            try:
                fp.parent.mkdir(parents=True, exist_ok=True)
                with open(fp, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                # Disk full / permission denied / read-only fs → never raise
                # back into the polish/review hot path.
                pass
        return entry

    # ------------------------------------------------------------ aggregate

    def aggregate(
        self,
        start_date: Optional[_dt.date] = None,
        end_date: Optional[_dt.date] = None,
    ) -> dict:
        """Read the per-day JSONLs in [start, end] and bucket totals.

        Default range = last 30 days ending today (inclusive).
        """
        if end_date is None:
            end_date = _dt.date.today()
        if start_date is None:
            start_date = end_date - _dt.timedelta(days=30)

        by_type: dict[str, dict] = {}
        by_day: dict[str, dict] = {}
        total = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "miss_tokens": 0,
            "cost_cny": 0.0,
        }

        d = start_date
        while d <= end_date:
            fp = self._base / f"cost_{d.isoformat()}.jsonl"
            day_total = {"calls": 0, "cost_cny": 0.0}
            if fp.exists():
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                e = json.loads(line)
                            except Exception:
                                continue
                            ct = e.get("call_type", "?")
                            tier = by_type.setdefault(
                                ct,
                                {
                                    "calls": 0,
                                    "prompt_tokens": 0,
                                    "completion_tokens": 0,
                                    "cached_tokens": 0,
                                    "miss_tokens": 0,
                                    "cost_cny": 0.0,
                                },
                            )
                            tier["calls"] += 1
                            for k in (
                                "prompt_tokens",
                                "completion_tokens",
                                "cached_tokens",
                                "miss_tokens",
                            ):
                                tier[k] += int(e.get(k, 0) or 0)
                            tier["cost_cny"] += float(e.get("cost_cny", 0.0) or 0.0)

                            day_total["calls"] += 1
                            day_total["cost_cny"] += float(
                                e.get("cost_cny", 0.0) or 0.0
                            )

                            total["calls"] += 1
                            for k in (
                                "prompt_tokens",
                                "completion_tokens",
                                "cached_tokens",
                                "miss_tokens",
                            ):
                                total[k] += int(e.get(k, 0) or 0)
                            total["cost_cny"] += float(e.get("cost_cny", 0.0) or 0.0)
                except Exception:
                    pass
            by_day[d.isoformat()] = day_total
            d += _dt.timedelta(days=1)

        return {
            "range": [start_date.isoformat(), end_date.isoformat()],
            "total": total,
            "by_type": by_type,
            "by_day": by_day,
        }

    def top_calls(
        self,
        date: Optional[_dt.date] = None,
        n: int = 10,
        sort_by: str = "cost_cny",
    ) -> list:
        """Return the N most expensive (or longest) calls of the given day."""
        if date is None:
            date = _dt.date.today()
        fp = self._base / f"cost_{date.isoformat()}.jsonl"
        if not fp.exists():
            return []
        rows: list = []
        try:
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            return []
        rows.sort(key=lambda r: float(r.get(sort_by, 0) or 0), reverse=True)
        return rows[:n]

    # ----------------------------------------------------------- maintenance

    def _cleanup_old(self, retention_days: int = 90) -> None:
        """Drop cost logs older than retention_days. Errors are swallowed."""
        try:
            cutoff = _dt.date.today() - _dt.timedelta(days=retention_days)
            for fp in self._base.glob("cost_*.jsonl"):
                try:
                    date_str = fp.stem.replace("cost_", "")
                    d = _dt.date.fromisoformat(date_str)
                    if d < cutoff:
                        fp.unlink()
                except Exception:
                    continue
        except Exception:
            pass


# ---------------------------------------------------------- helper for callers


def safe_record(
    call_type: str,
    model: str,
    response_json: dict,
    *,
    latency_ms: float = 0.0,
    input_chars: int = 0,
    output_chars: int = 0,
    extra: Optional[dict] = None,
) -> Optional[dict]:
    """Convenience wrapper: extracts usage fields from a typical OpenAI-
    compatible response and feeds them to the global tracker.

    Never raises — returns None on any failure so wiring this into hot paths
    like Polish stays safe even if the response shape is unexpected.
    """
    try:
        usage = (response_json or {}).get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        # DeepSeek-specific: prompt_cache_hit_tokens / prompt_cache_miss_tokens.
        # Other vendors may use cached_tokens / cache_read_input_tokens.
        cached_tokens = int(
            usage.get("prompt_cache_hit_tokens")
            or usage.get("cached_tokens")
            or usage.get("cache_read_input_tokens")
            or 0
        )
        return CostTracker.get_instance().record(
            call_type=call_type,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            latency_ms=latency_ms,
            input_chars=input_chars,
            output_chars=output_chars,
            extra=extra,
        )
    except Exception:
        return None
