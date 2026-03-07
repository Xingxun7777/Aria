#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Aria Launcher with error logging, singleton check, and splash screen."""

import sys
import os
import tempfile
import atexit
import time
import subprocess
import threading

# Fix OpenMP conflict between PyTorch libraries (MUST be before any imports)
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

# === Fix stdout/stderr for pythonw.exe / AriaRuntime.exe ===
# Under pythonw.exe, sys.stdout and sys.stderr are None.
# Any print() call would crash with: AttributeError: 'NoneType' has no attribute 'write'
# Redirect to devnull so all print() calls are safe. Debug mode (python.exe) is unaffected.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")


# === Detect if running from portable build ===
def _is_portable_build() -> bool:
    """Check if running from dist_portable directory."""
    script_path = os.path.abspath(__file__)
    # Portable build runs from: dist_portable/Aria/_internal/app/aria/launcher.py
    # or the embedded Python runs it as module
    return "dist_portable" in script_path or "_internal" in script_path


IS_PORTABLE = _is_portable_build()

# === Singleton Check with Named Mutex (Windows) + File Lock (fallback) ===
# Use different names for dev and portable to allow simultaneous running
if IS_PORTABLE:
    LOCK_FILE = os.path.join(tempfile.gettempdir(), "aria-portable.lock")
    MUTEX_NAME = "Aria-Portable-Singleton-Mutex"
else:
    LOCK_FILE = os.path.join(tempfile.gettempdir(), "aria-dev.lock")
    MUTEX_NAME = "Aria-Dev-Singleton-Mutex"

_lock_handle = None
_mutex_handle = None


def find_and_kill_aria_processes() -> int:
    """
    Find and kill any existing Aria processes.
    Returns number of processes killed.

    IMPORTANT: Only kills processes running launcher.py or main.py directly,
    not any process that happens to have 'aria' in the path.
    """
    if sys.platform != "win32":
        return 0

    killed = 0
    current_pid = os.getpid()

    try:
        # Use wmic to find python processes with aria in command line
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

            # Only match processes that are running Aria scripts directly
            # (not just any process with 'aria' in the path)
            is_aria_process = (
                "launcher.py" in line_lower
                or "aria.ui.qt.main" in line_lower  # module form
                or "ui\\qt\\main.py" in line_lower
                or "ui/qt/main.py" in line_lower
                or "splash_runner.py" in line_lower
            )

            if not is_aria_process:
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
                        print(f"[CLEANUP] Killed orphan Aria process: PID {pid}")
                        killed += 1
                except (ValueError, subprocess.TimeoutExpired):
                    pass
    except Exception as e:
        print(f"[CLEANUP] Warning: Could not scan for orphan processes: {e}")

    return killed


