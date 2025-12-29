# mock_backend.py
# Mock backend for testing Qt frontend without real ASR/TTS

from PySide6.QtCore import QTimer


class MockBackend:
    """
    Mock backend for testing Qt frontend.
    Simulates recording and transcription flow.
    """

    def __init__(self, bridge):
        self.bridge = bridge
        self.is_recording = False
        print('[MockBackend] Initialized')

    def toggle_recording(self):
        """Toggle recording state and emit mock events."""
        print(f'[MockBackend] toggle_recording called, was_recording={self.is_recording}')
        self.is_recording = not self.is_recording

        if self.is_recording:
            print('[MockBackend] Starting recording, emitting RECORDING state')
            self.bridge.emit_state('RECORDING')
            # Simulate streaming text
            QTimer.singleShot(500, lambda: self.bridge.emit_text('Aria', False))
            QTimer.singleShot(1000, lambda: self.bridge.emit_text('Aria demo', False))
            QTimer.singleShot(1500, lambda: self.bridge.emit_text('Aria demo mode', False))
        else:
            print('[MockBackend] Stopping recording, emitting TRANSCRIBING state')
            self.bridge.emit_state('TRANSCRIBING')
            # Simulate polishing
            QTimer.singleShot(800, lambda: self.bridge.emit_text('Aria Demo Mode is working.', True))
            QTimer.singleShot(1500, lambda: self._finish_transcription())

    def _finish_transcription(self):
        """Complete transcription and return to idle."""
        print('[MockBackend] Finishing transcription, emitting IDLE state')
        self.bridge.emit_state('IDLE')
        self.bridge.emit_insert_complete()

    def start(self):
        """Mock start - no-op for demo."""
        pass

    def stop(self):
        """Mock stop - no-op for demo."""
        pass

    def reload_config(self):
        """Mock reload - no-op for demo."""
        pass
