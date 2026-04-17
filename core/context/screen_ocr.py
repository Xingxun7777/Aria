"""
Screen OCR Module v2.0
======================
Provides screen text as ASR context for improved recognition accuracy.

Three-layer architecture:
  Layer 0: Window title (0ms, instant, all apps)
  Layer 1: UI Automation (200ms, non-browser native apps)
  Layer 2: RapidOCR screenshot (~2-3s, background, cached)

Window change detection via SetWinEventHook (event-driven, not polling).
Cache invalidated by hwnd + title hash change.
"""

import ctypes
import datetime
import hashlib
import os
import queue
import re
import sys
import threading
import time
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
# Layer 0: Window Title (instant, all apps)
# ============================================================================

# Browser suffixes to strip from window titles
_BROWSER_SUFFIXES = [
    " - Google Chrome",
    " - Microsoft Edge",
    " - Mozilla Firefox",
    " - Opera",
    " - Brave",
    " — Mozilla Firefox",
    " - Vivaldi",
]


def _extract_title_keywords(hwnd) -> str:
    """Extract keywords from window title. Returns cleaned text.

    Browser titles = active tab title = page topic.
    Examples:
        "丰川祥子-哔哩哔哩_bilibili - Google Chrome" → "丰川祥子 哔哩哔哩 bilibili"
        "app.py - voicetype - Visual Studio Code" → "app.py voicetype Visual Studio Code"
    """
    try:
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value.strip()
        if not title or len(title) < 2:
            return ""

        # Strip browser name suffix
        for suffix in _BROWSER_SUFFIXES:
            if title.endswith(suffix):
                title = title[: -len(suffix)]
                break

        # Normalize separators to spaces
        title = re.sub(r"[-_|·—:：/\\]", " ", title)
        # Remove noise (very short segments, pure numbers, common UI words)
        noise = {
            "新标签页",
            "首页",
            "设置",
            "New Tab",
            "Home",
            "Settings",
            "最大化",
            "最小化",
        }
        words = [
            w.strip()
            for w in title.split()
            if len(w.strip()) >= 2 and w.strip() not in noise
        ]

        return " ".join(words)
    except Exception:
        return ""


# ============================================================================
# Layer 1: UI Automation (non-browser native apps)
# ============================================================================

_UIA_BROWSER_CLASSES = {
    "chrome_widgetwin_1",
    "mozillawindowclass",
    "operawindowclass",
}


def _try_ui_automation(hwnd) -> tuple:
    """Extract text via targeted UIA probes. For non-browser apps.

    Returns:
        (text, has_document_content): text is the extracted string (or None),
        has_document_content is True if actual document/edit text was captured
        (not just toolbar labels).

    Thread safety:
        This function MUST initialize COM on entry because it runs on a
        background thread. UIA objects and patterns are created and consumed on
        the same thread only. We also avoid full GetChildren()/sibling walking:
        provider disconnects inside those native traversals can raise SEH
        exceptions that Python cannot catch reliably.
    """
    # Initialize COM for this thread in MTA mode.
    # MSDN recommends UIA clients use multi-threaded apartment: STA requires
    # a Windows message pump, which a daemon thread doesn't have, causing
    # UIA's cross-apartment marshaling to deadlock or disconnect
    # (RPC_E_DISCONNECTED / RPC_E_SERVER_DIED_DNE). comtypes (the layer
    # uiautomation sits on) defaults to STA on first COM call — this default
    # is precisely the root cause of the observed crash loop, so we must
    # initialize explicitly BEFORE any uiautomation access.
    co_initialized = False
    try:
        import pythoncom

        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
            co_initialized = True
        except pythoncom.com_error:
            # Already initialized with different mode on this thread —
            # don't uninit, we didn't own the initialization.
            pass
    except ImportError:
        # pywin32 missing: continue without explicit init (uiautomation may
        # still work if it initializes internally, though likely as STA).
        pass

    try:
        cls_buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, cls_buf, 256)
        class_name = cls_buf.value.lower()

        # Skip browsers — UIA only gets UI chrome, not page content
        if class_name in _UIA_BROWSER_CLASSES:
            return None, False

        import uiautomation as auto

        if hasattr(auto, "SetGlobalSearchTimeout"):
            auto.SetGlobalSearchTimeout(1.5)
        elif hasattr(auto, "uiautomation") and hasattr(
            auto.uiautomation, "SetGlobalSearchTimeout"
        ):
            auto.uiautomation.SetGlobalSearchTimeout(1.5)

        try:
            ctrl = auto.ControlFromHandle(hwnd)
        except Exception as e:
            _ocr_log(f"UIA ControlFromHandle failed: {e}")
            return None, False
        if not ctrl:
            return None, False

        MAX_EDIT_CHARS = 500  # Cap edit text to avoid pulling entire documents

        def _read_candidate(control) -> str:
            """Read text from a single Edit/Document-like control."""
            if not control:
                return ""
            try:
                value_pattern = control.GetValuePattern()
                if value_pattern:
                    value = (value_pattern.Value or "").strip()
                    if len(value) >= 4:
                        return value[:MAX_EDIT_CHARS]
            except Exception:
                pass

            try:
                text_pattern = control.GetTextPattern()
                if text_pattern:
                    value = (text_pattern.DocumentRange.GetText(MAX_EDIT_CHARS) or "")
                    value = value.strip()
                    if len(value) >= 4:
                        return value[:MAX_EDIT_CHARS]
            except Exception:
                pass

            return ""

        # Do not recursively enumerate the entire accessibility tree here.
        # For this feature we only need document text, and screenshot OCR can
        # supplement everything else. The broad tree walk was the main native
        # crash surface in the observed dumps.
        candidates = [ctrl]
        for control_getter in ("DocumentControl", "EditControl"):
            try:
                getter = getattr(ctrl, control_getter, None)
                candidate = getter(searchDepth=6) if getter else None
                if candidate:
                    candidates.append(candidate)
            except Exception:
                pass

        seen = set()
        for candidate in candidates:
            marker = id(candidate)
            if marker in seen:
                continue
            seen.add(marker)
            edit_text = _read_candidate(candidate)
            if edit_text:
                _ocr_log(f"UIA: captured {len(edit_text)} chars")
                return edit_text, True

        return None, False

    except ImportError:
        return None, False
    except Exception as e:
        _ocr_log(f"UIA error: {e}")
        return None, False
    finally:
        if co_initialized:
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            except Exception:
                pass


