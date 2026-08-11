"""Target text-surface classification for cross-application output.

The important boundary is the *text carrier*, not just the executable name.
Word documents, terminal prompts, standard Win32 edit controls, Chromium
editors, and protected/game surfaces expose very different guarantees.

This module deliberately contains pure classification only.  Runtime probes,
delivery and fallback UI remain in :mod:`aria.system.output`, which keeps this
policy easy to test without touching a real foreground window.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class SurfaceKind(str, Enum):
    """Coarse carrier type used for routing and privacy-safe telemetry."""

    STANDARD_TEXT = "standard_text"
    DOCUMENT = "document"
    TERMINAL = "terminal"
    ELECTRON = "electron"
    GAME = "game"
    CUSTOM = "custom"


class NewlinePolicy(str, Enum):
    """What an automatic delivery may do with line breaks."""

    PRESERVE = "preserve"
    FLATTEN = "flatten"
    REQUIRE_CONFIRMATION = "require_confirmation"


class DeliveryConfidence(str, Enum):
    """Strength of the current carrier contract, not ASR confidence."""

    NATIVE = "native"
    SUPPORTED = "supported"
    BEST_EFFORT = "best_effort"
    MANUAL_ONLY = "manual_only"


ELECTRON_WINDOW_CLASS = "Chrome_WidgetWin_1"

# A terminal process is only enough to establish conservative *delivery*
# semantics.  It is not enough to know whether the user is at a shell prompt,
# inside Vim, in WSL/tmux, or talking to an AI CLI.  Those finer modes require
# an explicit application/profile adapter.
TERMINAL_PROCESSES = frozenset(
    {
        "windowsterminal.exe",
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "conhost.exe",
        "wezterm-gui.exe",
        "alacritty.exe",
        "hyper.exe",
        "mintty.exe",
        "tabby.exe",
        "warp.exe",
        "ghostty.exe",
        "kitty.exe",
    }
)

# Full document editing needs an application adapter. Microsoft Word and the
# Windows WPS Writer expose the same Word-compatible Range contract, although
# WPS wraps its document canvas in a Qt top-level window. Until that adapter
# owns a verified range, atomic plain-text clipboard delivery is safer than
# per-character SendInput into either custom document canvas.
WORD_COMPATIBLE_DOCUMENT_PROCESSES = frozenset({"winword.exe", "wps.exe"})
DOCUMENT_PROCESSES = WORD_COMPATIBLE_DOCUMENT_PROCESSES

STANDARD_TEXT_CONTROL_CLASSES = frozenset(
    {
        "edit",
        "richedit",
        "richedit20a",
        "richedit20w",
        "richedit50w",
    }
)


@dataclass(frozen=True)
class TargetSurfaceProfile:
    """A conservative routing profile for the focused text carrier.

    ``addressable_text`` means Aria can currently identify and replace a
    concrete text range through a native control contract.  It must not be
    inferred merely because clipboard paste happens to work.
    """

    kind: SurfaceKind
    process_name: str
    window_class: str
    focused_class: str
    force_clipboard: bool
    newline_policy: NewlinePolicy
    addressable_text: bool
    confidence: DeliveryConfidence
    requires_explicit_profile: bool
    reason_code: str


@dataclass(frozen=True)
class TargetSnapshot:
    """Content-free identity of the carrier selected for one commit.

    A snapshot deliberately excludes window titles and text. ``hwnd`` plus
    ``pid`` protects against pasting into another top-level window; a known
    ``focused_hwnd`` also catches focus moving to a different native child
    control inside the same window. Custom canvases that expose no distinct
    child focus still require a richer adapter/bookmark for range-level
    guarantees.
    """

    hwnd: int
    pid: int
    focused_hwnd: int
    profile: TargetSurfaceProfile
    # Optional content-free native bookmark (for example Word selection
    # start/end/story). It must never contain text, titles, document paths or
    # apartment-bound COM objects. Generic HWND matching intentionally ignores
    # it; the owning adapter validates it at its own commit edge.
    adapter_token: Optional[Any] = None

    @property
    def is_valid(self) -> bool:
        return self.hwnd > 0 and self.pid > 0

    def matches(self, other: "TargetSnapshot") -> bool:
        if not self.is_valid or not other.is_valid:
            return False
        if self.hwnd != other.hwnd or self.pid != other.pid:
            return False
        if self.focused_hwnd > 0 and other.focused_hwnd != self.focused_hwnd:
            # Once the commit boundary identified a native child control, a
            # later inability to identify it is not permission to fall back to
            # top-level-window matching. Treat zero as unavailable/mismatched.
            return False
        return True


def supports_word_compatible_ranges(process_name: str) -> bool:
    """Return whether the process exposes the verified Word-style adapter."""

    return (process_name or "").strip().lower() in WORD_COMPATIBLE_DOCUMENT_PROCESSES


def _profile(
    *,
    kind: SurfaceKind,
    process_name: str,
    window_class: str,
    focused_class: str,
    force_clipboard: bool,
    newline_policy: NewlinePolicy,
    addressable_text: bool,
    confidence: DeliveryConfidence,
    requires_explicit_profile: bool,
    reason_code: str,
) -> TargetSurfaceProfile:
    return TargetSurfaceProfile(
        kind=kind,
        process_name=process_name,
        window_class=window_class,
        focused_class=focused_class,
        force_clipboard=force_clipboard,
        newline_policy=newline_policy,
        addressable_text=addressable_text,
        confidence=confidence,
        requires_explicit_profile=requires_explicit_profile,
        reason_code=reason_code,
    )


def classify_target_surface(
    *,
    process_name: str = "",
    window_class: str = "",
    focused_class: str = "",
    explicit_kind: Optional[SurfaceKind] = None,
) -> TargetSurfaceProfile:
    """Classify a foreground carrier without claiming unsupported powers.

    Games are intentionally never guessed from an executable name.  A game
    chat field can be a normal IME-enabled edit box, a custom renderer, or a
    protected/raw-input surface.  It is classified as ``GAME`` only through
    an explicit future per-app profile.
    """

    process = (process_name or "").strip().lower()
    top_class = (window_class or "").strip()
    focus_class = (focused_class or "").strip()
    focus_class_lower = focus_class.lower()

    if explicit_kind == SurfaceKind.GAME:
        return _profile(
            kind=SurfaceKind.GAME,
            process_name=process,
            window_class=top_class,
            focused_class=focus_class,
            force_clipboard=False,
            newline_policy=NewlinePolicy.REQUIRE_CONFIRMATION,
            addressable_text=False,
            confidence=DeliveryConfidence.BEST_EFFORT,
            requires_explicit_profile=True,
            reason_code="explicit_game_profile",
        )

    if explicit_kind == SurfaceKind.TERMINAL or process in TERMINAL_PROCESSES:
        return _profile(
            kind=SurfaceKind.TERMINAL,
            process_name=process,
            window_class=top_class,
            focused_class=focus_class,
            force_clipboard=True,
            newline_policy=NewlinePolicy.FLATTEN,
            addressable_text=False,
            confidence=DeliveryConfidence.SUPPORTED,
            requires_explicit_profile=False,
            reason_code="terminal_process",
        )

    if explicit_kind == SurfaceKind.DOCUMENT or process in DOCUMENT_PROCESSES:
        return _profile(
            kind=SurfaceKind.DOCUMENT,
            process_name=process,
            window_class=top_class,
            focused_class=focus_class,
            force_clipboard=True,
            newline_policy=NewlinePolicy.PRESERVE,
            addressable_text=False,
            confidence=DeliveryConfidence.SUPPORTED,
            requires_explicit_profile=False,
            reason_code="document_adapter_pending",
        )

    if (
        explicit_kind == SurfaceKind.STANDARD_TEXT
        or focus_class_lower in STANDARD_TEXT_CONTROL_CLASSES
    ):
        return _profile(
            kind=SurfaceKind.STANDARD_TEXT,
            process_name=process,
            window_class=top_class,
            focused_class=focus_class,
            force_clipboard=False,
            newline_policy=NewlinePolicy.PRESERVE,
            addressable_text=True,
            confidence=DeliveryConfidence.NATIVE,
            requires_explicit_profile=False,
            reason_code="standard_text_control",
        )

    if explicit_kind == SurfaceKind.ELECTRON or top_class == ELECTRON_WINDOW_CLASS:
        return _profile(
            kind=SurfaceKind.ELECTRON,
            process_name=process,
            window_class=top_class,
            focused_class=focus_class,
            force_clipboard=False,
            newline_policy=NewlinePolicy.PRESERVE,
            addressable_text=False,
            confidence=DeliveryConfidence.BEST_EFFORT,
            requires_explicit_profile=False,
            reason_code="electron_custom_control",
        )

    return _profile(
        kind=explicit_kind or SurfaceKind.CUSTOM,
        process_name=process,
        window_class=top_class,
        focused_class=focus_class,
        force_clipboard=False,
        newline_policy=NewlinePolicy.PRESERVE,
        addressable_text=False,
        confidence=DeliveryConfidence.BEST_EFFORT,
        requires_explicit_profile=True,
        reason_code="unknown_custom_control",
    )
