"""
Screen OCR Module
=================
Captures and OCR-reads text from the active window.
Provides screen text as ASR context for improved recognition accuracy.

Backend priority (per window type):
1. UI Automation — browsers/GUI apps: 100% accuracy, ~50-500ms
2. RapidOCR (PaddleOCR ONNX) — terminals/other: high accuracy, ~2-3s
3. WinRT OCR (winocr) — fallback: fast but lower accuracy

Triggered on VAD speech_start, runs async in background thread.
"""

import datetime
import os
import re
import sys
import threading
from pathlib import Path
from typing import Optional

# File-based debug log (pythonw.exe safe — stdout is None)
_OCR_LOG = Path(__file__).parent.parent.parent / "DebugLog" / "ocr_debug.log"


def _ocr_log(msg: str) -> None:
    try:
        _OCR_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with open(_OCR_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


# ============================================================================
# Backend 1: UI Automation (browsers/GUI apps — perfect accuracy)
# ============================================================================

_UIA_BROWSER_CLASSES = {
    "chrome_widgetwin_1",  # Chrome / Edge
    "mozillawindowclass",  # Firefox
    "operawindowclass",  # Opera
}


def _try_ui_automation(hwnd) -> Optional[str]:
    """Try extracting text via Windows UI Automation (accessibility tree).

    Works best for browsers where DOM text is exposed through accessibility.
    Returns None if UI Automation is not suitable for this window.
    """
    try:
        import ctypes
        from ctypes import wintypes

        # Check if this is a browser window
        class_buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, class_buf, 256)
        class_name = class_buf.value.lower()

        if class_name not in _UIA_BROWSER_CLASSES:
            return None  # Not a browser, skip UIA

        import uiautomation as auto

        ctrl = auto.ControlFromHandle(hwnd)
        if not ctrl:
            return None

        texts = set()
        count = [0]

        def walk(element, depth=0):
            if depth > 12 or count[0] > 800:
                return
            count[0] += 1
            try:
                name = element.Name
                if name and len(name) >= 2:
                    texts.add(name)
            except Exception:
                pass
            try:
                children = element.GetChildren()
                if children:
                    for child in children[:80]:
                        walk(child, depth + 1)
            except Exception:
                pass

        walk(ctrl)

        if len(texts) < 3:
            return None  # Too few results, UIA not working well

        result = " ".join(texts)
        _ocr_log(f"UIA: {len(texts)} elements, {len(result)} chars")
        return result

    except ImportError:
        return None  # uiautomation not installed
    except Exception as e:
        _ocr_log(f"UIA error: {e}")
        return None


# ============================================================================
# Backend 2: RapidOCR (PaddleOCR ONNX — high accuracy)
# ============================================================================

_rapidocr_engine = None
_rapidocr_available: Optional[bool] = None


def _init_rapidocr() -> bool:
    global _rapidocr_engine, _rapidocr_available
    if _rapidocr_available is not None:
        return _rapidocr_available
    try:
        ort_capi = os.path.join(
            sys.prefix, "Lib", "site-packages", "onnxruntime", "capi"
        )
        if os.path.isdir(ort_capi):
            os.add_dll_directory(ort_capi)

        from rapidocr_onnxruntime import RapidOCR

        _rapidocr_engine = RapidOCR()
        _rapidocr_available = True
        _ocr_log("RapidOCR initialized")
        return True
    except Exception as e:
        _ocr_log(f"RapidOCR init failed: {e}")
        _rapidocr_available = False
        return False


def _run_rapidocr(img) -> str:
    import numpy as np

    # Full resolution — don't scale down, small text matters
    img_np = np.array(img)
    result, _ = _rapidocr_engine(img_np, use_cls=False)

    if not result:
        return ""

    texts = [r[1] for r in result]
    return " ".join(texts)


# ============================================================================
# Backend 3: WinRT OCR (fallback)
# ============================================================================

_winocr = None
_winocr_available: Optional[bool] = None


def _init_winocr() -> bool:
    global _winocr, _winocr_available
    if _winocr_available is not None:
        return _winocr_available
    try:
        import winocr

        _winocr = winocr
        _winocr_available = True
        _ocr_log("WinRT OCR initialized (fallback)")
        return True
    except ImportError as e:
        _ocr_log(f"WinRT OCR init failed: {e}")
        _winocr_available = False
        return False


def _run_winocr(img) -> str:
    import asyncio

    try:
        result = asyncio.run(
            asyncio.wait_for(
                _winocr.recognize_pil(img, lang="zh-Hans"),
                timeout=5.0,
            )
        )
        return result.text if hasattr(result, "text") else ""
    except Exception as e:
        _ocr_log(f"WinRT OCR error: {e}")
        return ""


# ============================================================================
# ScreenOCR class
# ============================================================================