# ============================================================================
# Layer 2: Screenshot OCR (background, cached)
# ============================================================================

_rapidocr_engine = None
_winocr_available = None


def _init_rapidocr() -> bool:
    global _rapidocr_engine
    if _rapidocr_engine is not None:
        return True
    try:
        ort_dir = os.path.join(
            sys.prefix, "Lib", "site-packages", "onnxruntime", "capi"
        )
        if os.path.isdir(ort_dir):
            os.add_dll_directory(ort_dir)
        from rapidocr_onnxruntime import RapidOCR

        _rapidocr_engine = RapidOCR()
        return True
    except Exception as e:
        _ocr_log(f"RapidOCR init failed: {e}")
        return False


def _init_winocr() -> bool:
    global _winocr_available
    if _winocr_available is not None:
        return _winocr_available
    try:
        import winocr

        _winocr_available = True
        return True
    except ImportError:
        _winocr_available = False
        return False


def _run_rapidocr(img) -> Optional[str]:
    try:
        import numpy as np

        img_np = np.array(img)
        result, _ = _rapidocr_engine(img_np)
        if not result:
            return None
        return " ".join([r[1] for r in result])
    except Exception as e:
        _ocr_log(f"RapidOCR error: {e}")
        return None


def _run_winocr(img) -> Optional[str]:
    try:
        import winocr
        import asyncio

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(winocr.recognize_pil(img, "zh-Hans-CN"))
        loop.close()
        return result.text if result and result.text else None
    except Exception as e:
        _ocr_log(f"WinOCR error: {e}")
        return None


# ============================================================================
# Main ScreenOCR class
# ============================================================================


