"""Append-only JSONL store for user-correction events.

Why JSONL and not SQLite (talax-dictation uses SQLite for the same job):
talax needs query load for trigram training and per-context correction
libraries. Aria's correction stream is <10 events/day, the whole repo's
persistence convention is JSON/JSONL (data/history/*.jsonl,
data/cost/*.jsonl, data/auto_hotwords.json), and an append-only line file
gives us single-line atomic writes, skip-bad-line tolerance and grep-able
audit for free. See design doc §2.

The store is intentionally dumb: it records `original → corrected` pairs
with context/source/timestamp. All judgement (normalization, 3-strike
promotion, conflict arbitration) lives in `correction_policy`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


# Signal sources. v1 only wires `explicit` (history-browser "改正" entry)
# and `polish_diff` (input→output diff replay); `clipboard` and `respeak`
# are reserved so future collectors don't need a schema change.
SOURCE_EXPLICIT = "explicit"
SOURCE_CLIPBOARD = "clipboard"
SOURCE_RESPEAK = "respeak"
SOURCE_POLISH_DIFF = "polish_diff"

VALID_SOURCES = frozenset(
    {SOURCE_EXPLICIT, SOURCE_CLIPBOARD, SOURCE_RESPEAK, SOURCE_POLISH_DIFF}
)


def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


@dataclass
class CorrectionEvent:
    """One observed correction: the user replaced `original` with `corrected`."""

    original: str
    corrected: str
    context: str = ""  # surrounding sentence / window title, review evidence
    source: str = SOURCE_EXPLICIT
    ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {
            "original": self.original,
            "corrected": self.corrected,
            "context": self.context,
            "source": self.source,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CorrectionEvent | None":
        """Parse one JSONL record; None for structurally unusable rows."""
        if not isinstance(data, dict):
            return None
        original = data.get("original")
        corrected = data.get("corrected")
        if not isinstance(original, str) or not isinstance(corrected, str):
            return None
        if not original.strip() or not corrected.strip():
            return None
        source = data.get("source")
        if source not in VALID_SOURCES:
            source = SOURCE_EXPLICIT
        return cls(
            original=original,
            corrected=corrected,
            context=str(data.get("context") or ""),
            source=source,
            ts=str(data.get("ts") or ""),
        )


class CorrectionStore:
    """Thread-safe JSONL-backed store of CorrectionEvents.

    Loads eagerly on construction; `append()` writes one line per event.
    A short content-fingerprint window suppresses accidental duplicates
    (double-clicked UI button, replayed diff batch).
    """

    MAX_EVENTS = 5000  # compaction threshold: keep the newest on load
    DEDUP_WINDOW_S = 60.0

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._events: list[CorrectionEvent] = []
        self._last_fp: str = ""
        self._last_fp_ts: float = 0.0
        self._load()

    # ------------------------------------------------------------------ I/O

    def _load(self) -> None:
        if not self._path.exists():
            return
        events: list[CorrectionEvent] = []
        try:
            raw = self._path.read_text(encoding="utf-8")
        except Exception:
            return
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue  # tolerate a corrupt line, keep the rest
            event = CorrectionEvent.from_dict(data)
            if event is not None:
                events.append(event)
        if len(events) > self.MAX_EVENTS:
            events = events[-self.MAX_EVENTS :]
            self._rewrite(events)
        self._events = events

    def _rewrite(self, events: list[CorrectionEvent]) -> None:
        """Compaction rewrite (atomic tmp+replace, like the tracker)."""
        try:
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                "".join(
                    json.dumps(e.to_dict(), ensure_ascii=False) + "\n" for e in events
                ),
                encoding="utf-8",
            )
            import os

            os.replace(tmp, self._path)
        except Exception:
            # Keep in-memory state; next compaction retries.
            pass

    # --------------------------------------------------------- public ops

    def append(self, event: CorrectionEvent) -> bool:
        """Persist one correction event. Returns False when deduped/invalid.

        Never raises on I/O failure — the event stays in memory and is
        recovered by the next compaction rewrite, mirroring the tracker's
        tolerance for AV/cloud-sync interference.
        """
        if not event.original.strip() or not event.corrected.strip():
            return False
        fp = hashlib.md5(
            f"{event.original}\x00{event.corrected}\x00{event.source}".encode(
                "utf-8", errors="ignore"
            )
        ).hexdigest()[:16]
        now_ts = time.time()
        with self._lock:
            if self._last_fp == fp and (now_ts - self._last_fp_ts) < self.DEDUP_WINDOW_S:
                return False
            self._last_fp = fp
            self._last_fp_ts = now_ts
            if not event.ts:
                event.ts = _now_iso()
            self._events.append(event)
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            except Exception:
                pass
            if len(self._events) > self.MAX_EVENTS:
                self._events = self._events[-self.MAX_EVENTS :]
                self._rewrite(self._events)
        return True

    def events(self) -> list[CorrectionEvent]:
        """Snapshot of all loaded events (oldest first)."""
        with self._lock:
            return list(self._events)

    def count(self) -> int:
        with self._lock:
            return len(self._events)