_ImageGrab = None
_ctypes_mod = None


def _ensure_capture_imports() -> bool:
    global _ImageGrab, _ctypes_mod
    if _ImageGrab is not None:
        return True
    try:
        from PIL import ImageGrab
        import ctypes

        _ImageGrab = ImageGrab
        _ctypes_mod = ctypes
        return True
    except ImportError as e:
        _ocr_log(f"Capture imports failed: {e}")
        return False


class ScreenOCR:
    """
    Captures text from the active window.

    Auto-selects the best backend per window type:
    - Browsers → UI Automation (100% accurate, reads DOM text)
    - Terminals/Other → RapidOCR or WinRT OCR

    Usage:
        ocr = ScreenOCR()
        ocr.trigger()          # Start in background (non-blocking)
        text = ocr.get_text()  # Get latest result
    """

    def __init__(self, max_text_len: int = 1000):
        self._max_text_len = max_text_len
        self._latest_text: str = ""
        self._latest_hwnd: int = 0  # Window handle of captured content
        self._latest_time: float = 0.0  # time.time() of capture
        self._lock = threading.Lock()
        self._running = False
        self._available: Optional[bool] = None
        self._ocr_backend: str = "none"

    @property
    def available(self) -> bool:
        if self._available is None:
            if not _ensure_capture_imports():
                self._available = False
                return False

            if _init_rapidocr():
                self._ocr_backend = "rapidocr"
            elif _init_winocr():
                self._ocr_backend = "winocr"
            else:
                self._ocr_backend = "none"

            # Available if we have any OCR backend OR uiautomation
            self._available = self._ocr_backend != "none"
            _ocr_log(f"OCR backend: {self._ocr_backend}")
        return self._available

    def trigger(self) -> None:
        if not self.available:
            return
        if self._running:
            return
        thread = threading.Thread(target=self._run_ocr, daemon=True)
        thread.start()

    def get_text(self, current_hwnd: int = 0) -> str:
        """Get latest OCR result. Returns empty if stale (wrong window or too old).

        Args:
            current_hwnd: Current foreground window handle. If provided and
                          differs from captured window, returns empty to prevent
                          stale context from biasing ASR.
        """
        import time

        with self._lock:
            # Reject stale results: >10s old or different window
            age = time.time() - self._latest_time if self._latest_time else 999
            if age > 10:
                return ""
            if current_hwnd and self._latest_hwnd and current_hwnd != self._latest_hwnd:
                return ""
            return self._latest_text

    def _run_ocr(self) -> None:
        import time as _time

        self._running = True
        try:
            hwnd = self._get_foreground_hwnd()
            if not hwnd:
                return

            # Strategy 1: Try UI Automation for browsers (fast + accurate)
            text = _try_ui_automation(hwnd)
            if text:
                backend_used = "uia"
            else:
                # Strategy 2: Screenshot + OCR for non-browser apps
                img = self._capture_window(hwnd)
                if img is None:
                    return

                _ocr_log(
                    f"Captured {img.size[0]}x{img.size[1]}, backend={self._ocr_backend}"
                )

                if self._ocr_backend == "rapidocr":
                    text = _run_rapidocr(img)
                    backend_used = "rapidocr"
                elif self._ocr_backend == "winocr":
                    text = _run_winocr(img)
                    backend_used = "winocr"
                else:
                    return

            if text:
                text = self._clean_ocr_text(text, backend_used)
                with self._lock:
                    self._latest_text = text
                    self._latest_hwnd = hwnd
                    self._latest_time = _time.time()
                _ocr_log(f"Result ({backend_used}): {len(text)} chars")
        except Exception as e:
            _ocr_log(f"OCR error: {e}")
        finally:
            self._running = False

    def _get_foreground_hwnd(self):
        try:
            return _ctypes_mod.windll.user32.GetForegroundWindow()
        except Exception:
            return None

    def _capture_window(self, hwnd):
        try:
            from ctypes import wintypes

            rect = wintypes.RECT()
            _ctypes_mod.windll.user32.GetWindowRect(hwnd, _ctypes_mod.byref(rect))

            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 50 or h < 50:
                return None

            return _ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
        except Exception as e:
            _ocr_log(f"Capture failed: {e}")
            return None

    def _clean_ocr_text(self, text: str, backend: str) -> str:
        if backend == "winocr":
            cjk = r"[\u4e00-\u9fff\u3400-\u4dbf]"
            text = re.sub(f"({cjk})\\s+({cjk})", r"\1\2", text)
            text = re.sub(f"({cjk})\\s+({cjk})", r"\1\2", text)

        text = re.sub(r"\s+", " ", text).strip()

        if len(text) > self._max_text_len:
            text = text[: self._max_text_len]

        return text
