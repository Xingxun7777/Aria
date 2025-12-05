#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""VoiceType Launcher with error logging and singleton check."""

import sys
import os
import tempfile
import atexit

# Fix OpenMP conflict between PyTorch and faster-whisper (MUST be before any imports)
os.environ['KMP_DUPLICATE_LIB_OK'] = 'TRUE'

# === Singleton Check ===
# Prevent multiple instances from running simultaneously
LOCK_FILE = os.path.join(tempfile.gettempdir(), "voicetype.lock")
_lock_handle = None

def acquire_lock():
    """Try to acquire singleton lock. Returns True if successful."""
    global _lock_handle
    try:
        # On Windows, opening with exclusive access acts as a lock
        if sys.platform == 'win32':
            import msvcrt
            _lock_handle = open(LOCK_FILE, 'w')
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            _lock_handle.write(str(os.getpid()))
            _lock_handle.flush()
            return True
        else:
            # Unix: use fcntl
            import fcntl
            _lock_handle = open(LOCK_FILE, 'w')
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
            if sys.platform == 'win32':
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
if sys.platform == 'win32':
    import subprocess
    _orig_popen_init = subprocess.Popen.__init__
    def _patched_popen_init(self, args, **kwargs):
        if 'startupinfo' not in kwargs or kwargs['startupinfo'] is None:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            kwargs['startupinfo'] = si
        if 'creationflags' not in kwargs:
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        _orig_popen_init(self, args, **kwargs)
    subprocess.Popen.__init__ = _patched_popen_init

LOG_FILE = os.path.join(os.path.dirname(__file__), "launch_error.log")

def log(msg):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

try:
    log(f"=== Launch attempt: {__import__('datetime').datetime.now()} ===")
    log(f"Python: {sys.executable}")
    log(f"Version: {sys.version}")
    log(f"CWD: {os.getcwd()}")

    os.chdir(r"G:\AIBOX")
    sys.path.insert(0, r"G:\AIBOX")
    log("Path set OK")

    # CRITICAL: Load FunASR BEFORE PySide6 (they conflict due to modelscope)
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
            print("Pre-loading FunASR model (before Qt)...")
            from voicetype.core.asr.funasr_engine import FunASREngine, FunASRConfig
            funasr_cfg = config.get("funasr", {})
            pre_config = FunASRConfig(
                model_name=funasr_cfg.get("model", "paraformer-zh"),
                device=funasr_cfg.get("device", "cuda"),
                enable_vad=funasr_cfg.get("enable_vad", True),
                enable_punc=funasr_cfg.get("enable_punc", True)
            )
            # Pre-initialize engine and cache it globally
            _preloaded_asr = FunASREngine(pre_config)
            _preloaded_asr.load()
            # Store in a module-level variable that app.py can access
            import voicetype
            voicetype._preloaded_asr_engine = _preloaded_asr
            log("FunASR pre-loaded successfully")
            print("FunASR model pre-loaded!")
        elif asr_engine == "fireredasr":
            log("Pre-loading FireRedASR (before Qt imports)...")
            print("Pre-loading FireRedASR model (before Qt)...")
            # Add FireRedASR repo to path
            fireredasr_path = r"G:\AIBOX\FireRedASR"
            if os.path.exists(fireredasr_path) and fireredasr_path not in sys.path:
                sys.path.insert(0, fireredasr_path)
            from voicetype.core.asr.fireredasr_engine import FireRedASREngine, FireRedASRConfig
            firered_cfg = config.get("fireredasr", {})
            pre_config = FireRedASRConfig(
                model_type=firered_cfg.get("model_type", "aed"),
                model_path=firered_cfg.get("model_path", r"G:\AIBOX\FireRedASR\pretrained_models\FireRedASR-AED-L"),
                use_gpu=firered_cfg.get("use_gpu", True),
                beam_size=firered_cfg.get("beam_size", 2)
            )
            _preloaded_asr = FireRedASREngine(pre_config)
            _preloaded_asr.load()
            import voicetype
            voicetype._preloaded_asr_engine = _preloaded_asr
            log("FireRedASR pre-loaded successfully")
            print("FireRedASR model pre-loaded!")
    except Exception as e:
        log(f"FunASR pre-load failed: {e}, will fallback in app")

    # Set default hotkey to grave (`)
    sys.argv = [sys.argv[0], "--hotkey", "grave"]

    from voicetype.ui.qt.main import main
    log("Import OK, starting...")
    sys.exit(main())

except Exception as e:
    import traceback
    log(traceback.format_exc())
    sys.exit(1)
