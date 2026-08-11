"""
History Migrator
================
Migrates legacy data (DebugLog sessions + InsightStore) into unified HistoryStore.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import RecordType
from .store import HistoryStore


def _mlog(msg: str):
    """Migration log (pythonw.exe safe)."""
    if sys.stdout is not None:
        print(f"[MIGRATION] {msg}")


def migrate_debug_sessions(
    debug_dir: Path,
    history_store: HistoryStore,
) -> int:
    """
    Migrate DebugLog/session_*.json files to HistoryStore.

    Each session file contains:
    - asr.raw_text: pre-polish ASR text
    - final_text: post-polish text
    - start_time: ISO timestamp
    - audio.duration_seconds: audio duration

    Args:
        debug_dir: Path to DebugLog directory
        history_store: Target HistoryStore

    Returns:
        Number of records migrated
    """
    if not debug_dir.exists():
        _mlog(f"Debug dir not found: {debug_dir}")
        return 0

    session_files = sorted(debug_dir.glob("session_*.json"))
    if not session_files:
        _mlog("No session files to migrate")
        return 0

    _mlog(f"Found {len(session_files)} session files to migrate")
    count = 0

    for session_file in session_files:
        try:
            with open(session_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            asr_data = data.get("asr", {})
            raw_text = asr_data.get("raw_text", "").strip()
            final_text = data.get("final_text", "").strip()
            start_time = data.get("start_time", "")

            # Need at least some text
            if not raw_text and not final_text:
                continue

            # Build metadata
            metadata = {}
            if data.get("session_id"):
                metadata["session_id"] = data["session_id"]
            audio = data.get("audio", {})
            if audio and audio.get("duration_seconds"):
                metadata["duration_s"] = round(audio["duration_seconds"], 2)
            metadata["migrated_from"] = "debug_session"
            metadata["source_file"] = session_file.name

            record_id = history_store.add(
                record_type=RecordType.ASR,
                input_text=raw_text or final_text,
                output_text=final_text if raw_text else "",
                timestamp=start_time or datetime.now().isoformat(),
                metadata=metadata,
            )

            if record_id:
                count += 1

        except (json.JSONDecodeError, OSError) as e:
            _mlog(f"Failed to migrate {session_file.name}: {e}")
            continue

    _mlog(f"Migrated {count} debug session records")
    return count


def migrate_insight_store(
    insight_dir: Path,
    history_store: HistoryStore,
) -> int:
    """
    Migrate InsightStore monthly JSON files to HistoryStore.

    InsightStore format: YYYY-MM.json with entries array.
    Each entry: {id, timestamp, text, duration_s, session_id, type, attributes?}

    Args:
        insight_dir: Path to InsightStore data directory
        history_store: Target HistoryStore

    Returns:
        Number of records migrated
    """
    if not insight_dir.exists():
        _mlog(f"Insight dir not found: {insight_dir}")
        return 0

    month_files = sorted(insight_dir.glob("*.json"))
    if not month_files:
        _mlog("No InsightStore files to migrate")
        return 0

    _mlog(f"Found {len(month_files)} InsightStore month files")
    count = 0

    for month_file in month_files:
        try:
            with open(month_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            entries = data.get("entries", [])
            for entry in entries:
                text = entry.get("text", "").strip()
                if not text:
                    continue

                timestamp = entry.get("timestamp", "")
                entry_type = entry.get("type", "transcription")

                # Map InsightStore entry types to RecordType
                if entry_type == "highlight":
                    record_type = RecordType.HIGHLIGHT
                else:
                    record_type = RecordType.ASR

                metadata = {"migrated_from": "insight_store"}
                if entry.get("session_id"):
                    metadata["session_id"] = entry["session_id"]
                if entry.get("duration_s"):
                    metadata["duration_s"] = entry["duration_s"]
                if entry.get("attributes"):
                    metadata["attributes"] = entry["attributes"]

                record_id = history_store.add(
                    record_type=record_type,
                    input_text=text,
                    output_text="",
                    timestamp=timestamp or datetime.now().isoformat(),
                    metadata=metadata,
                )

                if record_id:
                    count += 1

        except (json.JSONDecodeError, OSError) as e:
            _mlog(f"Failed to migrate {month_file.name}: {e}")
            continue

    _mlog(f"Migrated {count} InsightStore records")
    return count


def run_migration(
    config_path: Path,
    debug_dir: Path,
    insight_dir: Path,
    history_store: HistoryStore,
) -> bool:
    """
    Run full migration if not already done.

    Checks config for "history_migrated" flag.

    Args:
        config_path: Path to hotwords.json config
        debug_dir: Path to DebugLog directory
        insight_dir: Path to InsightStore data directory
        history_store: Target HistoryStore

    Returns:
        True if migration was performed, False if already done or failed
    """
    if not history_store or not history_store.enabled:
        _mlog("History store disabled, skipping migration")
        return False

    # Check if already migrated — abort if config is unreadable
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        if config.get("history_migrated"):
            _mlog("Already migrated, skipping")
            return False
    except (json.JSONDecodeError, OSError) as e:
        _mlog(f"Cannot read config, aborting migration to avoid data loss: {e}")
        return False

    _mlog("Starting migration...")

    total = 0
    total += migrate_debug_sessions(debug_dir, history_store)
    total += migrate_insight_store(insight_dir, history_store)

    _mlog(f"Migration complete: {total} total records")

    # Mark as migrated — re-read config then atomic write
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        config["history_migrated"] = True

        import tempfile, os

        tmp_fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, config_path)
            _mlog("Migration flag saved to config (atomic)")
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except (OSError, PermissionError, json.JSONDecodeError) as e:
        _mlog(f"Failed to save migration flag: {e}")

    return True
