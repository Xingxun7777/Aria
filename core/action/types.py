"""
Action Types for Aria v1.1
================================
Action-driven architecture: Backend generates UIAction → QtBridge signal → UI responds

Based on three-way AI consultation (Claude + Codex + Gemini) consensus.
"""

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional
import uuid


class ActionType(Enum):
    """UI action types for the action-driven architecture."""

    REPLACE_TEXT = auto()  # Original behavior: replace selected text
    SHOW_TRANSLATION = auto()  # Translation popup (don't replace original)
    CLIPBOARD_TRANSLATION = auto()  # Translate and copy to clipboard
    OPEN_CHAT = auto()  # AI chat dialog


def _generate_request_id() -> str:
    """Generate a short unique request ID."""
    return str(uuid.uuid4())[:8]


@dataclass
class UIAction:
    """
    Base class for UI actions.

    Each action carries a unique request_id to:
    - Discard stale responses from slow network calls
    - Track action lifecycle in logs
    """

    type: ActionType
    request_id: str = field(default_factory=_generate_request_id)


@dataclass
class TranslationAction(UIAction):
    """
    Action to show translation in a popup.

    Flow:
    1. Backend creates TranslationAction with source_text
    2. QtBridge emits actionTriggered signal
    3. UI receives action, shows loading popup
    4. TranslationWorker performs translation
    5. Worker signals completion, UI updates popup with result
    """

    type: ActionType = field(default=ActionType.SHOW_TRANSLATION, init=False)
    source_text: str = ""
    source_lang: str = "auto"  # Source language (auto-detect)
    target_lang: str = "auto"  # Target language (auto based on source)
    translated_text: Optional[str] = None  # Filled by worker on completion
    error: Optional[str] = None  # Filled on error

    def __post_init__(self):
        # Ensure type is correct even if explicitly passed
        object.__setattr__(self, "type", ActionType.SHOW_TRANSLATION)


@dataclass
class ChatAction(UIAction):
    """
    Action to open AI chat dialog.

    Flow:
    1. Backend creates ChatAction with context_text
    2. QtBridge emits actionTriggered signal
    3. UI opens chat window with context displayed
    4. User can ask follow-up questions
    """

    type: ActionType = field(default=ActionType.OPEN_CHAT, init=False)
    context_text: str = ""  # Selected text as context
    initial_question: Optional[str] = None  # User's spoken question (if any)

    def __post_init__(self):
        object.__setattr__(self, "type", ActionType.OPEN_CHAT)


@dataclass
class ReplaceTextAction(UIAction):
    """
    Action to replace selected text (original behavior).

    This wraps the existing text replacement flow into the action system
    for consistency, though the original flow still works.
    """

    type: ActionType = field(default=ActionType.REPLACE_TEXT, init=False)
    original_text: str = ""  # Original selected text
    replacement_text: str = ""  # Text to replace with

    def __post_init__(self):
        object.__setattr__(self, "type", ActionType.REPLACE_TEXT)


@dataclass
class ClipboardTranslationAction(UIAction):
    """
    Action to translate and copy result to clipboard.

    Flow:
    1. Backend creates ClipboardTranslationAction with source_text and target_lang
    2. QtBridge emits actionTriggered signal
    3. UI receives action, starts translation worker
    4. Worker translates, copies to clipboard
    5. UI shows system tray notification "已粘贴到剪切板"
    """

    type: ActionType = field(default=ActionType.CLIPBOARD_TRANSLATION, init=False)
    source_text: str = ""
    target_lang: str = "en"  # "en" = English, "zh" = Chinese

    def __post_init__(self):
        object.__setattr__(self, "type", ActionType.CLIPBOARD_TRANSLATION)
