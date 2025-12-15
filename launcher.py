#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""VoiceType Launcher with error logging, singleton check, and splash screen."""

import sys
import os
import tempfile
import atexit
import time
import subprocess

# Fix OpenMP conflict between PyTorch and faster-whisper (MUST be before any imports)
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

# === Singleton Check ===
# Prevent multiple instances from running simultaneously
LOCK_FILE = os.path.join(tempfile.gettempdir(), "voicetype.lock")
_lock_handle = None


def acquire_lock():
    """Try to acquire singleton lock. Returns True if successful."""
    global _lock_handle
    try:
        # On Windows, opening with exclusive access acts as a lock
        if sys.platform == "win32":
            import msvcrt

            _lock_handle = open(LOCK_FILE, "w")
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            _lock_handle.write(str(os.getpid()))
            _lock_handle.flush()
            return True
        else:
            # Unix: use fcntl
            import fcntl

            _lock_handle = open(LOCK_FILE, "w")
            fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _lock_handle.write(str(os.getpid()))
            _lock_handle.flush()
            return True
    except (IOError, OSError):
        return False


def release_lock():
    """Release the singleton lock."""
    global _lock_handle
    if _lock_handle:
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
            _lock_handle.close()
        except Exception:
            pass
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass


# Check singleton before doing anything expensive
if not acquire_lock():
    print("VoiceType is already running! Only one instance allowed.")
    print("Check system tray or use Task Manager to close existing instance.")
    sys.exit(1)

# Register cleanup
atexit.register(release_lock)

# Patch subprocess to hide console windows on Windows (for ffmpeg calls from whisper)
if sys.platform == "win32":
    import subprocess

    _orig_popen_init = subprocess.Popen.__init__

    def _patched_popen_init(self, args, **kwargs):
        if "startupinfo" not in kwargs or kwargs["startupinfo"] is None:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            kwargs["startupinfo"] = si
        if "creationflags" not in kwargs:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        _orig_popen_init(self, args, **kwargs)

    subprocess.Popen.__init__ = _patched_popen_init

LOG_FILE = os.path.join(os.path.dirname(__file__), "launch_error.log")


def log(msg):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


# === Splash Screen Functions ===


