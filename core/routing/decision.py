"""RouteDecision record and always-on summary logging for final command routing."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from ..debug import DEBUG_DIR, append_log_line

ROUTE_DECISIONS_LOG = DEBUG_DIR / "route_decisions.log"


@dataclass
class RouteDecision:
    """One final-utterance command-layer decision (or dictation summary)."""

    session_id: int | str
    stage: str
    reason: str
    consumed: bool
    inserted: bool | None
    final_text: str | None
    command_id: str | None = None
    text_len: int = 0
    detail: dict = field(default_factory=dict)


def _fmt_flag(value: Optional[bool]) -> str:
    if value is None:
        return "-"
    return "1" if value else "0"


def format_route_decision_line(decision: RouteDecision) -> str:
    """Format one always-on summary line (no user text, no wakeword literals)."""

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    cmd = decision.command_id if decision.command_id else "-"
    delivery = decision.detail.get("delivery")
    inv = "1" if decision.detail.get("invocation") else "0"
    base = (
        f"{ts} | session={decision.session_id} | stage={decision.stage} | "
        f"reason={decision.reason} | consumed={_fmt_flag(decision.consumed)} | "
        f"inserted={_fmt_flag(decision.inserted)} | cmd={cmd} | "
        f"len={int(decision.text_len)} | inv={inv}"
    )
    if delivery is not None:
        return f"{base} | delivery={delivery}"
    return base


def write_route_decision(decision: RouteDecision, path: Path | None = None) -> None:
    """Append one route-decision summary line. Always-on; never raises."""

    append_log_line(path or ROUTE_DECISIONS_LOG, format_route_decision_line(decision))


def write_startup_fingerprint(
    *,
    version: str,
    base: Union[str, Path],
    pid: int | None = None,
    path: Path | None = None,
) -> None:
    """Append one startup fingerprint line to route_decisions.log. Never raises."""

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    pid_val = os.getpid() if pid is None else int(pid)
    append_log_line(
        path or ROUTE_DECISIONS_LOG,
        f"[{ts}] | startup | version={version} | base={base} | pid={pid_val}",
    )


def make_dictation_decision(
    session_id: int | str,
    *,
    delivery: str,
    text_len: int = 0,
    inserted: bool | None = None,
) -> RouteDecision:
    """Build the post-insert dictation summary record."""

    ok = delivery in ("insert_ok", "inserted")
    return RouteDecision(
        session_id=session_id,
        stage="dictation",
        reason="dictation",
        consumed=False,
        inserted=ok if inserted is None else inserted,
        final_text=None,
        command_id=None,
        text_len=text_len,
        detail={"delivery": "insert_ok" if ok else "fail"},
    )
