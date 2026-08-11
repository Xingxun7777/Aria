"""
llamacpp_engine.py — LlamaCppQwen3Engine: llama.cpp CUDA GGUF drop-in for
Aria's Qwen3ASREngine (the "quality mainline layer" per the 2026-07-19 eval,
_scratch/cluster_20260719/slim_merge/eval_llamacpp_report.md).

It SUBCLASSES the original ``Qwen3ASREngine`` and overrides ONLY the model
lifecycle (load / unload), exactly like SherpaQwen3Engine does. The entire
``transcribe()`` path — RMS normalization, near-silence pre-screens, the three
hallucination/leakage triggers, retry-without-context and filler filtering —
is INHERITED UNCHANGED.

``self._model`` becomes a ``LlamaServerModel`` (HTTP client to a local
llama-server subprocess) instead of the qwen-asr PyTorch model. Both expose
``model.transcribe(audio=(arr, sr), context, language) -> [obj.text/.language]``.

Lifecycle:
  * load()  = spawn ``llama-server`` (CREATE_NO_WINDOW, -ngl/-np 1/-c from
    config) -> poll /health until ready (<=30s) -> one dummy-audio warmup ->
    assemble the adapter. Port-in-use, spawn failure and health timeout each
    raise a clear RuntimeError; the app's startup dispatch then falls back to
    the torch engine (existing mechanism).
  * unload() = graceful terminate, then ``taskkill /T /F`` fallback — the
    llama.cpp eval showed Popen.terminate() can leave zombie llama-server
    processes on Windows.
  * runtime crash = requests fail -> adapter returns empty text -> the app's
    consecutive-failure self-heal reloads the engine, which restarts the
    server. This engine deliberately RELIES on that chain for crash recovery
    instead of adding its own supervisor.

The engine reports actual_device="cuda" (llama-server runs -ngl 99): it
inherits the GPU-side VAD/interim policy and the app-side CUDA warmup gate
(idempotent — load() already warmed the server once). It is deliberately
EXCLUDED from the torch GPU-pressure CPU-backup path (those gates stay
``== "qwen3"``): under GPU contention the eval showed tail-latency growth,
not failure, and hard failures route through the failure chain above.
"""

from __future__ import annotations

import os
import subprocess
import time

from .qwen3_engine import Qwen3ASREngine, Qwen3Config
from .llamacpp_adapter import LlamaServerModel

# Defaults resolve against the project root (dev tree or unpacked release);
# manual self-installs use these landing spots (see docs/ENGINES.md). The
# bundled GPU pack (build.py --gpu-pack) instead ships under
# models/llamacpp_runtime/ and writes explicit relative paths into config.
DEFAULT_LLAMACPP_SERVER = "llamacpp/llama-server.exe"
DEFAULT_LLAMACPP_MODEL = "models/qwen3-asr-gguf/Qwen3-ASR-1.7B-Q8_0.gguf"
DEFAULT_LLAMACPP_PORT = 18539
DEFAULT_LLAMACPP_NGL = 99
DEFAULT_LLAMACPP_CTX = 8192

# Canonical cache/bundle location of the llama.cpp GPU assets, relative to the
# project root: <subdir>/bin/ holds llama-server.exe + DLLs, <subdir>/models/
# holds the GGUF pair. Same layout in the dev tree and inside a --gpu-pack
# dist, so one set of relative config paths works everywhere.
LLAMACPP_RUNTIME_SUBDIR = os.path.join("models", "llamacpp_runtime")

_HEALTH_TIMEOUT_S = 30.0


