# sound.py
# Sound effects module for VoiceType
# Uses Windows system sounds or generates simple beeps

import sys
from pathlib import Path
from typing import Optional


class SoundManager:
    """
    Manages sound effects for VoiceType UI feedback.

    Sound events:
    - start_recording: When ASR starts
    - stop_recording: When ASR stops
    - insert_complete: When text is inserted
    - lock: When ball is locked
    - unlock: When ball is unlocked
    - error: When an error occurs
    """

    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self._sounds_dir = Path(__file__).parent / "resources" / "sounds"
        self._use_qt_audio = False
        self._player = None

        # Try to initialize Qt multimedia (optional)
        try:
            from PySide6.QtMultimedia import QSoundEffect

            self._use_qt_audio = True
        except ImportError:
            pass

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value

    def play(self, event: str):
        """Play sound for the given event."""
        if not self._enabled:
            return

        # Map events to sounds
        sound_map = {
            "start_recording": self._play_start,
            "stop_recording": self._play_stop,
            "insert_complete": self._play_success,
            "lock": self._play_lock,
            "unlock": self._play_unlock,
            "error": self._play_error,
        }

        if event in sound_map:
            sound_map[event]()

    def _play_start(self):
        """Play start recording sound (short, subtle)."""
        self._beep(800, 50)  # High short beep

    def _play_stop(self):
        """Play stop recording sound (short, subtle)."""
        self._beep(600, 50)  # Lower short beep

    def _play_success(self):
        """Play success sound - DISABLED for silent operation."""
        pass  # No sound

    def _play_lock(self):
        """Play lock sound - DISABLED for silent operation."""
        pass  # No sound

    def _play_unlock(self):
        """Play unlock sound - DISABLED for silent operation."""
        pass  # No sound

    def _play_error(self):
        """Play error sound - DISABLED for silent operation."""
        pass  # No sound

    def _beep(self, frequency: int, duration: int):
        """Generate a beep sound."""
        if sys.platform == "win32":
            try:
                import winsound

                winsound.Beep(frequency, duration)
            except Exception:
                pass
        else:
            # On non-Windows, just print (could use other audio libs)
            print(f"\a", end="", flush=True)


# Global instance
_sound_manager: Optional[SoundManager] = None


def get_sound_manager() -> SoundManager:
    """Get or create the global sound manager."""
    global _sound_manager
    if _sound_manager is None:
        _sound_manager = SoundManager()
    return _sound_manager


def play_sound(event: str):
    """Convenience function to play a sound."""
    get_sound_manager().play(event)