def get_process_creation_time(pid: int) -> float:
    """
    Get process creation time as a timestamp.
    Returns 0.0 if unable to get creation time.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return 0.0

            try:
                # FILETIME structures for GetProcessTimes
                creation_time = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel_time = wintypes.FILETIME()
                user_time = wintypes.FILETIME()

                if kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation_time),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                ):
                    # Convert FILETIME to Python timestamp
                    # FILETIME is 100-nanosecond intervals since 1601-01-01
                    filetime = (
                        creation_time.dwHighDateTime << 32
                    ) | creation_time.dwLowDateTime
                    # Convert to Unix timestamp (seconds since 1970-01-01)
                    # 116444736000000000 = difference between 1601 and 1970 in 100ns intervals
                    unix_time = (filetime - 116444736000000000) / 10000000.0
                    return unix_time
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            pass
    return 0.0


def is_process_alive(pid: int, expected_creation_time: float = None) -> bool:
    """
    Check if a process with given PID is still running.

    Args:
        pid: Process ID to check
        expected_creation_time: Optional creation time to verify (prevents PID reuse false positive)

    Returns:
        True if process is alive (and matches creation time if provided)
    """
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                # If creation time provided, verify it matches
                if expected_creation_time is not None and expected_creation_time > 0:
                    actual_time = get_process_creation_time(pid)
                    if actual_time > 0:
                        # Allow 2 second tolerance for timing differences
                        if abs(actual_time - expected_creation_time) > 2.0:
                            print(
                                f"[LOCK] PID {pid} exists but creation time mismatch (reused PID)"
                            )
                            return False
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


def safe_remove_lock_file(max_retries: int = 3, delay: float = 0.1) -> bool:
    """
    Safely remove the lock file with retry logic for WinError 32.

    Args:
        max_retries: Maximum number of removal attempts
        delay: Delay between retries in seconds

    Returns:
        True if file was removed or doesn't exist, False if removal failed
    """
    for attempt in range(max_retries):
        try:
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
            return True
        except PermissionError as e:
            # WinError 32: The process cannot access the file because it is being used
            if attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))  # Exponential backoff
            else:
                print(
                    f"[LOCK] Warning: Could not remove lock file after {max_retries} attempts: {e}"
                )
                return False
        except OSError as e:
            print(f"[LOCK] Warning: Error removing lock file: {e}")
            return False
    return True


def check_and_cleanup_stale_lock() -> bool:
    """
    Check if lock file exists and if the owning process is still alive.
    Returns True if lock was stale and cleaned up, False otherwise.

    Lock file format: PID:CREATION_TIME (e.g., "12345:1703637600.5")
    Legacy format (just PID) is also supported for backward compatibility.
    """
    if not os.path.exists(LOCK_FILE):
        return False

    try:
        with open(LOCK_FILE, "r") as f:
            content = f.read().strip()
            if not content:
                # Empty lock file - stale
                if safe_remove_lock_file():
                    print("[LOCK] Removed empty stale lock file")
                    return True
                return False

            # Parse lock file: new format "PID:CREATION_TIME" or legacy "PID"
            old_pid = 0
            creation_time = None
            if ":" in content:
                parts = content.split(":", 1)
                old_pid = int(parts[0])
                creation_time = float(parts[1])
            else:
                # Legacy format - just PID
                old_pid = int(content)

            if not is_process_alive(old_pid, creation_time):
                # Process is dead or PID was reused - stale lock
                if safe_remove_lock_file():
                    if creation_time:
                        print(
                            f"[LOCK] Removed stale lock (PID {old_pid} not running or reused)"
                        )
                    else:
                        print(f"[LOCK] Removed stale lock (PID {old_pid} not running)")
                    return True
                return False
            else:
                print(f"[LOCK] Aria is running (PID {old_pid})")
                return False
    except (ValueError, IOError, OSError) as e:
        # Corrupted lock file - try to remove
        if safe_remove_lock_file():
            print(f"[LOCK] Removed corrupted lock file: {e}")
            return True
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
            # Write PID:CREATION_TIME format for robust stale lock detection
            pid = os.getpid()
            creation_time = get_process_creation_time(pid)
            _lock_handle.write(f"{pid}:{creation_time}")
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
        # Use safe removal with retry for WinError 32
        safe_remove_lock_file()
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
    print("Aria is already running!")
    print("=" * 50)
    print(f"Lock file: {LOCK_FILE}")
    print("If Aria is not visible, check system tray.")
    print("If stuck, run: python tools/kill_and_restart.py")
    sys.exit(1)

# Register cleanup
atexit.register(release_lock)

# Set process title for easier identification (Windows)
if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW("Aria-Dev")
    except Exception:
        pass

LOG_FILE = os.path.join(os.path.dirname(__file__), "launch_error.log")
# Fallback log path if primary is not writable (e.g., Program Files, Controlled Folder Access)
_LOG_FILE_FALLBACK = os.path.join(tempfile.gettempdir(), "aria_launch_error.log")


def log(msg):
    for path in (LOG_FILE, _LOG_FILE_FALLBACK):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
            return
        except (OSError, PermissionError):
            continue


# Patch subprocess to hide console windows on Windows (for ffmpeg and other subprocess calls)
if sys.platform == "win32":
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


def get_project_python() -> str:
    """
    Get the correct Python interpreter path for this project.

    Priority:
    1. If in venv (sys.prefix != sys.base_prefix), use venv Python
    2. If project .venv exists, use that
    3. Fallback to sys.executable

    This prevents issues when system PATH has multiple Python installations.
    """
    launcher_dir = os.path.dirname(os.path.abspath(__file__))

    # Check if we're running in a venv
    if sys.prefix != sys.base_prefix:
        # We're in a venv - use its Python
        if sys.platform == "win32":
            venv_python = os.path.join(sys.prefix, "Scripts", "python.exe")
        else:
            venv_python = os.path.join(sys.prefix, "bin", "python")
        if os.path.exists(venv_python):
            return venv_python

    # Check for project's .venv directory
    if sys.platform == "win32":
        project_venv = os.path.join(launcher_dir, ".venv", "Scripts", "python.exe")
    else:
        project_venv = os.path.join(launcher_dir, ".venv", "bin", "python")
    if os.path.exists(project_venv):
        return project_venv

    # Fallback to sys.executable
    return sys.executable


# === Splash Screen Functions ===


def start_splash():
    """
    Launch splash screen in a separate subprocess.
    Returns (process, reporter) tuple, or (None, None) if splash fails.
    """
    try:
        from aria.progress_ipc import find_free_port, ProgressReporter

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
        python_exe = get_project_python()
        log(f"Python exe (from get_project_python): {python_exe}")
        if sys.platform == "win32" and python_exe.endswith("python.exe"):
            pythonw = python_exe.replace("python.exe", "pythonw.exe")
            if os.path.exists(pythonw):
                python_exe = pythonw
                log(f"Using pythonw: {pythonw}")

        log(f"Launching: {python_exe} {splash_script} {port}")
        # Use the directory containing this launcher as cwd
        launcher_dir = os.path.dirname(os.path.abspath(__file__))
        splash_proc = subprocess.Popen(
            [python_exe, "-s", splash_script, str(port)],
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
    # CRITICAL: Insert project_dir FIRST so local aria/ package takes precedence
    # over stable version at parent_dir/aria/
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
    if parent_dir not in sys.path:
        sys.path.insert(1, parent_dir)  # After project_dir
    log(f"Path set OK: project={project_dir}, parent={parent_dir}")

    # === Start Splash Screen ===
    splash_proc, reporter = start_splash()

    _emit_lock = threading.Lock()

    def emit_progress(stage, message=None, percent=None):
        """Helper to send progress - no-op if splash not available.
        Thread-safe: multiple threads (LoadingHeartbeat, tqdm workers) may call this."""
        if reporter:
            with _emit_lock:
                reporter.emit(stage, message, percent)

    class LoadingHeartbeat:
        """
        Background thread that sends periodic progress updates during blocking operations.
        Shows animated dots to indicate the app is not frozen.
        """

        def __init__(
            self, stage: str, base_message: str, percent: int, interval: float = 0.8
        ):
            self.stage = stage
            self.base_message = base_message
            self.percent = percent
            self.interval = interval
            self._stop_event = threading.Event()
            self._thread = None

        def start(self):
            """Start the heartbeat thread."""
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

        def stop(self):
            """Stop the heartbeat thread."""
            self._stop_event.set()
            if self._thread:
                self._thread.join(timeout=1.0)

        def _run(self):
            """Heartbeat loop - cycles through dot animation."""
            dots = [".", "..", "...", ""]
            idx = 0
            while not self._stop_event.is_set():
                msg = f"{self.base_message}{dots[idx]}"
                emit_progress(self.stage, msg, self.percent)
                idx = (idx + 1) % len(dots)
                self._stop_event.wait(self.interval)

        def __enter__(self):
            self.start()
            return self

        def __exit__(self, *args):
            self.stop()

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
    config = {}  # 默认空配置，防止 JSON 解析失败时 NameError
    asr_engine = "qwen3"  # Default before try block (prevents UnboundLocalError)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        asr_engine = config.get("asr_engine", "qwen3")
        log(f"ASR engine from config: '{asr_engine}'")
        # Backward compatibility: removed engines fall back to qwen3
        if asr_engine in ("whisper", "fireredasr"):
            log(
                f"ASR engine '{asr_engine}' is no longer supported, falling back to Qwen3-ASR"
            )
            print(f"[WARN] ASR engine '{asr_engine}' removed, using Qwen3-ASR instead")
            asr_engine = "qwen3"

        if asr_engine == "funasr":
            log("Pre-loading FunASR (before Qt imports)...")
            print("Pre-loading FunASR model (before Qt)...")
            from aria.core.asr.funasr_engine import FunASREngine, FunASRConfig

            funasr_cfg = config.get("funasr", {})
            model_name = funasr_cfg.get("model_name", "paraformer-zh")
            emit_progress("asr_model", f"初始化 FunASR ({model_name})...", 15)

            pre_config = FunASRConfig(
                model_name=model_name,
                vad_model=funasr_cfg.get("vad_model", "fsmn-vad"),
                punc_model=funasr_cfg.get("punc_model", "ct-punc"),
                device=funasr_cfg.get("device", "cuda"),
                enable_vad=funasr_cfg.get("enable_vad", False),
                enable_punc=funasr_cfg.get("enable_punc", False),
            )
            _preloaded_asr = FunASREngine(pre_config)
            with LoadingHeartbeat(
                "asr_model", f"加载模型中 ({model_name})", 25, interval=0.6
            ):
                _preloaded_asr.load()
            import aria

            aria._preloaded_asr_engine = _preloaded_asr
            log("FunASR pre-loaded successfully")
            emit_progress("asr_model", "模型加载完成", 50)
            print("FunASR model pre-loaded!")
        elif asr_engine == "qwen3":
            log("Pre-loading Qwen3-ASR model (before Qt imports)...")
            print("Pre-loading Qwen3-ASR model (before Qt)...")

            # Step 0: 检查 qwen-asr 是否已安装
            from aria.core.asr.qwen3_engine import (
                Qwen3ASREngine,
                Qwen3Config,
                check_qwen3_installation,
            )

            if not check_qwen3_installation():
                log("qwen_asr unavailable, cannot use Qwen3 engine")
                emit_progress("qwen3_error", "Qwen3-ASR 引擎不可用", 50)
                raise ImportError("qwen-asr unavailable. Check startup logs for details.")

            # HuggingFace endpoint: respect user's env, default to China mirror
            os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
            if "HF_ENDPOINT" not in os.environ:
                os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
                log("Set HF_ENDPOINT to hf-mirror.com (default for China users)")
            else:
                log(f"Using existing HF_ENDPOINT: {os.environ['HF_ENDPOINT']}")

            qwen3_cfg = config.get("qwen3", {})
            model_name = qwen3_cfg.get("model_name", "auto")

            # Resolve "auto" model selection based on VRAM
            if model_name == "auto":
                try:
                    import torch

                    if torch.cuda.is_available():
                        props = torch.cuda.get_device_properties(0)
                        total_vram = getattr(
                            props, "total_memory", getattr(props, "total_mem", 0)
                        )
                        vram_gb = total_vram / (1024**3)
                        model_name = (
                            "Qwen/Qwen3-ASR-1.7B"
                            if vram_gb >= 5
                            else "Qwen/Qwen3-ASR-0.6B"
                        )
                        log(
                            f"Auto-selected Qwen3 model by VRAM: {vram_gb:.1f}GB -> {model_name}"
                        )
                    else:
                        model_name = "Qwen/Qwen3-ASR-0.6B"
                except Exception:
                    model_name = "Qwen/Qwen3-ASR-0.6B"

            # Extract short model name for display (e.g., "1.7B" or "0.6B")
            short_name = "1.7B" if "1.7B" in model_name else "0.6B"
            model_size = "3.4GB" if "1.7B" in model_name else "1.2GB"
            model_size_bytes = 3_400_000_000 if "1.7B" in model_name else 1_200_000_000

            # --- Model existence check (3 sources) ---
            # 1. Bundled model (full/portable version with pre-packaged models)
            has_bundled = False
            if "/" in model_name:
                from aria.core.utils.paths import get_models_path

                local_model_name = model_name.split("/")[-1]
                bundled_path = get_models_path(local_model_name)
                has_bundled = bundled_path.is_dir() and any(
                    bundled_path.glob("*.safetensors")
                )
                if has_bundled:
                    log(f"Found bundled model at: {bundled_path}")

            # 2. HF cache (previously downloaded via huggingface_hub)
            cache_dir = (
                Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
                / "hub"
            )
            model_dir_name = f"models--{model_name.replace('/', '--')}"
            model_cache_path = cache_dir / model_dir_name
            has_cached = (
                model_cache_path.exists() and (model_cache_path / "snapshots").exists()
            )
            if has_cached:
                log(f"Found cached model at: {model_cache_path}")

            model_available = has_bundled or has_cached
            needs_download = not model_available and "/" in model_name

            # --- Phase 1: Pre-download with progress if model not available ---
            if needs_download:
                log(
                    f"Model not found locally, will download from HuggingFace: {model_name}"
                )
                emit_progress(
                    "qwen3_download",
                    f"首次使用 Qwen3-ASR {short_name}\n"
                    f"正在下载模型 ({model_size})，请耐心等待...",
                    15,
                )

                # Custom tqdm class to relay download progress to splash screen.
                # pythonw.exe has stdout=None, so normal tqdm output is invisible.
                # This class also runs its own heartbeat thread to keep splash
                # alive during large single-file downloads (snapshot_download only
                # invokes tqdm_class at the file level, NOT per-byte).
                class _SplashDownloadProgress:
                    """Relay HF download progress to splash screen via IPC.

                    Implements the tqdm interface required by huggingface_hub's
                    snapshot_download (thread_map iterator wrapper).

                    Also runs a self-contained heartbeat thread so the splash
                    never looks frozen during multi-minute large file downloads.
                    Only the heartbeat thread calls emit_progress, avoiding
                    concurrent percent conflicts.
                    """

                    _file_count = 0
                    _file_total = 0
                    _current_percent = 15
                    _current_message = ""
                    _lock = threading.Lock()
                    _heartbeat_stop = threading.Event()
                    _heartbeat_thread = None

                    def __init__(self, iterable=None, *args, **kwargs):
                        self._iterable = iterable
                        self.total = kwargs.get("total") or 0
                        self.n = 0
                        # If constructed with iterable, this is file-level progress
                        if iterable is not None:
                            with _SplashDownloadProgress._lock:
                                _SplashDownloadProgress._file_total = self.total

                    def __iter__(self):
                        if self._iterable is not None:
                            for item in self._iterable:
                                yield item
                                with _SplashDownloadProgress._lock:
                                    _SplashDownloadProgress._file_count += 1
                                    fc = _SplashDownloadProgress._file_count
                                    ft = _SplashDownloadProgress._file_total
                                    pct = 15 + min(34, fc * 34 // max(ft, 1))
                                    _SplashDownloadProgress._current_percent = pct
                                    _SplashDownloadProgress._current_message = (
                                        f"下载 Qwen3-ASR {short_name}: "
                                        f"文件 {fc}/{ft}"
                                    )

                    def __len__(self):
                        if hasattr(self._iterable, "__len__"):
                            return len(self._iterable)
                        return self.total or 0

                    def update(self, n=1):
                        # Byte-level update (if future HF versions pass tqdm_class
                        # to individual file downloads)
                        self.n += n
                        with _SplashDownloadProgress._lock:
                            mb_done = self.n / (1024 * 1024)
                            total = self.total or model_size_bytes
                            mb_total = total / (1024 * 1024)
                            pct = min(49, 15 + int(self.n / total * 34))
                            _SplashDownloadProgress._current_percent = pct
                            _SplashDownloadProgress._current_message = (
                                f"下载 Qwen3-ASR {short_name}: "
                                f"{mb_done:.0f}/{mb_total:.0f} MB"
                            )

                    def close(self):
                        pass

                    def refresh(self, *a, **kw):
                        pass

                    def set_description(self, *a, **kw):
                        pass

                    def set_postfix_str(self, *a, **kw):
                        pass

                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        pass

                    @classmethod
                    def get_lock(cls):
                        return cls._lock

                    @classmethod
                    def set_lock(cls, lock):
                        cls._lock = lock

                    @classmethod
                    def _start_heartbeat(cls):
                        """Start background heartbeat to keep splash alive."""
                        cls._heartbeat_stop.clear()
                        cls._current_message = (
                            f"下载 Qwen3-ASR {short_name} ({model_size})"
                        )

                        def _beat():
                            dots = [".", "..", "...", ""]
                            idx = 0
                            while not cls._heartbeat_stop.is_set():
                                with cls._lock:
                                    msg = cls._current_message
                                    pct = cls._current_percent
                                emit_progress(
                                    "qwen3_download", f"{msg}{dots[idx]}", pct
                                )
                                idx = (idx + 1) % len(dots)
                                cls._heartbeat_stop.wait(2.5)

                        cls._heartbeat_thread = threading.Thread(
                            target=_beat, daemon=True
                        )
                        cls._heartbeat_thread.start()

                    @classmethod
                    def _stop_heartbeat(cls):
                        """Stop the heartbeat thread."""
                        cls._heartbeat_stop.set()
                        if cls._heartbeat_thread:
                            cls._heartbeat_thread.join(timeout=2.0)

                # Disk space pre-check
                import shutil

                required_bytes = model_size_bytes + 500_000_000  # model + 500MB buffer
                _, _, free_space = shutil.disk_usage(bundled_path.parent.anchor)
                if free_space < required_bytes:
                    free_gb = free_space / (1024**3)
                    need_gb = required_bytes / (1024**3)
                    raise RuntimeError(
                        f"磁盘空间不足: 剩余 {free_gb:.1f}GB，需要 {need_gb:.1f}GB"
                    )

                try:
                    from huggingface_hub import snapshot_download

                    _SplashDownloadProgress._file_count = 0
                    _SplashDownloadProgress._current_percent = 15
                    # Download directly to bundled models/ dir (not HF cache)
                    # This makes lite version "evolve" into full version after
                    # download — no separate HF cache, truly portable
                    bundled_path.parent.mkdir(parents=True, exist_ok=True)
                    # Self-contained heartbeat: only the heartbeat thread
                    # calls emit_progress, __iter__/update only set class vars.
                    # This avoids concurrent percent conflicts entirely.
                    _SplashDownloadProgress._start_heartbeat()
                    try:
                        snapshot_download(
                            model_name,
                            local_dir=str(bundled_path),
                            tqdm_class=_SplashDownloadProgress,
                        )
                    finally:
                        _SplashDownloadProgress._stop_heartbeat()
                    log(f"Model downloaded to: {bundled_path}")
                    emit_progress("qwen3_download", "模型下载完成，正在加载...", 49)
                except Exception as dl_err:
                    log(f"Model download failed: {dl_err}")
                    import traceback

                    log(traceback.format_exc())
                    emit_progress(
                        "qwen3_download",
                        f"模型下载失败: {type(dl_err).__name__}\n"
                        f"请检查网络连接后重启应用",
                        15,
                    )
                    raise  # Re-raise to be caught by outer except
            else:
                # Model already available (bundled or cached)
                emit_progress(
                    "qwen3_load",
                    f"正在加载 Qwen3-ASR {short_name}...",
                    15,
                )
                log(f"Loading existing Qwen3-ASR model: {model_name}")

            # --- Phase 2: Load model into memory ---
            pre_config = Qwen3Config(
                model_name=model_name,
                device=qwen3_cfg.get("device", "cuda"),
                torch_dtype=qwen3_cfg.get("torch_dtype", "bfloat16"),
                language=qwen3_cfg.get("language", "Chinese"),
            )
            _preloaded_asr = Qwen3ASREngine(pre_config)

            with LoadingHeartbeat(
                "qwen3_load",
                f"正在加载 Qwen3-ASR {short_name}",
                25,
                interval=0.6,
            ):
                _preloaded_asr.load()

            # Phase 3: Done
            import aria

            aria._preloaded_asr_engine = _preloaded_asr
            log("Qwen3-ASR model pre-loaded successfully")
            emit_progress("asr_model", "Qwen3-ASR 就绪", 50)
            print(f"Qwen3-ASR model pre-loaded! ({short_name})")
        else:
            # Other engines - just show progress
            emit_progress("asr_model", "准备语音引擎...", 50)
    except Exception as e:
        log(f"ASR pre-load failed ({asr_engine}): {e}, will fallback in app")
        emit_progress("asr_model", "模型加载失败，使用备用引擎", 50)

    # Set hotkey from config (default: grave)
    hotkey = config.get("general", {}).get("hotkey", "grave")
    sys.argv = [sys.argv[0], "--hotkey", hotkey]
    log(f"Using hotkey: {hotkey}")

    emit_progress("qt_ui")  # 80%

    from aria.ui.qt.main import main

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