def resolve_llamacpp_asset(path: str) -> str:
    """Resolve one configured llamacpp asset path portably.

    Absolute paths pass through untouched. Relative paths try, in order:
      1. ``<root>/<path>`` — the installation root (aria package root:
         dev tree or unpacked release, per get_base_path()),
      2. ``<cache>/<path>`` — the GPU-asset cache, where ``<cache>`` is the
         ``ARIA_LLAMACPP_DIR`` env override when set, else
         ``<root>/models/llamacpp_runtime``.
    The first existing candidate wins; when nothing exists the root-relative
    candidate is returned so load() error messages point at the canonical
    location. Evaluated at engine-construction time, NOT import time, so
    config/env changes after import still take effect (same rationale as
    resolve_sherpa_model_dir).
    """
    from ..utils.paths import get_base_path

    root = str(get_base_path())
    p = (path or "").strip()
    if not p:
        return p
    if os.path.isabs(p):
        return p
    primary = os.path.join(root, p)
    if os.path.exists(primary):
        return primary
    cache_root = os.environ.get("ARIA_LLAMACPP_DIR", "").strip() or os.path.join(
        root, LLAMACPP_RUNTIME_SUBDIR
    )
    fallback = os.path.join(cache_root, p)
    if os.path.exists(fallback):
        return fallback
    return primary


def resolve_llamacpp_path(path: str | None, default_rel: str) -> str:
    """Resolve a configured path portably (no hardcoded dev path).

    Explicit ``path`` wins; empty falls back to ``default_rel``. Both go
    through resolve_llamacpp_asset (root first, then the
    models/llamacpp_runtime cache / ARIA_LLAMACPP_DIR override).
    """
    explicit = (path or "").strip()
    return resolve_llamacpp_asset(explicit if explicit else default_rel)


def default_mmproj_for(model_path: str) -> str:
    """Sibling ``mmproj-<model>.gguf`` next to the main GGUF (the official
    release ships them as a pair with this naming convention)."""
    d, fn = os.path.split(model_path)
    return os.path.join(d, f"mmproj-{fn}")


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True when something already listens on the port (a connect succeeds)."""
    import socket

    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def _pids_listening_on_port(port: int) -> list[int]:
    """PIDs with a LISTENING socket on 127.0.0.1/0.0.0.0:<port> (netstat)."""
    pids: list[int] = []
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return pids
    needle = f":{int(port)}"
    for line in out.splitlines():
        parts = line.split()
        # TCP  <local>  <remote>  LISTENING  <pid>
        if len(parts) >= 5 and parts[0].upper() == "TCP" and "LISTENING" in line:
            if parts[1].endswith(needle):
                try:
                    pid = int(parts[-1])
                    if pid > 0 and pid not in pids:
                        pids.append(pid)
                except ValueError:
                    pass
    return pids


def _process_image_name(pid: int) -> str:
    """Executable image name for a PID ("" when unknown)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return ""
    line = (out or "").strip().splitlines()[0] if (out or "").strip() else ""
    if line.startswith('"'):
        return line.split('","')[0].strip('"')
    return ""


def _probe_server_props(port: int, timeout_s: float = 2.0) -> str:
    """Raw /props response text from a llama-server on the port ("" on any
    failure — including a hung server that accepts connects but never answers)."""
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(port)}/props", timeout=timeout_s
        ) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def probe_port_status(port: int, *, server_path: str, model_path: str) -> str:
    """Classify what currently owns the port: "free" / "orphan" / "foreign".

    "orphan" = a llama-server WE own (previous Aria run / hung instance):
    either its /props response mentions our GGUF file name, or — for a hung
    server that no longer answers HTTP — the listening PID's image name
    matches our server executable. Anything else is "foreign" (never killed).
    """
    if not _port_in_use(port):
        return "free"
    model_fn = os.path.basename(model_path or "")
    if model_fn:
        props = _probe_server_props(port)
        if props and model_fn in props:
            return "orphan"
    server_exe = os.path.basename(server_path or "").lower()
    if server_exe:
        for pid in _pids_listening_on_port(port):
            if _process_image_name(pid).lower() == server_exe:
                return "orphan"
    return "foreign"


