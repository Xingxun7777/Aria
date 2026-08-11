"""Pure, explicit game-chat profile parsing and delivery planning."""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional


class GameChatTransport(str, Enum):
    MANUAL = "manual"
    CLIPBOARD = "clipboard"
    TYPEWRITER = "typewriter"


# These names map to OutputInjector.send_key().  The list deliberately omits
# mouse buttons, scan-code/driver inputs and arbitrary numeric virtual keys.
_SAFE_GAME_KEYS = frozenset(
    {
        "enter",
        "return",
        "escape",
        "tab",
        "space",
        "slash",
        *(chr(code) for code in range(ord("a"), ord("z") + 1)),
        *(str(number) for number in range(10)),
        *(f"f{number}" for number in range(1, 13)),
    }
)


@dataclass(frozen=True)
class GameChatProfile:
    """Content-free policy bound to one exact executable name."""

    process_name: str
    transport: GameChatTransport
    open_chat_key: Optional[str]
    chat_already_open: bool
    allow_same_focus_after_open: bool
    auto_submit: bool
    submit_key: Optional[str]
    open_delay_ms: int
    submit_delay_ms: int
    max_chars: int
    valid: bool
    reason_code: str

    @property
    def manual_only(self) -> bool:
        return not self.valid or self.transport == GameChatTransport.MANUAL


@dataclass(frozen=True)
class GameChatDeliveryPlan:
    profile: GameChatProfile
    text: str
    flattened_newlines: bool
    requires_manual: bool
    reason_code: str


def _normalize_process_name(value: Any, *, reject_paths: bool) -> str:
    name = str(value or "").strip().lower()
    if not name:
        return ""
    if reject_paths and ("/" in name or "\\" in name):
        return ""
    if not name.endswith(".exe"):
        return ""
    return name


def _normalize_key(value: Any) -> Optional[str]:
    key = str(value or "").strip().lower()
    if not key:
        return None
    return key if key in _SAFE_GAME_KEYS else None


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _invalid_profile(process_name: str, reason_code: str) -> GameChatProfile:
    return GameChatProfile(
        process_name=process_name,
        transport=GameChatTransport.MANUAL,
        open_chat_key=None,
        chat_already_open=False,
        allow_same_focus_after_open=False,
        auto_submit=False,
        submit_key=None,
        open_delay_ms=120,
        submit_delay_ms=80,
        max_chars=256,
        valid=False,
        reason_code=reason_code,
    )


def resolve_game_chat_profile(
    profiles: Any,
    process_name: str,
) -> Optional[GameChatProfile]:
    """Resolve an enabled profile by exact, case-insensitive executable name.

    Missing and disabled profiles return ``None`` so games are never guessed.
    A matching but malformed enabled profile returns an invalid/manual profile;
    callers can then classify the carrier as game while failing closed before
    any key or clipboard action.
    """

    process = _normalize_process_name(process_name, reject_paths=False)
    if not process or not isinstance(profiles, Mapping):
        return None

    matching_profiles = []
    for raw_name, candidate in profiles.items():
        normalized = _normalize_process_name(raw_name, reject_paths=True)
        if normalized and normalized == process:
            matching_profiles.append(candidate)
    if not matching_profiles:
        return None
    if len(matching_profiles) != 1:
        # JSON/dict keys are case-sensitive, Windows executable names are not.
        # Two differently-cased entries would otherwise make policy depend on
        # insertion order, so treat that configuration as ambiguous.
        return _invalid_profile(process, "game_profile_ambiguous")
    raw_profile = matching_profiles[0]
    if not isinstance(raw_profile, Mapping):
        return _invalid_profile(process, "game_profile_invalid")
    if raw_profile.get("enabled") is not True:
        return None

    raw_transport = str(raw_profile.get("transport", "manual")).strip().lower()
    try:
        transport = GameChatTransport(raw_transport)
    except ValueError:
        return _invalid_profile(process, "game_transport_invalid")

    chat_already_open = raw_profile.get("chat_already_open") is True
    allow_same_focus_after_open = (
        raw_profile.get("allow_same_focus_after_open") is True
    )
    raw_open_key = raw_profile.get("open_chat_key")
    open_chat_key = _normalize_key(raw_open_key)
    if transport != GameChatTransport.MANUAL:
        if raw_open_key not in (None, "") and open_chat_key is None:
            return _invalid_profile(process, "game_open_key_invalid")
        if open_chat_key is not None and chat_already_open:
            return _invalid_profile(process, "game_open_contract_ambiguous")
        if open_chat_key is None and not chat_already_open:
            return _invalid_profile(process, "game_open_contract_missing")

    auto_submit = raw_profile.get("auto_submit") is True
    raw_submit_key = raw_profile.get("submit_key")
    submit_key = _normalize_key(raw_submit_key)
    if auto_submit and submit_key is None:
        return _invalid_profile(process, "game_submit_key_invalid")

    return GameChatProfile(
        process_name=process,
        transport=transport,
        open_chat_key=open_chat_key,
        chat_already_open=chat_already_open,
        allow_same_focus_after_open=allow_same_focus_after_open,
        auto_submit=auto_submit,
        submit_key=submit_key if auto_submit else None,
        open_delay_ms=_bounded_int(
            raw_profile.get("open_delay_ms", 120), 120, 0, 2000
        ),
        submit_delay_ms=_bounded_int(
            raw_profile.get("submit_delay_ms", 80), 80, 0, 2000
        ),
        max_chars=_bounded_int(raw_profile.get("max_chars", 256), 256, 32, 2000),
        valid=True,
        reason_code=(
            "game_manual_profile"
            if transport == GameChatTransport.MANUAL
            else "game_profile_ready"
        ),
    )


def flatten_game_chat_newlines(text: str) -> tuple[str, bool]:
    """Keep a game chat transaction single-line without dropping content."""

    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    had_newlines = "\n" in normalized
    return normalized.replace("\n", " "), had_newlines


class GameChatAdapter:
    """Plan one profile-bound transaction; never touches a game process."""

    def __init__(self, profile: GameChatProfile) -> None:
        self.profile = profile

    def plan(self, text: str) -> GameChatDeliveryPlan:
        flattened, had_newlines = flatten_game_chat_newlines(text)
        if self.profile.manual_only:
            return GameChatDeliveryPlan(
                profile=self.profile,
                text=flattened,
                flattened_newlines=had_newlines,
                requires_manual=True,
                reason_code=self.profile.reason_code,
            )
        if len(flattened) > self.profile.max_chars:
            return GameChatDeliveryPlan(
                profile=self.profile,
                text=flattened,
                flattened_newlines=had_newlines,
                requires_manual=True,
                reason_code="game_text_too_long",
            )
        return GameChatDeliveryPlan(
            profile=self.profile,
            text=flattened,
            flattened_newlines=had_newlines,
            requires_manual=False,
            reason_code="game_text_ready",
        )
