"""
Screen OCR Module
=================
Captures and OCR-reads text from the active window.
Provides screen text as ASR context for improved recognition accuracy.

Uses Windows built-in OCR (WinRT) — no model download needed.
Triggered on VAD speech_start, runs async in background thread.
"""

import asyncio
import threading
import time
from typing import Optional

from ..logging import get_system_logger

logger = get_system_logger()

# Lazy imports to avoid slow startup
_winocr = None
_ImageGrab = None
_ctypes = None


def _ensure_imports() -> bool:
    """Lazy-load OCR dependencies. Returns True if available."""
    global _winocr, _ImageGrab, _ctypes
    if _winocr is not None:
        return True
    try:
        import winocr
        from PIL import ImageGrab
        import ctypes

        _winocr = winocr
        _ImageGrab = ImageGrab
        _ctypes = ctypes
        return True
    except ImportError as e:
        logger.warning(f"Screen OCR not available: {e}")
        return False


class ScreenOCR:
    """
    Captures text from the active window via Windows OCR.

    Usage:
        ocr = ScreenOCR()
        ocr.trigger()          # Start OCR in background (non-blocking)
        text = ocr.get_text()  # Get latest result (may be from previous trigger)
    """

    def __init__(self, max_text_len: int = 500):
        self._max_text_len = max_text_len
        self._latest_text: str = ""
        self._lock = threading.Lock()
        self._running = False
        self._available: Optional[bool] = None

    @property
    def available(self) -> bool:
        """Check if screen OCR is available on this system."""
        if self._available is None:
            self._available = _ensure_imports()
        return self._available

    def trigger(self) -> None:
        """Start OCR capture in background thread (non-blocking).

        Safe to call rapidly — skips if a previous OCR is still running.
        """
        if not self.available:
            return
        if self._running:
            return  # Previous OCR still in progress, skip

        thread = threading.Thread(target=self._run_ocr, daemon=True)
        thread.start()

    def get_text(self) -> str:
        """Get the latest OCR result (thread-safe)."""
        with self._lock:
            return self._latest_text

    def _run_ocr(self) -> None:
        """Run OCR in background thread."""
        self._running = True
        try:
            # Capture active window screenshot
            img = self._capture_active_window()
            if img is None:
                return

            # Run async OCR
            text = asyncio.run(self._ocr_image(img))
            if text:
                # Truncate and deduplicate
                text = self._clean_ocr_text(text)
                with self._lock:
                    self._latest_text = text
        except Exception as e:
            logger.debug(f"Screen OCR failed: {e}")
        finally:
            self._running = False

    def _capture_active_window(self):
        """Capture screenshot of the active window only."""
        try:
            from ctypes import wintypes

            user32 = _ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None

            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, _ctypes.byref(rect))
            bbox = (rect.left, rect.top, rect.right, rect.bottom)

            # Sanity check
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 50 or h < 50:
                return None

            return _ImageGrab.grab(bbox=bbox)
        except Exception as e:
            logger.debug(f"Window capture failed: {e}")
            return None

    async def _ocr_image(self, img) -> str:
        """Run WinRT OCR on a PIL image."""
        try:
            result = await _winocr.recognize_pil(img, lang="zh-Hans")
            return result.text if hasattr(result, "text") else ""
        except Exception as e:
            logger.debug(f"WinRT OCR failed: {e}")
            return ""

    def _clean_ocr_text(self, text: str) -> str:
        """Clean and truncate OCR text for use as ASR context."""
        # Remove excessive whitespace
        import re

        text = re.sub(r"\s+", " ", text).strip()

        # Truncate to max length
        if len(text) > self._max_text_len:
            text = text[: self._max_text_len]

        return text