def _taskkill_tree(pid: int) -> None:
    """taskkill /T /F a process tree (best effort, no window)."""
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(int(pid))],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def _assign_kill_on_close_job(proc) -> int | None:
    """Bind a child process to a Windows Job Object with
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.

    When Aria's process dies — clean exit, crash, or task-manager kill — the
    OS closes the job handle and terminates every process in the job. This is
    the primary orphan defense for llama-server (4GB VRAM + a bound port);
    taskkill in unload() is the cooperative path on top.

    Returns the job HANDLE (int, must stay referenced for the engine's
    lifetime) or None when binding failed (non-fatal: taskkill still works).
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, int(proc._handle)):
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _close_job_handle(job: int | None) -> None:
    """Close a job handle; with KILL_ON_JOB_CLOSE this also reaps the job."""
    if not job:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(job)
    except Exception:
        pass


class LlamaCppQwen3Engine(Qwen3ASREngine):
    """Qwen3-ASR engine backed by a local llama-server (llama.cpp CUDA, GGUF).

    Drop-in for ``Qwen3ASREngine``: same public interface, same transcribe
    guards, different (torch-free) model backend.
    """

    # The backend is an external subprocess: teardown = killing a process
    # (milliseconds, always safe), NOT freeing torch tensors behind a lock a
    # stuck CUDA thread may hold forever. App paths that deliberately avoid
    # synchronous unload for torch engines (self-heal build-first ordering,
    # stop(unload_asr=False) at GUI exit) check this flag and tear this
    # engine down synchronously instead — otherwise the old server keeps the
    # port bound (self-heal would deadlock against the port pre-check) or
    # outlives the app as an orphan holding 4GB VRAM.
    supports_sync_teardown = True

    def __init__(
        self,
        config: Qwen3Config | None = None,
        *,
        server_path: str | None = None,
        model_path: str | None = None,
        mmproj_path: str | None = None,
        port: int = DEFAULT_LLAMACPP_PORT,
        ngl: int = DEFAULT_LLAMACPP_NGL,
        ctx: int = DEFAULT_LLAMACPP_CTX,
        request_timeout_base_s: float = 8.0,
    ) -> None:
        super().__init__(config)
        self._server_path = resolve_llamacpp_path(server_path, DEFAULT_LLAMACPP_SERVER)
        self._model_path = resolve_llamacpp_path(model_path, DEFAULT_LLAMACPP_MODEL)
        explicit_mmproj = (mmproj_path or "").strip()
        if explicit_mmproj:
            self._mmproj_path = resolve_llamacpp_path(explicit_mmproj, "")
        else:
            self._mmproj_path = default_mmproj_for(self._model_path)
        # Same clamp as the app-side config parser so a direct-construction
        # caller (tests, tools) can't end up on a privileged/invalid port.
        self._port = Qwen3Config._as_int(
            port, DEFAULT_LLAMACPP_PORT, min_value=1024, max_value=65535
        )
        self._ngl = int(ngl if ngl is not None else DEFAULT_LLAMACPP_NGL)
        self._ctx = int(ctx or DEFAULT_LLAMACPP_CTX)
        self._request_timeout_base_s = max(
            1.0, float(request_timeout_base_s or 8.0)
        )
        self._server_proc: subprocess.Popen | None = None
        self._server_log_handle = None
        self._job_handle: int | None = None
        # Truthful device/model identity for every app-side check (GPU VAD
        # policy, warmup gating, telemetry) even before load(): llama-server
        # runs -ngl 99, the runtime device IS cuda.
        self.config.device = "cuda"
        self._actual_device = "cuda"
        self.config.model_name = self._gguf_label()
        self._loaded_model_name = self.config.model_name

    def _gguf_label(self) -> str:
        stem = os.path.splitext(os.path.basename(self._model_path))[0]
        return stem or "Qwen3-ASR-GGUF"

    @property
    def name(self) -> str:
        return f"Qwen3-ASR-llamacpp ({self._gguf_label()})"

    # --- overridden lifecycle (replaces the torch/CUDA machinery) -------------

    def load(self) -> None:
        if self._model is not None:
            return
        if not os.path.isfile(self._server_path):
            raise RuntimeError(
                f"llama-server 可执行文件不存在: {self._server_path}；"
                "请在 hotwords.json 的 qwen3_llamacpp.server_path 配置正确路径"
            )
        if not os.path.isfile(self._model_path):
            raise RuntimeError(
                f"Qwen3-ASR GGUF 模型不存在: {self._model_path}；"
                "请在 hotwords.json 的 qwen3_llamacpp.model_path 配置正确路径"
            )
        if not os.path.isfile(self._mmproj_path):
            raise RuntimeError(
                f"Qwen3-ASR mmproj 文件不存在: {self._mmproj_path}；"
                "请在 hotwords.json 的 qwen3_llamacpp.mmproj_path 配置正确路径"
            )
        if _port_in_use(self._port) and not self._reclaim_orphan_server():
            raise RuntimeError(
                f"llama-server 端口 {self._port} 已被其他程序占用；"
                "请修改 qwen3_llamacpp.port 或结束占用该端口的进程"
            )

        self._spawn_server()
        try:
            self._wait_healthy()
        except Exception:
            # Never leave a half-started server behind a failed load().
            self._kill_server()
            raise

        model = LlamaServerModel(
            self._port,
            max_tokens=self.config.max_new_tokens,
            timeout_base_s=self._request_timeout_base_s,
        )
        self._warmup_server(model)
        self._model = model
        self._actual_device = "cuda"
        self._device_reason = "llama.cpp CUDA (llama-server)"
        self._loaded_model_name = self._gguf_label()

    def _spawn_server(self) -> None:
        args = [
            self._server_path,
            "-m",
            self._model_path,
            "--mmproj",
            self._mmproj_path,
            "-ngl",
            str(self._ngl),
            "-np",
            "1",
            "-c",
            str(self._ctx),
            "--port",
            str(self._port),
            "--host",
            "127.0.0.1",
            "--temp",
            "0",
        ]
        stdout = subprocess.DEVNULL
        try:
            from pathlib import Path

            log_dir = Path(__file__).parent.parent.parent / "DebugLog"
            log_dir.mkdir(parents=True, exist_ok=True)
            self._server_log_handle = open(
                log_dir / "llamacpp_server.log", "w", encoding="utf-8", errors="replace"
            )
            stdout = self._server_log_handle
        except Exception:
            self._server_log_handle = None
        try:
            self._server_proc = subprocess.Popen(
                args,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                cwd=os.path.dirname(self._server_path) or None,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            self._close_server_log()
            raise RuntimeError(
                f"llama-server 启动失败: {type(exc).__name__}: {exc} "
                f"({self._server_path})"
            ) from exc
        # Primary orphan defense: bind the child to a KILL_ON_JOB_CLOSE job
        # so it dies with Aria on EVERY exit path (clean stop, crash, task
        # manager kill). taskkill in _kill_server stays as the cooperative
        # teardown on top. Non-fatal when binding fails.
        self._job_handle = _assign_kill_on_close_job(self._server_proc)
        if self._job_handle is None:
            from ..logging import get_system_logger

            get_system_logger().warning(
                "llama-server Job Object binding failed: orphan defense "
                "degrades to taskkill-on-unload only"
            )

    def _reclaim_orphan_server(self) -> bool:
        """Port pre-check found a listener: if it is OUR llama-server (a
        previous run's orphan or a hung instance the self-heal is replacing),
        kill it by port PID and continue startup. Returns True when the port
        is ours and now free; False for a foreign owner (caller raises).

        Without this, the self-heal loop can never recover from a hung-but-
        alive server: every fresh load() would hit the port check and fail,
        while the hung process stays alive forever.
        """
        from ..logging import get_system_logger

        logger = get_system_logger()
        status = probe_port_status(
            self._port, server_path=self._server_path, model_path=self._model_path
        )
        if status == "free":
            return True
        if status == "foreign":
            logger.warning(
                f"port {self._port} is held by a foreign process; not touching it"
            )
            return False
        pids = _pids_listening_on_port(self._port)
        logger.warning(
            f"reclaiming orphan llama-server on port {self._port} "
            f"(pids={pids or 'unknown'})"
        )
        for pid in pids:
            _taskkill_tree(pid)
        # The socket can linger briefly after the kill; wait for it to free.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if not _port_in_use(self._port):
                return True
            time.sleep(0.25)
        logger.warning(
            f"port {self._port} still bound after orphan reclaim attempt"
        )
        return False

    def _wait_healthy(self, timeout_s: float = _HEALTH_TIMEOUT_S) -> float:
        """Poll /health until 200; returns elapsed seconds. Raises on death
        or timeout with a clear message."""
        import urllib.request

        url = f"http://127.0.0.1:{self._port}/health"
        deadline = time.time() + timeout_s
        started = time.time()
        while time.time() < deadline:
            proc = self._server_proc
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(
                    f"llama-server 启动后异常退出 (exit={proc.returncode})；"
                    "详见 DebugLog/llamacpp_server.log"
                )
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        return time.time() - started
            except Exception:
                pass
            time.sleep(0.25)
        raise RuntimeError(
            f"llama-server 健康检查超时 ({timeout_s:.0f}s)：模型加载过慢或服务异常；"
            "详见 DebugLog/llamacpp_server.log"
        )

    def _warmup_server(self, model: LlamaServerModel) -> None:
        """One dummy-audio request: the first request pays prompt/graph init
        (~1s measured in the eval). Non-fatal — the server is already healthy."""
        import numpy as np

        try:
            silence = np.zeros(8000, dtype=np.float32)  # 0.5s @ 16k
            t0 = time.time()
            model.transcribe(
                audio=(silence, 16000), context=None, language=self.config.language
            )
            from ..logging import get_system_logger

            # Empty TEXT is expected for silence; a request-level failure
            # (adapter's last_error) means the server passed /health but is
            # only half-ready — surface it instead of loading silently.
            warmup_error = getattr(model, "last_error", "")
            if warmup_error:
                get_system_logger().warning(
                    f"llama-server warmup request failed ({warmup_error}): "
                    "server may be half-ready; first real request will retry"
                )
            else:
                get_system_logger().info(
                    f"llama-server warmup request done in {time.time() - t0:.2f}s"
                )
        except Exception:
            pass

    def is_backend_alive(self) -> bool:
        """False only when the OWNED llama-server process has exited.

        A reclaimed orphan server (loaded without spawning, so no proc
        handle) is unobservable here and reports True — the cooldown bypass
        must only fire on positive evidence of a dead backend.
        """
        proc = self._server_proc
        if proc is None:
            return True
        return proc.poll() is None

    def unload(self) -> None:
        self._model = None
        self._kill_server()

    def _kill_server(self) -> None:
        proc = self._server_proc
        self._server_proc = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
            if proc.poll() is None:
                # Windows: Popen.terminate() is known to leave llama-server
                # zombies (observed during the eval). taskkill /T also reaps
                # any child processes.
                try:
                    subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except Exception:
                    pass
                try:
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        # Closing the KILL_ON_JOB_CLOSE handle also reaps anything the
        # cooperative path missed.
        _close_job_handle(self._job_handle)
        self._job_handle = None
        self._close_server_log()

    def _close_server_log(self) -> None:
        handle = self._server_log_handle
        self._server_log_handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    def trim_runtime_cache(self, reason: str = "manual") -> None:
        """No torch tensors live in this process (the model is inside
        llama-server): the inherited impl would needlessly initialize a CUDA
        context in Aria's process (actual_device == "cuda") just to
        empty_cache. Plain gc is all there is to trim."""
        try:
            import gc

            gc.collect()
        except Exception:
            pass

    # --- hotwords: per-utterance via the inherited transcribe() full_context --
    # Biasing flows through full_context -> LlamaServerModel (chat system
    # message). super() just refreshes config.hotwords / _context_string.

    def set_hotwords(self, hotwords) -> None:
        super().set_hotwords(hotwords)

    def set_hotwords_with_context(self, context_string: str) -> None:
        super().set_hotwords_with_context(context_string)
