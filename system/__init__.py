"""System integration layer - hotkeys, focus management, device monitoring."""

from .hotkey import HotkeyManager, Modifiers, VK_CODES
from .output import OutputInjector, OutputConfig
from .target_surface import SurfaceKind, TargetSnapshot, TargetSurfaceProfile
from .word_adapter import WordAdapter, WordBookmark, WordInsertResult, WordInsertStatus
from .terminal_adapter import (
    PasteChord,
    TerminalAdapter,
    TerminalDeliveryPlan,
    TerminalDeliveryProfile,
    TerminalRecentTextBookmark,
    TerminalTailAnchor,
    TerminalTailProbeResult,
    TerminalTailProbeStatus,
    TerminalMode,
    capture_terminal_tail_guard,
    terminal_backspace_units,
    verify_terminal_tail_anchor,
)
from .game_chat_adapter import (
    GameChatAdapter,
    GameChatDeliveryPlan,
    GameChatProfile,
    GameChatTransport,
)
from .standard_text_adapter import (
    StandardTextAdapter,
    StandardTextEditResult,
    StandardTextEditStatus,
    StandardTextSelectionBookmark,
    StandardTextSelectionCapture,
    Win32StandardTextBackend,
)
from .selection_transaction import (
    SelectionCaptureResult,
    SelectionReplaceResult,
    SelectionTransactionStatus,
)

__all__ = [
    'HotkeyManager',
    'Modifiers',
    'VK_CODES',
    'OutputInjector',
    'OutputConfig',
    'SurfaceKind',
    'TargetSnapshot',
    'TargetSurfaceProfile',
    'WordAdapter',
    'WordBookmark',
    'WordInsertResult',
    'WordInsertStatus',
    'PasteChord',
    'TerminalAdapter',
    'TerminalDeliveryPlan',
    'TerminalDeliveryProfile',
    'TerminalRecentTextBookmark',
    'TerminalTailAnchor',
    'TerminalTailProbeResult',
    'TerminalTailProbeStatus',
    'TerminalMode',
    'capture_terminal_tail_guard',
    'terminal_backspace_units',
    'verify_terminal_tail_anchor',
    'GameChatAdapter',
    'GameChatDeliveryPlan',
    'GameChatProfile',
    'GameChatTransport',
    'StandardTextAdapter',
    'StandardTextEditResult',
    'StandardTextEditStatus',
    'StandardTextSelectionBookmark',
    'StandardTextSelectionCapture',
    'Win32StandardTextBackend',
    'SelectionCaptureResult',
    'SelectionReplaceResult',
    'SelectionTransactionStatus',
]
