"""
Streaming Display Module
========================
Implements "乐观UI" (Optimistic UI) for real-time transcription display.

Key concepts from 2025 market report:
- Show interim results immediately (gray text)
- Convert to final (black text) when confirmed
- User perceives instant response even with processing delay
"""

from dataclasses import dataclass, field
from typing import Optional, List, Callable
from enum import Enum, auto
import time
import threading
import queue

from ..core.asr.base import ASRResult, TranscriptType


class DisplayState(Enum):
    """Display states for transcription."""
    IDLE = auto()           # Not recording
    LISTENING = auto()      # Recording, waiting for speech
    TRANSCRIBING = auto()   # Processing speech
    SHOWING_INTERIM = auto() # Showing interim result
    SHOWING_FINAL = auto()   # Showing final result
    READY_TO_INSERT = auto() # Ready to insert into target


@dataclass
class TranscriptSegment:
    """A segment of transcribed text with display state."""
    text: str
    is_final: bool = False
    confidence: float = 1.0
    timestamp: float = field(default_factory=time.time)

    @property
    def display_style(self) -> str:
        """CSS-like style hint for rendering."""
        if self.is_final:
            return "final"  # Solid black text
        elif self.confidence > 0.7:
            return "interim-high"  # Dark gray
        else:
            return "interim-low"  # Light gray


@dataclass
class DisplayBuffer:
    """
    Buffer for streaming transcription display.

    Manages the transition from interim to final text,
    supporting the optimistic UI pattern.
    """
    segments: List[TranscriptSegment] = field(default_factory=list)
    state: DisplayState = DisplayState.IDLE
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # Callbacks
    _on_update: Optional[Callable[[str, bool], None]] = None
    _on_state_change: Optional[Callable[[DisplayState], None]] = None

    def set_callbacks(
        self,
        on_update: Optional[Callable[[str, bool], None]] = None,
        on_state_change: Optional[Callable[[DisplayState], None]] = None
    ) -> None:
        """
        Set callback functions.

        Args:
            on_update: Called when text changes. Args: (text, is_final)
            on_state_change: Called when state changes. Args: (new_state)
        """
        self._on_update = on_update
        self._on_state_change = on_state_change

    def _change_state(self, new_state: DisplayState) -> None:
        """Change state and notify callback."""
        if self.state != new_state:
            self.state = new_state
            if self._on_state_change:
                self._on_state_change(new_state)

    def start_listening(self) -> None:
        """Called when recording starts."""
        with self._lock:
            self.segments.clear()
            self._change_state(DisplayState.LISTENING)

    def start_transcribing(self) -> None:
        """Called when speech is detected and transcription begins."""
        with self._lock:
            self._change_state(DisplayState.TRANSCRIBING)

    def add_interim(self, text: str, confidence: float = 0.8) -> None:
        """
        Add or update interim transcription.

        Interim text replaces previous interim but preserves final text.
        """
        with self._lock:
            # Remove previous interim segments
            self.segments = [s for s in self.segments if s.is_final]

            # Add new interim
            if text.strip():
                self.segments.append(TranscriptSegment(
                    text=text.strip(),
                    is_final=False,
                    confidence=confidence
                ))

            self._change_state(DisplayState.SHOWING_INTERIM)
            self._notify_update()

    def add_final(self, text: str) -> None:
        """
        Add final (confirmed) transcription.

        Replaces any interim text with final version.
        """
        with self._lock:
            # Remove interim segments
            self.segments = [s for s in self.segments if s.is_final]

            # Add final
            if text.strip():
                self.segments.append(TranscriptSegment(
                    text=text.strip(),
                    is_final=True,
                    confidence=1.0
                ))

            self._change_state(DisplayState.SHOWING_FINAL)
            self._notify_update()

    def update_from_result(self, result: ASRResult) -> None:
        """Update display from ASR result."""
        if result.is_interim:
            self.add_interim(result.text, result.confidence)
        else:
            self.add_final(result.text)

    def mark_ready(self) -> None:
        """Mark transcription as ready to insert."""
        with self._lock:
            self._change_state(DisplayState.READY_TO_INSERT)

    def clear(self) -> None:
        """Clear all segments and reset to idle."""
        with self._lock:
            self.segments.clear()
            self._change_state(DisplayState.IDLE)
            self._notify_update()

    def _notify_update(self) -> None:
        """Notify callback of text update."""
        if self._on_update:
            text = self.get_full_text()
            is_final = all(s.is_final for s in self.segments) if self.segments else False
            self._on_update(text, is_final)

    def get_full_text(self) -> str:
        """Get concatenated text from all segments."""
        return " ".join(s.text for s in self.segments if s.text)

    def get_final_text(self) -> str:
        """Get only the final (confirmed) text."""
        return " ".join(s.text for s in self.segments if s.is_final and s.text)

    def get_display_segments(self) -> List[dict]:
        """
        Get segments for rendering with style hints.

        Returns:
            List of dicts with 'text', 'style', 'is_final' keys
        """
        return [
            {
                'text': s.text,
                'style': s.display_style,
                'is_final': s.is_final,
                'confidence': s.confidence
            }
            for s in self.segments
        ]

    @property
    def has_content(self) -> bool:
        """Check if there's any content to display."""
        return bool(self.segments)

    @property
    def is_complete(self) -> bool:
        """Check if all segments are final."""
        return self.segments and all(s.is_final for s in self.segments)


