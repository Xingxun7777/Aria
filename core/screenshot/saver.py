"""Saves QImage screenshots to disk and the system clipboard.

Default save location: ~/Pictures/Aria/. Falls back to ~/Aria-Screenshots/
if Pictures doesn't exist (Windows shells sometimes hide it). Filename
follows the system tool convention: `Aria_截图_YYYY-MM-DD_HHMMSS.png`.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class SaveResult:
    """Outcome of a single save operation.

    `path` is None when saving to disk failed but the clipboard write
    succeeded — that branch still returns success=True so the user
    doesn't lose the capture entirely.
    """

    success: bool
    path: Optional[Path]
    clipboard_set: bool
    error: Optional[str] = None


class ScreenshotSaver:
    """Save QImage screenshots; tries clipboard first so a disk failure
    doesn't lose the capture.
    """

    DEFAULT_SUBDIR = "Aria"
    FALLBACK_DIRNAME = "Aria-Screenshots"

    def __init__(self, base_dir: Optional[Path] = None):
        self._explicit_base = base_dir

    def resolve_base_dir(self) -> Path:
        """Return the directory where new screenshots go, creating it if missing."""
        if self._explicit_base is not None:
            self._explicit_base.mkdir(parents=True, exist_ok=True)
            return self._explicit_base

        home = Path.home()
        pictures = home / "Pictures"
        if pictures.exists():
            target = pictures / self.DEFAULT_SUBDIR
        else:
            target = home / self.FALLBACK_DIRNAME
        target.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def build_filename(when: Optional[datetime.datetime] = None) -> str:
        """Build a sortable filename for a single capture.

        Includes milliseconds so that two captures fired inside the
        wakeword cooldown window (500 ms default) still get unique
        filenames and don't overwrite each other.
        """
        when = when or datetime.datetime.now()
        # %f is microseconds (6 digits) — keep first 3 for milliseconds.
        ts = when.strftime("%Y-%m-%d_%H%M%S_") + f"{when.microsecond // 1000:03d}"
        return f"Aria_截图_{ts}.png"

    def save(
        self, image: "object", when: Optional[datetime.datetime] = None
    ) -> SaveResult:
        """Save image to clipboard AND disk. Returns success summary.

        Clipboard is set first because it's the cheapest path. If saving
        the PNG fails (disk full / permission denied), the clipboard copy
        still goes through and the user can paste immediately.
        """
        clipboard_ok = False
        clipboard_error: Optional[str] = None
        try:
            from PySide6.QtGui import QGuiApplication

            cb = QGuiApplication.clipboard()
            cb.setImage(image)
            clipboard_ok = True
        except Exception as e:
            clipboard_error = f"clipboard: {e}"

        disk_path: Optional[Path] = None
        disk_error: Optional[str] = None
        try:
            base = self.resolve_base_dir()
            disk_path = base / self.build_filename(when)
            ok = image.save(str(disk_path), "PNG")
            if not ok:
                disk_path = None
                disk_error = "QImage.save returned False"
        except Exception as e:
            disk_path = None
            disk_error = f"disk: {e}"

        # Success: at least one of the two channels worked
        overall = clipboard_ok or (disk_path is not None)
        errors = [e for e in (clipboard_error, disk_error) if e]
        return SaveResult(
            success=overall,
            path=disk_path,
            clipboard_set=clipboard_ok,
            error="; ".join(errors) if errors else None,
        )

    def encode_png_bytes(self, image: "object") -> bytes:
        """Encode a QImage to PNG bytes (for the pin window action payload)."""
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice

        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        try:
            image.save(buf, "PNG")
        finally:
            buf.close()
        return bytes(ba.data())
