# sound.py
# Unified UI sound cues for Aria (design spec ui_spec.md section 2).
#
# Playback backend: Win32 PlaySound via ctypes/winmm with
# SND_MEMORY | SND_ASYNC | SND_NODEFAULT.
#
# Why not QSoundEffect: play() is called from backend worker threads (hotkey
# action worker, ASR worker) that run no Qt event loop; QSoundEffect is
# event-loop bound and QtMultimedia would add a heavyweight optional Qt module
# to the portable build. PlaySound is async at the OS level — it returns
# immediately and the system mixer thread does the work — needs no event loop
# and no DLL beyond winmm (always present).
#
# Why ctypes instead of the winsound module: CPython's winsound.PlaySound
# refuses the SND_MEMORY|SND_ASYNC combination (it cannot guarantee the buffer
# outlives the call); we manage that lifetime ourselves by holding a reference
# to the playing buffer. In-memory playback keeps disk I/O off the hot path
# (recording start) and lets us bake the configured volume into the PCM data —
# PlaySound itself has no volume API.
#
# Fallback chain per event: wav asset -> legacy winsound.Beep square wave
# (what app.py used to emit directly). Missing files therefore degrade to the
# exact pre-existing behaviour instead of silence.

import collections
import sys
import threading
from pathlib import Path
from typing import Optional

_SND_ASYNC = 0x0001
_SND_NODEFAULT = 0x0002
_SND_MEMORY = 0x0004

_UNSET = object()


