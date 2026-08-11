"""
Auto-update anti-nuisance gates + durable prefs I/O (v1.0.5 spec).

Primitives kept stateless so they can be unit-tested without spinning up Qt/Win32.
Only `foreground_covers_work_area()` hits Win32 (wrapped in try/except).
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


BUSY_STATES = {
    "RECORDING",
    "TRANSCRIBING",
    "SELECTION_LISTENING",
    "SELECTION_PROCESSING",
}

# Caps per v1.0.5 spec
SKIPPED_VERSIONS_CAP = 32
LAST_PROMPT_CAP = 64
WORK_AREA_COVERAGE_THRESHOLD = 0.95

DEFAULT_PREFS: dict[str, Any] = {
    "skipped_versions": [],
    "last_check_at": "",
    "last_failed_count": 0,
    "backoff_until": None,
    "last_prompt_per_version": {},
    "last_successful_update": None,
}


# ─── Time helpers ────────────────────────────────────────


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ─── Stateless gate checks ──────────────────────────────


def is_busy_state(app_state: str) -> bool:
    return app_state in BUSY_STATES


def elapsed_since_boot_ok(boot_time: float, min_seconds: float = 30.0) -> bool:
    return (time.time() - boot_time) >= min_seconds


def version_skipped(to_version: str, skipped_list: list) -> bool:
    return to_version in (skipped_list or [])


def within_backoff(backoff_until_iso: str | None) -> bool:
    if not backoff_until_iso:
        return False
    dt = parse_iso(backoff_until_iso)
    if dt is None:
        return False
    return datetime.now(timezone.utc) < dt


def prompted_within_24h(to_version: str, last_prompt_per_version: dict) -> bool:
    entry = (last_prompt_per_version or {}).get(to_version)
    if not entry:
        return False
    dt = parse_iso(entry.get("first_shown_at", ""))
    if dt is None:
        return False
    return datetime.now(timezone.utc) - dt < timedelta(hours=24)


def foreground_covers_work_area() -> bool:
    """True if foreground window covers >= 95% of the primary monitor work area.

    Returns False on any Win32 error (conservative).
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        SPI_GETWORKAREA = 0x0030
        work = wintypes.RECT()
        if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(work), 0):
            return False
        fw_area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        wk_area = max(1, (work.right - work.left) * (work.bottom - work.top))
        return (fw_area / wk_area) >= WORK_AREA_COVERAGE_THRESHOLD
    except Exception:
        return False


# ─── LRU trim utilities ─────────────────────────────────


def lru_trim_list(items: list, cap: int) -> list:
    """Keep the last `cap` entries (insertion order = LRU)."""
    if len(items) <= cap:
        return items
    return items[-cap:]


def lru_trim_dict_by_ts(
    items: dict, cap: int, ts_field: str = "first_shown_at"
) -> dict:
    """Trim dict to cap entries, dropping the oldest by parse_iso(value[ts_field])."""
    if len(items) <= cap:
        return items
    ranked = sorted(
        items.items(),
        key=lambda kv: parse_iso(kv[1].get(ts_field, ""))
        or datetime.min.replace(tzinfo=timezone.utc),
    )
    keep = dict(ranked[-cap:])
    return keep


# ─── Prefs I/O ──────────────────────────────────────────


def load_update_prefs(config_path: Path) -> dict:
    """Load general.update_prefs from hotwords.json. Returns DEFAULT_PREFS if missing."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_PREFS)
    prefs = cfg.get("general", {}).get("update_prefs", {}) or {}
    # Merge defaults (additive-only migration)
    result = dict(DEFAULT_PREFS)
    for k, v in prefs.items():
        result[k] = v
    return result


def save_update_prefs(config_path: Path, prefs: dict) -> None:
    """Atomic write of general.update_prefs back into hotwords.json."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return  # Don't clobber a broken config
    cfg.setdefault("general", {})
    # Trim before save
    prefs = dict(prefs)
    prefs["skipped_versions"] = lru_trim_list(
        prefs.get("skipped_versions", []), SKIPPED_VERSIONS_CAP
    )
    prefs["last_prompt_per_version"] = lru_trim_dict_by_ts(
        prefs.get("last_prompt_per_version", {}), LAST_PROMPT_CAP
    )
    cfg["general"]["update_prefs"] = prefs
    tmp = config_path.with_suffix(config_path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    for _ in range(3):
        try:
            os.replace(tmp, config_path)
            return
        except OSError:
            time.sleep(0.1)
    try:
        tmp.unlink()
    except OSError:
        pass


# ─── Composite decision ─────────────────────────────────


def should_show_update_prompt(
    to_version: str,
    manifest_critical: bool,
    prefs: dict,
    app_state: str,
    boot_time: float,
    stage_is_ready: bool = False,
) -> tuple[bool, str]:
    """Top-level decision. Returns (show, reason_if_suppressed).

    When stage_is_ready=True, gates 1/2/3 (busy/boot/fullscreen) are bypassed so that
    "use-and-go" users can still see a downloaded update eventually.

    Critical updates bypass version-skip gate only.
    """
    if not stage_is_ready:
        if is_busy_state(app_state):
            return False, "busy"
        if not elapsed_since_boot_ok(boot_time):
            return False, "boot_too_recent"
        if foreground_covers_work_area():
            return False, "fullscreen"

    if (
        version_skipped(to_version, prefs.get("skipped_versions", []))
        and not manifest_critical
    ):
        return False, "user_skipped_version"

    if within_backoff(prefs.get("backoff_until")):
        return False, "backoff"

    if prompted_within_24h(to_version, prefs.get("last_prompt_per_version", {})):
        return False, "already_prompted_today"

    return True, ""
