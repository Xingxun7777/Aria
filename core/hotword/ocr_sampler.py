"""OCR raw-text sampler — disabled by default.

Purpose: when the auto-hotword learning chain misclassifies (e.g. a real proper
noun never makes it past the count threshold, or noise gets approved) the only
way to debug today is to add print() to record() and reproduce the screen.
That is impractical for users who can't reliably reproduce a moment hours
later.

This sampler keeps a tiny rolling diary of OCR frames + the candidate terms
they yielded so a power user (or me, when triaging a bug report) can look at
"what did the screen actually contain when this term first showed up".

Design constraints:
  - Default OFF. Privacy-sensitive — has to be opt-in.
  - Hard daily cap on number of frames written (default 10/day) so it can't
    become a disk-eater. Once the cap is hit for the day, further calls are
    no-ops until midnight rolls over.
  - Reservoir sampling within the cap so the kept frames are representative
    across the whole day, not just the first 10 minutes.
  - Append-only JSONL per day. tmp+rename atomic write per record.
  - 7-day retention; older files swept on first append per day.
  - Failures NEVER raise into the OCR callback. Sampling is best-effort.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import random
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_RETENTION_DAYS = 7


class OcrSampler:
    """Reservoir-sampled OCR diary with daily file rotation."""

    def __init__(
        self,
        sample_dir: Path | str,
        max_per_day: int = 10,
        enabled: bool = False,
    ):
        self._dir = Path(sample_dir)
        self._max_per_day = max(0, int(max_per_day))
        self._enabled = bool(enabled) and self._max_per_day > 0
        self._lock = threading.Lock()
        # Reservoir state, reset each day.
        # `_kept_today` is the in-memory mirror of the day-file's JSONL lines —
        # one dict payload per slot. Length grows up to `_max_per_day`, then
        # individual slots get overwritten by reservoir sampling. The day-file
        # on disk is rewritten from this list whenever a slot changes (small:
        # at most _max_per_day × ~4KB per rewrite).
        # Use a dedicated Random instance so a unit test can seed it without
        # disturbing the global random state.
        self._day_key: str = ""
        self._seen_today: int = 0  # frames offered to record() today
        self._kept_today: list[dict] = []  # in-memory mirror of day file
        self._rng = random.Random()
        if self._enabled:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                logger.warning(f"OcrSampler dir create failed: {exc}")
                self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        with self._lock:
            self._enabled = bool(value) and self._max_per_day > 0
            if self._enabled:
                try:
                    self._dir.mkdir(parents=True, exist_ok=True)
                except Exception as exc:
                    logger.warning(f"OcrSampler dir create failed: {exc}")
                    self._enabled = False

    def record(
        self,
        screen_text: str,
        window_title: str = "",
        candidate_terms: list[str] | None = None,
    ) -> bool:
        """Offer one OCR frame to the sampler. Returns True if it was kept."""
        if not self._enabled or not screen_text:
            return False
        try:
            return self._record_inner(screen_text, window_title, candidate_terms or [])
        except Exception as exc:
            # Sampling must never poison the OCR pipeline.
            logger.debug(f"OcrSampler.record swallowed: {exc}")
            return False

    def _record_inner(
        self, screen_text: str, window_title: str, candidate_terms: list[str]
    ) -> bool:
        now = _dt.datetime.now()
        day_key = now.strftime("%Y-%m-%d")
        with self._lock:
            if day_key != self._day_key:
                # Rolled into a new day → reset reservoir + sweep retention.
                self._day_key = day_key
                self._seen_today = 0
                self._kept_today = []
                self._sweep_retention_locked(now)

            self._seen_today += 1
            payload = {
                "ts": now.isoformat(timespec="seconds"),
                "title": window_title or "",
                "candidate_terms": candidate_terms[:200],
                "screen_text": screen_text[:4000],  # cap per-frame disk hit
            }

            day_file = self._day_file_path_locked(now)
            if len(self._kept_today) < self._max_per_day:
                # Cap not reached yet — always keep, append to day file.
                self._kept_today.append(payload)
                self._append_line_locked(day_file, payload)
                return True

            # Cap reached → reservoir sampling. Replace a random slot with
            # probability max_per_day / seen_today; this yields a uniform
            # sample over all frames the day will eventually see.
            idx = self._rng.randint(0, self._seen_today - 1)
            if idx >= self._max_per_day:
                # Reservoir says "drop this frame" — frame is NOT written.
                return False

            # Replace slot idx in memory + rewrite the day file from the
            # full in-memory list. The file is at most _max_per_day lines
            # (~ N × 4KB), so this is a cheap atomic tmp+replace.
            self._kept_today[idx] = payload
            self._rewrite_day_file_locked(day_file)
            return True

    def _day_file_path_locked(self, now: _dt.datetime) -> Path:
        """One file per day, one JSON per line so users can `jq` over it."""
        return self._dir / f"sample_{now.strftime('%Y-%m-%d')}.jsonl"

    def _append_line_locked(self, path: Path, payload: dict) -> None:
        """Append a single JSON line during the cap-not-reached phase.

        Append is atomic at the OS level for one ≤PIPE_BUF write of UTF-8
        JSON + newline (well under 4KB after our 4000-char cap), and the
        in-memory mirror is the source of truth so even a partial line at
        the end of the file gets overwritten by the next rewrite.
        """
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    def _rewrite_day_file_locked(self, path: Path) -> None:
        """Rewrite the whole day file from `_kept_today` via tmp+replace.

        Used when reservoir sampling replaces an existing slot. The file is
        bounded to _max_per_day lines so this is O(N) where N is small (10).
        """
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for entry in self._kept_today:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
        except Exception as exc:
            # Best-effort: drop the tmp if anything went wrong; in-memory
            # mirror still holds the right state for the next rewrite.
            logger.debug(f"OcrSampler rewrite failed: {exc}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    def _sweep_retention_locked(self, now: _dt.datetime) -> None:
        cutoff = now - _dt.timedelta(days=_RETENTION_DAYS)
        try:
            for child in self._dir.iterdir():
                if not child.is_file():
                    continue
                name = child.name
                if not name.startswith("sample_") or not name.endswith(".jsonl"):
                    continue
                stem = name[len("sample_") : -len(".jsonl")]
                try:
                    file_date = _dt.datetime.strptime(stem, "%Y-%m-%d")
                except ValueError:
                    continue
                if file_date < cutoff:
                    try:
                        os.remove(child)
                    except Exception:
                        pass
        except Exception:
            pass
