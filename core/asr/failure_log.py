"""
ASR final-segment failure telemetry — DebugLog/asr_failures.jsonl.

One JSON row per committed-segment timeout / unexplained-empty result, plus a
follow-up row per async rescue outcome.  This file is the raw material for
root-causing the "GPU idle yet 20x slower than realtime" timeout clusters,
so rows carry the GPU probe, driver version and process private bytes at the
moment of failure.  Best-effort by design: telemetry must never raise into
the ASR worker.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import threading
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024

_DEFAULT_PATH = Path(__file__).parent.parent.parent / "DebugLog" / "asr_failures.jsonl"

_write_lock = threading.Lock()

_driver_version_cache: dict = {}


def get_driver_version() -> str | None:
    """NVIDIA driver version, queried once per process (None if unavailable)."""
    if "value" not in _driver_version_cache:
        try:
            raw = subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=driver_version",
                    "--format=csv,noheader",
                ],
                stderr=subprocess.STDOUT,
                timeout=2.0,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            first = raw.strip().splitlines()[0].strip()
            _driver_version_cache["value"] = first or None
        except Exception:
            _driver_version_cache["value"] = None
    return _driver_version_cache["value"]


def get_process_private_mb() -> float | None:
    """Current process private bytes in MB (psutil is a hard dependency)."""
    try:
        import os

        import psutil

        mem = psutil.Process(os.getpid()).memory_info()
        private = getattr(mem, "private", 0) or getattr(mem, "rss", 0)
        return round(private / (1024 * 1024), 1)
    except Exception:
        return None


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            backup = path.with_suffix(path.suffix + ".1")
            if backup.exists():
                backup.unlink()
            path.rename(backup)
    except Exception:
        pass


def append_failure_record(record: dict, path: Path | str | None = None) -> bool:
    """Append one JSON row (adds ts if missing). Returns False on any failure."""
    target = Path(path) if path is not None else _DEFAULT_PATH
    row = dict(record)
    row.setdefault(
        "ts", _dt.datetime.now().isoformat(timespec="milliseconds")
    )
    try:
        line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
    except Exception:
        return False
    with _write_lock:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(target)
            with open(target, "a", encoding="utf-8") as f:
                f.write(line)
            return True
        except Exception:
            return False
