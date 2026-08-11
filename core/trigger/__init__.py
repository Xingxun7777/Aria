"""Trigger semantics for the recording hotkey (toggle / hold-to-talk)."""

from .state_machine import (
    ACTION_IGNORE,
    ACTION_START_RECORDING,
    ACTION_STOP_AND_COMMIT,
    ACTION_TOGGLE_LOCK,
    TriggerStateMachine,
)

__all__ = [
    "ACTION_IGNORE",
    "ACTION_START_RECORDING",
    "ACTION_STOP_AND_COMMIT",
    "ACTION_TOGGLE_LOCK",
    "TriggerStateMachine",
]
