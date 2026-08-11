# bridge.py
# Thread-safe signal bridge between backend and Qt UI
# Based on F3 spec section 4.1 with Thread-safe signal bridge
# v1.1: Added action-driven architecture support

from pathlib import Path
from typing import TYPE_CHECKING
from queue import Queue

from PySide6.QtCore import QObject, Signal, Slot, QMetaObject, Qt, Q_ARG

if TYPE_CHECKING:
    from aria.core.action import UIAction

# Debug log for bridge signals
_BRIDGE_LOG = Path(__file__).parent.parent.parent / "DebugLog" / "wakeword_debug.log"


def _blog(msg: str):
    """Write bridge debug message to file (pythonw.exe safe)."""
    import datetime
    import sys

    from core.debug import append_log_line

    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [BRIDGE] {msg}"
    # Guard for pythonw.exe (sys.stdout is None)
    if sys.stdout is not None:
        print(line)
    append_log_line(_BRIDGE_LOG, line)


class QtBridge(QObject):
    """
    Thread-safe bridge for backend -> UI communication.

    All emit_* methods are safe to call from any thread.
    They use QMetaObject.invokeMethod with QueuedConnection to ensure
    signals are emitted on the Qt main thread.
    """

    # State: "IDLE", "RECORDING", "TRANSCRIBING"
    stateChanged = Signal(str)

    # Text update: (text, is_final)
    textUpdated = Signal(str, bool)

    # Audio level: 0.0 - 1.0
    levelChanged = Signal(float)

    # Voice activity detected (VAD): is_speaking
    voiceActivity = Signal(bool)

    # Error message
    error = Signal(str)

    # Insert complete notification
    insertComplete = Signal()

    # Command executed: (command_id, success)
    commandExecuted = Signal(str, bool)

    # Setting changed: (setting_name, value)
    # For UI sync when backend changes settings (e.g., via wakeword)
    settingChanged = Signal(str, bool)

    # v1.1: Action-driven UI updates
    # Emits UIAction objects (TranslationAction, ChatAction, etc.)
    actionTriggered = Signal(object)

    # Highlight saved: (text_preview, tags)
    highlightSaved = Signal(str, list)

    # Slow pipeline stage: "gpu" or "api" (triggers ball glow indicator)
    slowStage = Signal(str)

    # Polish API failover status as JSON string
    apiStatusChanged = Signal(str)

    # ASR runtime status as JSON string
    asrStatusChanged = Signal(str)

    # ASR final-segment failure (timeout / empty): triggers a short error
    # flash on the floating ball. Payload is the failure phase ("final").
    asrFailure = Signal(str)

    # Quiet leveled notice near the floating ball:
    # (message, level, duration_ms), level in info/success/warning/error.
    notice = Signal(str, str, int)

    # Editable fallback for text that could not be delivered:
    # (full_text, delivery_status). Kept local and queued to the UI thread.
    draftRequested = Signal(str, str)

    # Open the local explicit-correction management window.  The signal
    # carries no rule operands; the UI reads them directly from the backend.
    correctionRulesRequested = Signal()

    # Update available: (local_version, remote_version)
    updateAvailable = Signal(str, str)

    def __init__(self):
        super().__init__()
        # Thread-safe queue for passing UIAction objects
        # (Q_ARG doesn't support arbitrary Python objects in PySide6)
        self._action_queue: Queue = Queue()

    # --- Thread-safe emitters (call from any thread) ---

    def emit_state(self, state: str):
        """Thread-safe state change emission."""
        QMetaObject.invokeMethod(
            self, "_do_emit_state", Qt.QueuedConnection, Q_ARG(str, state)
        )

    def emit_text(self, text: str, is_final: bool):
        """Thread-safe text update emission."""
        QMetaObject.invokeMethod(
            self,
            "_do_emit_text",
            Qt.QueuedConnection,
            Q_ARG(str, text),
            Q_ARG(bool, is_final),
        )

    def emit_level(self, level: float):
        """Thread-safe level change emission."""
        QMetaObject.invokeMethod(
            self, "_do_emit_level", Qt.QueuedConnection, Q_ARG(float, level)
        )

    def emit_error(self, message: str):
        """Thread-safe error emission."""
        QMetaObject.invokeMethod(
            self, "_do_emit_error", Qt.QueuedConnection, Q_ARG(str, message)
        )

    def emit_insert_complete(self):
        """Thread-safe insert complete emission."""
        QMetaObject.invokeMethod(self, "_do_emit_insert_complete", Qt.QueuedConnection)

    def emit_voice_activity(self, is_speaking: bool):
        """Thread-safe voice activity emission."""
        QMetaObject.invokeMethod(
            self,
            "_do_emit_voice_activity",
            Qt.QueuedConnection,
            Q_ARG(bool, is_speaking),
        )

    def emit_command(self, command_id: str, success: bool):
        """Thread-safe command execution emission."""
        QMetaObject.invokeMethod(
            self,
            "_do_emit_command",
            Qt.QueuedConnection,
            Q_ARG(str, command_id),
            Q_ARG(bool, success),
        )

    def emit_setting_changed(self, setting: str, value: bool):
        """Thread-safe setting change emission (for wakeword commands)."""
        QMetaObject.invokeMethod(
            self,
            "_do_emit_setting_changed",
            Qt.QueuedConnection,
            Q_ARG(str, setting),
            Q_ARG(bool, value),
        )

    def emit_action(self, action: "UIAction"):
        """
        Thread-safe action emission for v1.1 action-driven architecture.

        Args:
            action: UIAction subclass (TranslationAction, ChatAction, etc.)
        """
        # Put action in queue (thread-safe), then trigger slot on main thread
        self._action_queue.put(action)
        QMetaObject.invokeMethod(
            self,
            "_do_emit_action",
            Qt.QueuedConnection,
        )

    def emit_highlight_saved(self, text_preview: str, tags: list):
        """Thread-safe highlight saved emission for gold flash UI feedback."""
        # Use action queue pattern since Q_ARG doesn't support list
        self._action_queue.put(("highlight", text_preview, tags))
        QMetaObject.invokeMethod(
            self,
            "_do_emit_highlight_saved",
            Qt.QueuedConnection,
        )

    def emit_slow_stage(self, stage: str):
        """Thread-safe slow stage indicator. stage: 'gpu' or 'api'."""
        QMetaObject.invokeMethod(
            self, "_do_emit_slow_stage", Qt.QueuedConnection, Q_ARG(str, stage)
        )

    def emit_api_status(self, status_json: str):
        """Thread-safe Polish API status emission."""
        QMetaObject.invokeMethod(
            self,
            "_do_emit_api_status",
            Qt.QueuedConnection,
            Q_ARG(str, status_json),
        )

    def emit_asr_status(self, status_json: str):
        """Thread-safe ASR runtime status emission."""
        QMetaObject.invokeMethod(
            self,
            "_do_emit_asr_status",
            Qt.QueuedConnection,
            Q_ARG(str, status_json),
        )

    def emit_update_available(self, local_ver: str, remote_ver: str):
        """Thread-safe update notification."""
        QMetaObject.invokeMethod(
            self,
            "_do_emit_update_available",
            Qt.QueuedConnection,
            Q_ARG(str, local_ver),
            Q_ARG(str, remote_ver),
        )

    def emit_asr_failure(self, phase: str):
        """Thread-safe ASR final-segment failure emission."""
        QMetaObject.invokeMethod(
            self,
            "_do_emit_asr_failure",
            Qt.QueuedConnection,
            Q_ARG(str, phase),
        )

    def emit_notice(self, message: str, level: str = "info", duration_ms: int = 2200):
        """Thread-safe leveled quiet-notice emission."""
        QMetaObject.invokeMethod(
            self,
            "_do_emit_notice",
            Qt.QueuedConnection,
            Q_ARG(str, message),
            Q_ARG(str, level),
            Q_ARG(int, int(duration_ms)),
        )

    def emit_draft(self, text: str, reason: str = "failed"):
        """Thread-safe Draft Box request after an output transaction failure."""
        QMetaObject.invokeMethod(
            self,
            "_do_emit_draft",
            Qt.QueuedConnection,
            Q_ARG(str, text),
            Q_ARG(str, reason),
        )

    def emit_correction_rules_requested(self):
        """Thread-safe request to open the correction-rule manager."""

        QMetaObject.invokeMethod(
            self,
            "_do_emit_correction_rules_requested",
            Qt.QueuedConnection,
        )

    # --- Internal slots (must be called on main thread) ---

    @Slot(str)
    def _do_emit_state(self, state: str):
        _blog(f"_do_emit_state: '{state}'")
        self.stateChanged.emit(state)
        _blog(f"stateChanged.emit('{state}') done")

    @Slot(str, bool)
    def _do_emit_text(self, text: str, is_final: bool):
        self.textUpdated.emit(text, is_final)

    @Slot(float)
    def _do_emit_level(self, level: float):
        self.levelChanged.emit(level)

    @Slot(str)
    def _do_emit_error(self, message: str):
        self.error.emit(message)

    @Slot()
    def _do_emit_insert_complete(self):
        self.insertComplete.emit()

    @Slot(bool)
    def _do_emit_voice_activity(self, is_speaking: bool):
        self.voiceActivity.emit(is_speaking)

    @Slot(str, bool)
    def _do_emit_command(self, command_id: str, success: bool):
        self.commandExecuted.emit(command_id, success)

    @Slot(str, bool)
    def _do_emit_setting_changed(self, setting: str, value: bool):
        _blog(f"_do_emit_setting_changed: '{setting}' = {value}")
        self.settingChanged.emit(setting, value)
        _blog(f"settingChanged.emit('{setting}', {value}) done")

    @Slot(str, str)
    def _do_emit_update_available(self, local_ver: str, remote_ver: str):
        self.updateAvailable.emit(local_ver, remote_ver)

    @Slot()
    def _do_emit_action(self):
        """Process action from queue and emit signal."""
        try:
            action = self._action_queue.get_nowait()
            _blog(f"_do_emit_action: type={action.type}, id={action.request_id}")
            self.actionTriggered.emit(action)
            _blog(f"actionTriggered.emit({action.type}) done")
        except Exception as e:
            _blog(f"_do_emit_action error: {e}")

    @Slot()
    def _do_emit_highlight_saved(self):
        """Process highlight from queue and emit signal."""
        try:
            data = self._action_queue.get_nowait()
            if isinstance(data, tuple) and len(data) == 3 and data[0] == "highlight":
                _, text_preview, tags = data
                _blog(f"_do_emit_highlight_saved: '{text_preview}', tags={tags}")
                self.highlightSaved.emit(text_preview, tags)
                _blog(f"highlightSaved.emit done")
        except Exception as e:
            _blog(f"_do_emit_highlight_saved error: {e}")

    @Slot(str)
    def _do_emit_slow_stage(self, stage: str):
        _blog(f"_do_emit_slow_stage: '{stage}'")
        self.slowStage.emit(stage)

    @Slot(str)
    def _do_emit_api_status(self, status_json: str):
        self.apiStatusChanged.emit(status_json)

    @Slot(str)
    def _do_emit_asr_status(self, status_json: str):
        self.asrStatusChanged.emit(status_json)

    @Slot(str)
    def _do_emit_asr_failure(self, phase: str):
        _blog(f"_do_emit_asr_failure: '{phase}'")
        self.asrFailure.emit(phase)

    @Slot(str, str, int)
    def _do_emit_notice(self, message: str, level: str, duration_ms: int):
        _blog(f"_do_emit_notice: [{level}] '{message}'")
        self.notice.emit(message, level, duration_ms)

    @Slot(str, str)
    def _do_emit_draft(self, text: str, reason: str):
        _blog(f"_do_emit_draft: reason={reason}, chars={len(text)}")
        self.draftRequested.emit(text, reason)

    @Slot()
    def _do_emit_correction_rules_requested(self):
        self.correctionRulesRequested.emit()