class StreamingTranscriptionManager:
    """
    Manages the full streaming transcription workflow.

    Coordinates:
    - Audio capture with VAD
    - ASR engine (in separate worker thread)
    - Display buffer for UI

    Threading model:
    - Audio callback thread: captures audio, runs VAD, enqueues speech segments
    - ASR worker thread: dequeues speech, runs transcription, updates display
    - Main/UI thread: receives display updates via callbacks

    Usage:
        manager = StreamingTranscriptionManager()
        manager.set_on_text_update(lambda text, final: update_ui(text, final))
        manager.start()  # Start listening
        ...
        text = manager.stop()  # Stop and get final text
    """

    def __init__(self):
        self.display = DisplayBuffer()
        self._is_running = False

        # Components (set externally)
        self._audio_capture = None
        self._asr_engine = None

        # ASR worker thread components
        self._asr_queue: queue.Queue = queue.Queue(maxsize=10)
        self._asr_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def set_components(self, audio_capture, asr_engine) -> None:
        """Set audio capture and ASR engine."""
        self._audio_capture = audio_capture
        self._asr_engine = asr_engine

    def set_on_text_update(self, callback: Callable[[str, bool], None]) -> None:
        """Set callback for text updates."""
        self.display.set_callbacks(on_update=callback)

    def set_on_state_change(self, callback: Callable[[DisplayState], None]) -> None:
        """Set callback for state changes."""
        self.display._on_state_change = callback

    def _asr_worker(self) -> None:
        """
        Worker thread for ASR processing.

        Runs in separate thread to avoid blocking audio callback.
        """
        while not self._stop_event.is_set():
            try:
                # Wait for speech segment with timeout
                audio = self._asr_queue.get(timeout=0.1)

                if audio is None:  # Poison pill to stop
                    break

                # Run transcription (this is the slow part, ~400ms+)
                result = self._asr_engine.transcribe(audio)

                # Update display (thread-safe via DisplayBuffer's lock)
                self.display.update_from_result(result)
                self.display.mark_ready()

            except queue.Empty:
                continue
            except Exception as e:
                # Log error but keep worker running
                import traceback
                traceback.print_exc()

    def start(self) -> bool:
        """Start streaming transcription."""
        if not self._audio_capture or not self._asr_engine:
            return False

        self._is_running = True
        self._stop_event.clear()

        # Clear queue
        while not self._asr_queue.empty():
            try:
                self._asr_queue.get_nowait()
            except queue.Empty:
                break

        self.display.start_listening()

        # Start ASR worker thread
        self._asr_thread = threading.Thread(
            target=self._asr_worker,
            name="ASR-Worker",
            daemon=True
        )
        self._asr_thread.start()

        # Setup audio callbacks (these run in audio callback thread)
        def on_speech_start():
            self.display.start_transcribing()

        def on_speech_end(audio):
            # Non-blocking: just enqueue for ASR worker
            try:
                self._asr_queue.put_nowait(audio)
            except queue.Full:
                # Drop if queue full (backpressure)
                pass

        def on_speech_chunk(chunk, prob):
            # Could implement real-time partial transcription here
            # For now, just indicate we're listening
            pass

        self._audio_capture.set_callbacks(
            on_speech_start=on_speech_start,
            on_speech_end=on_speech_end,
            on_speech_chunk=on_speech_chunk
        )

        return self._audio_capture.start()

    def stop(self) -> str:
        """Stop transcription and return final text."""
        self._is_running = False

        if self._audio_capture:
            final_audio = self._audio_capture.stop()

            # Process any remaining audio through the worker
            if final_audio is not None and len(final_audio) > 1600:  # >100ms
                try:
                    self._asr_queue.put(final_audio, timeout=1.0)
                except queue.Full:
                    pass

        # Stop ASR worker
        self._stop_event.set()
        if self._asr_thread and self._asr_thread.is_alive():
            # Send poison pill
            try:
                self._asr_queue.put_nowait(None)
            except queue.Full:
                pass
            self._asr_thread.join(timeout=5.0)

        # Wait briefly for final transcription to complete
        time.sleep(0.5)

        return self.display.get_final_text()

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def current_text(self) -> str:
        return self.display.get_full_text()
