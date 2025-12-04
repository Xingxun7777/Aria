# bridge.py
# Thread-safe signal bridge between backend and Qt UI
# Based on F3 spec section 4.1 with Codex-recommended thread safety

from PySide6.QtCore import QObject, Signal, Slot, QMetaObject, Qt, Q_ARG

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

    def __init__(self):
        super().__init__()

    # --- Thread-safe emitters (call from any thread) ---

    def emit_state(self, state: str):
        """Thread-safe state change emission."""
        QMetaObject.invokeMethod(
            self, "_do_emit_state",
            Qt.QueuedConnection,
            Q_ARG(str, state)
        )

    def emit_text(self, text: str, is_final: bool):
        """Thread-safe text update emission."""
        QMetaObject.invokeMethod(
            self, "_do_emit_text",
            Qt.QueuedConnection,
            Q_ARG(str, text),
            Q_ARG(bool, is_final)
        )

    def emit_level(self, level: float):
        """Thread-safe level change emission."""
        QMetaObject.invokeMethod(
            self, "_do_emit_level",
            Qt.QueuedConnection,
            Q_ARG(float, level)
        )

    def emit_error(self, message: str):
        """Thread-safe error emission."""
        QMetaObject.invokeMethod(
            self, "_do_emit_error",
            Qt.QueuedConnection,
            Q_ARG(str, message)
        )

    def emit_insert_complete(self):
        """Thread-safe insert complete emission."""
        QMetaObject.invokeMethod(
            self, "_do_emit_insert_complete",
            Qt.QueuedConnection
        )

    def emit_voice_activity(self, is_speaking: bool):
        """Thread-safe voice activity emission."""
        QMetaObject.invokeMethod(
            self, "_do_emit_voice_activity",
            Qt.QueuedConnection,
            Q_ARG(bool, is_speaking)
        )

    # --- Internal slots (must be called on main thread) ---

    @Slot(str)
    def _do_emit_state(self, state: str):
        self.stateChanged.emit(state)

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
