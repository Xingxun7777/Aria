"""
Screen OCR Module v2.0
======================
Provides screen text as ASR context for improved recognition accuracy.

Three-layer architecture:
  Layer 0: Window title (0ms, instant, all apps)
  Layer 1: UI Automation (200ms, non-browser native apps)
  Layer 2: RapidOCR screenshot (~2-3s, background, cached)

Foreground-window changes are supplied by Aria's debounced HWND watcher.
Cache invalidated by hwnd + title hash change.
"""

import ctypes
import datetime
import base64
import hashlib
import json
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from ..debug import DebugConfig

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
        "项目标题-文档站点 - Google Chrome" → "项目标题 文档站点"
        "app.py - myproject - Visual Studio Code" → "app.py myproject Visual Studio Code"
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

_UIA_TERMINAL_CLASSES = {
    # Windows Terminal / wt.exe.  Its UIA provider is both slow and rarely
    # gives the rendered terminal buffer we need; screenshot OCR is faster and
    # more faithful for text-dense terminal sessions.
    "cascadia_hosting_window_class",
    # Classic conhost windows.
    "consolewindowclass",
    # Common alternate terminals used by Git Bash/MSYS environments.
    "mintty",
    "weztermwindow",
    "alacritty",
}

_UIA_SKIP_CLASSES = _UIA_BROWSER_CLASSES | _UIA_TERMINAL_CLASSES


def _get_window_class_name(hwnd) -> str:
    """Best-effort Win32 class name for a foreground window."""
    try:
        cls_buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, cls_buf, 256)
        return cls_buf.value.lower()
    except Exception:
        return ""


def _should_skip_uia_for_class(class_name: str) -> bool:
    """True when UIA is known to be slower/less useful than screenshot OCR."""
    return bool(class_name and class_name.lower() in _UIA_SKIP_CLASSES)


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
    class_name = _get_window_class_name(hwnd)
    if _should_skip_uia_for_class(class_name):
        if class_name in _UIA_TERMINAL_CLASSES:
            _ocr_log(f"UIA skipped for terminal class={class_name}")
        return None, False

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
                    value = text_pattern.DocumentRange.GetText(MAX_EDIT_CHARS) or ""
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
_rapidocr_tier: Optional[str] = (
    None  # "v5_dml" | "v5_cpu" | "v4_cpu" — what actually loaded
)
_rapidocr_force_cpu: Optional[bool] = None
_rapidocr_lock = threading.Lock()
_rapidocr_runtime_failures = 0
_winocr_available = None

_DML_PROVIDER = "DmlExecutionProvider"
_CPU_PROVIDER = "CPUExecutionProvider"

# v5 mobile model triplet (shipped with project). v4 models come bundled with
# rapidocr_onnxruntime package itself — used only as last-resort fallback.
_V5_MODEL_DIR = Path(__file__).parent.parent.parent / "models" / "rapidocr" / "v5"
_V5_PATHS = {
    "det_model_path": str(_V5_MODEL_DIR / "PP-OCRv5_det_mobile_infer.onnx"),
    "rec_model_path": str(_V5_MODEL_DIR / "PP-OCRv5_rec_mobile_infer.onnx"),
    "cls_model_path": str(_V5_MODEL_DIR / "ch_PP-OCRv4_cls_infer.onnx"),
    "rec_keys_path": str(_V5_MODEL_DIR / "ppocrv5_dict.txt"),
}