class SoundManager:
    """
    Manages sound effects for Aria UI feedback.

    Sound events (aligned with the ui_spec section 2.2 state matrix):
    - start_recording: recording begins (also selection-listening entry)
    - stop_recording: recording ends / transcription starts
    - error: a committed segment was lost with no rescue running
    - rescue: cloud rescue launched for a lost segment (soft, "still working")
    - lock / unlock: dictation lock latch (existing asset)
    - reminder: reminder notification fired
    - insert_complete: silent by design (success must not chirp)

    Gating (all must pass for any sound, including beep fallbacks):
    - enabled: persistent config switch (config sound.enabled)
    - muted: runtime tray-mute, session-scoped, never persisted
    - quiet_in_whisper: auto-silence while capture_mode == "whisper"
      (whisper mode exists for quiet night-time use; chirping there would
      defeat its purpose)
    """

    _EVENT_FILES = {
        "start_recording": "start.wav",
        "stop_recording": "stop.wav",
        "error": "error.wav",
        "rescue": "rescue.wav",
        "lock": "lock.wav",
        "unlock": "lock.wav",
        "reminder": "reminder.wav",
    }

    # Legacy square-wave tones app.py used to emit directly; kept as the
    # missing-asset fallback so behaviour never regresses below the old one.
    _EVENT_BEEPS = {
        "start_recording": (800, 50),
        "stop_recording": (400, 50),
        "error": (300, 120),
        "rescue": (300, 120),
        "lock": (600, 50),
        "unlock": (600, 50),
    }

    # Events that are intentionally silent (success feedback stays visual).
    _SILENT_EVENTS = frozenset({"insert_complete"})

    def __init__(self, enabled: bool = True, sounds_dir=None):
        self._enabled = bool(enabled)
        self._muted = False
        self._volume = 1.0
        self._quiet_in_whisper = True
        self._capture_mode = "standard"
        self._sounds_dir = (
            Path(sounds_dir)
            if sounds_dir is not None
            else Path(__file__).parent / "resources" / "sounds"
        )
        self._lock = threading.Lock()
        # filename -> (volume_when_rendered, wav_bytes)
        self._pcm_cache: dict = {}
        # Keep SND_MEMORY buffers alive while the mixer may read them. A ring
        # (rather than a single ref) makes concurrent play() calls from
        # different worker threads safe: a buffer stays pinned until 15 newer
        # sounds have started, far longer than any cue's <=150ms life. 16 slots
        # of <=50KB cues cost <1MB; a smaller ring risked evicting (and thus
        # freeing) a buffer the OS mixer was still reading asynchronously.
        self._playing_refs = collections.deque(maxlen=16)
        self._winmm = None

    # --- configuration -----------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = bool(value)

    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, value: bool):
        self._muted = bool(value)

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, value):
        try:
            vol = float(value)
        except (TypeError, ValueError):
            vol = 1.0
        self._volume = max(0.0, min(1.0, vol))

    @property
    def quiet_in_whisper(self) -> bool:
        return self._quiet_in_whisper

    @quiet_in_whisper.setter
    def quiet_in_whisper(self, value: bool):
        self._quiet_in_whisper = bool(value)

    def set_capture_mode(self, mode: str):
        """Track the audio capture mode for the whisper auto-quiet rule."""
        self._capture_mode = str(mode or "standard")

    def configure(self, enabled=_UNSET, volume=_UNSET, quiet_in_whisper=_UNSET):
        """Apply the config file's sound block (unset args stay unchanged)."""
        if enabled is not _UNSET:
            self.enabled = enabled
        if volume is not _UNSET:
            self.volume = volume
        if quiet_in_whisper is not _UNSET:
            self.quiet_in_whisper = quiet_in_whisper

    def is_audible(self) -> bool:
        """True when cue playback is currently allowed."""
        if not self._enabled or self._muted:
            return False
        if self._quiet_in_whisper and self._capture_mode == "whisper":
            return False
        return True

    # --- playback ----------------------------------------------------------

    def play(self, event: str):
        """Play the cue for an event. Never blocks, never raises."""
        try:
            if event in self._SILENT_EVENTS:
                return
            if not self.is_audible():
                return
            filename = self._EVENT_FILES.get(event)
            if filename is None:
                return
            if self._play_event_wav(filename):
                return
            # Asset missing/unplayable -> legacy fallback tones.
            if event == "reminder":
                self._reminder_chime_async()
                return
            beep = self._EVENT_BEEPS.get(event)
            if beep:
                self._beep_async(*beep)
        except Exception:
            # Sound is decoration; it must never take the pipeline down.
            pass

    def _play_event_wav(self, filename: str) -> bool:
        buf = self._load_pcm(filename)
        if buf is None:
            return False
        return self._play_bytes(buf)

    def _load_pcm(self, filename: str) -> Optional[bytes]:
        """Load a wav asset with the current volume baked in (cached)."""
        volume = self._volume
        with self._lock:
            cached = self._pcm_cache.get(filename)
            if cached is not None and cached[0] == volume:
                return cached[1]
        path = self._sounds_dir / filename
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        if volume < 0.999:
            try:
                raw = self._scale_wav_volume(raw, volume)
            except Exception:
                pass  # play unscaled rather than not at all
        with self._lock:
            self._pcm_cache[filename] = (volume, raw)
        return raw

    @staticmethod
    def _scale_wav_volume(raw: bytes, volume: float) -> bytes:
        """Rewrite a 16-bit PCM wav with scaled amplitude."""
        import io
        import wave

        with wave.open(io.BytesIO(raw), "rb") as r:
            params = r.getparams()
            frames = r.readframes(r.getnframes())
        if params.sampwidth != 2:
            return raw

        import numpy as np

        pcm = np.frombuffer(frames, dtype="<i2").astype(np.float32) * volume
        pcm = np.clip(np.round(pcm), -32768, 32767).astype("<i2")
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setparams(params)
            w.writeframes(pcm.tobytes())
        return out.getvalue()

    def _play_bytes(self, buf: bytes) -> bool:
        """Async in-memory playback via winmm PlaySound. Returns success."""
        if sys.platform != "win32":
            return False
        try:
            import ctypes

            if self._winmm is None:
                self._winmm = ctypes.WinDLL("winmm")
                self._winmm.PlaySoundA.argtypes = [
                    ctypes.c_char_p,
                    ctypes.c_void_p,
                    ctypes.c_uint32,
                ]
                self._winmm.PlaySoundA.restype = ctypes.c_int
            # Play from a private copy: the object handed to the OS is owned
            # solely by this ring, so no cache re-render (volume change) or
            # caller-side reuse can ever touch memory the mixer is reading.
            # bytes(memoryview(...)) forces a real copy (bytes(b) would alias);
            # cues are <=50KB so the copy is negligible.
            buf = bytes(memoryview(buf))
            self._playing_refs.append(buf)  # pin before the mixer sees it
            ok = self._winmm.PlaySoundA(
                buf, None, _SND_MEMORY | _SND_ASYNC | _SND_NODEFAULT
            )
            return bool(ok)
        except Exception:
            return False

    def _beep_async(self, frequency: int, duration: int):
        """Legacy square-wave fallback; Beep blocks, so run it off-thread."""
        if sys.platform == "win32":
            try:
                import winsound

                threading.Thread(
                    target=winsound.Beep,
                    args=(int(frequency), int(duration)),
                    daemon=True,
                ).start()
            except Exception:
                pass
        else:
            print("\a", end="", flush=True)

    def _reminder_chime_async(self):
        """Two-tone ascending chime fallback for reminders (no wav asset)."""
        if sys.platform != "win32":
            return
        try:
            import winsound

            def _chime():
                winsound.Beep(784, 150)  # G5
                winsound.Beep(1047, 200)  # C6

            threading.Thread(target=_chime, daemon=True).start()
        except Exception:
            pass


# Global instance
_sound_manager: Optional[SoundManager] = None
_sound_manager_lock = threading.Lock()


def get_sound_manager() -> SoundManager:
    """Get or create the global sound manager (thread-safe)."""
    global _sound_manager
    if _sound_manager is None:
        with _sound_manager_lock:
            if _sound_manager is None:
                _sound_manager = SoundManager()
    return _sound_manager


def play_sound(event: str):
    """Convenience function to play a sound."""
    get_sound_manager().play(event)
