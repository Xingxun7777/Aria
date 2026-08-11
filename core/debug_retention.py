"""
DebugLog retention policy
=========================

Keeps the DebugLog/ directory bounded: per-dictation artifacts
(session_*.json and audio_*.wav) plus expired rotated log backups (*.log.N)
are deleted once they exceed the retention window, and if the directory
total still exceeds the size cap, the oldest artifacts are deleted first
until it fits — but never files written within the last 24h (today's
session JSONs feed the tray history popup).

Active text logs are bounded separately by size rotation
(core.debug.append_log_line), and telemetry files such as
asr_failures.jsonl are explicitly exempt.

Defaults (overridable via environment variables):
    ARIA_DEBUG_RETENTION_DAYS  retention window in days   (default 14)
    ARIA_DEBUG_MAX_MB          directory size cap in MB   (default 500)

Intended usage from the app: ``start_retention_thread()`` once at startup;
it runs immediately and then every 24h on a daemon thread.
"""

import os
import re
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_TOTAL_MB = 500
DEFAULT_INTERVAL_S = 24 * 3600

# Files that must never be deleted by the retention policy, even if they
# match a deletable pattern (e.g. telemetry appended by other subsystems).
# asr_failures.jsonl.1 is the failure-telemetry rotation backup
# (core.asr.failure_log); listed explicitly so a future widening of the
# rotated-backup pattern below cannot silently reclaim it.
EXEMPT_NAMES = frozenset({"asr_failures.jsonl", "asr_failures.jsonl.1"})

DEBUG_DIR = Path(__file__).parent.parent / "DebugLog"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def resolve_retention_days(config_days: Optional[int] = None) -> int:
    """Env var wins over config value, which wins over the default."""
    if os.environ.get("ARIA_DEBUG_RETENTION_DAYS", "").strip():
        return _env_int("ARIA_DEBUG_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)
    if config_days is not None:
        try:
            return int(config_days)
        except (TypeError, ValueError):
            pass
    return DEFAULT_RETENTION_DAYS


def resolve_max_total_bytes() -> int:
    return _env_int("ARIA_DEBUG_MAX_MB", DEFAULT_MAX_TOTAL_MB) * 1024 * 1024


# Rotated text-log backups (pipeline_debug.log.1, qwen3_debug.log.2, ...).
# Active .log files are bounded by size rotation and never touched here, but
# expired backups are reclaimable — legacy pre-rotation backups can be 100MB+.
_ROTATED_LOG_BACKUP_RE = re.compile(r".+\.log\.\d+$")

# Never size-delete anything written less than this long ago: today's session
# JSONs feed the tray history popup, and a size-pressured first run (legacy
# multi-hundred-MB backlog) must not eat brand-new files to meet the cap.
SIZE_PASS_MIN_AGE_S = 86400


def _is_artifact(name: str) -> bool:
    """Per-dictation artifacts: deletable by both the age and size pass."""
    if name.startswith("session_") and name.endswith(".json"):
        return True
    if name.startswith("audio_") and name.endswith(".wav"):
        return True
    return False


def _is_rotated_log_backup(name: str) -> bool:
    return bool(_ROTATED_LOG_BACKUP_RE.match(name))


def run_retention(
    debug_dir,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_MB * 1024 * 1024,
    exempt_names: Iterable[str] = EXEMPT_NAMES,
) -> dict:
    """Apply the retention policy to debug_dir. Returns a stats dict.

    Single os.scandir pass (the directory can hold ~90k files); individual
    delete failures (file held open on Windows, etc.) are skipped.
    retention_days <= 0 disables the age pass; max_total_bytes <= 0 disables
    the size pass.
    """
    stats = {"deleted_expired": 0, "deleted_for_size": 0, "total_bytes": 0}
    debug_dir = Path(debug_dir)
    if not debug_dir.is_dir():
        return stats

    now = time.time()
    exempt = frozenset(exempt_names) | EXEMPT_NAMES
    cutoff = now - retention_days * 86400 if retention_days > 0 else None
    size_pass_floor = now - SIZE_PASS_MIN_AGE_S

    total_bytes = 0
    # Size-pass candidates surviving the age pass: (mtime, size, path)
    survivors: list = []

    try:
        with os.scandir(debug_dir) as entries:
            for entry in entries:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue

                name = entry.name
                if name in exempt:
                    total_bytes += st.st_size
                    continue

                is_artifact = _is_artifact(name)
                # Expired rotated backups are reclaimed by the age pass only;
                # recent backups may still be wanted for diagnosis.
                age_deletable = is_artifact or _is_rotated_log_backup(name)

                if age_deletable and cutoff is not None and st.st_mtime < cutoff:
                    try:
                        os.unlink(entry.path)
                        stats["deleted_expired"] += 1
                        continue
                    except OSError:
                        pass  # Counts toward the total; may go in the size pass.

                total_bytes += st.st_size
                if is_artifact:
                    survivors.append((st.st_mtime, st.st_size, entry.path))
    except OSError:
        return stats

    if max_total_bytes > 0 and total_bytes > max_total_bytes:
        survivors.sort()  # oldest mtime first
        for mtime, size, path in survivors:
            if total_bytes <= max_total_bytes:
                break
            if mtime >= size_pass_floor:
                # Sorted oldest-first: everything from here on was written in
                # the last 24h. Today's files are never sacrificed to the cap.
                break
            try:
                os.unlink(path)
            except OSError:
                continue
            stats["deleted_for_size"] += 1
            total_bytes -= size

    stats["total_bytes"] = total_bytes
    return stats


def start_retention_thread(
    debug_dir=None,
    retention_days: Optional[int] = None,
    max_total_bytes: Optional[int] = None,
    interval_s: float = DEFAULT_INTERVAL_S,
) -> threading.Thread:
    """Run retention now and then every interval_s on a daemon thread."""
    resolved_dir = Path(debug_dir) if debug_dir is not None else DEBUG_DIR
    days = (
        retention_days
        if retention_days is not None
        else resolve_retention_days()
    )
    cap = max_total_bytes if max_total_bytes is not None else resolve_max_total_bytes()

    def _loop() -> None:
        while True:
            try:
                stats = run_retention(resolved_dir, days, cap)
                deleted = stats["deleted_expired"] + stats["deleted_for_size"]
                if deleted:
                    print(
                        f"[DEBUGLOG] Retention: removed {deleted} files "
                        f"(expired={stats['deleted_expired']}, "
                        f"size_cap={stats['deleted_for_size']}, "
                        f"now {stats['total_bytes'] / 1e6:.1f}MB, "
                        f"retention={days}d, cap={cap / 1e6:.0f}MB)"
                    )
            except Exception as e:
                print(f"[DEBUGLOG] Retention failed: {e}")
            time.sleep(interval_s)

    thread = threading.Thread(target=_loop, daemon=True, name="debuglog-retention")
    thread.start()
    return thread
