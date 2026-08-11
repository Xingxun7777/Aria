"""
Hold-to-talk trigger state machine (pure, clock-injected)
=========================================================

Turns raw hotkey press/release events (with caller-supplied monotonic
timestamps, in milliseconds) into recording actions. No wall clock, no
threads, no timers — every transition is driven by an incoming event, so
the machine is fully deterministic and unit-testable with a mock clock.

Semantics (trigger_mode = "hold_to_talk"):

    state                 event    condition                     action           next state
    --------------------  -------  ----------------------------  ---------------  --------------------
    idle                  press    -                             start_recording  held
    idle                  release  (stray / lost press)          ignore           idle
    held                  press    (autorepeat, defensive)       ignore           held
    held                  release  held < TAP_MAX_MS  (tap)      toggle_lock      locked
    held                  release  held >= TAP_MAX_MS (hold)     stop_and_commit  idle
    locked                press    within DOUBLE_TAP_WINDOW_MS   ignore           locked_absorbed_press
                                   of entering locked
    locked                press    otherwise                     stop_and_commit  stopping
    locked                release  (stray)                       ignore           locked
    locked_absorbed_press release  -                             ignore           locked
    locked_absorbed_press press    (defensive)                   ignore           locked_absorbed_press
    stopping              release  -                             ignore           idle
    stopping              press    (defensive)                   ignore           stopping

Design decisions:

- Press starts recording immediately (never wait to disambiguate tap vs
  hold): the recording is already running either way, so first-word loss
  is impossible at this layer. The VAD pre-buffer inside the capture
  pipeline additionally covers the speech onset after stream start.
- A short tap (< TAP_MAX_MS) enters LOCKED continuous dictation. This is
  intentionally the same outcome as the legacy toggle press, so users
  keeping the old "tap to start" habit lose nothing.
- Double-tap is MERGED with single-tap semantics rather than kept as a
  distinct gesture: the first tap already locks, and a second press
  arriving within DOUBLE_TAP_WINDOW_MS of entering LOCKED is absorbed
  (ignored, including its release, regardless of how long it is held).
  Both mental models — "tap to lock" (legacy) and "double-tap to lock"
  (industry standard) — therefore land in LOCKED. Stopping a locked
  dictation requires a press at least DOUBLE_TAP_WINDOW_MS after locking.
"""

from __future__ import annotations

# Action strings returned by on_event(). The app layer maps these onto
# _start_recording / _stop_recording; "toggle_lock" is a semantic marker
# (recording simply continues) reserved for the UI/sound bridge hook.
ACTION_START_RECORDING = "start_recording"
ACTION_STOP_AND_COMMIT = "stop_and_commit"
ACTION_TOGGLE_LOCK = "toggle_lock"
ACTION_IGNORE = "ignore"

# States
STATE_IDLE = "idle"
STATE_HELD = "held"
STATE_LOCKED = "locked"
STATE_LOCKED_ABSORBED_PRESS = "locked_absorbed_press"
STATE_STOPPING = "stopping"

# Release earlier than this after press = "tap" (enter locked mode);
# at or beyond = "hold" (release commits).
DEFAULT_TAP_MAX_MS = 300.0
# A second press within this window of entering LOCKED is treated as the
# tail of a double-tap lock gesture and absorbed.
DEFAULT_DOUBLE_TAP_WINDOW_MS = 400.0

EVENT_PRESS = "press"
EVENT_RELEASE = "release"


class TriggerStateMachine:
    """Pure event -> action state machine for hold-to-talk triggering.

    Timestamps are caller-supplied milliseconds on a monotonic clock.
    """

    def __init__(
        self,
        tap_max_ms: float = DEFAULT_TAP_MAX_MS,
        double_tap_window_ms: float = DEFAULT_DOUBLE_TAP_WINDOW_MS,
    ):
        self.tap_max_ms = float(tap_max_ms)
        self.double_tap_window_ms = float(double_tap_window_ms)
        self.state = STATE_IDLE
        self._press_at_ms = 0.0
        self._locked_at_ms = 0.0

    def reset(self) -> None:
        """Force back to idle (e.g. recording failed to start / app override)."""
        self.state = STATE_IDLE
        self._press_at_ms = 0.0
        self._locked_at_ms = 0.0

    def sync_external_start(self, t_ms: float) -> None:
        """Recording was started OUTSIDE the press/release event flow.

        Deep-sleep wake auto-start (and any other programmatic start) begins
        a recording session without a press event. Aligning the machine to
        LOCKED makes the next press a stop_and_commit — without this, every
        subsequent press would try (and fail) to start a new recording,
        leaving the hotkey permanently unable to stop the session.
        """
        self.state = STATE_LOCKED
        self._press_at_ms = float(t_ms)
        self._locked_at_ms = float(t_ms)

    def on_event(self, kind: str, t_ms: float) -> str:
        """Feed one event; returns the action string to perform."""
        if kind == EVENT_PRESS:
            return self._on_press(float(t_ms))
        if kind == EVENT_RELEASE:
            return self._on_release(float(t_ms))
        return ACTION_IGNORE

    def _on_press(self, t_ms: float) -> str:
        if self.state == STATE_IDLE:
            self.state = STATE_HELD
            self._press_at_ms = t_ms
            return ACTION_START_RECORDING
        if self.state == STATE_LOCKED:
            if (t_ms - self._locked_at_ms) < self.double_tap_window_ms:
                # Tail of a double-tap lock gesture: absorb entirely.
                self.state = STATE_LOCKED_ABSORBED_PRESS
                return ACTION_IGNORE
            self.state = STATE_STOPPING
            return ACTION_STOP_AND_COMMIT
        # held / locked_absorbed_press / stopping: autorepeat or lost-event
        # artifacts — never act on them.
        return ACTION_IGNORE

    def _on_release(self, t_ms: float) -> str:
        if self.state == STATE_HELD:
            if (t_ms - self._press_at_ms) < self.tap_max_ms:
                self.state = STATE_LOCKED
                self._locked_at_ms = t_ms
                return ACTION_TOGGLE_LOCK
            self.state = STATE_IDLE
            return ACTION_STOP_AND_COMMIT
        if self.state == STATE_LOCKED_ABSORBED_PRESS:
            # Absorbed press ends: stay locked no matter how long it was held.
            self.state = STATE_LOCKED
            return ACTION_IGNORE
        if self.state == STATE_STOPPING:
            self.state = STATE_IDLE
            return ACTION_IGNORE
        # idle / locked: stray release.
        return ACTION_IGNORE
