"""
Reminder Store
==============
JSON-based persistence for timed reminders.
Atomic writes (tmp + fsync + os.replace) to prevent corruption on crash.
Thread-safe via threading.Lock.
"""

import json
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional


def _debug(msg: str):
    """Debug log that works with pythonw.exe (no stdout)."""
    if sys.stdout is not None:
        print(f"[REMINDER_STORE] {msg}")


class ReminderStore:
    """Persistent storage for reminders with atomic JSON writes."""

    def __init__(self, data_path: Path):
        self._path = Path(data_path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> Dict:
        """Load reminders from JSON file. Returns empty structure on error."""
        if not self._path.exists():
            return {"version": 1, "reminders": []}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "reminders" not in data:
                data["reminders"] = []
            return data
        except (json.JSONDecodeError, IOError) as e:
            _debug(f"Load error (using empty): {e}")
            return {"version": 1, "reminders": []}

    def _save(self, data: Dict):
        """Atomic write: tmp + fsync + os.replace."""
        tmp_path = str(self._path) + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except Exception as e:
            _debug(f"Save error: {e}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

    def add(
        self,
        content: str,
        trigger_time: datetime,
        original_text: str = "",
        repeat_interval_seconds: int = 0,
    ) -> str:
        """Add a one-shot or fixed-interval reminder. Returns reminder ID."""
        reminder_id = str(uuid.uuid4())[:8]
        repeat_interval_seconds = max(0, int(repeat_interval_seconds or 0))
        record = {
            "id": reminder_id,
            "content": content,
            "trigger_time": trigger_time.isoformat(),
            "created_at": datetime.now().isoformat(),
            "confirmed": True,  # Undo model: default active, user can cancel
            "status": "pending",
            "original_text": original_text,
        }
        if repeat_interval_seconds:
            record["repeat_interval_seconds"] = repeat_interval_seconds
            record["fire_count"] = 0
        with self._lock:
            data = self._load()
            data["reminders"].append(record)
            self._save(data)
        _debug(f"Added: id={reminder_id}, content='{content}', time={trigger_time}")
        return reminder_id

    def confirm(self, reminder_id: str) -> bool:
        """Mark a reminder as confirmed by user."""
        with self._lock:
            data = self._load()
            for r in data["reminders"]:
                if r["id"] == reminder_id and r["status"] == "pending":
                    r["confirmed"] = True
                    self._save(data)
                    _debug(f"Confirmed: {reminder_id}")
                    return True
        _debug(f"Confirm failed (not found/not pending): {reminder_id}")
        return False

    def cancel(self, reminder_id: str) -> bool:
        """Cancel a reminder."""
        with self._lock:
            data = self._load()
            for r in data["reminders"]:
                if r["id"] == reminder_id:
                    r["status"] = "cancelled"
                    self._save(data)
                    _debug(f"Cancelled: {reminder_id}")
                    return True
        return False

    @staticmethod
    def _is_recurring(record: Dict) -> bool:
        try:
            return int(record.get("repeat_interval_seconds", 0) or 0) >= 60
        except (TypeError, ValueError):
            return False

    def _cancel_pending_where(self, predicate) -> List[Dict]:
        """Cancel pending reminders matching predicate and return snapshots."""
        cancelled = []
        with self._lock:
            data = self._load()
            for reminder in data["reminders"]:
                if reminder.get("status") != "pending" or not predicate(reminder):
                    continue
                reminder["status"] = "cancelled"
                cancelled.append(dict(reminder))
            if cancelled:
                self._save(data)
        return cancelled

    def cancel_latest_pending(self, recurring_only: bool = False) -> Optional[Dict]:
        """Cancel the most recently created pending reminder."""
        with self._lock:
            data = self._load()
            candidates = [
                reminder
                for reminder in data["reminders"]
                if reminder.get("status") == "pending"
                and (not recurring_only or self._is_recurring(reminder))
            ]
            if not candidates:
                return None
            latest = max(candidates, key=lambda item: item.get("created_at", ""))
            latest["status"] = "cancelled"
            snapshot = dict(latest)
            self._save(data)
        _debug(f"Cancelled latest: {snapshot.get('id', '')}")
        return snapshot

    def cancel_all_pending(self, recurring_only: bool = False) -> List[Dict]:
        """Cancel all pending reminders, optionally recurring ones only."""
        return self._cancel_pending_where(
            lambda reminder: not recurring_only or self._is_recurring(reminder)
        )

    def cancel_matching_pending(
        self, query: str, recurring_only: bool = False
    ) -> List[Dict]:
        """Cancel pending reminders whose content contains the query."""
        query_normalized = re.sub(r"\s+", "", str(query or "")).casefold()
        if not query_normalized:
            return []

        def _matches(reminder: Dict) -> bool:
            if recurring_only and not self._is_recurring(reminder):
                return False
            content = re.sub(r"\s+", "", str(reminder.get("content", ""))).casefold()
            return query_normalized in content

        return self._cancel_pending_where(_matches)

    def mark_fired(self, reminder_id: str, now: datetime = None) -> bool:
        """Complete one delivery.

        One-shot reminders transition to ``fired``. Fixed-interval reminders
        remain pending and advance from their scheduled cadence to the first
        future occurrence, skipping missed cycles after sleep/hibernate.
        If the user cancelled between ``get_due`` and this method, it is a no-op.
        """
        if now is None:
            now = datetime.now()
        with self._lock:
            data = self._load()
            for r in data["reminders"]:
                if r["id"] == reminder_id and r["status"] == "pending":
                    interval = 0
                    try:
                        interval = int(r.get("repeat_interval_seconds", 0) or 0)
                    except (TypeError, ValueError):
                        interval = 0
                    if interval >= 60:
                        try:
                            next_time = datetime.fromisoformat(r["trigger_time"])
                        except (TypeError, ValueError):
                            next_time = now
                        step = timedelta(seconds=interval)
                        next_time += step
                        while next_time <= now:
                            next_time += step
                        r["trigger_time"] = next_time.isoformat()
                        r["last_fired_at"] = now.isoformat()
                        r["fire_count"] = int(r.get("fire_count", 0) or 0) + 1
                    else:
                        r["status"] = "fired"
                    self._save(data)
                    if interval >= 60:
                        _debug(
                            f"Recurring fired: {reminder_id}, next={r['trigger_time']}"
                        )
                    else:
                        _debug(f"Fired: {reminder_id}")
                    return True
        _debug(f"mark_fired skipped (not pending): {reminder_id}")
        return False

    def mark_error(self, reminder_id: str) -> bool:
        """Mark a reminder as delivery_error (retries exhausted, NOT lost)."""
        with self._lock:
            data = self._load()
            for r in data["reminders"]:
                if r["id"] == reminder_id:
                    r["status"] = "delivery_error"
                    self._save(data)
                    _debug(f"Delivery error: {reminder_id}")
                    return True
        return False

    def get_pending(self) -> List[Dict]:
        """Get all pending reminders (regardless of confirmed status)."""
        with self._lock:
            data = self._load()
            return [r for r in data["reminders"] if r["status"] == "pending"]

    def get_due(self, now: datetime = None) -> List[Dict]:
        """Get confirmed pending reminders whose trigger_time has passed."""
        if now is None:
            now = datetime.now()
        now_iso = now.isoformat()
        with self._lock:
            data = self._load()
            due = []
            for r in data["reminders"]:
                if (
                    r["status"] == "pending"
                    and r.get("confirmed", False)
                    and r["trigger_time"] <= now_iso
                ):
                    due.append(r)
            return due

    def cleanup(self, days: int = 30):
        """Remove fired/cancelled reminders older than N days."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._lock:
            data = self._load()
            before = len(data["reminders"])
            data["reminders"] = [
                r
                for r in data["reminders"]
                if r["status"] == "pending" or r.get("created_at", "") > cutoff
            ]
            after = len(data["reminders"])
            if before != after:
                self._save(data)
                _debug(f"Cleanup: removed {before - after} old reminders")
