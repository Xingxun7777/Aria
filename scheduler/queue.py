"""
Task Queue Manager
==================
Manages the queue of UtteranceTasks, ensuring ordered execution.
"""

import threading
from queue import Queue, Empty
from typing import Optional, List, Callable
from collections import OrderedDict

from .task import UtteranceTask, TaskState
from ..core.logging import get_scheduler_logger

logger = get_scheduler_logger()


class TaskQueue:
    """
    Thread-safe task queue with ordered insertion.

    Key behaviors:
    - STT and LLM can run in parallel for different tasks
    - Insertion must be sequential (preserve order)
    - Failed tasks don't block the queue
    """

    def __init__(self, max_size: int = 100):
        self._tasks: OrderedDict[str, UtteranceTask] = OrderedDict()
        self._insert_queue: Queue[str] = Queue()  # Task IDs waiting for insertion
        self._lock = threading.RLock()
        self._max_size = max_size

        # Callbacks
        self._on_task_complete: Optional[Callable[[UtteranceTask], None]] = None
        self._on_task_failed: Optional[Callable[[UtteranceTask], None]] = None

    def add(self, task: UtteranceTask) -> bool:
        """Add a new task to the queue."""
        with self._lock:
            if len(self._tasks) >= self._max_size:
                logger.warning(f"Queue full, rejecting task {task.id}")
                return False

            self._tasks[task.id] = task
            logger.debug(f"Task {task.id} added to queue")
            return True

    def get(self, task_id: str) -> Optional[UtteranceTask]:
        """Get a task by ID."""
        with self._lock:
            return self._tasks.get(task_id)

    def remove(self, task_id: str) -> Optional[UtteranceTask]:
        """Remove a task from the queue."""
        with self._lock:
            return self._tasks.pop(task_id, None)

    def queue_for_insert(self, task_id: str) -> None:
        """Queue a task for text insertion (maintains order)."""
        self._insert_queue.put(task_id)
        logger.debug(f"Task {task_id} queued for insertion")

    def get_next_insert(self, timeout: float = 0.1) -> Optional[UtteranceTask]:
        """Get the next task ready for insertion."""
        try:
            task_id = self._insert_queue.get(timeout=timeout)
            task = self.get(task_id)

            if task and task.state == TaskState.WAITING_INSERT:
                return task

            # Task may have been cancelled or failed
            return None
        except Empty:
            return None

    def get_active_tasks(self) -> List[UtteranceTask]:
        """Get all non-terminal tasks."""
        with self._lock:
            return [t for t in self._tasks.values() if not t.is_terminal]

    def get_recording_task(self) -> Optional[UtteranceTask]:
        """Get the currently recording task (should be at most one)."""
        with self._lock:
            for task in self._tasks.values():
                if task.state == TaskState.RECORDING:
                    return task
            return None

    def cleanup_completed(self, max_age_seconds: float = 60) -> int:
        """Remove old completed tasks."""
        import time
        with self._lock:
            to_remove = []
            now = time.time()

            for task_id, task in self._tasks.items():
                if task.is_terminal:
                    age = now - task.created_at.timestamp()
                    if age > max_age_seconds:
                        to_remove.append(task_id)

            for task_id in to_remove:
                del self._tasks[task_id]

            if to_remove:
                logger.debug(f"Cleaned up {len(to_remove)} completed tasks")

            return len(to_remove)

    def cancel_all(self) -> int:
        """Cancel all non-terminal tasks."""
        with self._lock:
            count = 0
            for task in self._tasks.values():
                if not task.is_terminal and task.cancel():
                    count += 1
            return count

    @property
    def size(self) -> int:
        """Current queue size."""
        with self._lock:
            return len(self._tasks)

    @property
    def pending_inserts(self) -> int:
        """Number of tasks waiting for insertion."""
        return self._insert_queue.qsize()

    def set_callbacks(
        self,
        on_complete: Optional[Callable[[UtteranceTask], None]] = None,
        on_failed: Optional[Callable[[UtteranceTask], None]] = None
    ) -> None:
        """Set callback functions for task events."""
        self._on_task_complete = on_complete
        self._on_task_failed = on_failed

    def notify_complete(self, task: UtteranceTask) -> None:
        """Notify that a task completed."""
        if self._on_task_complete:
            self._on_task_complete(task)

    def notify_failed(self, task: UtteranceTask) -> None:
        """Notify that a task failed."""
        if self._on_task_failed:
            self._on_task_failed(task)