def start_splash():
    """
    Launch splash screen in a separate subprocess.
    Returns (process, reporter) tuple, or (None, None) if splash fails.
    """
    try:
        from voicetype.progress_ipc import find_free_port, ProgressReporter

        # Find available port for IPC
        port = find_free_port()
        address = ("localhost", port)
        log(f"Starting splash on port {port}")

        # Get the splash runner script path
        splash_script = os.path.join(
            os.path.dirname(__file__), "ui", "qt", "splash_runner.py"
        )
        log(f"Splash script path: {splash_script}")
        log(f"Splash script exists: {os.path.exists(splash_script)}")

        # Launch splash as separate Python process using subprocess
        # Use pythonw.exe on Windows to avoid console window
        python_exe = sys.executable
        log(f"Python exe: {python_exe}")
        if sys.platform == "win32" and python_exe.endswith("python.exe"):
            pythonw = python_exe.replace("python.exe", "pythonw.exe")
            if os.path.exists(pythonw):
                python_exe = pythonw
                log(f"Using pythonw: {pythonw}")

        log(f"Launching: {python_exe} {splash_script} {port}")
        splash_proc = subprocess.Popen(
            [python_exe, splash_script, str(port)],
            cwd=r"G:\AIBOX\voicetype",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log(f"Splash process PID: {splash_proc.pid}")

        # Create reporter to send progress - retry connection multiple times
        # Splash needs time to start Python and import PySide6
        reporter = ProgressReporter(address)
        max_retries = 10
        retry_delay = 0.5  # Start with 500ms

        for attempt in range(max_retries):
            time.sleep(retry_delay)
            log(f"Splash connection attempt {attempt + 1}/{max_retries}...")
            if reporter.connect(timeout=1.0):
                log("Splash connection established")
                return splash_proc, reporter
            retry_delay = min(retry_delay * 1.2, 1.0)  # Increase delay, max 1s

        log("Failed to connect to splash after retries - continuing without splash")
        splash_proc.terminate()
        return None, None

    except Exception as e:
        log(f"Splash startup failed: {e}")
        import traceback

        log(traceback.format_exc())
        return None, None


try:
    log(f"=== Launch attempt: {__import__('datetime').datetime.now()} ===")
    log(f"Python: {sys.executable}")
    log(f"Version: {sys.version}")
    log(f"CWD: {os.getcwd()}")

    os.chdir(r"G:\AIBOX")
    sys.path.insert(0, r"G:\AIBOX")
    log("Path set OK")

    # === Start Splash Screen ===
    splash_proc, reporter = start_splash()

    def emit_progress(stage, message=None, percent=None):
        """Helper to send progress - no-op if splash not available."""
        if reporter:
            reporter.emit(stage, message, percent)

    emit_progress("python_env")  # 5%

    # CRITICAL: Import PySide6 FIRST to initialize Windows DLLs properly
    # This prevents PyTorch DLL loading crash (access violation in _load_dll_libraries)
    log("Pre-importing PySide6 for DLL initialization...")
    import PySide6.QtCore  # Minimal Qt import to init DLLs

    log("PySide6 pre-import done")

    # Now safe to load FunASR/PyTorch
    # Check if FunASR is configured and pre-load it
    import json
    from pathlib import Path

    config_path = Path(__file__).parent / "config" / "hotwords.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        asr_engine = config.get("asr_engine", "whisper")
        if asr_engine == "funasr":
            log("Pre-loading FunASR (before Qt imports)...")
            emit_progress("funasr_model", "加载语音模型中...", 20)
            print("Pre-loading FunASR model (before Qt)...")
            from voicetype.core.asr.funasr_engine import FunASREngine, FunASRConfig

            funasr_cfg = config.get("funasr", {})
            pre_config = FunASRConfig(
                model_name=funasr_cfg.get("model_name", "paraformer-zh"),
                vad_model=funasr_cfg.get("vad_model", "fsmn-vad"),
                punc_model=funasr_cfg.get("punc_model", "ct-punc"),
                device=funasr_cfg.get("device", "cuda"),
            )
            _preloaded_asr = FunASREngine(pre_config)
            _preloaded_asr.load()
            import voicetype

            voicetype._preloaded_asr_engine = _preloaded_asr
            log("FunASR pre-loaded successfully")
            emit_progress("funasr_model", "模型加载完成", 50)
            print("FunASR model pre-loaded!")
        elif asr_engine == "fireredasr":
            log("Pre-loading FireRedASR (before Qt imports)...")
            emit_progress("funasr_model", "加载模型中...", 20)
            print("Pre-loading FireRedASR model (before Qt)...")
            # Add FireRedASR repo to path
            fireredasr_path = r"G:\AIBOX\FireRedASR"
            if os.path.exists(fireredasr_path) and fireredasr_path not in sys.path:
                sys.path.insert(0, fireredasr_path)
            from voicetype.core.asr.fireredasr_engine import (
                FireRedASREngine,
                FireRedASRConfig,
            )

            firered_cfg = config.get("fireredasr", {})
            pre_config = FireRedASRConfig(
                model_type=firered_cfg.get("model_type", "aed"),
                model_path=firered_cfg.get(
                    "model_path",
                    r"G:\AIBOX\FireRedASR\pretrained_models\FireRedASR-AED-L",
                ),
                use_gpu=firered_cfg.get("use_gpu", True),
                beam_size=firered_cfg.get("beam_size", 2),
            )
            _preloaded_asr = FireRedASREngine(pre_config)
            _preloaded_asr.load()
            import voicetype

            voicetype._preloaded_asr_engine = _preloaded_asr
            log("FireRedASR pre-loaded successfully")
            emit_progress("funasr_model", "模型加载完成", 50)
            print("FireRedASR model pre-loaded!")
        else:
            # Whisper or other engines - still show progress
            emit_progress("funasr_model", "准备语音引擎...", 50)
    except Exception as e:
        log(f"FunASR pre-load failed: {e}, will fallback in app")
        emit_progress("funasr_model", "模型加载失败，使用备用引擎", 50)

    # Set default hotkey to grave (`)
    sys.argv = [sys.argv[0], "--hotkey", "grave"]

    emit_progress("qt_ui")  # 80%

    from voicetype.ui.qt.main import main

    log("Import OK, starting...")

    emit_progress("audio_capture")  # 95%

    # Signal done - splash will fade out
    emit_progress("done")  # 100%

    # Close reporter
    if reporter:
        reporter.close()

    # Give splash time to show "done" and fade out
    time.sleep(0.5)

    # Run main application
    exit_code = main()

    # Clean up splash if still running
    if (
        splash_proc and splash_proc.poll() is None
    ):  # poll() returns None if still running
        splash_proc.terminate()
        splash_proc.wait(timeout=1)

    sys.exit(exit_code)

except Exception as e:
    import traceback

    log(traceback.format_exc())
    sys.exit(1)
