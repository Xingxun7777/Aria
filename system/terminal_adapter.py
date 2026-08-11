"""Pure terminal delivery planning with conservative explicit profiles."""

import hashlib
from dataclasses import dataclass
from enum import Enum
import unicodedata


class TerminalMode(str, Enum):
    UNKNOWN = "unknown"
    SHELL = "shell"
    AI_CLI = "ai_cli"
    EDITOR = "editor"
    REMOTE = "remote"


class PasteChord(str, Enum):
    CTRL_V = "ctrl_v"
    SHIFT_INSERT = "shift_insert"


class TerminalTailProbeStatus(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class TerminalDeliveryProfile:
    name: str
    mode: TerminalMode
    paste_chord: PasteChord
    manual_only: bool = False


@dataclass(frozen=True)
class TerminalDeliveryPlan:
    profile: TerminalDeliveryProfile
    chunks: tuple[str, ...]
    flattened_newlines: bool
    requires_manual: bool
    reason_code: str
    auto_submit: bool = False


@dataclass(frozen=True)
class TerminalRecentTextBookmark:
    """Logical tail range for text Aria just pasted into one terminal input.

    Terminals do not expose a native editable range.  This receipt therefore
    authorizes only one narrower operation: while the exact same terminal
    target is still foreground and this is still Aria's latest logical tail,
    send a bounded number of Backspace presses and paste one replacement.
    ``start``/``end`` are deletion units, not screen-buffer coordinates.
    """

    hwnd: int
    start: int
    end: int
    pid: int
    focused_hwnd: int
    paste_chord: PasteChord
    uia_verified: bool = False
    anchor_chars: int = 0
    anchor_sha256: str = ""


@dataclass(frozen=True)
class TerminalTailAnchor:
    """Content-free fingerprint of the text immediately before a source tail."""

    hwnd: int
    chars: int
    sha256: str


@dataclass(frozen=True)
class TerminalTailProbeResult:
    status: TerminalTailProbeStatus
    anchor: TerminalTailAnchor | None = None


def terminal_backspace_units(text: str) -> int | None:
    """Return a conservative Backspace count, or ``None`` when ambiguous.

    Ordinary CJK/Latin text and punctuation map one-for-one to terminal editor
    Backspace actions.  Newlines may execute commands and tabs/control bytes or
    multi-code-point graphemes vary across shells/TUIs, so those inputs are not
    eligible for automatic tail replacement.
    """

    value = str(text or "")
    if not value:
        return None
    for char in value:
        codepoint = ord(char)
        if char in "\r\n\t" or codepoint < 0x20 or codepoint == 0x7F:
            return None
        if (
            unicodedata.combining(char)
            or char == "\u200d"
            or 0xFE00 <= codepoint <= 0xFE0F
            or 0xE0100 <= codepoint <= 0xE01EF
            or 0x1F3FB <= codepoint <= 0x1F3FF
            or 0x1F1E6 <= codepoint <= 0x1F1FF
        ):
            return None
    return len(value)


def _read_terminal_text_before_caret(
    hwnd: int, requested_chars: int
) -> str | None:
    """Read only a bounded pre-caret suffix from Windows Terminal UIA.

    No text is logged or persisted.  Unsupported terminal providers return
    ``None`` so callers can fail closed instead of falling back to blind
    Backspace.  UI Automation must be initialized in the worker thread that
    creates and consumes its Control/TextRange objects.
    """

    window = int(hwnd or 0)
    limit = min(4096, max(1, int(requested_chars or 1)))
    if window <= 0:
        return None
    try:
        import uiautomation as auto
        from uiautomation import TextPatternRangeEndpoint, TextUnit

        with auto.UIAutomationInitializerInThread():
            root = auto.ControlFromHandle(window)
            if root is None:
                return None
            terminal = None
            for control, _depth in auto.WalkControl(
                root, includeTop=True, maxDepth=8
            ):
                if (
                    control.ClassName == "TermControl"
                    and control.HasKeyboardFocus
                ):
                    terminal = control
                    break
            if terminal is None:
                return None
            pattern = terminal.GetTextPattern()
            selections = pattern.GetSelection() if pattern else []
            if len(selections) != 1:
                return None
            caret = selections[0]
            # A non-degenerate screen selection means the input caret is not a
            # safe terminal-tail deletion target.
            if caret.GetText(1):
                return None
            before = caret.Clone()
            before.MoveEndpointByRange(
                TextPatternRangeEndpoint.End,
                caret,
                TextPatternRangeEndpoint.Start,
                waitTime=0,
            )
            before.MoveEndpointByUnit(
                TextPatternRangeEndpoint.Start,
                TextUnit.Character,
                -limit,
                waitTime=0,
            )
            value = str(before.GetText(-1) or "")
            # Some providers count CR/LF as one TextUnit but return two Python
            # characters.  Allow that bounded expansion, never an unbounded
            # screen/scrollback read.
            if len(value) > limit * 3 + 16:
                return None
            return value
    except Exception:
        return None


def capture_terminal_tail_guard(
    hwnd: int,
    expected_text: str,
    *,
    anchor_chars: int = 64,
) -> TerminalTailProbeResult:
    """Verify that ``expected_text`` is immediately before the UIA caret."""

    expected = str(expected_text or "")
    units = terminal_backspace_units(expected)
    if units is None or units > 2000:
        return TerminalTailProbeResult(TerminalTailProbeStatus.UNAVAILABLE)
    anchor_limit = min(128, max(0, int(anchor_chars or 0)))
    observed = _read_terminal_text_before_caret(
        int(hwnd or 0), units + anchor_limit
    )
    if observed is None:
        return TerminalTailProbeResult(TerminalTailProbeStatus.UNAVAILABLE)
    if not observed.endswith(expected):
        return TerminalTailProbeResult(TerminalTailProbeStatus.MISMATCH)
    prefix = observed[: -len(expected)] if expected else observed
    anchor = prefix[-anchor_limit:] if anchor_limit else ""
    return TerminalTailProbeResult(
        TerminalTailProbeStatus.MATCH,
        TerminalTailAnchor(
            hwnd=int(hwnd or 0),
            chars=len(anchor),
            sha256=hashlib.sha256(anchor.encode("utf-8")).hexdigest(),
        ),
    )


def verify_terminal_tail_anchor(anchor: TerminalTailAnchor) -> TerminalTailProbeStatus:
    """Confirm the caret returned to the content-free pre-source anchor."""

    if not isinstance(anchor, TerminalTailAnchor) or int(anchor.hwnd or 0) <= 0:
        return TerminalTailProbeStatus.UNAVAILABLE
    requested = max(1, int(anchor.chars))
    observed = _read_terminal_text_before_caret(anchor.hwnd, requested)
    if observed is None:
        return TerminalTailProbeStatus.UNAVAILABLE
    if anchor.chars == 0:
        candidate = observed
    elif len(observed) >= anchor.chars:
        candidate = observed[-anchor.chars :]
    else:
        return TerminalTailProbeStatus.MISMATCH
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return (
        TerminalTailProbeStatus.MATCH
        if digest == anchor.sha256
        else TerminalTailProbeStatus.MISMATCH
    )


_PROFILES = {
    "safe": TerminalDeliveryProfile(
        "safe", TerminalMode.UNKNOWN, PasteChord.CTRL_V
    ),
    "shift_insert": TerminalDeliveryProfile(
        "shift_insert", TerminalMode.SHELL, PasteChord.SHIFT_INSERT
    ),
    "ai_cli": TerminalDeliveryProfile(
        "ai_cli", TerminalMode.AI_CLI, PasteChord.CTRL_V
    ),
    "ai_cli_shift_insert": TerminalDeliveryProfile(
        "ai_cli_shift_insert", TerminalMode.AI_CLI, PasteChord.SHIFT_INSERT
    ),
    "remote_manual": TerminalDeliveryProfile(
        "remote_manual", TerminalMode.REMOTE, PasteChord.CTRL_V, manual_only=True
    ),
    "editor_manual": TerminalDeliveryProfile(
        "editor_manual", TerminalMode.EDITOR, PasteChord.CTRL_V, manual_only=True
    ),
}


def resolve_terminal_profile(name: str) -> TerminalDeliveryProfile:
    """Unknown names fail back to the single-paste safe profile."""
    key = str(name or "safe").strip().lower()
    return _PROFILES.get(key, _PROFILES["safe"])


def flatten_terminal_newlines(text: str) -> tuple[str, bool]:
    """Make automatic terminal delivery non-executable by removing newlines."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    had_newlines = "\n" in normalized
    return normalized.replace("\n", " "), had_newlines


def split_terminal_chunks(text: str, max_chars: int) -> tuple[str, ...]:
    """Split without adding/removing characters; prefer a nearby soft boundary."""
    value = str(text or "")
    limit = max(1, int(max_chars))
    if not value:
        return ()
    if len(value) <= limit:
        return (value,)

    chunks = []
    start = 0
    minimum_soft_cut = max(1, int(limit * 0.6))
    while len(value) - start > limit:
        window = value[start : start + limit]
        cut = limit
        best = -1
        for separator in (" ", "\t", "。", "！", "？", ".", ",", ";", "，", "；"):
            found = window.rfind(separator)
            if found >= minimum_soft_cut and found > best:
                best = found
        if best >= 0:
            cut = best + 1
        chunks.append(value[start : start + cut])
        start += cut
    chunks.append(value[start:])
    return tuple(chunks)


class TerminalAdapter:
    """Plan terminal delivery; never emits Enter or terminal escape sequences."""

    def __init__(
        self,
        profile_name: str = "safe",
        *,
        chunking_enabled: bool = False,
        chunk_chars: int = 1000,
    ) -> None:
        self.profile = resolve_terminal_profile(profile_name)
        self.chunking_enabled = bool(chunking_enabled)
        # Provisional, explicit-profile-only bound. Default routing does not
        # chunk; live CLI matrices must tune this before global activation.
        try:
            parsed_chunk_chars = int(chunk_chars or 1000)
        except (TypeError, ValueError):
            parsed_chunk_chars = 1000
        self.chunk_chars = min(4000, max(256, parsed_chunk_chars))

    def plan(self, text: str) -> TerminalDeliveryPlan:
        flattened, had_newlines = flatten_terminal_newlines(text)
        if self.profile.manual_only:
            return TerminalDeliveryPlan(
                profile=self.profile,
                chunks=(),
                flattened_newlines=had_newlines,
                requires_manual=True,
                reason_code="terminal_manual_profile",
            )

        should_chunk = (
            self.profile.mode == TerminalMode.AI_CLI
            and self.chunking_enabled
            and len(flattened) > self.chunk_chars
        )
        chunks = (
            split_terminal_chunks(flattened, self.chunk_chars)
            if should_chunk
            else ((flattened,) if flattened else ())
        )
        if should_chunk:
            reason = "terminal_ai_cli_chunked"
        elif had_newlines:
            reason = "terminal_newlines_flattened"
        else:
            reason = "terminal_single_paste"
        return TerminalDeliveryPlan(
            profile=self.profile,
            chunks=chunks,
            flattened_newlines=had_newlines,
            requires_manual=False,
            reason_code=reason,
        )
