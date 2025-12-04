# VoiceType Qt Frontend
# PySide6-based GUI components

from .bridge import QtBridge
from .floating_ball import FloatingBall
from .settings import SettingsWindow, get_audio_input_devices
from .sound import SoundManager, play_sound
from .history import HistoryWindow
from .mock_backend import MockBackend
from .main import main

# Legacy components (kept for F3 branch compatibility)
from .overlay import RecordingOverlay
from .tray import SystemTray

__all__ = [
    'QtBridge',
    'FloatingBall',
    'SettingsWindow',
    'SoundManager',
    'play_sound',
    'HistoryWindow',
    'MockBackend',
    'get_audio_input_devices',
    'main',
    # Legacy
    'RecordingOverlay',
    'SystemTray',
]