class ScreenOCR:
    """
    Three-layer screen text extraction for ASR context biasing.

    Layer 0: Window title (instant) → always available
    Layer 1: UIA (fast, non-browser apps) → supplementary
    Layer 2: RapidOCR (slow, background) → enriches subsequent sentences

    Window changes detected via get_title_context() polling or external event.
    """

    def __init__(self, max_text_len: int = 1000):
        self._max_text_len = max_text_len
        # Layer 0: title context (instant)
        self._title_text: str = ""
        self._title_hwnd: int = 0
        self._title_hash: str = ""
        # Layer 2: OCR context (slow, background)
        self._ocr_text: str = ""
        self._ocr_cache_key: str = ""  # hwnd + title_hash
        self._ocr_time: float = 0.0
        self._ocr_ttl: float = 10.0  # seconds
        # Shared
        self._lock = threading.Lock()
        self._running = False
        self._available: Optional[bool] = None
        self._ocr_backend: str = "none"

    @property
    def available(self) -> bool:
        if self._available is None:
            try:
                from PIL import ImageGrab

                _ = ImageGrab
            except ImportError:
                self._available = False
                return False

            if _init_rapidocr():
                self._ocr_backend = "rapidocr"
            elif _init_winocr():
                self._ocr_backend = "winocr"
            else:
                self._ocr_backend = "none"

            self._available = True  # At minimum, Layer 0 (title) always works
            _ocr_log(f"OCR backend: {self._ocr_backend}")
        return self._available

    def update_title(self, hwnd: int = 0) -> str:
        """Layer 0: Update and return window title keywords (instant, 0ms).

        Call this on every speech_start or window change event.
        Returns extracted keywords from the window title.
        """
        if not hwnd:
            try:
                hwnd = ctypes.windll.user32.GetForegroundWindow()
            except Exception:
                return ""

        title_text = _extract_title_keywords(hwnd)
        title_hash = hashlib.md5(title_text.encode()).hexdigest()[:8]

        with self._lock:
            title_changed = title_hash != self._title_hash
            self._title_text = title_text
            self._title_hwnd = hwnd
            self._title_hash = title_hash

        if title_changed and title_text:
            _ocr_log(f"Title: '{title_text[:60]}' (hash={title_hash})")

        return title_text

    def trigger(self) -> None:
        """Layer 1+2: Trigger UIA + OCR in background (non-blocking)."""
        if not self.available:
            return
        if self._running:
            return

        # Also update title immediately (Layer 0)
        self.update_title()

        thread = threading.Thread(
            target=self._run_background, daemon=True, name="screen-ocr"
        )
        thread.start()

    def get_text(self, current_hwnd: int = 0) -> str:
        """Get combined context (title + OCR). Returns empty if stale."""
        with self._lock:
            parts = []

            # Layer 0: title is always fresh (updated on every call)
            if self._title_text:
                parts.append(self._title_text)

            # Layer 2: OCR result (check staleness)
            if self._ocr_text:
                age = time.time() - self._ocr_time if self._ocr_time else 999
                if age <= self._ocr_ttl:
                    # Check cache validity: same hwnd+title
                    current_key = f"{self._title_hwnd}_{self._title_hash}"
                    if self._ocr_cache_key == current_key:
                        parts.append(self._ocr_text)

            return " ".join(parts) if parts else ""

    def _run_background(self) -> None:
        """Run Layer 1 (UIA) + Layer 2 (OCR) in background."""
        self._running = True
        try:
            hwnd = self._get_foreground_hwnd()
            if not hwnd:
                return

            # Check if OCR cache is still valid
            title_text = _extract_title_keywords(hwnd)
            title_hash = hashlib.md5(title_text.encode()).hexdigest()[:8]
            cache_key = f"{hwnd}_{title_hash}"

            with self._lock:
                if self._ocr_cache_key == cache_key:
                    age = time.time() - self._ocr_time
                    if age < self._ocr_ttl:
                        _ocr_log(f"Cache hit (age={age:.1f}s)")
                        return  # Cache still fresh

            # Layer 1: Try UIA for non-browser apps
            uia_text, has_doc_content = _try_ui_automation(hwnd)
            text = uia_text
            backend_used = "uia"

            # Quality gate: if UIA returned only toolbar/chrome text (no actual
            # document content), supplement with screenshot OCR. This handles
            # apps like WPS Office, Adobe, etc. that use custom rendering and
            # don't expose document text via UIA accessibility APIs.
            need_screenshot = not uia_text or (uia_text and not has_doc_content)

            if need_screenshot:
                ocr_text = self._run_screenshot_ocr(hwnd)
                if ocr_text:
                    # Prefer screenshot OCR if it captured more content
                    if not text or len(ocr_text) > len(text):
                        text = ocr_text
                        backend_used = self._ocr_backend
                        if uia_text:
                            _ocr_log(
                                f"OCR supplement: UIA had {len(uia_text)} chars "
                                f"(no doc content), OCR got {len(ocr_text)} chars"
                            )

            if text:
                text = self._clean_text(text, backend_used)
                with self._lock:
                    self._ocr_text = text
                    self._ocr_cache_key = cache_key
                    self._ocr_time = time.time()
                _ocr_log(f"Result ({backend_used}): {len(text)} chars")

        except Exception as e:
            _ocr_log(f"Background OCR error: {e}")
        finally:
            self._running = False

    def _run_screenshot_ocr(self, hwnd) -> Optional[str]:
        """Layer 2: Capture window screenshot and run OCR."""
        if self._ocr_backend == "none":
            return None
        try:
            from PIL import ImageGrab
            from ctypes import wintypes

            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 50 or h < 50:
                return None
            img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
        except Exception as e:
            _ocr_log(f"Capture failed: {e}")
            return None

        _ocr_log(f"Captured {img.size[0]}x{img.size[1]}, backend={self._ocr_backend}")

        if self._ocr_backend == "rapidocr":
            return _run_rapidocr(img)
        elif self._ocr_backend == "winocr":
            return _run_winocr(img)
        return None

    def _get_foreground_hwnd(self):
        try:
            return ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            return None

    def _clean_text(self, text: str, backend: str) -> str:
        if backend == "winocr":
            cjk = r"[\u4e00-\u9fff\u3400-\u4dbf]"
            text = re.sub(f"({cjk})\\s+({cjk})", r"\1\2", text)
            text = re.sub(f"({cjk})\\s+({cjk})", r"\1\2", text)

        text = re.sub(r"\s+", " ", text).strip()

        if len(text) > self._max_text_len:
            text = text[: self._max_text_len]

        return text
