"""
Reminder Store
==============
JSON-based persistence for timed reminders.
Atomic writes (tmp + fsync + os.replace) to prevent corruption on crash.
Thread-safe via threading.Lock.
"""

import json
import os
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
    ) -> str:
        """Add a new reminder (unconfirmed). Returns reminder ID."""
        reminder_id = str(uuid.uuid4())[:8]
        record = {
            "id": reminder_id,
            "content": content,
            "trigger_time": trigger_time.isoformat(),
            "created_at": datetime.now().isoformat(),
            "confirmed": True,  # Undo model: default active, user can cancel
            "status": "pending",
            "original_text": original_text,
        }
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

    def mark_fired(self, reminder_id: str) -> bool:
        """Mark a reminder as fired (delivered to user).

        Only transitions from 'pending' to 'fired'. If user already
        cancelled it between get_due() and mark_fired(), this is a no-op.
        """
        with self._lock:
            data = self._load()
            for r in data["reminders"]:
                if r["id"] == reminder_id and r["status"] == "pending":
                    r["status"] = "fired"
                    self._save(data)
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
