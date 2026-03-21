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

            if len(due) == 1:
                # Single reminder — notify first, then mark fired
                reminder = due[0]
                rid = reminder["id"]
                _debug(f"Firing: id={rid}, content='{reminder.get('content', '')}'")
                try:
                    self._on_due(reminder)
                    self._store.mark_fired(rid)
                except Exception as e:
                    _debug(f"Callback error for {rid}: {e}")
                    # Don't mark fired — will retry next poll
            else:
                # Multiple due (batched after sleep/hibernate)
                _debug(f"Batching {len(due)} overdue reminders")
                summary = {
                    "id": "batch",
                    "content": "\n".join(f"- {r.get('content', '提醒')}" for r in due),
                    "trigger_time": due[0]["trigger_time"],
                    "created_at": due[0]["created_at"],
                    "batch_count": len(due),
                    "batch_items": due,
                }
                try:
                    self._on_due(summary)
                    # Only mark fired after successful notification
                    for reminder in due:
                        self._store.mark_fired(reminder["id"])
                except Exception as e:
                    _debug(f"Batch callback error: {e}")
                    # Don't mark fired — will retry next poll
        except Exception as e:
            _debug(f"Check error: {e}")