def _read_exact(stream, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("OCR worker pipe closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class _RapidOCRWorkerProxy:
    """Persistent out-of-process RapidOCR engine, used for DML crash isolation."""

    def __init__(
        self,
        use_v5: bool,
        use_dml: bool,
        init_timeout: float = 8.0,
        infer_timeout: float = 20.0,
    ):
        self.use_v5 = bool(use_v5)
        self.use_dml = bool(use_dml)
        self._infer_timeout = float(infer_timeout)
        self._lock = threading.Lock()
        self._responses: "queue.Queue[dict]" = queue.Queue()
        self._closed = False
        self._providers: dict[str, list[str]] = {}

        python_exe = _get_ocr_worker_python()
        worker_path = Path(__file__).with_name("rapidocr_worker.py")
        args = [
            python_exe,
            "-u",
            str(worker_path),
        ]
        if self.use_v5:
            args.append("--use-v5")
        if self.use_dml:
            args.append("--use-dml")

        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        self._proc = subprocess.Popen(
            args,
            cwd=str(Path(__file__).resolve().parents[2]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self._reader = threading.Thread(
            target=self._reader_loop,
            name="rapidocr-worker-reader",
            daemon=True,
        )
        self._reader.start()

        ready = self._recv(timeout=init_timeout)
        if not ready.get("ok"):
            self.close()
            raise RuntimeError(ready.get("error") or "OCR worker init failed")
        self._providers = ready.get("providers") or {}
        _ocr_log(
            "RapidOCR isolated worker ready: "
            f"pid={self._proc.pid}, use_v5={self.use_v5}, use_dml={self.use_dml}"
        )

    def _reader_loop(self) -> None:
        try:
            stdout = self._proc.stdout
            if stdout is None:
                self._responses.put({"ok": False, "error": "worker stdout missing"})
                return
            while True:
                header = stdout.read(4)
                if not header:
                    self._responses.put(
                        {
                            "ok": False,
                            "error": f"worker exited rc={self._proc.poll()}",
                            "event": "eof",
                        }
                    )
                    return
                size = struct.unpack(">I", header)[0]
                if size <= 0 or size > 80_000_000:
                    self._responses.put(
                        {"ok": False, "error": f"invalid worker message size {size}"}
                    )
                    return
                data = _read_exact(stdout, size)
                self._responses.put(json.loads(data.decode("utf-8")))
        except Exception as exc:
            self._responses.put(
                {
                    "ok": False,
                    "error": f"worker reader error: {type(exc).__name__}: {exc}",
                }
            )

    def _send(self, payload: dict) -> None:
        if self._closed:
            raise RuntimeError("OCR worker is closed")
        if self._proc.poll() is not None:
            raise RuntimeError(f"OCR worker already exited rc={self._proc.returncode}")
        stdin = self._proc.stdin
        if stdin is None:
            raise RuntimeError("OCR worker stdin missing")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        stdin.write(struct.pack(">I", len(data)))
        stdin.write(data)
        stdin.flush()

    def _recv(self, timeout: float) -> dict:
        try:
            return self._responses.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"OCR worker timed out after {timeout:.1f}s")

    def provider_map(self) -> dict[str, list[str]]:
        return dict(self._providers)

    def __call__(self, img_np):
        import numpy as np

        arr = np.asarray(img_np)
        if arr.ndim == 2:
            mode = "L"
            height, width = arr.shape
        elif arr.ndim == 3 and arr.shape[2] >= 3:
            if arr.shape[2] > 3:
                arr = arr[:, :, :3]
            mode = "RGB"
            height, width = arr.shape[:2]
        else:
            raise ValueError(f"unsupported image shape for OCR worker: {arr.shape}")

        if arr.dtype != np.uint8:
            arr = arr.astype(np.uint8)
        arr = np.ascontiguousarray(arr)
        payload = {
            "cmd": "infer",
            "mode": mode,
            "size": [int(width), int(height)],
            "data": base64.b64encode(arr.tobytes()).decode("ascii"),
        }
        with self._lock:
            self._send(payload)
            response = self._recv(timeout=self._infer_timeout)
        if not response.get("ok"):
            raise RuntimeError(response.get("error") or "OCR worker inference failed")
        texts = response.get("texts") or []
        elapsed = response.get("elapsed") or []
        return [(None, text) for text in texts], elapsed

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._proc.poll() is None:
                try:
                    self._send({"cmd": "shutdown"})
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=1.5)
                except Exception:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=1.5)
                    except Exception:
                        self._proc.kill()
        finally:
            for pipe in (self._proc.stdin, self._proc.stdout):
                try:
                    if pipe:
                        pipe.close()
                except Exception:
                    pass
        self._closed = True


def _get_ocr_worker_python() -> str:
    """Prefer the project venv Python so worker deps match Aria's requirements."""

    repo_root = Path(__file__).resolve().parents[2]
    if sys.platform == "win32":
        venv_python = repo_root / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = repo_root / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def _close_rapidocr_engine(engine) -> None:
    try:
        close = getattr(engine, "close", None)
        if callable(close):
            close()
    except Exception as exc:
        _ocr_log(f"OCR engine close failed: {type(exc).__name__}: {exc}")


def _v5_models_present() -> bool:
    return all(os.path.exists(p) for p in _V5_PATHS.values())


def _rapidocr_component_providers(engine) -> dict[str, list[str]]:
    """Return ONNX Runtime providers for RapidOCR det/cls/rec sessions.

    rapidocr_onnxruntime 1.4.x stores detector/classifier sessions under
    ``component.infer.session`` and recognizer under ``component.session.session``.
    This helper is intentionally defensive so future package changes degrade to
    "unknown providers" instead of crashing unrelated OCR startup paths.
    """
    provider_map = getattr(engine, "provider_map", None)
    if callable(provider_map):
        try:
            return provider_map()
        except Exception:
            return {}

    providers: dict[str, list[str]] = {}
    for name, attr in (
        ("det", "text_det"),
        ("cls", "text_cls"),
        ("rec", "text_rec"),
    ):
        component = getattr(engine, attr, None)
        runner = getattr(component, "infer", None) or getattr(
            component, "session", None
        )
        session = getattr(runner, "session", None)
        if hasattr(session, "get_providers"):
            try:
                providers[name] = list(session.get_providers())
            except Exception:
                providers[name] = []
        else:
            providers[name] = []
    return providers


def _format_provider_map(providers: dict[str, list[str]]) -> str:
    return ", ".join(
        f"{name}={provider_list or ['unknown']}"
        for name, provider_list in providers.items()
    )


def _verify_rapidocr_providers(engine, use_dml: bool) -> None:
    """Ensure the requested provider actually attached to every OCR stage."""
    providers = _rapidocr_component_providers(engine)
    _ocr_log(f"RapidOCR provider map: {_format_provider_map(providers)}")

    if use_dml:
        bad = {
            name: provider_list
            for name, provider_list in providers.items()
            if not provider_list or provider_list[0] != _DML_PROVIDER
        }
        if bad:
            raise RuntimeError(
                "DML provider did not attach to all OCR stages: "
                f"{_format_provider_map(bad)}"
            )
    else:
        bad = {
            name: provider_list
            for name, provider_list in providers.items()
            if provider_list and _CPU_PROVIDER not in provider_list
        }
        if bad:
            raise RuntimeError(
                "CPU provider missing from OCR stages: " f"{_format_provider_map(bad)}"
            )


def _build_rapidocr_smoke_image():
    """Small text-bearing image that exercises det/cls/rec at init time."""
    from PIL import Image, ImageDraw, ImageFont
    import numpy as np

    img = Image.new("RGB", (360, 120), (245, 246, 248))
    draw = ImageDraw.Draw(img)
    font = None
    for font_path in (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
    ):
        try:
            if os.path.exists(font_path):
                font = ImageFont.truetype(font_path, 28)
                break
        except Exception:
            font = None
    if font is None:
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None
    draw.text((18, 38), "Aria OCR 123", fill=(20, 20, 40), font=font)
    return np.array(img)


def _try_init_rapidocr(use_v5: bool, use_dml: bool):
    """Attempt one RapidOCR configuration. Returns engine on success, None on failure.

    Three valid configurations worth trying in order:
      (v5, dml) → fast+accurate on DX12 GPU
      (v5, cpu) → accurate on any machine
      (v4, cpu) → legacy bundled (last-resort)
    A smoke test is run to confirm both providers and ops work.
    """
    engine = None
    try:
        kwargs = {}
        if use_v5:
            if not _v5_models_present():
                _ocr_log(f"v5 models missing under {_V5_MODEL_DIR}")
                return None
            kwargs.update(_V5_PATHS)

        if use_dml:
            # DML can native-crash inside ORT.  Keep it outside Aria's main
            # process so a bad driver/provider failure becomes a normal tier
            # fallback instead of a full app crash.
            engine = _RapidOCRWorkerProxy(use_v5=use_v5, use_dml=True)
        else:
            # Ensure ORT DLL lookup works when launched from pythonw (no stdout)
            ort_dir = os.path.join(
                sys.prefix, "Lib", "site-packages", "onnxruntime", "capi"
            )
            if os.path.isdir(ort_dir):
                os.add_dll_directory(ort_dir)

            from rapidocr_onnxruntime import RapidOCR

            engine = RapidOCR(**kwargs)

        _verify_rapidocr_providers(engine, use_dml=use_dml)

        # Smoke test: DML provider may fall back to CPU silently if session
        # can't attach to the GPU. A tiny text-bearing inference forces the
        # actual det/rec kernels — if DML is broken we'll see it here and move on.
        result, _elapsed = engine(_build_rapidocr_smoke_image())
        _ocr_log(
            "RapidOCR init smoke ok "
            f"(v5={use_v5}, dml={use_dml}, boxes={len(result) if result else 0})"
        )
        return engine
    except Exception as e:
        if engine is not None:
            _close_rapidocr_engine(engine)
        _ocr_log(
            f"RapidOCR init failed (v5={use_v5}, dml={use_dml}): "
            f"{type(e).__name__}: {e}"
        )
        return None


def _init_rapidocr(force_cpu: bool = False, enable_dml: bool = True) -> bool:
    """Three-tier probe with auto-downgrade.

    Tier order is v5+CPU → v4+CPU by default, or
    v5+DML → v5+CPU → v4+CPU when enable_dml=True.
    force_cpu=True skips the DML tier (for diagnostic UI toggle).
    enable_dml=False also skips the DML tier.
    Returns True if any tier succeeded.
    """
    global _rapidocr_engine, _rapidocr_tier, _rapidocr_force_cpu
    global _rapidocr_runtime_failures
    # Treat "DML disabled" as an effective CPU request so the cache key never
    # reuses a DML engine for a runtime that explicitly asked for the CPU path.
    requested_force_cpu = bool(force_cpu)
    enable_dml = bool(enable_dml)
    force_cpu = requested_force_cpu or not enable_dml

    with _rapidocr_lock:
        if _rapidocr_engine is not None:
            if _rapidocr_force_cpu == force_cpu:
                return True
            _ocr_log(
                "OCR backend reset: force_cpu changed "
                f"{_rapidocr_force_cpu} -> {force_cpu}"
            )
            _close_rapidocr_engine(_rapidocr_engine)
            _rapidocr_engine = None
            _rapidocr_tier = None
            _rapidocr_force_cpu = None
            _rapidocr_runtime_failures = 0

        # Candidate order — each tuple: (label, use_v5, use_dml)
        attempts = []
        if not force_cpu:
            attempts.append(("v5_dml", True, True))
        attempts.append(("v5_cpu", True, False))
        attempts.append(("v4_cpu", False, False))

        for label, use_v5, use_dml in attempts:
            engine = _try_init_rapidocr(use_v5, use_dml)
            if engine is not None:
                _rapidocr_engine = engine
                _rapidocr_tier = label
                _rapidocr_force_cpu = force_cpu
                _rapidocr_runtime_failures = 0
                _ocr_log(
                    f"OCR backend ready: tier={label} force_cpu={requested_force_cpu} "
                    f"enable_dml={enable_dml} effective_cpu={force_cpu}"
                )
                return True

    _ocr_log("OCR init: all tiers failed")
    return False


def reset_rapidocr_backend(reason: str = "manual") -> None:
    """Clear the cached RapidOCR engine so the next access re-probes tiers."""
    global _rapidocr_engine, _rapidocr_tier, _rapidocr_force_cpu
    global _rapidocr_runtime_failures
    with _rapidocr_lock:
        _close_rapidocr_engine(_rapidocr_engine)
        _rapidocr_engine = None
        _rapidocr_tier = None
        _rapidocr_force_cpu = None
        _rapidocr_runtime_failures = 0
    _ocr_log(f"OCR backend reset: {reason}")


def _fallback_rapidocr_to_cpu(reason: str) -> bool:
    """Switch a failing v5_dml runtime to v5_cpu without user intervention."""
    global _rapidocr_engine, _rapidocr_tier, _rapidocr_force_cpu
    global _rapidocr_runtime_failures
    with _rapidocr_lock:
        if _rapidocr_tier != "v5_dml":
            return False
        _ocr_log(f"OCR runtime fallback: v5_dml -> v5_cpu ({reason})")
        old_engine = _rapidocr_engine
        engine = _try_init_rapidocr(use_v5=True, use_dml=False)
        if engine is None:
            engine = _try_init_rapidocr(use_v5=False, use_dml=False)
            label = "v4_cpu"
        else:
            label = "v5_cpu"

        if engine is None:
            _ocr_log("OCR runtime fallback failed: CPU tiers unavailable")
            return False

        _close_rapidocr_engine(old_engine)
        _rapidocr_engine = engine
        _rapidocr_tier = label
        _rapidocr_force_cpu = True
        _rapidocr_runtime_failures = 0
        _ocr_log(f"OCR runtime fallback ready: tier={label}")
        return True


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
    """Run one frame through the loaded RapidOCR engine.

    Logs per-stage timing (det/cls/rec) so we can diagnose which stage is slow
    in the wild — on DML, det tends to dominate; on CPU it's rec per-box.
    """
    try:
        import numpy as np
        import time as _t

        img_np = np.array(img)
        with _rapidocr_lock:
            engine = _rapidocr_engine
            tier = _rapidocr_tier
        if engine is None:
            _ocr_log("RapidOCR error: engine is not initialized")
            return None

        t0 = _t.perf_counter()
        result, elapsed = engine(img_np)
        total_ms = (_t.perf_counter() - t0) * 1000

        # elapsed is [det_s, cls_s, rec_s] from RapidOCR (may be None on empty)
        if elapsed and isinstance(elapsed, (list, tuple)) and len(elapsed) >= 3:
            det_ms = (elapsed[0] or 0) * 1000
            cls_ms = (elapsed[1] or 0) * 1000
            rec_ms = (elapsed[2] or 0) * 1000
            _ocr_log(
                f"OCR[{tier}] total={total_ms:.0f}ms "
                f"(det={det_ms:.0f} cls={cls_ms:.0f} rec={rec_ms:.0f})"
            )
        else:
            _ocr_log(f"OCR[{tier}] total={total_ms:.0f}ms (no boxes)")

        if not result:
            return None
        with _rapidocr_lock:
            global _rapidocr_runtime_failures
            _rapidocr_runtime_failures = 0
        return " ".join([r[1] for r in result])
    except Exception as e:
        with _rapidocr_lock:
            tier = _rapidocr_tier
            _rapidocr_runtime_failures += 1
            failures = _rapidocr_runtime_failures
        _ocr_log(f"RapidOCR error[{tier}] #{failures}: {type(e).__name__}: {e}")
        if tier == "v5_dml" and _fallback_rapidocr_to_cpu(
            f"runtime_error:{type(e).__name__}"
        ):
            try:
                with _rapidocr_lock:
                    retry_engine = _rapidocr_engine
                    retry_tier = _rapidocr_tier
                if retry_engine is None:
                    return None
                t0 = _t.perf_counter()
                result, elapsed = retry_engine(img_np)
                total_ms = (_t.perf_counter() - t0) * 1000
                _ocr_log(f"OCR retry[{retry_tier}] total={total_ms:.0f}ms")
                if not result:
                    return None
                return " ".join([r[1] for r in result])
            except Exception as retry_e:
                _ocr_log(
                    f"RapidOCR retry error[{_rapidocr_tier}]: "
                    f"{type(retry_e).__name__}: {retry_e}"
                )
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

    def __init__(
        self,
        max_text_len: int = 2000,
        force_cpu: bool = False,
        enable_dml: bool = True,
        on_text_extracted=None,
    ):
        self._max_text_len = max_text_len
        # DirectML runs out-of-process so native ORT access violations kill only
        # the OCR worker.  The parent then falls back to CPU instead of losing
        # Aria's main pythonw.exe process.
        self._force_cpu = bool(force_cpu)
        self._enable_dml = bool(enable_dml)
        # Optional sink for downstream session-scoped consumers (e.g. the
        # auto-hotword tracker). Called as `cb(ocr_text, window_title)` on
        # every successful OCR result. Failures are swallowed to never break
        # the OCR pipeline itself.
        self._on_text_extracted = on_text_extracted
        # Layer 0: title context (instant)
        self._title_text: str = ""
        self._title_hwnd: int = 0
        self._title_hash: str = ""
        # Layer 2: OCR context (slow, background) — keeps original layout (newlines)
        self._ocr_text: str = ""
        # Cache key is hwnd-only so that in-window title changes (tabs, cursors,
        # file-name updates in IDEs) don't invalidate expensively-captured OCR.
        # Only a real foreground-window switch should flush the cache.
        self._ocr_cache_key: str = ""
        self._ocr_time: float = 0.0
        self._ocr_ttl: float = 30.0  # seconds — long dictations keep context fresh
        # Shared
        self._lock = threading.Lock()
        self._running = False
        self._pending_trigger = False
        self._pending_force = False
        # Runtime timing model used by the polish layer to decide whether a
        # small extra wait is likely to actually catch the in-flight OCR.  The
        # default is overwritten once the real OCR tier is known.  It remains
        # conservative before init so a cold unknown backend does not consume a
        # 0.45-1.2s latency budget unless it is already close to finishing.
        self._running_started_at: float = 0.0
        self._ocr_duration_ema: float = 4.2
        self._ocr_duration_samples: int = 0
        self._ocr_duration_default: float = 4.2
        # Short-lived dynamic screen memory.  When the user types/sees a rare
        # term in one window and immediately switches to another window to
        # dictate about it, current OCR alone can lose that context.  This is
        # not a static hotword table: entries are captured from OCR/title at
        # runtime, expire quickly, and are excluded from same-hwnd reads to
        # avoid duplication.
        self._recent_context: list[tuple[float, str, str]] = []
        self._recent_context_ttl: float = 180.0
        self._recent_context_max: int = 4
        self._recent_context_chars: int = 600
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

            if _init_rapidocr(
                force_cpu=self._force_cpu,
                enable_dml=self._enable_dml,
            ):
                # Downstream code keeps using "rapidocr" as the coarse selector;
                # the precise tier (v5_dml / v5_cpu / v4_cpu) lives in the
                # module-level _rapidocr_tier and is surfaced via ocr_tier().
                self._ocr_backend = "rapidocr"
            elif _init_winocr():
                self._ocr_backend = "winocr"
            else:
                self._ocr_backend = "none"

            self._available = True  # At minimum, Layer 0 (title) always works
            self._apply_initial_timing_profile()
            _ocr_log(
                f"OCR backend: {self._ocr_backend} tier={_rapidocr_tier or 'n/a'} "
                f"force_cpu={self._force_cpu} enable_dml={self._enable_dml}"
            )
        return self._available

    def _apply_initial_timing_profile(self) -> None:
        """Choose an OCR-duration prior from the selected backend tier."""
        if self._ocr_backend != "rapidocr":
            default = 1.2
        elif _rapidocr_tier == "v5_dml":
            # Local v5_dml warm runs are usually 150-650ms after screenshot
            # capture.  Use 0.9s as a cautious first-run prior so the 1.2s
            # cold/window-switch wait path can actually catch a nearly-finished
            # OCR instead of inheriting the old CPU-era 4.2s estimate.
            default = 0.9
        elif _rapidocr_tier == "v5_cpu":
            default = 1.8
        else:
            default = 3.0

        with self._lock:
            self._ocr_duration_default = default
            if self._ocr_duration_samples <= 0:
                self._ocr_duration_ema = default

    def ocr_tier(self) -> str:
        """Precise backend identifier for UI/diagnostics: v5_dml | v5_cpu | v4_cpu | winocr | none."""
        if self._ocr_backend == "rapidocr":
            return _rapidocr_tier or "unknown"
        return self._ocr_backend

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
            if DebugConfig.save_screen_text:
                _ocr_log(f"Title: '{title_text[:60]}' (hash={title_hash})")
            else:
                _ocr_log(f"Title updated: len={len(title_text)} hash={title_hash}")

        return title_text

    def trigger(self, force: bool = False) -> None:
        """Layer 1+2: Trigger UIA + OCR in background (non-blocking).

        Args:
            force: If True, bypass _run_background's TTL cache-hit short-circuit
                so a fresh screenshot is taken even within the TTL window.
                Used by _on_speech_start to capture the screen the user is
                literally looking at right now (window content can change
                without an hwnd switch — e.g. scrolling, tab content reload).
        """
        if not self.available:
            return

        # Also update title immediately (Layer 0)
        self.update_title()

        with self._lock:
            if self._running:
                # Coalesce concurrent requests instead of dropping them.  This
                # matters when a slow OCR run is still processing an old window
                # and the user switches to another terminal/browser then speaks:
                # the latest screen request must run right after the old one.
                self._pending_trigger = True
                self._pending_force = self._pending_force or force
                _ocr_log(f"Trigger queued while OCR running (force={force})")
                return
            self._running = True
            self._running_started_at = time.time()

        self._start_worker(force)

    def _start_worker(self, force: bool) -> None:
        thread = threading.Thread(
            target=self._run_background,
            args=(force,),
            daemon=True,
            name="screen-ocr",
        )
        thread.start()

    def get_text(self, current_hwnd: int = 0) -> str:
        """Flattened context (title + OCR, single line).

        Historically consumed by ASR hotword biasing. Polish layer should use
        get_text_for_polish() instead which preserves newlines.
        """
        with self._lock:
            parts = []

            if self._title_text:
                parts.append(self._title_text)

            if self._ocr_text:
                age = time.time() - self._ocr_time if self._ocr_time else 999
                if age <= self._ocr_ttl:
                    current_key = str(self._title_hwnd)
                    if self._ocr_cache_key == current_key:
                        # Flatten newlines for legacy ASR consumers
                        parts.append(re.sub(r"\s+", " ", self._ocr_text).strip())

            return " ".join(parts) if parts else ""

    def get_text_for_polish(self, max_chars: int = 1200) -> str:
        """OCR text for LLM Polish layer — preserves layout, deduped, capped.

        Cache semantics:
        - Cache is valid as long as the HWND matches. TTL does NOT invalidate
          the returned value — it only gates whether _on_speech_start should
          re-trigger a background refresh. Reason: a user who stared at a
          Chrome page for 60 seconds then spoke would otherwise lose the OCR
          data they're literally looking at (TTL was 30s). Screen content
          changes are detected via hwnd switch (cache key flushes) or
          on-demand trigger(); stale-same-window content is still far better
          signal for the LLM than "title only".
        - If OCR never ran for this hwnd yet, fallback to title.

        Differences from get_text():
        - Keeps newlines so the LLM can see structure
        - Dedupes repeated lines (OCR often recognizes the same UI element twice)
        - Drops pure-digit / single-char lines (noise)
        - Caps at max_chars, keeping the tail (newer content more likely relevant)
        """
        with self._lock:
            title = self._title_text
            ocr_text = self._ocr_text
            current_key = str(self._title_hwnd)
            cache_valid = self._ocr_cache_key == current_key
            recent_context = self._format_recent_context_locked(
                exclude_key=current_key,
                max_chars=min(400, max_chars // 3),
            )

        if not cache_valid or not ocr_text:
            return self._compose_polish_text(
                title=title,
                body_text="",
                recent_context=recent_context,
                max_chars=max_chars,
            )

        # Dedupe lines (preserve order)
        seen = set()
        kept_lines = []
        for line in ocr_text.split("\n"):
            stripped = line.strip()
            if not stripped or len(stripped) < 2:
                continue
            if stripped.isdigit():
                continue
            if stripped in seen:
                continue
            seen.add(stripped)
            kept_lines.append(stripped)

        text = "\n".join(kept_lines)

        # Reserve a small fixed budget for recent cross-window context.  The
        # current screen still gets the majority of the prompt, but recent
        # runtime context should survive a window switch.
        body_budget = max(300, max_chars - len(recent_context) - len(title) - 80)

        # Cap, keeping tail (most recent/relevant content usually at end)
        if len(text) > body_budget:
            text = text[-body_budget:]
            # Drop leading partial line after tail-cut
            nl = text.find("\n")
            if 0 < nl < 50:
                text = text[nl + 1 :]

        return self._compose_polish_text(
            title=title,
            body_text=text,
            recent_context=recent_context,
            max_chars=max_chars,
        )

    def _compose_polish_text(
        self, title: str, body_text: str, recent_context: str, max_chars: int
    ) -> str:
        """Build labeled context for the Polish layer."""
        parts = []
        if title:
            # High-confidence signal: Win32 GetWindowText, no OCR error.
            parts.append(f"窗口标题: {title}")
        if recent_context:
            # Runtime-only memory from the last few OCR/title captures.  Placed
            # before 页面内容 so deterministic screen_corrector treats these
            # recent terms as trusted context, but the label makes provenance
            # clear to the LLM prompt.
            parts.append(f"近期屏幕上下文(短期记忆):\n{recent_context}")
        if body_text:
            parts.append(f"页面内容(OCR):\n{body_text}")

        combined = "\n\n".join(parts)
        if len(combined) > max_chars + len(title) + 64:
            combined = combined[-(max_chars + len(title) + 64) :]
            nl = combined.find("\n")
            if 0 < nl < 80:
                combined = combined[nl + 1 :]
        return combined

    def wait_for_pending(self, timeout: float = 2.5) -> bool:
        """Block up to `timeout` seconds waiting for a background OCR to finish.

        Used by the Polish layer: if OCR just started (e.g. Aria cold start or
        right after a window switch), give it a brief window to complete so
        we can feed the LLM current screen text. Without this, a user who
        speaks within ~10s of launching Aria or switching windows would hit
        the empty-cache fallback and lose screen-aware correction.

        Returns True if OCR finished (or wasn't running), False on timeout.
        """
        with self._lock:
            if not self._running:
                return True
        import time as _t

        start = _t.time()
        while (_t.time() - start) < timeout:
            with self._lock:
                if not self._running:
                    return True
            _t.sleep(0.05)
        with self._lock:
            return not self._running

    def wait_for_current_cache(self, timeout: float = 2.5) -> bool:
        """Wait until OCR text for the current title HWND is available.

        This differs from wait_for_pending(): ScreenOCR keeps _running=True
        while handing off to a coalesced queued trigger, but the first finished
        run may already have populated usable OCR for the current window.  The
        Polish layer cares about having current screen text, not about the whole
        background refresh queue becoming idle.
        """
        try:
            timeout = max(0.0, float(timeout))
        except Exception:
            timeout = 0.0

        with self._lock:
            target_key = str(self._title_hwnd) if self._title_hwnd else ""
            if (
                target_key
                and self._ocr_cache_key == target_key
                and bool(self._ocr_text)
            ):
                return True

        if timeout <= 0:
            return False

        import time as _t

        start = _t.time()
        while (_t.time() - start) < timeout:
            with self._lock:
                if (
                    target_key
                    and self._ocr_cache_key == target_key
                    and bool(self._ocr_text)
                ):
                    return True
                if not self._running and not self._pending_trigger:
                    return False
            _t.sleep(0.05)

        with self._lock:
            return bool(
                target_key
                and self._ocr_cache_key == target_key
                and bool(self._ocr_text)
            )

    def plan_wait_for_pending(
        self,
        max_wait: float,
        safety_margin: float = 0.15,
        allow_queued_latest: bool = False,
        allow_overdue: bool = False,
    ) -> tuple[float, dict]:
        """Return a precise wait budget for the current OCR, or 0 to skip.

        The app calls this before wait_for_pending().  We only spend user-visible
        latency when the in-flight OCR is predicted to finish inside the caller's
        budget.  If a newer trigger is queued, the default remains to skip: the
        currently running OCR may be stale relative to the latest screen.  The
        caller can opt into waiting anyway for no-cache/name-test paths, where a
        slightly stale OCR result is still much better than title-only context.

        Returns:
            (wait_seconds, info) where wait_seconds is 0 when waiting would be
            unlikely to help.  info is log/debug metadata.
        """
        try:
            max_wait = float(max_wait)
        except Exception:
            max_wait = 0.0

        now = time.time()
        with self._lock:
            running = self._running
            pending_trigger = self._pending_trigger
            started_at = self._running_started_at
            estimate = self._ocr_duration_ema or self._ocr_duration_default
            samples = self._ocr_duration_samples

        info = {
            "max_wait": max_wait,
            "running": running,
            "pending_trigger": pending_trigger,
            "elapsed": 0.0,
            "estimate": estimate,
            "remaining": 0.0,
            "samples": samples,
            "decision": "skip",
            "reason": "",
            "allow_queued_latest": allow_queued_latest,
            "allow_overdue": allow_overdue,
        }

        if max_wait <= 0:
            info["reason"] = "no_budget"
            return 0.0, info
        if not running:
            info["decision"] = "done"
            info["reason"] = "idle"
            return 0.0, info
        if pending_trigger and not allow_queued_latest:
            info["reason"] = "queued_latest"
            return 0.0, info
        if started_at <= 0:
            info["reason"] = "unknown_start"
            return 0.0, info

        elapsed = max(0.0, now - started_at)
        remaining = max(0.0, estimate - elapsed)
        info["elapsed"] = elapsed
        info["remaining"] = remaining

        if elapsed >= estimate and allow_overdue:
            # The predictor can be optimistic on a cold worker or when UIA/capture
            # has already run longer than the fast DML prior.  For screen-critical
            # no-cache utterances, spend the caller's bounded budget instead of
            # doing a token 150ms wait that almost always misses the finishing OCR.
            info["decision"] = "wait"
            info["reason"] = "queued_overdue" if pending_trigger else "overdue_running"
            return max_wait, info

        # Require a small safety margin; otherwise a "1.2s wait" frequently
        # becomes a full timeout for OCRs whose estimate was optimistic.
        if remaining + max(0.0, safety_margin) <= max_wait:
            wait_time = min(max_wait, max(0.05, remaining + safety_margin))
            info["decision"] = "wait"
            info["reason"] = (
                "queued_predicted_finish" if pending_trigger else "predicted_finish"
            )
            return wait_time, info

        info["reason"] = "not_enough_budget"
        return 0.0, info

    def has_cached_for_current_hwnd(self) -> bool:
        """True if we have usable OCR content matching the current foreground hwnd.

        Callers use this to choose how long to wait_for_pending(): with a
        cache, a short wait is fine (the stale-but-matching content is still
        useful signal); without one, a longer wait is justified since the
        fallback is title-only.
        """
        with self._lock:
            return (
                bool(self._ocr_cache_key)
                and bool(self._ocr_text)
                and self._ocr_cache_key == str(self._title_hwnd)
            )

    def get_ocr_age(self) -> float:
        """Age of cached OCR text in seconds. Returns large number if never run."""
        with self._lock:
            if self._ocr_time == 0.0:
                return 99999.0
            return time.time() - self._ocr_time

    def _run_background(self, force: bool = False) -> None:
        """Run Layer 1 (UIA) + Layer 2 (OCR) in background.

        Args:
            force: If True, skip the TTL cache-hit short-circuit and always
                take a fresh screenshot. Used by speech_start triggers so the
                LLM gets the screen the user is looking at right now (window
                content can change without an hwnd switch — scrolling, tab
                content reload, IDE file change).
        """
        run_started_at = time.time()
        measured_run = False
        with self._lock:
            self._running_started_at = run_started_at

        try:
            hwnd = self._get_foreground_hwnd()
            if not hwnd:
                return

            # Cache key is hwnd-only. Title changes within the same window
            # (tab switches, cursor position, IDE status line) must not
            # invalidate the expensively-captured OCR.
            cache_key = str(hwnd)

            if not force:
                with self._lock:
                    if self._ocr_cache_key == cache_key:
                        age = time.time() - self._ocr_time
                        if age < self._ocr_ttl:
                            _ocr_log(f"Cache hit (age={age:.1f}s)")
                            return  # Cache still fresh

            # Layer 1: Try UIA for non-browser apps
            measured_run = True
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
                title_snapshot = _extract_title_keywords(hwnd)
                with self._lock:
                    self._ocr_text = text
                    self._ocr_cache_key = cache_key
                    self._ocr_time = time.time()
                    self._remember_recent_context_locked(
                        hwnd=hwnd,
                        title=title_snapshot,
                        text=text,
                    )
                _ocr_log(f"Result ({backend_used}): {len(text)} chars")
                # Fire downstream sink (e.g. auto-hotword tracker). Wrapped in
                # try/except so a misbehaving consumer can never poison OCR.
                if self._on_text_extracted is not None:
                    try:
                        self._on_text_extracted(text, title_snapshot)
                    except Exception as cb_err:
                        _ocr_log(f"on_text_extracted callback error: {cb_err}")

        except Exception as e:
            _ocr_log(f"Background OCR error: {e}")
        finally:
            next_force: Optional[bool] = None
            with self._lock:
                if measured_run:
                    self._record_ocr_duration_locked(time.time() - run_started_at)
                if self._pending_trigger:
                    next_force = self._pending_force
                    self._pending_trigger = False
                    self._pending_force = False
                    # Keep _running=True across the handoff so waiters know
                    # that the coalesced latest-screen run is still pending.
                else:
                    self._running = False

            if next_force is not None:
                _ocr_log(f"Running queued OCR trigger (force={next_force})")
                self._start_worker(next_force)

    def _record_ocr_duration_locked(self, duration_s: float) -> None:
        """Update OCR duration EMA. Must be called with self._lock held."""
        if duration_s <= 0:
            return
        # Bound pathological values so a one-off hang or cache race does not
        # poison the predictor for the rest of the session.
        duration_s = max(0.05, min(15.0, duration_s))
        if self._ocr_duration_samples <= 0:
            self._ocr_duration_ema = duration_s
        else:
            alpha = 0.25
            self._ocr_duration_ema = (
                alpha * duration_s + (1.0 - alpha) * self._ocr_duration_ema
            )
        self._ocr_duration_samples += 1
        _ocr_log(
            f"OCR duration EMA updated: last={duration_s:.2f}s, "
            f"ema={self._ocr_duration_ema:.2f}s, n={self._ocr_duration_samples}"
        )

    def _run_screenshot_ocr(self, hwnd) -> Optional[str]:
        """Layer 2: Capture window screenshot and run OCR."""
        if self._ocr_backend == "none":
            return None
        try:
            from PIL import ImageGrab, Image
            from ctypes import wintypes

            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 50 or h < 50:
                return None
            img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))

            # Downscale large screenshots before OCR. RapidOCR runtime is
            # dominated by per-character recognition; fewer detected chars =
            # faster. Capping long edge at 1500px takes ~5-7s 4K captures
            # down to ~2-3s with negligible accuracy loss on body-text UI.
            # Tiny status-bar pixels may become unreadable but those aren't
            # the signal we want anyway.
            MAX_LONG_EDGE = 1500
            orig_w, orig_h = img.size
            if max(orig_w, orig_h) > MAX_LONG_EDGE:
                scale = MAX_LONG_EDGE / max(orig_w, orig_h)
                new_w = int(orig_w * scale)
                new_h = int(orig_h * scale)
                img = img.resize((new_w, new_h), Image.BILINEAR)
                _ocr_log(f"Resized {orig_w}x{orig_h} -> {new_w}x{new_h} for faster OCR")
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
        """Clean OCR output while preserving newlines for structure-aware consumers.

        Newlines are kept so get_text_for_polish() can see the layout. Legacy
        get_text() (ASR consumers) flattens whitespace at read time.
        """
        if backend == "winocr":
            cjk = r"[\u4e00-\u9fff\u3400-\u4dbf]"
            text = re.sub(f"({cjk})\\s+({cjk})", r"\1\2", text)
            text = re.sub(f"({cjk})\\s+({cjk})", r"\1\2", text)

        # Collapse intra-line whitespace runs to single space, but keep newlines
        text = "\n".join(
            re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()
        )
        # Collapse 3+ consecutive newlines to 2
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        if len(text) > self._max_text_len:
            text = text[: self._max_text_len]

        return text

    def _remember_recent_context_locked(self, hwnd: int, title: str, text: str) -> None:
        """Store a compact runtime-only OCR/title snapshot.

        Must be called with self._lock held.
        """
        cache_key = str(hwnd)
        snippet = self._build_recent_context_snippet(title, text)
        if not snippet:
            return

        now = time.time()
        cutoff = now - self._recent_context_ttl
        self._recent_context = [
            item
            for item in self._recent_context
            if item[0] >= cutoff and item[1] != cache_key
        ]
        self._recent_context.append((now, cache_key, snippet))
        if len(self._recent_context) > self._recent_context_max:
            self._recent_context = self._recent_context[-self._recent_context_max :]

    def _build_recent_context_snippet(self, title: str, text: str) -> str:
        parts = []
        if title:
            parts.append(f"窗口标题: {title}")
        compact = re.sub(r"\s+", " ", text or "").strip()
        if compact:
            if len(compact) > self._recent_context_chars:
                compact = compact[-self._recent_context_chars :]
            parts.append(compact)
        return "\n".join(parts).strip()

    def _format_recent_context_locked(self, exclude_key: str, max_chars: int) -> str:
        """Return newest cross-window runtime context, capped for prompt size.

        Must be called with self._lock held.
        """
        now = time.time()
        cutoff = now - self._recent_context_ttl
        self._recent_context = [
            item for item in self._recent_context if item[0] >= cutoff
        ]

        chunks = []
        total = 0
        for _ts, key, snippet in reversed(self._recent_context):
            if key == exclude_key:
                continue
            if not snippet:
                continue
            if total + len(snippet) > max_chars and chunks:
                break
            chunks.append(snippet)
            total += len(snippet) + 2

        text = "\n\n".join(chunks)
        if len(text) > max_chars:
            text = text[-max_chars:]
            nl = text.find("\n")
            if 0 < nl < 80:
                text = text[nl + 1 :]
        return text
