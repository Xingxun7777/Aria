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

    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [BRIDGE] {msg}\n"
    # Guard for pythonw.exe (sys.stdout is None)
    if sys.stdout is not None:
        print(line.strip())
    try:
        with open(_BRIDGE_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


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
