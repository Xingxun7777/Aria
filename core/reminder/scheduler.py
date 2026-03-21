"""
Reminder Scheduler
==================
Background daemon thread that polls for due reminders every 15 seconds.

Why polling instead of precise timers:
- Survives system sleep/hibernate (Timer would drift)
- Simple and reliable (no timer management/cancellation races)
- 15-second max delay is acceptable for human reminders
- Matches existing _config_watcher pattern in app.py
"""

import sys
import threading
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import ReminderStore


def _debug(msg: str):
    """Debug log safe for pythonw.exe."""
    if sys.stdout is not None:
        print(f"[REMINDER_SCHED] {msg}")


class ReminderScheduler:
    """Polls ReminderStore every 15 seconds and fires due reminders."""

    POLL_INTERVAL_S = 15

    def __init__(
        self,
        store: "ReminderStore",
        on_reminder_due,
        stop_event: threading.Event,
    ):
        """
        Args:
            store: ReminderStore instance
            on_reminder_due: Callback(reminder_dict) called for each due reminder.
                             Must be thread-safe (typically emits via QtBridge).
            stop_event: Shared stop event for graceful shutdown.
        """
        self._store = store
        self._on_due = on_reminder_due
        self._stop_event = stop_event
        self._thread: threading.Thread = None
        self._retry_counts: dict = {}  # reminder_id -> retry count
        self.MAX_RETRIES = 5  # Stop retrying after this many failures

    def start(self):
        """Start the scheduler daemon thread."""
        if self._thread and self._thread.is_alive():
            _debug("Already running")
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="ReminderScheduler"
        )
        self._thread.start()
        _debug("Started")

    def _run(self):
        """Main polling loop."""
        _debug(f"Polling every {self.POLL_INTERVAL_S}s")

        # Initial check on startup (catch overdue reminders after restart)
        self._check_due()

        while not self._stop_event.is_set():
            self._stop_event.wait(self.POLL_INTERVAL_S)
            if self._stop_event.is_set():
                break
            self._check_due()

        _debug("Stopped")

    def _check_due(self):
        """Check for due reminders and fire them.

        If multiple reminders are due (e.g. after system wake from sleep),
        batch them into a single callback to avoid popup/sound overload.
        """
        try:
            now = datetime.now()
            due = self._store.get_due(now)
            if not due:
                return

            # Filter out reminders that exceeded max retries
            actionable = []
            for r in due:
                rid = r["id"]
                retries = self._retry_counts.get(rid, 0)
                if retries >= self.MAX_RETRIES:
                    _debug(
                        f"Max retries ({self.MAX_RETRIES}) for {rid}, marking as error (NOT fired)"
                    )
                    # Mark as delivery_error — NOT fired. User can see it in pending list.
                    self._store.mark_error(rid)
                    self._retry_counts.pop(rid, None)
                else:
                    actionable.append(r)

            if not actionable:
                return

            if len(actionable) == 1:
                reminder = actionable[0]
                rid = reminder["id"]
                _debug(f"Firing: id={rid}, content='{reminder.get('content', '')}'")
                try:
                    self._on_due(reminder)
                    self._store.mark_fired(rid)
                    self._retry_counts.pop(rid, None)
                except Exception as e:
                    self._retry_counts[rid] = self._retry_counts.get(rid, 0) + 1
                    _debug(
                        f"Callback error for {rid} (retry {self._retry_counts[rid]}): {e}"
                    )
            else:
                _debug(f"Batching {len(actionable)} overdue reminders")
                summary = {
                    "id": "batch",
                    "content": "\n".join(
                        f"- {r.get('content', '提醒')}" for r in actionable
                    ),
                    "trigger_time": actionable[0]["trigger_time"],
                    "created_at": actionable[0]["created_at"],
                    "batch_count": len(actionable),
                    "batch_items": actionable,
                }
                try:
                    self._on_due(summary)
                    for reminder in actionable:
                        self._store.mark_fired(reminder["id"])
                        self._retry_counts.pop(reminder["id"], None)
                except Exception as e:
                    for reminder in actionable:
                        rid = reminder["id"]
                        self._retry_counts[rid] = self._retry_counts.get(rid, 0) + 1
                    _debug(f"Batch callback error (will retry): {e}")
        except Exception as e:
            _debug(f"Check error: {e}")
