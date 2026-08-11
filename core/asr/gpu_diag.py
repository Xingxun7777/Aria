"""
GPU slow-path diagnostic snapshots.

Motivation: the "GPU looks idle yet ASR takes 20x realtime" clusters cannot be
explained with the existing pressure probe (utilization + free memory only).
When ASR times out / trips the primary-suppression breaker, or a transcription
finishes suspiciously slowly, this module captures one nvidia-smi snapshot —
P-state, SM clocks vs max, power draw vs limit, temperature, and the list of
GPU compute processes — so night-time driver power states or a competing GPU
process can be identified from the logs after the fact.

Snapshots always run on a daemon thread and are throttled to one per
5 minutes.  This module must never raise and never block the transcription
path.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Callable, Sequence

# Signature matches app._pipeline_log(stage, msg).
LogFn = Callable[[str, str], None]

LOG_STAGE = "GPU-DIAG"
THROTTLE_S = 300.0
QUERY_TIMEOUT_S = 5.0
# --query-compute-apps on WDDM machines lists every process with a GPU
# context (can be 50+); cap the per-process lines so one snapshot stays small.
MAX_APP_LINES = 40

_GPU_QUERY_FIELDS = (
    "pstate",
    "sm_clock",
    "sm_clock_max",
    "power_draw",
    "power_limit",
    "temp_gpu",
    "util_gpu",
    "mem_used",
)
_GPU_QUERY_CMD = (
    "nvidia-smi",
    "--query-gpu=pstate,clocks.sm,clocks.max.sm,power.draw,power.limit,"
    "temperature.gpu,utilization.gpu,memory.used",
    "--format=csv,noheader",
)
_APPS_QUERY_CMD = (
    "nvidia-smi",
    "--query-compute-apps=pid,process_name,used_memory",
    "--format=csv,noheader",
)

_throttle_lock = threading.Lock()
_last_snapshot_at: float | None = None


def reset_throttle_for_tests() -> None:
    global _last_snapshot_at
    with _throttle_lock:
        _last_snapshot_at = None


def _run_query(cmd: Sequence[str]) -> str:
    """Run one nvidia-smi query; return placeholder text instead of raising."""
    try:
        return subprocess.check_output(
            list(cmd),
            stderr=subprocess.STDOUT,
            timeout=QUERY_TIMEOUT_S,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).strip()
    except Exception as exc:
        return f"<query-failed {type(exc).__name__}: {exc}>"


def collect_snapshot_lines(reason: str) -> list[str]:
    """Build one-line-per-record log messages describing current GPU state.

    ``reason`` should be a single token (no spaces) so ``reason=`` stays a
    clean field for later ``rg`` statistics.
    """
    lines: list[str] = []

    gpu_raw = _run_query(_GPU_QUERY_CMD)
    gpu_rows = [row.strip() for row in gpu_raw.splitlines() if row.strip()]
    if not gpu_rows:
        gpu_rows = ["<empty>"]
    for idx, row in enumerate(gpu_rows):
        parts = [p.strip() for p in row.split(",")]
        if len(parts) == len(_GPU_QUERY_FIELDS):
            fields = " ".join(
                f"{name}={value}" for name, value in zip(_GPU_QUERY_FIELDS, parts)
            )
            lines.append(f"gpu{idx} reason={reason} {fields}")
        else:
            lines.append(f"gpu{idx} reason={reason} raw={row}")

    apps_raw = _run_query(_APPS_QUERY_CMD)
    app_rows = [row.strip() for row in apps_raw.splitlines() if row.strip()]
    lines.append(f"apps reason={reason} count={len(app_rows)}")
    for row in app_rows[:MAX_APP_LINES]:
        parts = [p.strip() for p in row.split(",")]
        if len(parts) >= 3:
            # process_name may itself contain commas — keep the middle intact.
            pid, name, mem = parts[0], ",".join(parts[1:-1]), parts[-1]
            lines.append(f"app pid={pid} mem={mem} name={name}")
        else:
            lines.append(f"app raw={row}")
    if len(app_rows) > MAX_APP_LINES:
        lines.append(f"apps truncated={len(app_rows) - MAX_APP_LINES}")
    return lines


def _snapshot_worker(reason: str, log_fn: LogFn) -> None:
    try:
        for line in collect_snapshot_lines(reason):
            log_fn(LOG_STAGE, line)
    except Exception:
        pass


def maybe_snapshot_async(reason: str, log_fn: LogFn) -> threading.Thread | None:
    """Trigger a throttled GPU diagnostic snapshot on a daemon thread.

    Returns the worker thread when a snapshot was scheduled, or None when
    throttled (at most one snapshot per THROTTLE_S seconds).  The throttle
    check is the only synchronous work; nvidia-smi runs entirely on the
    daemon thread.  Never raises.
    """
    global _last_snapshot_at
    try:
        now = time.monotonic()
        with _throttle_lock:
            if _last_snapshot_at is not None and now - _last_snapshot_at < THROTTLE_S:
                return None
            _last_snapshot_at = now
        worker = threading.Thread(
            target=_snapshot_worker,
            args=(reason, log_fn),
            daemon=True,
            name="gpu-diag-snapshot",
        )
        worker.start()
        return worker
    except Exception:
        return None
