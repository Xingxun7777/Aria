"""
UtteranceTask State Machine
===========================
Core task representation for voice-to-text pipeline.

State transitions:
  CREATED -> RECORDING -> STT_PROCESSING -> LLM_PROCESSING ->
  WAITING_INSERT -> INSERTING -> DONE/FAILED/CANCELLED
"""

import uuid
import time
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, Any
from datetime import datetime


class TaskState(Enum):
    """Task states for the utterance pipeline."""
    CREATED = auto()         # Task just created
    RECORDING = auto()       # Recording audio
    STT_PROCESSING = auto()  # Running ASR
    LLM_PROCESSING = auto()  # Running LLM cleanup
    WAITING_INSERT = auto()  # Queued for insertion
    INSERTING = auto()       # Inserting text to target
    DONE = auto()            # Successfully completed
    FAILED = auto()          # Failed at some stage
    CANCELLED = auto()       # Cancelled by user


@dataclass
class TargetContext:
    """Context of the target window for text insertion."""
    hwnd: int = 0                    # Window handle
    process_name: str = ""           # e.g., "notepad.exe"
    window_title: str = ""           # Window title
    cursor_position: tuple = (0, 0)  # Cursor position when recorded


@dataclass
class UtteranceTask:
    """
    Represents a single voice-to-text task.

    Lifecycle:
    1. User triggers hotkey -> CREATED
    2. Start recording -> RECORDING
    3. User releases/timeout -> STT_PROCESSING
    4. ASR complete -> LLM_PROCESSING
    5. LLM complete -> WAITING_INSERT
    6. Queue turn -> INSERTING
    7. Insertion complete -> DONE
    """
    # Identity
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: datetime = field(default_factory=datetime.now)

    # State
    state: TaskState = TaskState.CREATED
    error_message: Optional[str] = None

    # Target window context (captured at task creation)
    target: TargetContext = field(default_factory=TargetContext)

    # Data through pipeline
    audio_buffer: Optional[bytes] = None
    audio_duration_ms: int = 0

    stt_result: Optional[str] = None      # Raw ASR output
    stt_confidence: float = 0.0

    llm_result: Optional[str] = None      # Cleaned text
    final_text: Optional[str] = None      # Text to insert

    # Timing
    recording_start: float = 0.0
    recording_end: float = 0.0
    stt_start: float = 0.0
    stt_end: float = 0.0
    llm_start: float = 0.0
    llm_end: float = 0.0
    insert_start: float = 0.0
    insert_end: float = 0.0

    # Metadata
    retry_count: int = 0
    max_retries: int = 2

    def transition(self, new_state: TaskState, error: Optional[str] = None) -> bool:
        """
        Transition to a new state with validation.

        Returns:
            True if transition was valid and executed
        """
        valid_transitions = {
            TaskState.CREATED: [TaskState.RECORDING, TaskState.CANCELLED],
            TaskState.RECORDING: [TaskState.STT_PROCESSING, TaskState.CANCELLED, TaskState.FAILED],
            TaskState.STT_PROCESSING: [TaskState.LLM_PROCESSING, TaskState.FAILED, TaskState.CANCELLED],
            TaskState.LLM_PROCESSING: [TaskState.WAITING_INSERT, TaskState.FAILED, TaskState.CANCELLED],
            TaskState.WAITING_INSERT: [TaskState.INSERTING, TaskState.CANCELLED],
            TaskState.INSERTING: [TaskState.DONE, TaskState.FAILED, TaskState.WAITING_INSERT],
            # Terminal states - no transitions out
            TaskState.DONE: [],
            TaskState.FAILED: [],
            TaskState.CANCELLED: [],
        }

        if new_state not in valid_transitions.get(self.state, []):
            return False

        self.state = new_state
        if error:
            self.error_message = error

        return True

    def start_recording(self, hwnd: int = 0, process_name: str = "", title: str = "") -> bool:
        """Start recording phase."""
        if not self.transition(TaskState.RECORDING):
            return False

        self.recording_start = time.time()
        self.target = TargetContext(
            hwnd=hwnd,
            process_name=process_name,
            window_title=title
        )
        return True

    def stop_recording(self, audio_data: bytes, duration_ms: int) -> bool:
        """Stop recording and move to STT."""
        self.recording_end = time.time()
        self.audio_buffer = audio_data
        self.audio_duration_ms = duration_ms
        self.stt_start = time.time()
        return self.transition(TaskState.STT_PROCESSING)

    def complete_stt(self, text: str, confidence: float = 1.0) -> bool:
        """Complete STT and move to LLM processing."""
        self.stt_end = time.time()
        self.stt_result = text
        self.stt_confidence = confidence
        self.llm_start = time.time()
        return self.transition(TaskState.LLM_PROCESSING)

    def complete_llm(self, cleaned_text: str) -> bool:
        """Complete LLM cleanup and queue for insertion."""
        self.llm_end = time.time()
        self.llm_result = cleaned_text
        self.final_text = cleaned_text
        return self.transition(TaskState.WAITING_INSERT)

    def skip_llm(self) -> bool:
        """Skip LLM processing (use raw STT result)."""
        self.llm_result = self.stt_result
        self.final_text = self.stt_result
        return self.transition(TaskState.WAITING_INSERT)

    def start_insert(self) -> bool:
        """Start text insertion."""
        self.insert_start = time.time()
        return self.transition(TaskState.INSERTING)

    def complete_insert(self) -> bool:
        """Complete insertion successfully."""
        self.insert_end = time.time()
        return self.transition(TaskState.DONE)

    def fail(self, error: str) -> bool:
        """Mark task as failed."""
        return self.transition(TaskState.FAILED, error)

    def cancel(self) -> bool:
        """Cancel the task."""
        return self.transition(TaskState.CANCELLED)

    def can_retry(self) -> bool:
        """Check if task can be retried."""
        return self.retry_count < self.max_retries

    def retry(self) -> bool:
        """Retry from WAITING_INSERT state."""
        if not self.can_retry():
            return False

        self.retry_count += 1
        self.state = TaskState.WAITING_INSERT
        return True

    @property
    def is_terminal(self) -> bool:
        """Check if task is in a terminal state."""
        return self.state in [TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED]

    @property
    def total_duration_ms(self) -> float:
        """Total time from creation to completion."""
        if self.insert_end > 0:
            return (self.insert_end - self.created_at.timestamp()) * 1000
        return 0

    @property
    def stt_duration_ms(self) -> float:
        """STT processing time."""
        if self.stt_end > 0 and self.stt_start > 0:
            return (self.stt_end - self.stt_start) * 1000
        return 0

    @property
    def llm_duration_ms(self) -> float:
        """LLM processing time."""
        if self.llm_end > 0 and self.llm_start > 0:
            return (self.llm_end - self.llm_start) * 1000
        return 0

    def __repr__(self) -> str:
        return f"<UtteranceTask {self.id} state={self.state.name}>"
