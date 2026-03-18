"""
Screen OCR Module
=================
Captures and OCR-reads text from the active window.
Provides screen text as ASR context for improved recognition accuracy.

Uses Windows built-in OCR (WinRT) — no model download needed.
Triggered on VAD speech_start, runs async in background thread.
"""

import asyncio
import datetime
import threading
from pathlib import Path
from typing import Optional

# File-based debug log (pythonw.exe safe — stdout is None)
_OCR_LOG = Path(__file__).parent.parent.parent / "DebugLog" / "ocr_debug.log"


def _ocr_log(msg: str) -> None:
    """Write OCR debug message to file (always works, even under pythonw.exe)."""
    try:
        _OCR_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with open(_OCR_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


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
        _ocr_log("Imports OK: winocr + PIL + ctypes")
        return True
    except ImportError as e:
        _ocr_log(f"Import FAILED: {e}")
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
        """Run OCR in background thread with timeout protection."""
        self._running = True
        try:
            # Step 1: Capture active window screenshot
            img = self._capture_active_window()
            if img is None:
                _ocr_log("_run_ocr: capture returned None, skipping")
                return

            _ocr_log(f"_run_ocr: captured {img.size[0]}x{img.size[1]}")

            # Step 2: Run OCR (with 5s timeout via asyncio.wait_for)
            try:
                text = asyncio.run(self._ocr_with_timeout(img, timeout=5.0))
            except Exception as e:
                _ocr_log(f"_run_ocr: asyncio.run FAILED: {e}")
                return

            # Step 3: Store result
            if text:
                text = self._clean_ocr_text(text)
                with self._lock:
                    self._latest_text = text
                _ocr_log(f"_run_ocr: stored {len(text)} chars")
            else:
                _ocr_log("_run_ocr: OCR returned empty text")
        except Exception as e:
            _ocr_log(f"_run_ocr: EXCEPTION: {e}")
        finally:
            self._running = False

    def _capture_active_window(self):
        """Capture screenshot of the active window only."""
        try:
            from ctypes import wintypes

            user32 = _ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                _ocr_log("_capture: no foreground window")
                return None

            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, _ctypes.byref(rect))
            bbox = (rect.left, rect.top, rect.right, rect.bottom)

            # Sanity check
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 50 or h < 50:
                _ocr_log(f"_capture: window too small ({w}x{h})")
                return None

            return _ImageGrab.grab(bbox=bbox)
        except Exception as e:
            _ocr_log(f"_capture: FAILED: {e}")
            return None

    async def _ocr_with_timeout(self, img, timeout: float = 5.0) -> str:
        """Run WinRT OCR with timeout protection."""
        try:
            result = await asyncio.wait_for(
                _winocr.recognize_pil(img, lang="zh-Hans"),
                timeout=timeout,
            )
            text = result.text if hasattr(result, "text") else ""
            _ocr_log(f"_ocr: success, {len(text)} chars")
            return text
        except asyncio.TimeoutError:
            _ocr_log(f"_ocr: TIMEOUT ({timeout}s)")
            return ""
        except Exception as e:
            _ocr_log(f"_ocr: FAILED: {e}")
            return ""

    def _clean_ocr_text(self, text: str) -> str:
        """Clean and truncate OCR text for use as ASR context."""
        import re

        text = re.sub(r"\s+", " ", text).strip()

        if len(text) > self._max_text_len:
            text = text[: self._max_text_len]

        return text
