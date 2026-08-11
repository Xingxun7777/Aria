"""
Action Types for Aria v1.1
================================
Action-driven architecture: Backend generates UIAction → QtBridge signal → UI responds

Based on architectural review consensus.
"""

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional
import uuid


class ActionType(Enum):
    """UI action types for the action-driven architecture."""

    REPLACE_TEXT = auto()  # Original behavior: replace selected text
    SHOW_TRANSLATION = auto()  # Translation popup (don't replace original)
    SHOW_SUMMARY = auto()  # Summary popup (don't replace original)
    CLIPBOARD_TRANSLATION = auto()  # Translate and copy to clipboard
    OPEN_CHAT = auto()  # AI chat dialog
    SHOW_REPLY = auto()  # Reply popup (generate reply to message)
    SHOW_REMINDER_CONFIRM = auto()  # Reminder set confirmation (undo model)
    SHOW_REMINDER_NOTIFY = auto()  # Reminder fired notification
    REMINDER_CANCELLED = auto()  # Reminder(s) cancelled; dismiss matching popup
    SCREENSHOT_FULL = auto()  # Capture primary screen, save + clipboard
    SCREENSHOT_REGION = auto()  # Open region selector overlay
    SHOW_PIN_WINDOW = auto()  # Show pinned screenshot floating window


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
class SummaryAction(UIAction):
    """
    Action to show summary in a popup.

    Flow:
    1. Backend creates SummaryAction with source_text
    2. QtBridge emits actionTriggered signal
    3. UI receives action, shows loading popup
    4. SummaryWorker performs summarization
    5. Worker signals completion, UI updates popup with result
    """

    type: ActionType = field(default=ActionType.SHOW_SUMMARY, init=False)
    source_text: str = ""
    summary_text: Optional[str] = None
    error: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "type", ActionType.SHOW_SUMMARY)


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


@dataclass
class ReplyAction(UIAction):
    """
    Action to show reply suggestion in a popup.

    Flow:
    1. Backend creates ReplyAction with source_text (the message to reply to)
    2. QtBridge emits actionTriggered signal
    3. UI receives action, shows loading popup (reuses TranslationPopup)
    4. ReplyWorker generates reply via LLM
    5. Worker signals completion, UI updates popup with result

    v1.2: Supports style_hint from capture_following (e.g., "语气强硬一点")
    """

    type: ActionType = field(default=ActionType.SHOW_REPLY, init=False)
    source_text: str = ""  # The message user wants to reply to
    reply_text: Optional[str] = None  # Filled by worker on completion
    error: Optional[str] = None  # Filled on error
    style_hint: Optional[str] = None  # Optional style from capture_following

    def __post_init__(self):
        object.__setattr__(self, "type", ActionType.SHOW_REPLY)


@dataclass
class ReminderConfirmAction(UIAction):
    """
    Action to show reminder confirmation popup (undo model).

    Default: reminder is already active (confirmed=True in store).
    Popup shows what was set and offers [撤销] button.
    If user doesn't interact within 30s, reminder stays active.
    """

    type: ActionType = field(default=ActionType.SHOW_REMINDER_CONFIRM, init=False)
    reminder_id: str = ""
    content: str = ""
    trigger_time: str = ""  # ISO format
    trigger_display: str = ""  # Human-readable, e.g. "YYYY-MM-DD HH:MM (in N hours)"
    repeat_interval_seconds: int = 0

    def __post_init__(self):
        object.__setattr__(self, "type", ActionType.SHOW_REMINDER_CONFIRM)


@dataclass
class ReminderNotifyAction(UIAction):
    """
    Action to show reminder notification when time arrives.

    For batched reminders (after sleep/hibernate), batch_count > 0
    and content contains merged bullet list.
    """

    type: ActionType = field(default=ActionType.SHOW_REMINDER_NOTIFY, init=False)
    reminder_id: str = ""
    content: str = ""
    created_at: str = ""
    batch_count: int = 0  # >0 means batched/merged notification
    repeat_interval_seconds: int = 0

    def __post_init__(self):
        object.__setattr__(self, "type", ActionType.SHOW_REMINDER_NOTIFY)


@dataclass
class ReminderCancelAction(UIAction):
    """Dismiss cancelled reminder UI and show the cancellation result."""

    type: ActionType = field(default=ActionType.REMINDER_CANCELLED, init=False)
    reminder_ids: tuple[str, ...] = field(default_factory=tuple)
    message: str = "提醒已关闭"
    dismiss_active: bool = False

    def __post_init__(self):
        object.__setattr__(self, "type", ActionType.REMINDER_CANCELLED)


@dataclass
class ScreenshotFullAction(UIAction):
    """
    Action to capture the primary screen and save + copy to clipboard.

    No user interaction needed — fires immediately on the Qt main thread,
    saves PNG to ~/Pictures/Aria/, and copies QImage to clipboard.
    """

    type: ActionType = field(default=ActionType.SCREENSHOT_FULL, init=False)

    def __post_init__(self):
        object.__setattr__(self, "type", ActionType.SCREENSHOT_FULL)


@dataclass
class ScreenshotRegionAction(UIAction):
    """
    Action to open the region selector overlay across every screen.

    Overlay freezes each screen's background (so dragging is stable even
    if the live desktop is animating), lets the user drag a selection,
    then offers a 3-button toolbar (confirm / reselect / pin).
    """

    type: ActionType = field(default=ActionType.SCREENSHOT_REGION, init=False)

    def __post_init__(self):
        object.__setattr__(self, "type", ActionType.SCREENSHOT_REGION)


@dataclass
class PinScreenshotAction(UIAction):
    """
    Action to show a pinned screenshot floating on top of the desktop.

    image_bytes is the PNG-encoded pinned area. global_x/y is the source
    region's top-left in logical pixels (so the pin can appear right
    where the user cropped it). `device_pixel_ratio` MUST come from the
    actual capture (not from a guess at the primary screen) so a pin
    cropped on a secondary screen with a different DPR renders at the
    right logical size.
    """

    type: ActionType = field(default=ActionType.SHOW_PIN_WINDOW, init=False)
    image_bytes: bytes = b""  # PNG-encoded image data
    width_px: int = 0  # Physical pixel width of the captured area
    height_px: int = 0  # Physical pixel height of the captured area
    global_x: int = 0  # Logical-pixel screen X where the crop started
    global_y: int = 0  # Logical-pixel screen Y where the crop started
    device_pixel_ratio: float = 1.0  # DPR of the dominant capture screen

    def __post_init__(self):
        object.__setattr__(self, "type", ActionType.SHOW_PIN_WINDOW)
