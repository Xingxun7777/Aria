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

# === Singleton Check with Named Mutex (Windows) + File Lock (fallback) ===
LOCK_FILE = os.path.join(tempfile.gettempdir(), "voicetype-dev.lock")
MUTEX_NAME = "VoiceType-Dev-Singleton-Mutex"
_lock_handle = None
_mutex_handle = None


def find_and_kill_voicetype_processes() -> int:
    """
    Find and kill any existing VoiceType processes.
    Returns number of processes killed.

    IMPORTANT: Only kills processes running launcher.py or main.py directly,
    not any process that happens to have 'voicetype' in the path.
    """
    if sys.platform != "win32":
        return 0

    killed = 0
    current_pid = os.getpid()

    try:
        # Use wmic to find python processes with voicetype in command line
        result = subprocess.run(
            [
                "wmic",
                "process",
                "where",
                "name like '%python%'",
                "get",
                "processid,commandline",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        for line in result.stdout.split("\n"):
            line_lower = line.lower()

            # Only match processes that are running VoiceType scripts directly
            # (not just any process with 'voicetype' in the path)
            is_voicetype_process = (
                "launcher.py" in line_lower
                or "voicetype.ui.qt.main" in line_lower  # module form
                or "ui\\qt\\main.py" in line_lower
                or "ui/qt/main.py" in line_lower
                or "splash_runner.py" in line_lower
            )

            if not is_voicetype_process:
                continue

            # Extract PID (last number in line)
            parts = line.strip().split()
            if parts:
                try:
                    pid = int(parts[-1])
                    if pid != current_pid:
                        # Kill the process
                        subprocess.run(
                            ["taskkill", "/F", "/PID", str(pid)],
                            capture_output=True,
                            timeout=5,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                        print(f"[CLEANUP] Killed orphan VoiceType process: PID {pid}")
                        killed += 1
                except (ValueError, subprocess.TimeoutExpired):
                    pass
    except Exception as e:
        print(f"[CLEANUP] Warning: Could not scan for orphan processes: {e}")

    return killed


def is_process_alive(pid: int) -> bool:
    """Check if a process with given PID is still running."""
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def try_acquire_mutex() -> bool:
    """Try to acquire Windows Named Mutex. Returns True if successful."""
    global _mutex_handle
    if sys.platform != "win32":
        return True  # Skip on non-Windows

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32

        # CreateMutexW
        _mutex_handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
        if _mutex_handle:
            last_error = ctypes.get_last_error()
            ERROR_ALREADY_EXISTS = 183
            if last_error == ERROR_ALREADY_EXISTS:
                # Mutex exists - another instance is running
                kernel32.CloseHandle(_mutex_handle)
                _mutex_handle = None
                return False
            return True
        return False
    except Exception as e:
        print(f"[MUTEX] Warning: Could not create mutex: {e}")
        return True  # Fall through to file lock


def release_mutex():
    """Release Windows Named Mutex."""
    global _mutex_handle
    if _mutex_handle and sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.ReleaseMutex(_mutex_handle)
            ctypes.windll.kernel32.CloseHandle(_mutex_handle)
        except Exception:
            pass
        _mutex_handle = None


def check_and_cleanup_stale_lock() -> bool:
    """
    Check if lock file exists and if the owning process is still alive.
    Returns True if lock was stale and cleaned up, False otherwise.
    """
    if not os.path.exists(LOCK_FILE):
        return False

    try:
        with open(LOCK_FILE, "r") as f:
            content = f.read().strip()
            if not content:
                # Empty lock file - stale
                os.remove(LOCK_FILE)
                print("[LOCK] Removed empty stale lock file")
                return True

            old_pid = int(content)

            if not is_process_alive(old_pid):
                # Process is dead - stale lock
                os.remove(LOCK_FILE)
                print(f"[LOCK] Removed stale lock (PID {old_pid} not running)")
                return True
            else:
                print(f"[LOCK] VoiceType is running (PID {old_pid})")
                return False
    except (ValueError, IOError, OSError) as e:
        # Corrupted lock file - try to remove
        try:
            os.remove(LOCK_FILE)
            print(f"[LOCK] Removed corrupted lock file: {e}")
            return True
        except Exception:
            return False


def acquire_lock():
    """Try to acquire singleton lock. Returns True if successful."""
    global _lock_handle

    # First, try Windows Named Mutex (more reliable)
    if not try_acquire_mutex():
        return False

    # Check for stale file locks
    check_and_cleanup_stale_lock()

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
        release_mutex()  # Release mutex if file lock fails
        return False


def release_lock():
    """Release the singleton lock."""
    global _lock_handle

    # Release file lock
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
        _lock_handle = None

    # Release mutex
    release_mutex()


# === Cleanup orphan processes before singleton check ===
# NOTE: Orphan process cleanup disabled - too aggressive and kills parent processes
# The Named Mutex + stale lock file cleanup is sufficient for singleton enforcement
# If you need to force-kill stuck processes, use: python tools/kill_and_restart.py

# Check singleton before doing anything expensive
if not acquire_lock():
    print("=" * 50)
    print("VoiceType is already running!")
    print("=" * 50)
    print(f"Lock file: {LOCK_FILE}")
    print("If VoiceType is not visible, check system tray.")
    print("If stuck, run: python tools/kill_and_restart.py")
    sys.exit(1)

# Register cleanup
atexit.register(release_lock)

# Set process title for easier identification (Windows)
if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW("VoiceType-Dev")
    except Exception:
        pass

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
        # Use the directory containing this launcher as cwd
        launcher_dir = os.path.dirname(os.path.abspath(__file__))
        splash_proc = subprocess.Popen(
            [python_exe, splash_script, str(port)],
            cwd=launcher_dir,
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

    # Set working directory and path relative to this launcher
    project_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(project_dir)
    os.chdir(parent_dir)
    # CRITICAL: Insert project_dir FIRST so local voicetype/ package takes precedence
    # over stable version at parent_dir/voicetype/
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
    if parent_dir not in sys.path:
        sys.path.insert(1, parent_dir)  # After project_dir
    log(f"Path set OK: project={project_dir}, parent={parent_dir}")

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
        log(f"ASR engine from config: '{asr_engine}'")
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
            # Add FireRedASR repo to path (check config first, then sibling directory)
            firered_cfg = config.get("fireredasr", {})
            fireredasr_path = firered_cfg.get("repo_path", "")
            if not fireredasr_path or not os.path.exists(fireredasr_path):
                # Try sibling directory (../FireRedASR)
                fireredasr_path = os.path.join(parent_dir, "FireRedASR")
            if os.path.exists(fireredasr_path) and fireredasr_path not in sys.path:
                sys.path.insert(0, fireredasr_path)
                log(f"Added FireRedASR to path: {fireredasr_path}")
            from voicetype.core.asr.fireredasr_engine import (
                FireRedASREngine,
                FireRedASRConfig,
            )

            # Default model path: look in FireRedASR sibling directory
            default_model_path = os.path.join(
                fireredasr_path, "pretrained_models", "FireRedASR-AED-L"
            )
            pre_config = FireRedASRConfig(
                model_type=firered_cfg.get("model_type", "aed"),
                model_path=firered_cfg.get("model_path", default_model_path),
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
        elif asr_engine == "whisper":
            log("Pre-loading Whisper model (before Qt imports)...")
            print("Pre-loading Whisper model (before Qt)...")

            # Step 0: 检查 faster-whisper 是否已安装
            try:
                import faster_whisper  # noqa: F401
            except ImportError:
                log("faster-whisper not installed, cannot use Whisper engine")
                emit_progress("whisper_error", "Whisper 引擎依赖未安装", 50)
                raise ImportError(
                    "faster-whisper not installed. Run: pip install faster-whisper"
                )

            # 设置 HuggingFace 国内镜像（加速中国用户下载）
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
            log("Set HF_ENDPOINT to hf-mirror.com for China users")

            from voicetype.core.asr.whisper_engine import WhisperEngine, WhisperConfig
            from pathlib import Path

            whisper_cfg = config.get("whisper", {})
            model_name = whisper_cfg.get("model_name", "large-v3-turbo")

            # 检测模型是否已存在
            cache_dir = (
                Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
                / "hub"
            )
            model_pattern = f"models--Systran--faster-whisper-{model_name}"
            model_exists = (cache_dir / model_pattern).exists()

            if model_exists:
                emit_progress(
                    "whisper_load", f"加载 Whisper 模型 ({model_name})...", 20
                )
            else:
                emit_progress(
                    "whisper_download",
                    f"首次使用，正在下载 Whisper 模型 ({model_name})...\n"
                    "这可能需要 2-5 分钟，请耐心等待",
                    20,
                )

            pre_config = WhisperConfig(
                model_name=model_name,
                device=whisper_cfg.get("device", "cuda"),
                language=whisper_cfg.get("language", "zh"),
                compute_type=whisper_cfg.get("compute_type", "float16"),
            )
            _preloaded_asr = WhisperEngine(pre_config)
            _preloaded_asr.load()
            import voicetype

            voicetype._preloaded_asr_engine = _preloaded_asr
            log("Whisper model pre-loaded successfully")
            emit_progress("funasr_model", "模型加载完成", 50)
            print("Whisper model pre-loaded!")
        else:
            # Other engines - just show progress
            emit_progress("funasr_model", "准备语音引擎...", 50)
    except Exception as e:
        log(f"FunASR pre-load failed: {e}, will fallback in app")
        emit_progress("funasr_model", "模型加载失败，使用备用引擎", 50)

    # Set hotkey from config (default: grave)
    hotkey = config.get("general", {}).get("hotkey", "grave")
    sys.argv = [sys.argv[0], "--hotkey", hotkey]
    log(f"Using hotkey: {hotkey}")

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
