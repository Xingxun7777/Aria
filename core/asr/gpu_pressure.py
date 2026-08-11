"""
GPU pressure detection for ASR routing.

This module intentionally depends only on the NVIDIA CLI instead of NVML so the
portable build does not need another binary/package.  Query failures are treated
as "not busy" by default: GPU fallback must never block normal dictation.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Sequence


CommandRunner = Callable[[Sequence[str], float], str]


@dataclass(frozen=True)
class GpuPressureConfig:
    """Configuration for GPU pressure sampling."""

    enabled: bool = True
    gpu_index: int = 0
    utilization_threshold: int = 85
    memory_free_mb_threshold: int = 2048
    cooldown_s: float = 30.0
    cache_ttl_s: float = 1.0
    query_timeout_s: float = 0.8

    @staticmethod
    def _as_bool(value: object, default: bool) -> bool:
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

    @staticmethod
    def _as_int(value: object, default: int, *, min_value: int, max_value: int) -> int:
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
        return max(min_value, min(max_value, parsed))

    @staticmethod
    def _as_float(
        value: object, default: float, *, min_value: float, max_value: float
    ) -> float:
        try:
            parsed = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
        return max(min_value, min(max_value, parsed))

    @classmethod
    def from_mapping(cls, data: dict | None) -> "GpuPressureConfig":
        data = data or {}
        return cls(
            enabled=cls._as_bool(data.get("enabled"), True),
            gpu_index=cls._as_int(data.get("gpu_index"), 0, min_value=0, max_value=16),
            utilization_threshold=cls._as_int(
                data.get("utilization_threshold"), 85, min_value=1, max_value=100
            ),
            memory_free_mb_threshold=cls._as_int(
                data.get("memory_free_mb_threshold"),
                2048,
                min_value=0,
                max_value=256 * 1024,
            ),
            cooldown_s=cls._as_float(
                data.get("cooldown_s"), 30.0, min_value=0.0, max_value=3600.0
            ),
            cache_ttl_s=cls._as_float(
                data.get("cache_ttl_s"), 1.0, min_value=0.0, max_value=60.0
            ),
            query_timeout_s=cls._as_float(
                data.get("query_timeout_s"), 0.8, min_value=0.05, max_value=10.0
            ),
        )


@dataclass(frozen=True)
class GpuPressureSample:
    """One GPU pressure decision."""

    busy: bool
    reason: str
    utilization_gpu: int | None = None
    memory_used_mb: int | None = None
    memory_total_mb: int | None = None
    memory_free_mb: int | None = None
    sampled_at: float = 0.0
    error: str = ""


class GpuPressureMonitor:
    """Cached NVIDIA GPU pressure sampler with cooldown hysteresis."""

    def __init__(
        self,
        config: GpuPressureConfig | None = None,
        *,
        runner: CommandRunner | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config or GpuPressureConfig()
        self._runner = runner or self._default_runner
        self._clock = clock or time.monotonic
        self._last_sample: GpuPressureSample | None = None
        self._last_busy_at: float = -1.0
        self._last_busy_reason: str = ""

    @staticmethod
    def _default_runner(cmd: Sequence[str], timeout: float) -> str:
        return subprocess.check_output(
            list(cmd),
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def sample(self, *, force: bool = False) -> GpuPressureSample:
        """Return current pressure decision. Never raises."""
        now = self._clock()
        if not self.config.enabled:
            return GpuPressureSample(False, "disabled", sampled_at=now)

        if (
            not force
            and self._last_sample is not None
            and now - self._last_sample.sampled_at <= self.config.cache_ttl_s
        ):
            return self._apply_cooldown(self._last_sample, now)

        try:
            raw = self._runner(self._build_query_command(), self.config.query_timeout_s)
            sample = self._parse_nvidia_smi(raw, now)
        except Exception as exc:
            sample = GpuPressureSample(
                busy=False,
                reason="query_failed",
                sampled_at=now,
                error=str(exc),
            )

        decided = self._apply_cooldown(sample, now)
        self._last_sample = decided
        if decided.busy and not decided.reason.startswith("cooldown"):
            self._last_busy_at = now
            self._last_busy_reason = decided.reason
        return decided

    def _build_query_command(self) -> list[str]:
        return [
            "nvidia-smi",
            "--query-gpu=index,utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ]

    def _parse_nvidia_smi(self, raw: str, now: float) -> GpuPressureSample:
        rows: list[tuple[int, int, int, int]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip().replace("%", "") for p in line.split(",")]
            if len(parts) < 4:
                continue
            try:
                rows.append((int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])))
            except ValueError:
                continue

        if not rows:
            return GpuPressureSample(False, "no_gpu_rows", sampled_at=now, error=raw[:200])

        chosen = None
        for row in rows:
            if row[0] == self.config.gpu_index:
                chosen = row
                break
        if chosen is None:
            chosen = rows[0]

        index, util, used, total = chosen
        free = max(total - used, 0)
        busy_reasons: list[str] = []
        if util >= self.config.utilization_threshold:
            busy_reasons.append(
                f"gpu{index} util {util}% >= {self.config.utilization_threshold}%"
            )
        if (
            self.config.memory_free_mb_threshold > 0
            and free <= self.config.memory_free_mb_threshold
        ):
            busy_reasons.append(
                f"gpu{index} free {free}MB <= {self.config.memory_free_mb_threshold}MB"
            )

        if busy_reasons:
            return GpuPressureSample(
                True,
                "; ".join(busy_reasons),
                utilization_gpu=util,
                memory_used_mb=used,
                memory_total_mb=total,
                memory_free_mb=free,
                sampled_at=now,
            )

        return GpuPressureSample(
            False,
            f"gpu{index} idle util {util}%, free {free}MB",
            utilization_gpu=util,
            memory_used_mb=used,
            memory_total_mb=total,
            memory_free_mb=free,
            sampled_at=now,
        )

    def _apply_cooldown(
        self, sample: GpuPressureSample, now: float
    ) -> GpuPressureSample:
        if sample.busy:
            return sample
        if (
            self.config.cooldown_s > 0
            and self._last_busy_at >= 0
            and now - self._last_busy_at < self.config.cooldown_s
        ):
            remaining = self.config.cooldown_s - (now - self._last_busy_at)
            return GpuPressureSample(
                True,
                f"cooldown {remaining:.1f}s after {self._last_busy_reason or 'gpu busy'}",
                utilization_gpu=sample.utilization_gpu,
                memory_used_mb=sample.memory_used_mb,
                memory_total_mb=sample.memory_total_mb,
                memory_free_mb=sample.memory_free_mb,
                sampled_at=sample.sampled_at,
                error=sample.error,
            )
        return sample
