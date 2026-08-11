"""
Aria Application
=====================
Main application that orchestrates all components.

Usage:
    python -m aria.app

Press the hotkey (default: backtick `) to toggle recording.
Press Ctrl+C to exit.
"""

import sys
import io
import os
import signal
import time
import threading
import queue
import winsound
import json
import subprocess
import math
from dataclasses import dataclass, replace as dataclass_replace
from enum import Enum, auto
from pathlib import Path

# Fix Windows console UTF-8 encoding (skip if no console, e.g. pythonw.exe)
# NOTE: Use reconfigure() instead of creating a new TextIOWrapper.
# Under pythonw.exe, launcher.py sets sys.stdout = open(os.devnull, "w").
# If we do sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...), the OLD wrapper
# loses all references (sys.__stdout__ is still None), gets GC'd, and its close()
# closes the shared buffer — causing "I/O operation on closed file" for all prints.
if sys.platform == "win32" and sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass  # Not a TextIOWrapper or not reconfigurable
if sys.platform == "win32" and sys.stderr is not None:
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# === Safe print for pythonw.exe (sys.stdout/stderr can be None) ===
import builtins

_original_print = builtins.print


def _safe_print(*args, **kwargs):
    """Safe print that handles pythonw.exe environment where stdout is None."""
    if sys.stdout is None:
        return  # Silent fail when no console
    try:
        _original_print(*args, **kwargs)
    except (OSError, ValueError):
        pass  # OSError: [Errno 22] Invalid argument; ValueError: I/O operation on closed file


builtins.print = _safe_print

# === Centralized Debug Logging (works with pythonw.exe) ===
import datetime
import traceback
import faulthandler

from .core.debug import append_log_line as _append_log_line

_DEBUG_LOG_PATH = Path(__file__).parent / "DebugLog" / "pipeline_debug.log"
_CRASH_LOG_PATH = Path(__file__).parent / "DebugLog" / "crash.log"


_PIPELINE_LOG_ENABLED = os.environ.get("ARIA_DEBUG", "1") == "1"

# Module-level pinyin cache (shared across all _screen_pinyin_correct calls)
try:
    from functools import lru_cache as _lru_cache
    from pypinyin import pinyin as _pinyin, Style as _PinyinStyle

    @_lru_cache(maxsize=512)
    def _get_pinyin_cached(s: str) -> tuple:
        return tuple(
            p[0] for p in _pinyin(s, style=_PinyinStyle.NORMAL, errors="ignore")
        )

    _PYPINYIN_AVAILABLE = True
except ImportError:
    _PYPINYIN_AVAILABLE = False

    def _get_pinyin_cached(s: str) -> tuple:
        return ()


def _pipeline_log(stage: str, msg: str):
    """Log to pipeline debug file - works even without console. Gated by ARIA_DEBUG env."""
    if not _PIPELINE_LOG_ENABLED:
        return
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    _append_log_line(_DEBUG_LOG_PATH, f"[{ts}] [{stage}] {msg}")


def _is_personal_build(start: "Path | None" = None) -> bool:
    """True when running inside a personal portable package.

    refresh_release.py stamps PERSONAL_BUILD.txt at the package root
    (next to Aria.exe; app code lives at <root>/_internal/app/aria/).
    Personal packages carry the owner's dev-tree code and config, so the
    public update channel must not swap their code for the sanitized
    public source. Walks a few levels up from this file (or `start`, for
    tests); the dev source tree has no marker anywhere above it, so this
    stays False there.
    """
    try:
        here = (start or Path(__file__)).resolve().parent
        for ancestor in [here, *list(here.parents)[:4]]:
            if (ancestor / "PERSONAL_BUILD.txt").is_file():
                return True
    except Exception:
        pass
    return False


def _screen_ocr_polish_opted_in(vad_cfg: dict | None) -> bool:
    """Return the explicit screen-to-polish consent flag.

    Missing/invalid configuration must fail closed. Older releases treated a
    missing key as enabled, which could send screen context when API polish was
    active even though the user had never enabled the screen enhancement.
    """
    return isinstance(vad_cfg, dict) and vad_cfg.get("screen_ocr_polish") is True


def _auto_hotword_opted_in(auto_hotword_cfg: dict | None) -> bool:
    """Return whether OCR-derived hotword learning was explicitly enabled."""
    return (
        isinstance(auto_hotword_cfg, dict)
        and auto_hotword_cfg.get("enabled") is True
    )


# === Global Exception Hooks (catch crashes in all threads) ===
def _append_crash_entry(source: str, msg: str) -> None:
    """Append one crash entry, keeping crash.log size-bounded.

    crash.log cannot use append_log_line's rename-rotation: faulthandler holds
    a raw fd open on it, so os.replace fails on Windows and the fd must be
    re-enabled after any rotation anyway. Reuse the faulthandler-aware
    rotation instead (size check inside is a no-op below the limit). The lock
    acquire is bounded so a crash inside the rotation path itself can never
    deadlock the hook — worst case we append without rotating.
    """
    try:
        try:
            if _faulthandler_lock.acquire(timeout=2):
                try:
                    _rotate_crash_log_locked()
                finally:
                    _faulthandler_lock.release()
        except Exception:
            pass
        _CRASH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_CRASH_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n[{ts}] UNCAUGHT EXCEPTION ({source})\n{msg}\n")
    except Exception:
        pass


def _global_excepthook(exc_type, exc_value, exc_tb):
    """Catch uncaught exceptions in main thread and log to crash file."""
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _append_crash_entry("main thread", msg)
    _pipeline_log("CRASH", f"Uncaught exception: {exc_type.__name__}: {exc_value}")


def _thread_excepthook(args):
    """Catch uncaught exceptions in worker threads."""
    msg = "".join(
        traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
    )
    thread_name = args.thread.name if args.thread else "unknown"
    _append_crash_entry(f"thread: {thread_name}", msg)
    _pipeline_log(
        "CRASH",
        f"Thread exception ({args.thread}): {args.exc_type.__name__}: {args.exc_value}",
    )


sys.excepthook = _global_excepthook
threading.excepthook = _thread_excepthook

# Enable faulthandler for segfaults/aborts (writes to crash log).
# faulthandler does not support size limits or RotatingFileHandler; it keeps a
# raw file descriptor open. We therefore rotate externally and re-enable it.
_CRASH_LOG_MAX_BYTES = int(
    os.environ.get("ARIA_CRASH_LOG_MAX_BYTES", str(2 * 1024 * 1024))
)
_CRASH_LOG_BACKUP_COUNT = max(
    1, int(os.environ.get("ARIA_CRASH_LOG_BACKUP_COUNT", "2"))
)
_CRASH_LOG_CHECK_INTERVAL_S = float(
    os.environ.get("ARIA_CRASH_LOG_CHECK_INTERVAL_S", "30")
)
_faulthandler_file = None
_faulthandler_lock = threading.Lock()
_crash_log_monitor_started = False


def _crash_log_backup_path(index: int) -> Path:
    return _CRASH_LOG_PATH.with_name(f"{_CRASH_LOG_PATH.name}.{index}")


def _close_faulthandler_file_locked() -> None:
    global _faulthandler_file
    if _faulthandler_file:
        try:
            _faulthandler_file.flush()
        except Exception:
            pass
        try:
            _faulthandler_file.close()
        except Exception:
            pass
        _faulthandler_file = None


def _rotate_crash_log_files_locked() -> None:
    for index in range(_CRASH_LOG_BACKUP_COUNT, 0, -1):
        source = _CRASH_LOG_PATH if index == 1 else _crash_log_backup_path(index - 1)
        target = _crash_log_backup_path(index)
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass
        try:
            if source.exists():
                source.replace(target)
        except OSError:
            pass


def _enable_faulthandler_locked() -> None:
    global _faulthandler_file
    _CRASH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _faulthandler_file = open(_CRASH_LOG_PATH, "ab", buffering=0)
    faulthandler.enable(file=_faulthandler_file, all_threads=False)


def _rotate_crash_log_locked(force: bool = False) -> bool:
    try:
        size = _CRASH_LOG_PATH.stat().st_size if _CRASH_LOG_PATH.exists() else 0
    except OSError:
        size = 0

    if not force and size < _CRASH_LOG_MAX_BYTES:
        return False

    try:
        if faulthandler.is_enabled():
            faulthandler.disable()
    except Exception:
        pass

    _close_faulthandler_file_locked()
    _rotate_crash_log_files_locked()
    _enable_faulthandler_locked()
    _pipeline_log("CRASH", f"Rotated crash.log at {size} bytes")
    return True


def _configure_faulthandler() -> None:
    with _faulthandler_lock:
        try:
            rotated = _rotate_crash_log_locked()
            if not rotated:
                _enable_faulthandler_locked()
        except Exception:
            try:
                _enable_faulthandler_locked()
            except Exception:
                faulthandler.enable(all_threads=False)  # Fallback to stderr


def _crash_log_monitor() -> None:
    while True:
        time.sleep(_CRASH_LOG_CHECK_INTERVAL_S)
        try:
            with _faulthandler_lock:
                _rotate_crash_log_locked()
        except Exception:
            pass


def _start_crash_log_monitor() -> None:
    global _crash_log_monitor_started
    with _faulthandler_lock:
        if _crash_log_monitor_started:
            return
        thread = threading.Thread(
            target=_crash_log_monitor,
            daemon=True,
            name="crash-log-monitor",
        )
        thread.start()
        _crash_log_monitor_started = True


_configure_faulthandler()
_start_crash_log_monitor()


from .core.audio.capture import AudioCapture, AudioConfig
from .core.audio.vad import VADConfig
from .core.asr.funasr_engine import FunASREngine, FunASRConfig
from .core.asr.qwen3_engine import Qwen3ASREngine, Qwen3Config

# Module import is torch/sherpa-free (both lazy inside load paths), so this is
# safe on deployments without the sherpa_onnx wheel; only load() would fail.
from .core.asr.sherpa_engine import (
    SherpaQwen3Engine,
    default_sherpa_num_threads,
    resolve_sherpa_model_dir,
)

# Torch-free at import time too (httpx/numpy only); load() spawns llama-server.
from .core.asr.llamacpp_engine import (
    LlamaCppQwen3Engine,
    resolve_llamacpp_path,
    default_mmproj_for,
    DEFAULT_LLAMACPP_SERVER,
    DEFAULT_LLAMACPP_MODEL,
    DEFAULT_LLAMACPP_PORT,
    DEFAULT_LLAMACPP_NGL,
    DEFAULT_LLAMACPP_CTX,
)
from .core.asr.gpu_pressure import (
    GpuPressureConfig,
    GpuPressureMonitor,
    GpuPressureSample,
)
from .core.asr.rescue_policy import RescueConfig
from .core.hotword import (
    HotWordManager,
    HotWordProcessor,
    AIPolisher,
    PolishStreamError,
    PinyinFuzzyMatcher,
    FuzzyMatchConfig,
)
from .core.learning import (
    CorrectionMutationResult,
    ExplicitCorrectionStore,
    parse_voice_correction,
)
from .core.command import CommandDetector, CommandExecutor
from .core.wakeword import WakewordDetector, WakewordExecutor
from .core.debug import DebugSession, DebugConfig
from .core.insight_store import InsightStore
from .core.history import HistoryStore, RecordType, compose_asr_history_entry
from .core.selection import (
    SelectionDetector,
    SelectionProcessor,
    SelectionCommand,
    CommandType,
)
from .core.editing import (
    RecentVoiceCommandMode,
    RecentVoiceGroupTracker,
    parse_recent_voice_command,
)
from .core.routing.decision import (
    RouteDecision,
    make_dictation_decision,
    write_route_decision,
    write_startup_fingerprint,
)
from .core.action import TranslationAction
from .core.trigger import (
    ACTION_START_RECORDING,
    ACTION_STOP_AND_COMMIT,
    ACTION_TOGGLE_LOCK,
    TriggerStateMachine,
)
from .system.hotkey import HotkeyManager
from .system.output import OutputInjector, OutputConfig
from .system.target_surface import (
    SurfaceKind,
    TargetSnapshot,
    classify_target_surface,
)
from .ui.streaming_display import DisplayBuffer, DisplayState
from .core.logging import get_system_logger
from .core.utils import get_config_path, get_models_path

logger = get_system_logger()


def _record_screen_text_debug(screen_text: str, digest: str) -> None:
    """Record OCR prompt telemetry without exposing page text by default."""
    if DebugConfig.save_screen_text:
        preview = screen_text.replace("\n", " ")[:60]
        _pipeline_log(
            "POST",
            f"Screen text for polish: {len(screen_text)} chars "
            f"(hash={digest}) '{preview}...'",
        )
        try:
            dump_path = Path(__file__).parent / "DebugLog" / "screen_text_dump.log"
            ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            _append_log_line(
                dump_path,
                f"\n===== [{ts}] hash={digest} len={len(screen_text)} =====\n"
                f"{screen_text}",
            )
        except Exception:
            pass
        return

    _pipeline_log(
        "POST",
        f"Screen text for polish: {len(screen_text)} chars "
        f"(hash={digest}, content logging disabled)",
    )


def _record_app_context_debug(app_name: str, category: str) -> None:
    """Record foreground-app telemetry without exposing its name by default."""
    if DebugConfig.save_screen_text:
        _pipeline_log(
            "POST",
            f"Screen context: app={app_name!r}, category={category}",
        )
        return

    import hashlib as _hl

    digest = _hl.sha256(app_name.encode("utf-8", errors="replace")).hexdigest()[:8]
    _pipeline_log(
        "POST",
        f"Screen context: app_hash={digest}, category={category} "
        "(app name logging disabled)",
    )

# Hotkey re-registration cadence after a failed grab (seconds). The key being
# held by another process (another Aria instance, other software) is usually
# transient — retrying in the background turns "dead until app restart" into
# "recovers within ~30s of the key being freed" (2026-07-19 slim-trial
# forensics: second instance lost F11 for the whole session).
HOTKEY_RETRY_INTERVAL_S = 30.0


class AppState(Enum):
    """Application states."""

    IDLE = auto()
    RECORDING = auto()
    TRANSCRIBING = auto()
    SELECTION_LISTENING = auto()  # Listening for voice command on selected text
    SELECTION_PROCESSING = auto()  # Processing selected text with LLM


class SleepMode(Enum):
    """Sleep mode tiers."""

    AWAKE = auto()  # Normal operation
    LIGHT = auto()  # Voice-triggered: model loaded, audio active, input ignored
    DEEP = auto()  # Manual: model unloaded, audio stopped, GPU idle


@dataclass
class StreamingConfig:
    """流式识别配置"""

    enabled: bool = True  # 是否启用流式显示
    chunk_interval_ms: int = 1000  # 每1秒触发中间识别（平衡响应性和GPU压力）
    min_chunk_samples: int = 12000  # 最少0.75秒音频才处理 (12000 samples @ 16kHz)
    min_speech_ms: int = 800  # 最少说话0.8秒才开始流式识别


def _session_speech_segment_count(
    vad_stats,
    buffered_count: int,
    deferred_count: int,
    tail_audio_usable: bool,
) -> int:
    """True speech_segments value for one worker commit.

    Fixes the dead session-JSON field (mining/report.md section C: always 0 —
    nothing ever passed it to log_audio).  Counts the VAD-detected speech
    segments contributing to THIS worker iteration: buffered soft-split texts
    and deferred audio chunks drained by a 'final', plus the tail segment when
    the VAD state machine actually committed one (endpoint_reason present) or
    a manual stop caught in-flight voiced audio.  Legacy stats-less paths fall
    back to "tail audio long enough to be queued" as a best-effort truth.
    """
    count = int(buffered_count) + int(deferred_count)
    if isinstance(vad_stats, dict):
        if vad_stats.get("endpoint_reason") in (
            "tail_silence",
            "max_speech",
            "soft_split",
        ):
            count += 1
        else:
            # manual_stop (or reason-less stats): count the tail only when
            # voiced frames were actually observed in the in-flight window.
            try:
                if float(vad_stats.get("voiced_ratio", -1.0)) > 0.0:
                    count += 1
            except (TypeError, ValueError):
                pass
    elif tail_audio_usable:
        count += 1
    return count


class AriaApp:
    """
    Main Aria application.

    Orchestrates:
    - Hotkey listening (default: backtick ` toggle)
    - Audio capture with VAD
    - ASR transcription (Qwen3/FunASR)
    - HotWord correction:
      - Layer 1: ASR initial_prompt (zero latency)
      - Layer 2: Regex replacement (zero latency)
      - Layer 2.5: Pinyin fuzzy match (zero latency)
      - Layer 3: AI polish via LLM (optional, ~100ms)
    - Text insertion

    Usage (Qt mode):
        app = AriaApp(hotkey="grave")
        app.set_bridge(bridge)  # QtBridge for UI updates
        app.start()  # Non-blocking
        ...
        app.stop()  # Cleanup

    Usage (CLI mode):
        app = AriaApp(hotkey="grave")
        app.run()  # Blocking
    """

    # Manual CPU acceleration intentionally runs the small Qwen3 0.6B profile.
    # That keeps dictation usable when the GPU is busy, but CPU cannot sustain
    # the same long-buffer interim strategy as CUDA 1.7B.  These runtime-only
    # caps keep commit segments short and make final text take priority over
    # optional right-corner live subtitles.
    # Field telemetry 2026-06-10: 0.6B/float32 decodes at ~0.5-0.8x realtime on
    # CPU even with zero context, so the caps match the GPU VAD defaults
    # (10s/5s) instead of the old 8s/3.5s, which fragmented every sentence.
    _CPU_ASR_MAX_NEW_TOKENS = 512
    _GPU_ASR_MAX_NEW_TOKENS = 1024
    _CPU_ASR_MAX_SPEECH_MS = 10000
    _CPU_ASR_SOFT_SPLIT_MIN_SPEECH_MS = 5000
    # CPU keeps hotword biasing (it is what makes 0.6B usable for domain terms)
    # but bounds the prompt so prefill stays cheap on slow CPUs.  recent_context
    # stays OFF on CPU — it is the regurgitation vector with the worst
    # cost/benefit there (a parroting retry doubles an already slow decode).
    _CPU_ASR_CONTEXT_MAX_CHARS = 600
    # GPU recent_context (committed sentences + in-flight same-utterance
    # prefix) tail cap. Field logs show a normal utterance's committed buffer
    # plus in-flight prefix sits well under this; the cap only guards against
    # an unusually long monologue growing the prompt without bound.
    _RECENT_CTX_MAX_CHARS = 1200
    _CPU_INTERIM_MAX_AUDIO_S = 3.0
    _GPU_INTERIM_MAX_AUDIO_S = 10.0
    _CPU_INTERIM_TIMEOUT_S = 4.0
    _GPU_INTERIM_TIMEOUT_S = 10.0
    _FAST_WAKEWORD_ACTIONS = frozenset({"open_path", "open_directory"})
    # Independent selected-text tools (translation, summary, reply, explicit
    # chat) keep their dedicated UI, but a more specific recent-voice request
    # gets first refusal before they run.  Preset editing commands such as
    # polish/expand/shorten/rewrite are retired and no longer reach this set.
    # All other wakeword actions are deterministic/local side effects and must
    # win before any AI fallback.
    _CONTEXTUAL_AI_WAKEWORD_ACTIONS = frozenset(
        {
            "selection_process",
            "translate_popup",
            "summarize_popup",
            "ask_ai",
            "reply_popup",
        }
    )
    # An independent selection-tool trigger can also appear inside natural
    # feedback. Only an exact tool phrase or an explicit selection reference
    # may keep the selection route.
    _EXPLICIT_SELECTION_REFERENCES = (
        "选中文字",
        "选中的文字",
        "选中文本",
        "选中的文本",
        "选中内容",
        "选中的内容",
        "当前选区",
        "这个选区",
        "所选内容",
    )
    _CONTEXTUAL_REQUEST_LEAD_INS = (
        "可以请你",
        "你能不能",
        "麻烦你",
        "请你",
        "能不能",
        "能否",
        "麻烦",
        "请",
        "可以",
        "给我",
    )
    # The general AI chat window is deliberately opt-in.  Broad verbs such as
    # "帮我看一下" or "分析一下" usually refer to the latest dictated passage
    # and must not unexpectedly open a separate conversation window.
    _EXPLICIT_AI_CHAT_TERMS = (
        "问ai",
        "问一下ai",
        "问问ai",
        "打开ai对话",
        "打开ai聊天",
        "ai对话",
        "ai聊天",
        "跟ai聊",
        "和ai聊",
        "让ai回答",
        "问人工智能",
    )
    _FAST_WAKEWORD_SUPPRESS_TTL_S = 12.0
    _VOICE_EDIT_CHOICE_TTL_S = 20.0
    _VOICE_EDIT_UNDO_TTL_S = 120.0

    def __init__(self, hotkey: str = "grave"):
        self.hotkey = hotkey
        self.state = AppState.IDLE
        self._lock = threading.Lock()
        self._running = False

        # Hotkey trigger mode: "toggle" (default, legacy press-to-toggle) or
        # "hold_to_talk" (opt-in: hold = push-to-talk, tap/double-tap = lock).
        # The state machine is only fed in hold_to_talk mode.
        self._trigger_mode = self._load_trigger_mode()
        self._trigger_sm = TriggerStateMachine()

        # UI Bridge (optional, for Qt frontend)
        self._bridge = None

        # Components
        self.hotkey_manager = HotkeyManager()
        self.audio_capture: AudioCapture = None
        self.asr_engine: ASREngine = None
        # Output injector with config (supports typewriter mode for game compatibility)
        output_config = self._load_output_config()
        self.output_injector = OutputInjector(output_config)
        self._clipboard_lock = threading.Lock()  # Thread-safe clipboard access
        self.output_injector.set_clipboard_lock(self._clipboard_lock)
        self._asr_engine_type: str = "qwen3"  # "qwen3" or "funasr"
        self.display = DisplayBuffer()

        # HotWord system (Layer 1: ASR prompt + Layer 2: Regex + Layer 2.5: Fuzzy + Layer 3: AI Polish)
        self.hotword_manager: HotWordManager = None
        self.hotword_processor: HotWordProcessor = None
        self.fuzzy_matcher: PinyinFuzzyMatcher = None
        self.polisher: AIPolisher = None
        self._pending_voice_edit_choice = None
        self._last_voice_edit_undo = None
        self._voice_edit_mutation_lock = threading.Lock()
        self._recent_voice_groups = RecentVoiceGroupTracker()
        self._explicit_correction_store = ExplicitCorrectionStore(
            Path(__file__).parent / "data" / "explicit_corrections.jsonl"
        )

        # Auto-hotword system: screen-OCR-driven session hotword tracker, gated
        # by an LLM reviewer. See `core/hotword/session_tracker.py` and
        # `auto_hotword_reviewer.py`.
        self._auto_hotword_tracker = None  # SessionHotwordTracker
        self._auto_hotword_reviewer = None  # AutoHotwordReviewer
        self._auto_hotword_cfg: dict = {}
        self._auto_hotword_review_thread = None  # threading.Thread (daily timer)
        self._auto_hotword_review_lock = threading.Lock()
        self._auto_hotword_review_stop = threading.Event()
        # T6: optional OCR raw-text sampler (default OFF — opt-in audit hook).
        # Built in _init_auto_hotword once we know the config block.
        self._ocr_sampler = None

        # Voice command system (Layer 0: Command detection before text insertion)
        self.command_detector: CommandDetector = None
        self.command_executor: CommandExecutor = None

        # Wakeword system (Layer -1: App-level commands via "小助手")
        self.wakeword_detector: WakewordDetector = None
        self.wakeword_executor: WakewordExecutor = None

        # ASR worker thread (non-blocking transcription).
        # Queue payload is a 3-tuple (session_id, audio, kind) where kind is one of:
        #   'soft_split' — mid-utterance pause split: worker runs ASR, buffers raw text,
        #                  DOES NOT polish / insert / emit_insert_complete
        #   'final'      — true EOS (speech_end or user release): worker runs ASR on
        #                  this final audio (if any), joins with any buffered soft-split
        #                  raw texts, then polishes once + pastes once + emits once
        # Keep enough room for long-dictation soft-splits while GPU/CPU fallback
        # is temporarily slow.  A 10s mono float32 chunk is only ~0.6MB; a deeper
        # queue is safer than silently losing the middle of a sentence.
        self._asr_queue: queue.Queue = queue.Queue(maxsize=16)
        self._asr_thread: threading.Thread = None
        self._stop_event = threading.Event()

        # Session-level raw-text accumulator — keyed by session_id to prevent
        # cross-session contamination when F11 toggles rapidly.
        # Each recording's soft-split ASR results land in dict[session_id]; the
        # matching 'final' worker iteration pops that key and commits. Stale
        # buckets from older sessions are GC'd in _start_recording.
        self._session_raw_segments: dict[int, list[str]] = {}
        # Audio seconds behind each session's buffered soft-split texts.
        # Segment audio is discarded right after transcription, so the final
        # commit can't recompute chain duration — accumulate it here so the
        # history record covers the whole utterance, not just the tail
        # segment (BACKLOG DATA-1). Drained/GC'd alongside the text bucket.
        self._session_soft_seg_seconds: dict[int, float] = {}
        # Soft-split audio that looked like real speech but produced no ASR
        # text (GPU/CPU pressure timeout, filtered hallucination, etc.).  The
        # final segment gets one retry with these chunks concatenated instead
        # of silently losing the middle of the utterance.
        self._session_deferred_audio_segments: dict[int, list] = {}
        # Ephemeral recovery copy of the most recent real transcript. This is
        # independent of persistent history so Paste/Copy last still works
        # when history storage is disabled; it disappears with the process.
        self._last_transcript_text = ""
        # Target captured when the session reaches its final commit boundary.
        # ASR/polish may take seconds; output is refused if focus moves before
        # the worker is ready to inject.
        self._session_output_targets: dict[int, TargetSnapshot] = {}
        # Audio-time window for each final commit.  This is independent of ASR
        # and model latency, so recent-passage grouping follows the user's
        # spoken pauses rather than worker backlog.
        self._session_voice_started_at: dict[int, float] = {}
        self._session_voice_windows: dict[int, tuple[float, float]] = {}
        self._session_lock = threading.Lock()
        self._worker_busy = False  # True while worker is processing a segment

        # Hotkey action queue (non-blocking hotkey callback → dedicated action thread)
        self._hotkey_action_queue: queue.Queue = queue.Queue(maxsize=4)
        self._hotkey_action_thread: threading.Thread = None
        # Background re-registration after a failed hotkey grab (key held by
        # another instance / other software). Exits with _stop_event.
        self._hotkey_retry_thread: threading.Thread = None

        # Stats
        self._session_count = 0

        # Sound control
        self._sound_enabled = True

        # Selection mode (smart detection: same hotkey, auto-detect if text selected)
        self._selection_mode = False
        self._selected_text: str = None
        self._selection_target = None
        self._selection_capture_reason = "target_unavailable"
        self._original_clipboard: str = None
        # Full-format snapshot (text/image/files) matching _original_clipboard;
        # lets _cleanup_selection_mode put back non-text clipboards too.
        self._original_clipboard_formats = None
        self.selection_detector: SelectionDetector = None
        self.selection_processor: SelectionProcessor = None

        # Auto-send control (press Enter after text insertion)
        self._auto_send_enabled = False

        # Pre-ASR energy gate (configurable from settings, updated by hot-reload)
        self._energy_threshold = 0.003
        # User's "base" energy threshold (from config). When the capture mode
        # has an energy_gate_override, _energy_threshold is set to the
        # override; reverting to a non-overriding mode restores _base.
        self._energy_threshold_base = 0.003
        # Same idea for the Silero VAD threshold (0..1 probability). The
        # actual VAD lives on audio_capture._vad.config.threshold; we cache
        # the user's configured value here so a mode-switch can revert.
        self._vad_threshold_base = 0.2

        # Pre-ASR capture-mode DSP (HPF→Gate→AGC→Limiter pipeline).
        #   "standard" — daily default, near-transparent for near-field
        #   "noisy"    — strong-environment-noise, aggressive gate to reject
        #                steady noise (HVAC/fan/typing)
        #   "whisper"  — quiet-environment + soft-voice rescue, aggressive AGC
        #                + lower VAD threshold + lower energy gate
        # See core/audio/dsp.py MODE_PRESETS for parameter values.
        self._capture_mode: str = "standard"
        # Software microphone receive volume.  1.0 = 100%; applied before VAD
        # and ASR so the user-facing slider behaves like a simple input trim.
        self._mic_input_gain: float = 1.0

        # Noise text filter (post-ASR, drops filler-only outputs like 嗯/啊/呃)
        self._noise_filter_enabled = True

        # Anti-hallucination gates (2026-07-20, cluster_20260720/hallucination
        # H2). Defaults mirror _apply_hallucination_gate_config; real values
        # load from config there (startup + hot-reload).
        from .core.asr.acoustic_policy import (
            CONF_GATE_AVG_LOGPROB_FLOOR,
            CONF_GATE_MIN_LOGPROB_FLOOR,
            VAD_JOINT_GATE_ENERGY_MAX,
            VAD_JOINT_GATE_VAD_MAX_BELOW,
        )

        self._vad_joint_gate_enabled = True
        self._vad_joint_gate_energy_max = VAD_JOINT_GATE_ENERGY_MAX
        self._vad_joint_gate_vad_max = VAD_JOINT_GATE_VAD_MAX_BELOW
        self._template_blacklist_enabled = True
        self._template_blacklist_extra: tuple = ()
        self._conf_gate_enabled = False
        self._conf_gate_floor = CONF_GATE_AVG_LOGPROB_FLOOR
        self._conf_gate_min_enabled = True
        self._conf_gate_min_floor = CONF_GATE_MIN_LOGPROB_FLOOR

        # Energy-gate VAD exemption (2026-07-23 collapsed-mic incident):
        # rescue high-VAD speech below the energy gate, and warn the user
        # once the pattern repeats (their system mic level is broken).
        self._energy_gate_vad_exempt_enabled = True
        self._low_level_speech_streak = 0
        self._low_level_mic_warn_at = 0.0

        # Recent ASR context buffer for continuity across speech segments
        self._recent_asr_buffer: list = []  # [text, text, ...]
        self._recent_context_max = 10  # keep last N entries
        # In-flight prefix for the CURRENT utterance: the text already
        # transcribed from earlier soft-split segments of the SAME sentence.
        # The committed buffer above only updates at final-commit, so without
        # this a long sentence split into 8s chunks transcribes each chunk
        # blind to the earlier ones — the ASR worker (single thread) sets this
        # per segment so _prepare_asr_engine_for_segment can extend the engine's
        # recent_context with the same-utterance prefix. Reset to "" per segment.
        self._active_session_ctx_prefix: str = ""

        # Screen OCR for ASR context (triggered on speech start)
        self._screen_ocr = None  # Lazy init
        self._screen_ocr_enabled = False
        self._screen_ocr_polish_enabled = False  # OCR → Polish requires opt-in

        # Sleep mode: AWAKE (normal), LIGHT (voice-triggered), DEEP (model unloaded)
        self._sleep_mode = SleepMode.AWAKE
        self._deep_sleep_lock = threading.Lock()  # Prevent concurrent reload
        self._reload_thread: threading.Thread | None = None
        # Memory guard: keep Qwen3 1.7B quality while releasing the model when
        # Aria is idle. This uses the existing deep-sleep path; it does not
        # change model, dtype, hotwords, or decoding settings.
        self._auto_deep_sleep_enabled: bool = True
        self._auto_deep_sleep_idle_s: float = 300.0
        self._auto_deep_sleep_timer: threading.Timer | None = None

        # GPU pressure fallback: when ComfyUI/SD saturates CUDA, keep dictation
        # responsive by routing Qwen3-ASR to a separate CPU 0.6B engine.
        self._gpu_fallback_enabled: bool = False
        self._gpu_pressure_monitor: GpuPressureMonitor | None = None
        self._gpu_fallback_qwen3_cfg: dict = {}
        self._gpu_fallback_engine: Qwen3ASREngine | None = None
        self._gpu_fallback_lock = threading.Lock()
        self._gpu_fallback_preload_thread: threading.Thread | None = None
        self._gpu_fallback_probe_thread: threading.Thread | None = None
        self._gpu_fallback_unload_timer: threading.Timer | None = None
        self._gpu_fallback_last_used_at: float = 0.0
        self._gpu_fallback_idle_unload_s: float = 120.0
        self._gpu_fallback_max_segment_s: float = 6.0
        self._gpu_fallback_failed_until: float = 0.0
        self._primary_asr_suppressed_until: float = 0.0
        self._gpu_fallback_retry_on_stall: bool = True
        self._gpu_fallback_primary_stall_timeout_s: float = 8.0

        # ASR final-segment rescue chain: consecutive timeout/empty failures
        # trigger a primary engine rebuild (fresh CUDA context), optionally a
        # cloud second-pass transcription of the lost audio.  Streak/timestamps
        # are only touched from the single ASR worker thread.
        self._asr_rescue_cfg: RescueConfig = RescueConfig()
        self._asr_failure_streak: int = 0
        self._asr_last_self_heal_at: float = 0.0
        self._asr_self_heal_thread: threading.Thread | None = None
        self._last_success_insert_at: float = 0.0
        # Guards the late-insert TOCTOU window: the cloud rescue thread holds
        # this around "re-check gates + insert" while the ASR worker holds it
        # for "mark busy" / "refresh insert anchor", so a rescue result can
        # never slip in after a newer segment started committing.
        self._rescue_insert_lock = threading.Lock()
        # At most one cloud rescue upload in flight (cost + ordering safety).
        self._cloud_rescue_inflight = threading.Event()
        self._asr_hot_reload_lock = threading.Lock()
        self._asr_hot_reload_thread: threading.Thread | None = None
        self._asr_hot_reload_in_progress: bool = False
        self._asr_hot_reload_target_cfg: dict | None = None
        self._asr_hot_reload_pending_cfg: dict | None = None

        # Disabled mode: hotkey toggles back to enabled (for elevation dialog)
        self._is_disabled = False

        # Config file watcher (hot-reload)
        self._config_path = get_config_path("hotwords.json")
        self._config_mtime = 0.0
        self._watcher_thread: threading.Thread = None
        self._deepseek_setup_lock = threading.Lock()
        self._deepseek_setup_thread: threading.Thread | None = None
        self._deepseek_setup_in_progress = False
        self._deepseek_setup_error = ""

        # Streaming ASR (interim results while speaking)
        self._streaming_config = StreamingConfig()
        self._interim_timer: threading.Timer = None
        self._last_interim_text: str = ""
        self._interim_generation: int = 0  # Generation token to prevent stale updates
        self._asr_lock = threading.Lock()  # Prevent concurrent ASR calls
        # Conservative wakeword fast path: optional interim ASR may execute a
        # short, command-only action before the final ASR commit arrives.  This
        # is session-scoped so final processing can suppress duplicate execution.
        self._fast_wakeword_lock = threading.Lock()
        self._fast_wakeword_session: int | None = None
        self._fast_wakeword_cmd_id: str = ""
        self._fast_wakeword_success: bool = False
        self._fast_wakeword_started_at: float = 0.0

        # Audio stream health monitoring
        self._last_audio_callback_time: float = 0.0
        self._audio_stale_threshold_s: float = 5.0  # No audio for 5s = stream dead

        # Window-change OCR refresh during continuous recording
        self._ocr_watcher_thread: threading.Thread = None
        self._ocr_last_hwnd: int = 0

    def _beep(self, frequency: int, duration: int) -> None:
        """Play beep if sound is enabled (non-blocking)."""
        if self._sound_enabled:
            threading.Thread(
                target=winsound.Beep, args=(frequency, duration), daemon=True
            ).start()

    # Last-resort square-wave tones for when the SoundManager itself cannot
    # be imported (UI package unavailable). Matches the legacy direct beeps.
    _SOUND_FALLBACK_BEEPS = {
        "start_recording": (800, 50),
        "stop_recording": (400, 50),
        "error": (300, 120),
        "rescue": (300, 120),
        "lock": (600, 50),
    }

    def _play_sound(self, event: str) -> None:
        """Route a feedback event through the unified SoundManager.

        Replaces the old direct winsound.Beep calls (ui_spec section 2.3:
        "kill the dual-track"). Never blocks the calling thread and never
        raises. The manager handles wav playback, the missing-wav beep
        fallback, volume, and the whisper-mode auto-quiet rule; this method
        only adds the backend's master switch and an import-failure fallback.

        getattr default False: partially-constructed apps (tests build
        skeletons via AriaApp.__new__) must stay silent.
        """
        if not getattr(self, "_sound_enabled", False):
            return
        try:
            from .ui.qt.sound import play_sound
        except Exception:
            beep = self._SOUND_FALLBACK_BEEPS.get(event)
            if beep:
                self._beep(*beep)
            return
        try:
            play_sound(event)
        except Exception:
            pass

    def _configure_sound(self, asr_cfg: dict) -> None:
        """Apply the config file's sound block to the shared SoundManager."""
        block = dict((asr_cfg or {}).get("sound", {}) or {})
        try:
            from .ui.qt.sound import get_sound_manager

            manager = get_sound_manager()
            manager.configure(
                enabled=bool(block.get("enabled", True)),
                volume=block.get("volume", 1.0),
                quiet_in_whisper=bool(block.get("quiet_in_whisper", True)),
            )
            manager.set_capture_mode(getattr(self, "_capture_mode", "standard"))
            print(
                f"[SOUND] enabled={manager.enabled}, volume={manager.volume:.2f}, "
                f"quiet_in_whisper={manager.quiet_in_whisper}"
            )
        except Exception as exc:
            logger.warning(f"Sound config apply failed: {exc}")

    def set_sound_enabled(self, enabled: bool) -> None:
        """Enable or disable sound effects."""
        self._sound_enabled = enabled
        print(f"[Aria] Sound {'enabled' if enabled else 'disabled'}")

    def set_auto_send(self, enabled: bool) -> None:
        """Enable or disable auto-send (press Enter after text insertion)."""
        self._auto_send_enabled = enabled
        print(f"[Aria] Auto-send {'enabled' if enabled else 'disabled'}")

    def get_auto_send(self) -> bool:
        """Check if auto-send is enabled."""
        return self._auto_send_enabled

    def _remember_last_transcript(self, text: str) -> None:
        """Remember one real transcript for explicit user recovery actions."""
        value = str(text or "").strip()
        if not value:
            return
        lock = getattr(self, "_session_lock", None)
        if lock is None:
            self._last_transcript_text = value
            return
        with lock:
            self._last_transcript_text = value

    def get_last_transcript(self) -> str:
        """Return runtime last transcript, then persisted history fallback."""
        lock = getattr(self, "_session_lock", None)
        if lock is None:
            value = getattr(self, "_last_transcript_text", "")
        else:
            with lock:
                value = getattr(self, "_last_transcript_text", "")
        value = str(value or "").strip()
        if value:
            return value

        store = getattr(self, "history_store", None)
        latest = getattr(store, "latest_asr_text", None)
        if not callable(latest):
            return ""
        try:
            return str(latest() or "").strip()
        except Exception:
            logger.debug("Could not load last transcript from history", exc_info=True)
            return ""

    def _maybe_auto_send_after_insert(
        self,
        insert_ok: bool,
        expected_target: TargetSnapshot | None = None,
    ) -> bool:
        """Submit only after the text transaction was accepted.

        Pressing Enter after a failed paste (especially a focus-generation
        abort) can execute an unrelated command in the newly focused window.
        """
        if not self._auto_send_enabled:
            return False
        if not insert_ok:
            print("[AUTO-SEND] Skipped because text was not inserted")
            _pipeline_log("OUTPUT", "Auto-send skipped: insertion not accepted")
            return False

        # A terminal paste and an Enter key are two different transactions.
        # Never let the global chat-style auto-send toggle turn terminal text
        # into an executed shell/CLI command.  Use both the captured carrier
        # and the just-completed content-free delivery report so this remains
        # safe even when one of those signals is unavailable.
        target_kind = getattr(
            getattr(expected_target, "profile", None), "kind", None
        )
        if target_kind in {SurfaceKind.TERMINAL, SurfaceKind.GAME}:
            print(f"[AUTO-SEND] Skipped for {target_kind.value} target")
            _pipeline_log(
                "OUTPUT", f"Auto-send skipped: {target_kind.value} target"
            )
            return False

        metadata_getter = getattr(
            getattr(self, "output_injector", None),
            "get_last_delivery_metadata",
            None,
        )
        if callable(metadata_getter):
            try:
                delivery = metadata_getter().get("delivery", {})
            except Exception:
                # A modern injector that cannot report the committed carrier
                # must not authorize a destructive post-action.
                logger.debug("Auto-send delivery report failed", exc_info=True)
                print(
                    "[AUTO-SEND] Skipped because delivery report was unavailable"
                )
                _pipeline_log(
                    "OUTPUT", "Auto-send skipped: delivery report unavailable"
                )
                return False
            if (
                delivery.get("surface")
                in {SurfaceKind.TERMINAL.value, SurfaceKind.GAME.value}
                or delivery.get("allow_auto_send") is False
            ):
                print("[AUTO-SEND] Skipped by delivery policy")
                _pipeline_log("OUTPUT", "Auto-send skipped: delivery policy")
                return False

        time.sleep(0.05)
        if expected_target is not None:
            target_guard = getattr(
                self.output_injector, "is_target_snapshot_current", None
            )
            try:
                target_current = bool(
                    callable(target_guard) and target_guard(expected_target)
                )
            except Exception:
                logger.debug("Auto-send target guard failed", exc_info=True)
                target_current = False
            if not target_current:
                print("[AUTO-SEND] Skipped because target focus changed")
                _pipeline_log("OUTPUT", "Auto-send skipped: target focus changed")
                self._emit_notice(
                    "目标窗口已变化，文字已输入但未自动发送", "info", 2600
                )
                return False
        if expected_target is None:
            sent = self.output_injector.send_key("enter")
        else:
            sent = self.output_injector.send_key(
                "enter", expected_target=expected_target
            )
        if sent:
            print("[AUTO-SEND] Enter pressed")
            return True
        if expected_target is not None:
            try:
                target_current = bool(target_guard(expected_target))
            except Exception:
                target_current = False
            if not target_current:
                print("[AUTO-SEND] Enter aborted because target focus changed")
                _pipeline_log("OUTPUT", "Auto-send Enter aborted at commit edge")
                self._emit_notice(
                    "目标窗口已变化，文字已输入但未自动发送", "info", 2600
                )
                return False
        print("[AUTO-SEND] Failed to send Enter")
        return False

    def set_sleeping(self, sleeping: bool, *, force_emit: bool = False) -> None:
        """
        Set light sleeping mode (voice-triggered).

        When sleeping:
        - VAD and ASR continue running (wakeword must still work)
        - All non-wakeword input is ignored
        - UI shows sleeping indicator

        Args:
            sleeping: True to enter light sleep, False to wake up
            force_emit: If True, emit UI signals even if state didn't change
                       (useful for wakeword to re-sync UI if it got out of sync)
        """
        with self._lock:
            # Ignore if in deep sleep (audio is off, wakeword can't work)
            if self._sleep_mode == SleepMode.DEEP:
                print("[SLEEPING] Ignored: currently in deep sleep mode")
                return
            target = SleepMode.LIGHT if sleeping else SleepMode.AWAKE
            changed = self._sleep_mode != target
            self._sleep_mode = target
            bridge = self._bridge  # Save reference to avoid race condition

        # Debug logging helper
        def _log(msg):
            import datetime
            from pathlib import Path

            ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            line = f"[{ts}] [SLEEPING] {msg}"
            print(line)
            log_path = Path(__file__).parent / "DebugLog" / "wakeword_debug.log"
            _append_log_line(log_path, line)

        # Log state change
        if changed:
            action = "Entering" if sleeping else "Exiting"
            _log(f"{action} sleeping mode")
        elif force_emit:
            _log(f"Re-sync UI: sleeping={sleeping}")

        # Emit UI signals if changed or forced
        if not bridge or not (changed or force_emit):
            _log(
                f"Early return: bridge={bridge is not None}, changed={changed}, force_emit={force_emit}"
            )
            return

        # Emit settingChanged first (for popup menu)
        _log(f"About to emit signals, bridge={bridge is not None}")
        try:
            bridge.emit_setting_changed("sleeping", sleeping)
            _log(f"emit_setting_changed('sleeping', {sleeping}) called OK")
        except Exception as e:
            _log(f"Warning: Failed to emit settingChanged: {e}")

        # Emit stateChanged (for floating ball visual)
        try:
            if sleeping:
                bridge.emit_state("SLEEPING")
            else:
                # When waking up, check if we're currently recording
                # If so, restore to RECORDING state (user still has hotkey pressed)
                if self.state == AppState.RECORDING:
                    bridge.emit_state("RECORDING")
                    _log("emit_state('RECORDING') called OK (wake during recording)")
                else:
                    bridge.emit_state("IDLE")
            _log(
                f"emit_state({'SLEEPING' if sleeping else 'IDLE/RECORDING'}) called OK"
            )
        except Exception as e:
            _log(f"Warning: Failed to emit stateChanged: {e}")

    def set_deep_sleep(self, deep: bool) -> None:
        """
        Enter or exit deep sleep (full engine unload to free GPU VRAM).

        Deep sleep:
        - ASR model is unloaded from GPU
        - Audio capture is blocked (hotkey triggers auto-wake)
        - Only manual button or hotkey can wake up

        Args:
            deep: True to enter deep sleep, False to wake up
        """
        if deep:
            self._enter_deep_sleep()
        else:
            self._exit_deep_sleep()

    def _cancel_auto_deep_sleep_timer(self) -> None:
        """Cancel pending automatic deep sleep timer."""
        timer = self._auto_deep_sleep_timer
        self._auto_deep_sleep_timer = None
        if timer:
            try:
                timer.cancel()
            except Exception:
                pass

    def _schedule_auto_deep_sleep(self, reason: str) -> None:
        """Schedule model unload after an idle period, preserving ASR quality."""
        self._cancel_auto_deep_sleep_timer()
        if not self._auto_deep_sleep_enabled:
            return
        if self._sleep_mode == SleepMode.DEEP:
            return
        idle_s = max(60.0, float(self._auto_deep_sleep_idle_s or 300.0))
        timer = threading.Timer(idle_s, self._auto_deep_sleep_fire, args=(reason,))
        timer.daemon = True
        self._auto_deep_sleep_timer = timer
        timer.start()
        _pipeline_log(
            "MEMORY",
            f"Auto deep sleep scheduled in {idle_s:.0f}s (reason={reason})",
        )

    def _auto_deep_sleep_fire(self, reason: str) -> None:
        """Timer callback: unload ASR model only if the app is truly idle."""
        self._auto_deep_sleep_timer = None
        if not self._auto_deep_sleep_enabled or not self._running:
            return
        if self.asr_engine is None:
            # No engine to unload (degraded no-ASR state): skip silently so
            # the idle timer never spams the refusal toast.
            return
        with self._lock:
            state = self.state
            sleep_mode = self._sleep_mode
        if sleep_mode != SleepMode.AWAKE or state != AppState.IDLE:
            _pipeline_log(
                "MEMORY",
                f"Auto deep sleep skipped: state={state.name}, sleep={sleep_mode.name}",
            )
            self._schedule_auto_deep_sleep("still_active")
            return
        if self.audio_capture and self.audio_capture.is_recording:
            self._schedule_auto_deep_sleep("recording")
            return
        if not self._asr_queue.empty() or self._worker_busy:
            self._schedule_auto_deep_sleep("asr_busy")
            return
        _pipeline_log("MEMORY", f"Auto deep sleep firing (reason={reason})")
        print("[MEMORY] Auto deep sleep: idle timeout reached, unloading ASR model")
        self.set_deep_sleep(True)

    def _enter_deep_sleep(self) -> None:
        """Enter deep sleep: stop recording, wait for pending commits, unload model."""
        if self.asr_engine is None:
            # Degraded no-ASR state (slim install whose engine failed to
            # load): deep sleep's whole point is unloading the engine, and
            # there is none — refuse instead of dereferencing None below.
            print("[DEEP_SLEEP] Refused: no ASR engine loaded (nothing to unload)")
            _pipeline_log("DEEP_SLEEP", "Refused: no ASR engine (degraded state)")
            self._emit_error("无可用识别引擎，无法进入深度休眠")
            bridge = self._bridge
            if bridge:
                # Confirm the unchanged state so an optimistic UI toggle resyncs.
                bridge.emit_setting_changed("deep_sleeping", False)
            return
        self._cancel_auto_deep_sleep_timer()
        # Atomic: check + stop active operations + set DEEP flag in single lock
        with self._lock:
            if self._sleep_mode == SleepMode.DEEP:
                print("[DEEP_SLEEP] Already in deep sleep, ignoring")
                return
            print("[DEEP_SLEEP] Entering deep sleep...")
            if self.state == AppState.RECORDING:
                self._stop_recording()
            elif self.state in (
                AppState.SELECTION_LISTENING,
                AppState.SELECTION_PROCESSING,
            ):
                self._cancel_selection_mode()
            self._sleep_mode = SleepMode.DEEP

        # CRITICAL: wait for the ASR worker to drain any 'final' items that
        # _stop_recording just enqueued. Otherwise those items — which carry
        # the session-buffered raw text from soft-split precompute — get
        # silently discarded below, and the user loses minutes of dictation.
        # Bounded wait (12s) prevents deadlock if ASR hangs. Terminal-bypass
        # commits (no remote polish) typically finish in <1s.
        _drain_start = time.time()
        _drain_deadline = 12.0
        while True:
            if self._asr_queue.empty() and not self._worker_busy:
                break
            if time.time() - _drain_start > _drain_deadline:
                _pipeline_log(
                    "DEEP_SLEEP",
                    f"Queue drain timeout ({_drain_deadline}s) — forcing discard "
                    f"(pending={self._asr_queue.qsize()}, worker_busy={self._worker_busy})",
                )
                print(
                    f"[DEEP_SLEEP] WARN: drain timeout, discarding {self._asr_queue.qsize()} pending"
                )
                break
            time.sleep(0.05)
        _drain_ms = (time.time() - _drain_start) * 1000
        _pipeline_log(
            "DEEP_SLEEP",
            f"Pre-unload drain complete in {_drain_ms:.0f}ms",
        )

        # Also proactively flush any session buckets that were never turned
        # into a 'final' (e.g., the recording was force-terminated without
        # a speech_end firing and without _stop_recording reaching its
        # enqueue path). Logged for debuggability; entries cannot be
        # recovered once we exit to DEEP_SLEEPING state.
        with self._session_lock:
            _orphans = {
                sid: segs for sid, segs in self._session_raw_segments.items() if segs
            }
            if _orphans:
                for _sid, _segs in _orphans.items():
                    _text = "".join(_segs)
                    _pipeline_log(
                        "DEEP_SLEEP",
                        f"WARN: stranded {len(_text)} chars from session {_sid}: '{_text[:80]}'",
                    )
                self._session_raw_segments.clear()
            _audio_orphans = {
                sid: segs
                for sid, segs in self._session_deferred_audio_segments.items()
                if segs
            }
            if _audio_orphans:
                for _sid, _segs in _audio_orphans.items():
                    _pipeline_log(
                        "DEEP_SLEEP",
                        f"WARN: stranded {len(_segs)} deferred audio chunks from session {_sid}",
                    )
                self._session_deferred_audio_segments.clear()
            # Third session bucket: chain-duration seconds accumulated
            # alongside the raw-text bucket. Clear it symmetrically so a
            # later session id reuse can never inherit stale seconds into
            # its history record.
            _seconds_orphans = {
                sid: secs
                for sid, secs in self._session_soft_seg_seconds.items()
                if secs
            }
            if _seconds_orphans:
                for _sid, _secs in _seconds_orphans.items():
                    _pipeline_log(
                        "DEEP_SLEEP",
                        f"WARN: stranded {_secs:.2f}s soft-split audio seconds from session {_sid}",
                    )
                self._session_soft_seg_seconds.clear()
            # Audio-time metadata has no recoverable content and must follow
            # the same lifecycle as the session buckets it describes.
            getattr(self, "_session_voice_started_at", {}).clear()
            getattr(self, "_session_voice_windows", {}).clear()

        # Now safe to drain any stragglers + unload
        with self._asr_lock:
            while not self._asr_queue.empty():
                try:
                    self._asr_queue.get_nowait()
                    self._asr_queue.task_done()
                except queue.Empty:
                    break

            # Unload ASR model (frees VRAM)
            if self.asr_engine and hasattr(self.asr_engine, "unload"):
                try:
                    self.asr_engine.unload()
                    print("[DEEP_SLEEP] ASR engine unloaded (VRAM freed)")
                except Exception as e:
                    print(f"[DEEP_SLEEP] Engine unload failed: {e}")
        self._unload_gpu_fallback_engine("deep_sleep")

        # Notify UI
        bridge = self._bridge
        print(f"[DEEP_SLEEP] Notifying UI... bridge={bridge is not None}")
        if bridge:
            bridge.emit_state("DEEP_SLEEPING")
            bridge.emit_setting_changed("deep_sleeping", True)
        print(
            "[DEEP_SLEEP] Deep sleep active — GPU idle, "
            f"model={getattr(self.asr_engine, '_model', None) is None}"
        )

    def _exit_deep_sleep(self) -> None:
        """Exit deep sleep: reload model in background thread."""
        with self._deep_sleep_lock:
            with self._lock:
                if self._sleep_mode != SleepMode.DEEP:
                    print("[DEEP_SLEEP] Not in deep sleep, ignoring wake request")
                    return
            if self._reload_thread and self._reload_thread.is_alive():
                print("[DEEP_SLEEP] Already reloading, ignoring duplicate request")
                return

            print("[DEEP_SLEEP] Waking up — reloading engine...")

            # Notify UI: loading state
            bridge = self._bridge
            if bridge:
                bridge.emit_state("LOADING")

            self._reload_thread = threading.Thread(
                target=self._reload_engine, daemon=True
            )
            self._reload_thread.start()

    def _reload_engine(self) -> None:
        """Reload ASR engine from deep sleep (runs on background thread)."""
        try:
            import numpy as _np
            import time as _time
            import traceback

            reload_start = _time.time()

            # Step 1: Reload model
            print("[DEEP_SLEEP] Step 1/4: Loading ASR engine...")
            self.asr_engine.load()
            print(
                f"[DEEP_SLEEP] Step 1/4: OK — model={self.asr_engine._model is not None}"
            )

            # Step 2: GPU warmup
            print("[DEEP_SLEEP] Step 2/4: GPU warmup...")
            with self._asr_lock:
                silence = _np.zeros(16000, dtype=_np.float32)
                _cm = getattr(self, "_capture_mode", "standard")
                _ = self.asr_engine.transcribe(silence, capture_mode=_cm)
                noise = _np.random.randn(16000).astype(_np.float32) * 0.01
                _ = self.asr_engine.transcribe(noise, capture_mode=_cm)
                if hasattr(self.asr_engine, "trim_runtime_cache"):
                    self.asr_engine.trim_runtime_cache("deep_sleep_warmup")
            print("[DEEP_SLEEP] Step 2/4: OK — warmup complete")

            reload_ms = (_time.time() - reload_start) * 1000
            print(f"[DEEP_SLEEP] Step 3/4: Engine ready ({reload_ms:.0f}ms)")

            # Step 3: Restore awake state
            with self._lock:
                self._sleep_mode = SleepMode.AWAKE
            print("[DEEP_SLEEP] Step 3/4: OK — sleep_mode=AWAKE")

            # Step 4: Ensure app is fully enabled + notify UI
            with self._lock:
                self._is_disabled = False
            bridge = self._bridge
            print(
                f"[DEEP_SLEEP] Step 4/4: Emitting IDLE... bridge={bridge is not None}"
            )
            if bridge:
                bridge.emit_state("IDLE")
                bridge.emit_setting_changed("deep_sleeping", False)
                bridge.emit_setting_changed("enabled", True)
            print("[DEEP_SLEEP] Step 4/4: OK — wake complete, app enabled")

            # PERF-1: cold polish calls concentrate on "the first sentence
            # after hours idle" — deep-sleep wake is exactly that moment.
            # Prewarm runs on its own thread and never blocks the first ASR.
            self._schedule_polish_prewarm(delay_s=1.0, reason="deep_sleep_wake")

            # Auto-start recording: user woke the engine = they want to use voice
            # F11 is a toggle (ON/OFF), not push-to-talk. Wake = resume listening.
            # hold_to_talk needs the dedicated wake action: a bare "toggle"
            # would start recording behind the trigger state machine's back,
            # leaving it in idle so no later press could ever stop the session.
            try:
                if getattr(self, "_trigger_mode", "toggle") == "hold_to_talk":
                    self._hotkey_action_queue.put_nowait("wake_start_locked")
                else:
                    self._hotkey_action_queue.put_nowait("toggle")
                print("[DEEP_SLEEP] Auto-starting recording (F11 ON)")
            except queue.Full:
                pass

        except Exception as e:
            import traceback

            print(f"[DEEP_SLEEP] Engine reload FAILED: {e}")
            traceback.print_exc()
            # Stay in deep sleep on failure
            with self._lock:
                self._sleep_mode = SleepMode.DEEP
            bridge = self._bridge
            if bridge:
                bridge.emit_state("DEEP_SLEEPING")
                bridge.emit_setting_changed("deep_sleeping", True)
                bridge.emit_error(f"引擎重载失败: {e}")

    def set_bridge(self, bridge) -> None:
        """
        Set the UI bridge for Qt frontend integration.

        The bridge should have these methods:
        - emit_state(state: str)  # "IDLE", "RECORDING", "TRANSCRIBING"
        - emit_text(text: str, is_final: bool)
        - emit_level(level: float)  # 0.0 - 1.0
        - emit_error(message: str)
        - emit_insert_complete()
        - emit_api_status(status_json: str)
        """
        self._bridge = bridge
        # Also update wakeword executor's bridge reference
        # (it was initialized with None before set_bridge was called)
        if self.wakeword_executor:
            self.wakeword_executor.bridge = bridge
            print(f"[BRIDGE] Updated wakeword executor bridge: {bridge is not None}")
        self._emit_api_status()
        self._emit_asr_status()

    def _emit_state(self, state: str) -> None:
        """Emit state change to UI bridge if available."""
        _pipeline_log("STATE", f"emit_state('{state}') internal={self.state.name}")
        if self._bridge:
            self._bridge.emit_state(state)

    def _emit_text(self, text: str, is_final: bool) -> None:
        """Emit text update to UI bridge if available."""
        if self._bridge:
            self._bridge.emit_text(text, is_final)

    def _wakeword_result_is_fast_safe(self, wakeword_result) -> bool:
        """Return True only for short, side-effect-bounded wakeword commands.

        Interim ASR is useful for latency, but it is not final text.  Therefore
        the fast path must stay deliberately narrow:
        - no capture-following commands (reminders, notes, ask-AI, replies);
        - no keyboard shortcuts / send / screenshot / shell/custom launch;
        - command text must be an exact configured trigger, or a detector-
          accepted open-path fallback like "打开网页".
        """
        if not wakeword_result or not self.wakeword_detector:
            return False
        try:
            cmd_id, action, _value, _response, following_text, command_text = (
                wakeword_result
            )
        except Exception:
            return False

        if action not in self._FAST_WAKEWORD_ACTIONS or following_text:
            return False
        if str(cmd_id).startswith(("keyboard:", "custom_instruction:")):
            return False

        normalizer = getattr(self.wakeword_detector, "_normalize_for_match", None)
        if not callable(normalizer):
            return False
        command_norm = normalizer(str(command_text or ""))
        if not command_norm:
            return False

        cmd_config = None
        commands = getattr(self.wakeword_detector, "commands", {}) or {}
        if isinstance(commands, dict):
            candidate = commands.get(cmd_id)
            if isinstance(candidate, dict):
                cmd_config = candidate

        if cmd_config:
            for trigger in cmd_config.get("triggers", []) or []:
                trigger_norm = normalizer(str(trigger))
                if trigger_norm and command_norm == trigger_norm:
                    return True

        # Natural open-path fallback accepts phrases such as "打开网页" that may
        # not be listed as exact triggers in older configs.  Re-check the same
        # detector fallback instead of inventing a broader rule here.
        fallback = getattr(self.wakeword_detector, "_find_open_path_fallback", None)
        if callable(fallback):
            try:
                fallback_match = fallback(command_norm)
            except Exception:
                fallback_match = None
            if fallback_match and fallback_match[0] == cmd_id:
                return True

        return False

    def _try_fast_wakeword_command(self, text: str, source: str) -> bool:
        """Execute a safe wakeword command from interim ASR once per utterance."""
        if not text or not self.wakeword_detector or not self.wakeword_executor:
            return False
        session_id = getattr(self, "_session_count", 0)
        already_handled, _cmd_id, _success = self._fast_wakeword_already_handled(
            session_id
        )
        if already_handled:
            return True

        wakeword_result = self.wakeword_detector.detect(text)
        if not self._wakeword_result_is_fast_safe(wakeword_result):
            return False

        cmd_id, action, value, response, following_text, command_text = wakeword_result
        with self._fast_wakeword_lock:
            if self._fast_wakeword_session == session_id:
                return True
            # Mark before execution so a concurrent final/interim callback cannot
            # run the same command twice.  A failure is still session-scoped to
            # avoid duplicate "not executed" notices from interim + final.
            self._fast_wakeword_session = session_id
            self._fast_wakeword_cmd_id = str(cmd_id)
            self._fast_wakeword_success = False
            self._fast_wakeword_started_at = time.monotonic()

        try:
            self.wakeword_executor._pending_command_text = command_text
            success = self.wakeword_executor.execute(
                cmd_id, action, value, response, following_text
            )
        except Exception as exc:
            success = False
            logger.warning(f"Fast wakeword command failed: {exc}", exc_info=True)

        with self._fast_wakeword_lock:
            self._fast_wakeword_success = bool(success)

        status = "OK" if success else "FAIL"
        _pipeline_log(
            "WAKEWORD",
            f"Fast {source} command {status}: {cmd_id} text='{text[:60]}'",
        )
        print(f"[WAKEWORD] fast-{source} {status}: {cmd_id} (interim: '{text}')")
        if self._bridge and hasattr(self._bridge, "emit_command"):
            self._bridge.emit_command(f"小助手:{cmd_id}", bool(success))
        return True

    def _fast_wakeword_already_handled(self, session_id: int) -> tuple[bool, str, bool]:
        """Return whether current utterance already executed a fast wakeword command."""
        expired_cmd = ""
        expired_age = 0.0
        with self._fast_wakeword_lock:
            if self._fast_wakeword_session != session_id:
                return (False, "", False)
            started_at = float(getattr(self, "_fast_wakeword_started_at", 0.0) or 0.0)
            if started_at > 0:
                age = time.monotonic() - started_at
                if age > self._FAST_WAKEWORD_SUPPRESS_TTL_S:
                    expired_cmd = self._fast_wakeword_cmd_id
                    expired_age = age
                    self._fast_wakeword_session = None
                    self._fast_wakeword_cmd_id = ""
                    self._fast_wakeword_success = False
                    self._fast_wakeword_started_at = 0.0
                    handled = (False, "", False)
                else:
                    handled = (
                        True,
                        self._fast_wakeword_cmd_id,
                        self._fast_wakeword_success,
                    )
            else:
                handled = (
                    True,
                    self._fast_wakeword_cmd_id,
                    self._fast_wakeword_success,
                )
        if expired_cmd:
            _pipeline_log(
                "WAKEWORD",
                f"Fast command state expired after {expired_age:.1f}s: {expired_cmd}",
            )
        return handled

    def _clear_fast_wakeword_state(self, reason: str = "") -> None:
        """Clear the one-utterance fast wakeword suppression latch."""
        with self._fast_wakeword_lock:
            had_state = self._fast_wakeword_session is not None
            old_cmd = self._fast_wakeword_cmd_id
            self._fast_wakeword_session = None
            self._fast_wakeword_cmd_id = ""
            self._fast_wakeword_success = False
            self._fast_wakeword_started_at = 0.0
        if had_state and reason:
            _pipeline_log(
                "WAKEWORD", f"Fast command state cleared ({reason}): {old_cmd}"
            )

    def _fast_wakeword_final_is_still_command(
        self, text: str, fast_cmd_id: str
    ) -> bool:
        """Return True if final ASR text is still the same command-only wakeword."""
        if not text:
            return True
        if not fast_cmd_id or not self.wakeword_detector:
            return False
        try:
            wakeword_result = self.wakeword_detector.detect(text)
        except Exception:
            return False
        if not wakeword_result:
            return False
        try:
            cmd_id = str(wakeword_result[0])
        except Exception:
            return False
        return cmd_id == str(fast_cmd_id) and self._wakeword_result_is_fast_safe(
            wakeword_result
        )

    def _emit_level(self, level: float) -> None:
        """Emit audio level to UI bridge if available."""
        if self._bridge:
            self._bridge.emit_level(level)

    def _emit_error(self, message: str) -> None:
        """Emit error to UI bridge if available."""
        if self._bridge:
            self._bridge.emit_error(message)

    def _emit_insert_complete(self) -> None:
        """Emit insert complete notification to UI bridge."""
        _pipeline_log(
            "STATE", f"emit_insert_complete (bridge={'YES' if self._bridge else 'NO'})"
        )
        if self._bridge:
            self._bridge.emit_insert_complete()
        self._schedule_auto_deep_sleep("insert_complete")

    def _emit_voice_activity(self, is_speaking: bool) -> None:
        """Emit voice activity (VAD) to UI bridge."""
        if self._bridge:
            self._bridge.emit_voice_activity(is_speaking)

    def _emit_action(self, action) -> None:
        """Emit UI action to bridge (v1.1 action-driven architecture)."""
        if self._bridge:
            self._bridge.emit_action(action)

    def _emit_api_status(self, status: dict | None = None) -> None:
        """Emit current Polish API failover status to the Qt settings UI."""
        if not self._bridge or not hasattr(self._bridge, "emit_api_status"):
            return
        try:
            if status is None:
                status = self.get_api_status()
            else:
                status = self._decorate_api_status(status)
            self._bridge.emit_api_status(json.dumps(status, ensure_ascii=False))
        except Exception as exc:
            _pipeline_log("POST", f"emit_api_status failed: {exc}")

    def _emit_asr_status(self, status: dict | None = None) -> None:
        """Emit current ASR runtime status to the Qt floating UI."""
        if not self._bridge or not hasattr(self._bridge, "emit_asr_status"):
            return
        try:
            payload = (
                status if isinstance(status, dict) else self.get_asr_runtime_status()
            )
            self._bridge.emit_asr_status(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            _pipeline_log("ASR", f"emit_asr_status failed: {exc}")

    def _is_hallucination(self, text: str) -> bool:
        """
        Detect ASR hallucinations (random outputs when no real speech).

        Common hallucination patterns:
        - IP addresses (192.168.x.x)
        - Timestamps (2022-09-15 16:15:22)
        - Repeated characters/patterns
        - Random number sequences
        - Repeated sentences/phrases (3+ times, not 2 - see _deduplicate_sentences)
        """
        import re

        # Pattern 1: IP address like patterns
        if re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}", text):
            return True

        # Pattern 2: Timestamp patterns (YYYY-MM-DD or HH:MM:SS)
        if re.search(r"\d{4}-\d{2}-\d{2}", text) or re.search(
            r"\d{2}:\d{2}:\d{2}", text
        ):
            return True

        # Pattern 3: Too many numbers (more than 50% digits)
        digit_ratio = sum(c.isdigit() for c in text) / max(len(text), 1)
        if digit_ratio > 0.5 and len(text) > 5:
            return True

        # Pattern 4: Repeated non-CJK patterns (same char 4+ times)
        # CJK repetition like "落落落落" is an ASR stutter bug, not hallucination
        # — handled by _deduplicate_sentences instead
        non_cjk_repeat = re.search(r"(.)\1{3,}", text)
        if non_cjk_repeat:
            char = non_cjk_repeat.group(1)
            if not re.match(r"[\u4e00-\u9fff\u3400-\u4dbf]", char):
                return True

        # Pattern 5: Repeated sentences (same phrase 3+ times = hallucination)
        # Note: 2x repetition is handled by _deduplicate_sentences (ASR bug, not hallucination)
        sentences = re.split(r"[。！？，,\.!?]", text)
        sentences = [s.strip() for s in sentences if len(s.strip()) > 3]
        if len(sentences) >= 3:
            from collections import Counter

            counts = Counter(sentences)
            for phrase, count in counts.items():
                if count >= 3 and len(phrase) > 5:
                    return True

        # Pattern 6 removed: Whisper-era hallucination phrases ("字幕", "订阅", etc.)
        # were only relevant to Whisper's YouTube training data residue.
        # Qwen3-ASR and FunASR do not produce these patterns.
        # Patterns 1-5 already cover their actual hallucination modes.

        return False

    def _extract_ocr_keywords(self, ocr_text: str) -> str:
        """Extract meaningful keywords from raw OCR text for ASR context.

        Raw OCR contains UI noise (window titles, line numbers, buttons).
        Qwen3's context parameter needs a clean word list, not paragraphs.
        This extracts unique CJK terms and English words, filters noise.
        """
        import re

        cjk_range = r"\u4e00-\u9fff\u3400-\u4dbf"

        # Extract CJK sequences (2-10 chars; >10 is a sentence fragment, not a keyword)
        cjk_words = [
            w for w in re.findall(f"[{cjk_range}]{{2,}}", ocr_text) if len(w) <= 10
        ]

        # Extract English/mixed words (3+ chars, skip pure numbers)
        eng_words = re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}", ocr_text)

        # Common UI noise to filter out
        noise = {
            "到",
            "的",
            "在",
            "了",
            "是",
            "和",
            "有",
            "不",
            "这",
            "那",
            "为",
            "与",
            "或",
            "按",
            "从",
            "被",
            "把",
            "让",
            "向",
            "将",
            "可以",
            "进行",
            "使用",
            "设置",
            "选择",
            "显示",
            "输入",
            "打开",
            "关闭",
            "确认",
            "取消",
            "保存",
            "删除",
            "编辑",
            "文件",
            "窗口",
            "菜单",
            "按钮",
            "选项",
            "工具",
            "帮助",
            # Browser UI noise (from UI Automation)
            "书签",
            "标签页",
            "分组",
            "返回",
            "前进",
            "查找",
            "刷新",
            "下载",
            "扩展",
            "收藏",
            "收藏夹",
            "历史",
            "新建",
            "地址栏",
            "已停用",
            "更多",
            "最小化",
            "最大化",
            "the",
            "and",
            "for",
            "this",
            "that",
            "with",
            "from",
            "are",
            "not",
            "was",
            "but",
            "has",
            "have",
            "will",
            "None",
            "True",
            "False",
            "null",
            "undefined",
        }

        # Substring noise check — "未命名书签" contains "书签" → filtered
        def _is_noise(word: str) -> bool:
            wl = word.lower()
            if wl in noise:
                return True
            # For CJK: check if word contains any noise substring (2+ chars)
            for n in noise:
                if len(n) >= 2 and n in wl:
                    return True
            return False

        seen = set()
        keywords = []

        for w in cjk_words:
            if w not in seen and not _is_noise(w) and len(w) >= 2:
                seen.add(w)
                keywords.append(w)

        for w in eng_words:
            w_lower = w.lower()
            if w_lower not in seen and w_lower not in noise and len(w) >= 3:
                seen.add(w_lower)
                keywords.append(w)

        # Cap at 20 keywords to keep context focused and each word impactful
        keywords = keywords[:20]

        return " ".join(keywords)

    def _filter_screen_keywords_for_asr(self, keywords: str, base_context: str) -> str:
        """Keep only low-risk screen keywords for fast-mode ASR context.

        Qwen3-ASR is sensitive to context biasing. We therefore avoid injecting
        arbitrary English/UI tokens from OCR (the historical "FBX" -> full
        window-title regression). New screen-only CJK names/terms are allowed;
        English tokens are allowed only when they already exist in the static
        ASR context, where the screen is merely reinforcing a configured term.
        """
        import re

        if not keywords:
            return ""

        base_lower = (base_context or "").lower()
        cjk_range = r"\u4e00-\u9fff\u3400-\u4dbf"
        safe_keywords = []
        seen = set()

        for token in keywords.split():
            token_key = token.lower()
            if token_key in seen:
                continue

            has_cjk = re.search(f"[{cjk_range}]", token) is not None
            reinforces_static_context = bool(base_lower and token_key in base_lower)

            if has_cjk or reinforces_static_context:
                safe_keywords.append(token)
                seen.add(token_key)

            if len(safe_keywords) >= 8:
                break

        return " ".join(safe_keywords)

    def _polish_change_supported_by_evidence(
        self, raw: str, polished: str, evidence_text: str
    ) -> bool:
        """Decide whether a Polish-introduced change is backed by evidence.

        Used by the recent_context buffer write site to prevent Polish's
        ungrounded rewrites from poisoning the ASR biasing pool. A change is
        considered supported when:
          - Polish did not introduce any new CJK term (only punctuation /
            whitespace / casing changes), or
          - every new CJK 2-8 char term Polish introduces appears verbatim in
            the evidence pool (current screen text + user hotwords).

        We deliberately ignore Latin-only differences here: Latin
        capitalization fixes (DeepSeek vs deepseek) are a known-correct Polish
        responsibility, and gating them on screen evidence would over-reject.
        """
        if raw == polished or not polished:
            return True
        import re

        cjk_char_re = re.compile(r"[一-鿿㐀-䶿]")
        raw_cjk = "".join(cjk_char_re.findall(raw))
        polished_cjk = "".join(cjk_char_re.findall(polished))

        def _is_subsequence(needle: str, haystack: str) -> bool:
            if not needle:
                return True
            pos = 0
            for ch in haystack:
                if ch == needle[pos]:
                    pos += 1
                    if pos == len(needle):
                        return True
            return False

        # Deletion-only / de-dup Polish corrections are safe to write back:
        # examples include "去调调研一下" -> "去调研一下" and
        # "很多就是出来的地方" -> "很多出来的地方". These introduce no new
        # CJK characters; rejecting them feeds known-bad raw ASR back into Qwen3.
        if raw_cjk and polished_cjk and _is_subsequence(polished_cjk, raw_cjk):
            return True

        import difflib

        introduced_terms = set()
        matcher = difflib.SequenceMatcher(None, raw_cjk, polished_cjk, autojunk=False)
        for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
            if tag not in ("insert", "replace"):
                continue
            segment = polished_cjk[j1:j2]
            if len(segment) >= 2:
                introduced_terms.add(segment)

        if not introduced_terms:
            return True
        # Treat raw itself as part of the evidence pool: a polished CJK
        # fragment that already occurs verbatim inside raw is just a shorter
        # slice of an existing term (e.g. raw "我用迪普seek" → polished
        # "我用deepseek" leaves "我用" as the only "new" CJK fragment, but it
        # was already in raw — Polish merely transliterated 迪普seek→deepseek
        # and we must not flag this as ungrounded).
        pool = (evidence_text or "") + "\n" + raw
        return all(term in pool for term in introduced_terms)

    def _polish_content_addition_rejection(self, raw: str, polished: str) -> str:
        """Return a rejection reason when Polish invents new CJK content.

        This is deliberately narrower than the recent-context evidence guard:
        normal ASR correction may replace same-length homophones, and filler
        cleanup may delete words. The dangerous live failure is content
        completion, e.g. "好像又有。" -> "好像又有问题。".
        """
        if raw == polished or not polished:
            return ""

        import difflib
        import re

        cjk_re = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

        def _cjk_len(value: str) -> int:
            return len(cjk_re.findall(value or ""))

        matcher = difflib.SequenceMatcher(a=raw or "", b=polished or "", autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag not in ("insert", "replace"):
                continue

            added = polished[j1:j2]
            removed = raw[i1:i2]
            added_cjk = _cjk_len(added)
            removed_cjk = _cjk_len(removed)

            if added_cjk >= 2 and added_cjk - removed_cjk >= 2:
                return (
                    "unsupported content addition "
                    f"({removed!r} -> {added!r}, +{added_cjk - removed_cjk} CJK chars)"
                )

        return ""

    def _init_auto_hotword(self, polish_block: dict) -> None:
        """Build the screen-OCR-driven SessionHotwordTracker + LLM reviewer.

        When the user does not configure a separate API for the reviewer,
        we inherit api_url/api_key/model from the main Polish endpoint.
        Tracking thresholds, the daily review timer and the reviewer prompt
        live in SessionHotwordTracker / the daily-timer thread / the reviewer
        module respectively.
        """
        from .core.hotword.session_tracker import SessionHotwordTracker
        from .core.hotword.auto_hotword_reviewer import (
            AutoHotwordReviewer,
            ReviewerConfig,
        )

        cfg = self._auto_hotword_cfg or {}

        # T6: build the optional OCR sampler regardless of tracker.enabled,
        # so a user who disabled auto-learning can still turn on the sampler
        # for diagnostic purposes (and vice-versa). Sampler is opt-in and
        # capped per day; see ocr_sampler.OcrSampler for the policy.
        from .core.hotword.ocr_sampler import OcrSampler
        from .core.utils.paths import get_base_path as _get_base

        sampling_cfg = (
            (cfg.get("sample_logging") or {})
            if isinstance(cfg.get("sample_logging"), dict)
            else {}
        )
        try:
            self._ocr_sampler = OcrSampler(
                sample_dir=_get_base() / "data" / "ocr_samples",
                max_per_day=int(sampling_cfg.get("max_per_day", 10) or 10),
                enabled=bool(sampling_cfg.get("enabled", False)),
            )
            if self._ocr_sampler.enabled:
                print(
                    f"[AUTO-HOTWORD] OCR sampler enabled "
                    f"(max_per_day={sampling_cfg.get('max_per_day', 10)}, "
                    f"dir=data/ocr_samples)"
                )
        except Exception as exc:
            logger.warning(f"OCR sampler init failed: {exc}")
            self._ocr_sampler = None

        if not _auto_hotword_opted_in(cfg):
            self._auto_hotword_tracker = None
            self._auto_hotword_reviewer = None
            return

        # Persist tracker state under data/auto_hotwords.json so we keep
        # learned approvals + the rejection blacklist across restarts.
        from .core.utils.paths import get_base_path

        data_path = get_base_path() / "data" / "auto_hotwords.json"

        user_words = set(getattr(self.hotword_manager.config, "prompt_words", []) or [])
        self._auto_hotword_tracker = SessionHotwordTracker(
            data_path=data_path, user_hotwords=user_words
        )

        api_url = (cfg.get("api_url") or polish_block.get("api_url", "")).strip()
        api_key = (cfg.get("api_key") or polish_block.get("api_key", "")).strip()
        model = (cfg.get("model") or polish_block.get("model", "")).strip()
        reviewer_defaults = ReviewerConfig()
        # Reviewer batches are much larger than one polish call (up to 50
        # candidates by default).  Inheriting the polish timeout verbatim
        # caused the automatic review to fail silently when polish.timeout=20s
        # but a 50-term review needed ~22s+.  Only use an explicit
        # auto_hotword.timeout override; otherwise keep at least the reviewer
        # default.
        timeout = int(
            cfg.get("timeout")
            or max(
                int(polish_block.get("timeout", reviewer_defaults.timeout) or 0),
                reviewer_defaults.timeout,
            )
        )

        self._auto_hotword_reviewer = AutoHotwordReviewer(
            ReviewerConfig(
                enabled=True,
                api_url=api_url or "https://api.deepseek.com",
                api_key=api_key,
                model=model or "deepseek-v4-flash",
                timeout=timeout,
                max_terms_per_call=int(cfg.get("max_terms_per_review", 50) or 50),
                # T2: distance-based trigger. Old daily_review_hour kept being
                # parsed silently for backwards compat in case existing user
                # configs still carry it, but the value is no longer consulted.
                review_interval_hours=int(
                    cfg.get(
                        "review_interval_hours",
                        reviewer_defaults.review_interval_hours,
                    )
                    or reviewer_defaults.review_interval_hours
                ),
                min_batch_size=int(
                    cfg.get("min_batch_size", reviewer_defaults.min_batch_size)
                    or reviewer_defaults.min_batch_size
                ),
            )
        )
        self._auto_hotword_tracker.MIN_COUNT_FOR_REVIEW = int(
            cfg.get("min_count_for_review", 3) or 3
        )

        stats = self._auto_hotword_tracker.stats()
        reviewer_ready = self._auto_hotword_reviewer.is_available()
        print(
            f"[AUTO-HOTWORD] Tracker loaded: "
            f"approved={stats['approved']} pending={stats['pending']} "
            f"rejected={stats['rejected']}; "
            f"reviewer={'ready' if reviewer_ready else 'no-api-key'}"
        )
        _pipeline_log(
            "AUTO-HOTWORD",
            "Tracker loaded: "
            f"approved={stats['approved']} pending={stats['pending']} "
            f"rejected={stats['rejected']} reviewer_ready={reviewer_ready} "
            f"interval={self._auto_hotword_reviewer.config.review_interval_hours}h "
            f"min_batch={self._auto_hotword_reviewer.config.min_batch_size}",
        )

        # Optional: review pending terms accumulated from previous sessions.
        if cfg.get("review_on_startup", False):
            threading.Thread(
                target=self._run_auto_hotword_review,
                kwargs={"reason": "startup"},
                daemon=True,
                name="AutoHotwordStartupReview",
            ).start()

        # Distance-based review timer (default: every 6h when enough pending
        # candidates exist). Older configs may still contain daily_review_hour,
        # but the scheduler no longer reads wall-clock time.
        self._auto_hotword_review_stop.clear()
        self._auto_hotword_review_thread = threading.Thread(
            target=self._auto_hotword_daily_loop,
            daemon=True,
            name="AutoHotwordDailyTimer",
        )
        self._auto_hotword_review_thread.start()

    def _run_auto_hotword_review(self, reason: str = "manual") -> None:
        """Single review pass: pull pending → call LLM → apply verdicts → save.

        Bypass policy for T4 min-batch floor:
          - reason="manual"  : user pressed 立即审查 → bypass (their explicit ask)
          - reason="startup" : Aria just launched → bypass (let new state surface fast)
          - reason="auto"    : background scheduler → enforce min_batch_size
        """
        if self._auto_hotword_tracker is None or self._auto_hotword_reviewer is None:
            _pipeline_log(
                "AUTO-HOTWORD",
                f"Review skipped ({reason}): tracker/reviewer not initialized",
            )
            return
        if not self._auto_hotword_reviewer.is_available():
            cfg = self._auto_hotword_reviewer.config
            _pipeline_log(
                "AUTO-HOTWORD",
                f"Review skipped ({reason}): reviewer unavailable "
                f"(api_url_set={bool(cfg.api_url)}, key_set={bool(cfg.api_key)}, "
                f"model={cfg.model or '<empty>'})",
            )
            return
        # Single-flight: never run two reviews concurrently.
        if not self._auto_hotword_review_lock.acquire(blocking=False):
            _pipeline_log(
                "AUTO-HOTWORD",
                f"Review skipped ({reason}): another review is already running",
            )
            return
        try:
            tracker = self._auto_hotword_tracker
            reviewer = self._auto_hotword_reviewer
            pending = tracker.get_pending_for_review(
                max_terms=reviewer.config.max_terms_per_call
            )
            review_approved_enabled = bool(
                (self._auto_hotword_cfg or {}).get("review_approved_enabled", True)
            )
            max_approved_recheck = int(
                (self._auto_hotword_cfg or {}).get(
                    "max_approved_recheck_per_review",
                    reviewer.config.max_terms_per_call,
                )
                or 0
            )
            approved_recheck = (
                tracker.get_approved_for_recheck(max_terms=max_approved_recheck)
                if review_approved_enabled and max_approved_recheck > 0
                else []
            )
            total_candidates = len(pending) + len(approved_recheck)
            if not total_candidates:
                _pipeline_log(
                    "AUTO-HOTWORD",
                    f"Review skipped ({reason}): no eligible pending/approved terms",
                )
                return

            # T4: min-batch floor (auto trigger only).
            min_batch = int(getattr(reviewer.config, "min_batch_size", 8) or 0)
            if reason == "auto" and min_batch > 0 and total_candidates < min_batch:
                print(
                    f"[AUTO-HOTWORD] Review skipped (auto): candidates={total_candidates} "
                    f"< min_batch_size={min_batch}"
                )
                _pipeline_log(
                    "AUTO-HOTWORD",
                    f"Review skipped (auto): candidates={total_candidates} "
                    f"< min_batch_size={min_batch}",
                )
                return

            print(
                f"[AUTO-HOTWORD] Review starting (reason={reason}, "
                f"pending={len(pending)}, approved_recheck={len(approved_recheck)})"
            )
            _pipeline_log(
                "AUTO-HOTWORD",
                f"Review starting (reason={reason}, pending={len(pending)}, "
                f"approved_recheck={len(approved_recheck)})",
            )
            outcome = reviewer.review(pending) if pending else None
            if outcome is not None and not outcome.success:
                print(f"[AUTO-HOTWORD] Review failed: {outcome.error}")
                _pipeline_log(
                    "AUTO-HOTWORD",
                    f"Review failed ({reason}): {outcome.error}",
                )
                return
            summary = (
                tracker.apply_review_results(outcome.verdicts, outcome.reasons)
                if outcome is not None
                else {"approved": 0, "rejected": 0, "unsure": 0}
            )

            recheck_outcome = (
                reviewer.review_approved(approved_recheck) if approved_recheck else None
            )
            recheck_failed = recheck_outcome is not None and not recheck_outcome.success
            if recheck_failed:
                print(
                    f"[AUTO-HOTWORD] Approved recheck failed: {recheck_outcome.error}"
                )
                _pipeline_log(
                    "AUTO-HOTWORD",
                    f"Approved recheck failed ({reason}): {recheck_outcome.error}",
                )
                recheck_summary = {"kept": 0, "demoted": 0, "unsure": 0}
            else:
                recheck_summary = (
                    tracker.apply_approved_recheck_results(
                        recheck_outcome.verdicts, recheck_outcome.reasons
                    )
                    if recheck_outcome is not None
                    else {"kept": 0, "demoted": 0, "unsure": 0}
                )

            # T2: stamp last_review_at so the distance-based scheduler knows.
            # BUT only if at least one of the two phases actually applied a
            # verdict — otherwise marking "complete" makes the scheduler wait
            # another 6h before retrying when the LLM was just transiently
            # broken. The pending phase short-circuits at L1343 on its own
            # failure (early return) so when we reach this line "pending
            # didn't apply" means simply pending was empty (outcome is None).
            # A round counts as zero-progress when pending was empty AND
            # recheck either never ran or failed — we don't want to penalize
            # the next 6h window for a broken API response.
            nothing_applied = outcome is None and (
                recheck_outcome is None or recheck_failed
            )
            if not nothing_applied:
                tracker.mark_review_completed()
            else:
                _pipeline_log(
                    "AUTO-HOTWORD",
                    f"Review applied 0 verdicts ({reason}); not stamping "
                    f"last_review_at so scheduler can retry sooner",
                )
            # T7: opportunistic sweep — review is the natural moment to age out
            # stale tracker entries because we just updated approvals + we're
            # about to save anyway, so there's no extra disk hit.
            sweep = tracker.housekeeping()
            tracker.save()
            print(
                f"[AUTO-HOTWORD] Review done in "
                f"{(outcome.api_time_ms if outcome else 0):.0f}ms"
                f"+{(recheck_outcome.api_time_ms if recheck_outcome else 0):.0f}ms: "
                f"approved={summary['approved']} rejected={summary['rejected']} "
                f"unsure={summary['unsure']}; "
                f"recheck kept={recheck_summary['kept']} "
                f"demoted={recheck_summary['demoted']} "
                f"unsure={recheck_summary['unsure']}; "
                f"swept approved={sweep['dropped_approved']} "
                f"pending_noise={sweep['dropped_pending']}"
            )
            _pipeline_log(
                "AUTO-HOTWORD",
                f"Review done ({reason}) in "
                f"{(outcome.api_time_ms if outcome else 0):.0f}ms"
                f"+{(recheck_outcome.api_time_ms if recheck_outcome else 0):.0f}ms: "
                f"approved={summary['approved']} rejected={summary['rejected']} "
                f"unsure={summary['unsure']} "
                f"recheck_kept={recheck_summary['kept']} "
                f"recheck_demoted={recheck_summary['demoted']} "
                f"recheck_unsure={recheck_summary['unsure']} "
                f"pending_noise={sweep['dropped_pending']}",
            )
            # Push refreshed approvals to the live ASR context immediately so
            # the user doesn't have to wait for the next config reload.
            self._refresh_qwen3_context_after_auto_hotword()
        except Exception as exc:
            _pipeline_log(
                "AUTO-HOTWORD",
                f"Review crashed ({reason}): {type(exc).__name__}: {exc}",
            )
            logger.exception(f"Auto-hotword review crashed ({reason})")
        finally:
            self._auto_hotword_review_lock.release()

    def _auto_hotword_daily_loop(self) -> None:
        """Background loop covering three responsibilities at one 30s cadence:

        1. **T1 periodic save** — call `tracker.save_if_dirty()` every tick so
           a crash/Alt+F4/power-loss never costs more than ~30s of in-memory
           pending state. tracker.save() is itself a tmp+replace atomic write.
        2. **T2 distance-based review trigger** — fire `_run_auto_hotword_review`
           when (now - tracker.last_review_at) ≥ review_interval_hours AND
           pending ≥ min_batch_size. Replaces the broken 04:00 wall-clock
           trigger that almost never fired in practice (sleeping machines).
        3. **Manual sentinel** — consume the settings panel's "立即审查"
           flag file as before.
        """
        import datetime as _dt

        from .core.utils.paths import get_base_path

        sentinel = get_base_path() / "data" / "auto_hotword_review_request.flag"
        while not self._auto_hotword_review_stop.is_set():
            tracker = self._auto_hotword_tracker
            reviewer = self._auto_hotword_reviewer

            # 1. Periodic dirty-flush (T1)
            if tracker is not None:
                try:
                    tracker.save_if_dirty()
                except Exception as exc:
                    logger.warning(f"Auto-hotword tracker save failed: {exc}")

            # 2. Distance-based auto trigger (T2)
            if tracker is not None and reviewer is not None:
                try:
                    interval_hours = int(
                        getattr(reviewer.config, "review_interval_hours", 24) or 24
                    )
                    min_batch = int(getattr(reviewer.config, "min_batch_size", 8) or 0)
                    last = tracker.get_last_review_at()
                    now = _dt.datetime.now()
                    interval_due = last is None or (now - last) >= _dt.timedelta(
                        hours=interval_hours
                    )
                    if interval_due and reviewer.is_available():
                        # Cheap pre-check: avoid calling _run_auto_hotword_review
                        # entirely when there isn't even one eligible candidate.
                        # The full min-batch floor is enforced inside that
                        # method (so manual sentinel can still bypass it).
                        pending_peek = tracker.get_pending_for_review(max_terms=1)
                        if pending_peek:
                            stats = tracker.stats()
                            if stats["pending"] >= max(1, min_batch):
                                self._run_auto_hotword_review(reason="auto")
                except Exception as exc:
                    logger.warning(f"Auto-hotword scheduler error: {exc}")

            # 3. Manual sentinel
            if sentinel.exists():
                try:
                    sentinel.unlink()
                except Exception:
                    pass
                _pipeline_log("AUTO-HOTWORD", "Manual review sentinel consumed")
                self._run_auto_hotword_review(reason="manual")

            self._auto_hotword_review_stop.wait(timeout=30)

    def _refresh_qwen3_context_after_auto_hotword(self) -> None:
        """Re-push the ASR context AND the polisher's session_hotwords after a
        review so newly approved hotwords take effect mid-session without
        waiting for hot-reload.

        The polisher.session_hotwords push is what makes L1 mode (auto-hotword
        only) viable: instead of pushing every screen frame's OCR text into
        Polish (cache-killer), we push a slow-changing approved word list once
        per review cycle. The Polish prompt prefix stays stable between
        reviews → DeepSeek prefix-cache hits ramp from ~20% to ~85-95%.
        """
        # 1. Refresh ASR context
        if self.asr_engine and self._asr_engine_type in ("qwen3", "qwen3_sherpa", "qwen3_llamacpp"):
            if hasattr(self.asr_engine, "set_context"):
                try:
                    ctx = self._build_asr_context_for_current_mode(include_screen=False)
                    self.asr_engine.set_context(ctx or "")
                except Exception as exc:
                    logger.warning(f"Failed to refresh ASR context after review: {exc}")

        # 2. Push approved session hotwords into the polisher
        if self.polisher and self._auto_hotword_tracker:
            try:
                active_words = self._auto_hotword_tracker.get_active_hotwords()
                if hasattr(self.polisher, "config"):
                    self.polisher.config.session_hotwords = list(active_words or [])
                    print(
                        f"[AUTO-HOTWORD] Pushed {len(active_words)} approved words "
                        f"to polisher.session_hotwords"
                    )
            except Exception as exc:
                logger.warning(f"Failed to push session hotwords to polisher: {exc}")

    def _build_asr_context_for_current_mode(self, include_screen: bool = True) -> str:
        """Build the Qwen3-ASR context for the current polish mode.

        Mode split:
        - quality/off: static hotword/domain context only. Screen OCR is reserved
          for the Polish layer so ASR does not overfit to noisy UI text.
        - fast: append low-risk currently available screen keywords to ASR
          context, but never wait for OCR. get_text() only returns title +
          fresh cached OCR.
        """
        if not self.hotword_manager:
            return ""

        base_context = self.hotword_manager.to_qwen3_context()
        parts = [base_context] if base_context else []

        try:
            polish_mode = self.hotword_manager.polish_mode
        except Exception:
            polish_mode = ""

        if (
            include_screen
            and polish_mode == "fast"
            and self._screen_ocr_enabled
            and self._screen_ocr
        ):
            try:
                # Non-blocking by design. This is the legacy flattened ASR view
                # (title + same-window fresh OCR cache), not the Polish layer's
                # bounded-wait path.
                screen_text = self._screen_ocr.get_text()
                raw_screen_keywords = self._extract_ocr_keywords(screen_text)
                screen_keywords = self._filter_screen_keywords_for_asr(
                    raw_screen_keywords,
                    base_context,
                )
            except Exception as exc:
                screen_keywords = ""
                _pipeline_log(
                    "ASR",
                    f"Fast-mode screen ASR context skipped: {type(exc).__name__}",
                )

            if screen_keywords:
                parts.append(f"当前屏幕关键词：{screen_keywords}")
                _pipeline_log(
                    "ASR",
                    f"Fast-mode screen keywords added to ASR context: {len(screen_keywords)} chars",
                )

        # Auto-learned session hotwords go to the WEAKEST position (end of
        # context) so user-curated hotwords always outrank them. We use a
        # plain space-joined token list with no labelled prefix so the ASR's
        # token-level biasing treats them as additional vocab hints rather
        # than instructions.
        if self._auto_hotword_tracker is not None:
            try:
                auto_words = self._auto_hotword_tracker.get_active_hotwords()
                if auto_words:
                    parts.append(" ".join(auto_words))
            except Exception as exc:
                _pipeline_log(
                    "ASR",
                    f"Auto-hotword inject skipped: {type(exc).__name__}: {exc}",
                )

        return "\n".join(parts)

    def _build_cpu_asr_context(self) -> str:
        """Bounded hotword context for the CPU 0.6B runtime.

        Hotword biasing is what keeps the small model usable for domain terms
        (field telemetry: without it "Fable" comes out as "F A B L E"), so CPU
        mode must NOT run audio-only.  It still differs from the GPU context:
        - no screen-OCR keywords (extra prompt tokens, weakest signal), and
        - hard char cap so a pathologically large hotword list cannot inflate
          CPU prefill time.
        The cap truncates at a whitespace/newline boundary to avoid feeding the
        model a half hotword, which would bias toward a nonexistent token.
        """
        try:
            context = self._build_asr_context_for_current_mode(include_screen=False)
        except Exception as exc:
            _pipeline_log(
                "ASR",
                f"CPU ASR context build failed, going audio-only: "
                f"{type(exc).__name__}: {exc}",
            )
            return ""

        max_chars = self._CPU_ASR_CONTEXT_MAX_CHARS
        if not context or len(context) <= max_chars:
            return context or ""

        cut = context.rfind(" ", 0, max_chars)
        cut_nl = context.rfind("\n", 0, max_chars)
        cut = max(cut, cut_nl)
        if cut <= 0:
            cut = max_chars
        trimmed = context[:cut].rstrip()
        _pipeline_log(
            "ASR",
            f"CPU ASR context capped: {len(context)} -> {len(trimmed)} chars",
        )
        return trimmed

    def _screen_pinyin_correct(self, text: str, screen_keywords: str) -> tuple:
        """Unified L2.5 Phonetic Matcher — screen-aware homophone correction.

        Design:
        - Pre-compute pinyin array for entire text (one pass, cached)
        - Sliding window scan for screen keywords (CJK 3+ chars only)
        - Toneless pinyin for broader recall (ASR tones unreliable)
        - 3+ char minimum to avoid 2-char false positives (银行/银航)
        - Longest match first to prevent overlap conflicts

        Returns: (corrected_text, num_corrections)
        """
        import re

        if not _PYPINYIN_AVAILABLE:
            return text, 0

        cjk_range = r"\u4e00-\u9fff\u3400-\u4dbf"
        cjk_re = re.compile(f"[{cjk_range}]")

        # Screen keywords: 3-8 chars (min 3 to avoid false positives)
        screen_words = [
            w
            for w in re.findall(f"[{cjk_range}]{{3,}}", screen_keywords)
            if len(w) <= 8
        ]

        if not screen_words:
            return text, 0

        # Pre-compute screen word pinyin, sort longest first
        screen_py = {}
        for w in screen_words:
            py = _get_pinyin_cached(w)
            if py:
                screen_py[w] = py

        if not screen_py:
            return text, 0

        sorted_screen = sorted(screen_py.items(), key=lambda x: len(x[0]), reverse=True)

        # Pre-compute per-character pinyin array for entire text (one pass)
        text_chars = list(text)
        text_pinyin = []
        for c in text_chars:
            if cjk_re.match(c):
                py = _get_pinyin_cached(c)
                text_pinyin.append(py[0] if py else "")
            else:
                text_pinyin.append(None)

        corrections = 0
        replaced = set()

        for screen_word, s_py in sorted_screen:
            n = len(screen_word)
            for i in range(len(text_pinyin) - n + 1):
                if any(j in replaced for j in range(i, i + n)):
                    continue

                window_py = text_pinyin[i : i + n]
                if any(p is None for p in window_py):
                    continue

                candidate = "".join(text_chars[i : i + n])
                if candidate == screen_word:
                    continue

                if tuple(window_py) == s_py:
                    print(
                        f"[SCREEN-FIX] '{candidate}' → '{screen_word}' (pinyin: {list(s_py)})"
                    )
                    _pipeline_log(
                        "POST",
                        f"Screen homophone: '{candidate}' → '{screen_word}'",
                    )
                    for j, ch in enumerate(screen_word):
                        text_chars[i + j] = ch
                        py = _get_pinyin_cached(ch)
                        text_pinyin[i + j] = py[0] if py else ""
                    for j in range(i, i + n):
                        replaced.add(j)
                    corrections += 1

        return "".join(text_chars), corrections

    def _deduplicate_sentences(self, text: str) -> str:
        """
        Fix ASR sentence repetition bug.

        ASR engines sometimes output the same sentence twice:
        "我现在在进行一个新的测试。我现在在进行一个新的测试。"

        This extracts unique sentences while preserving order.
        """
        import re

        # Split by sentence-ending punctuation
        parts = re.split(r"([。！？!?])", text)

        seen = set()
        result = []

        i = 0
        while i < len(parts):
            sentence = parts[i].strip()
            punct = parts[i + 1] if i + 1 < len(parts) else ""

            if sentence and len(sentence) > 3:
                if sentence not in seen:
                    seen.add(sentence)
                    result.append(sentence + punct)
            elif sentence:
                result.append(sentence + punct)

            i += 2 if punct else 1

        return "".join(result)

    def _load_output_config(self) -> OutputConfig:
        """Load output configuration from hotwords.json.

        Supports typewriter mode for game/app compatibility where Ctrl+V doesn't work.
        Also enables permission detection to warn users about elevated windows.
        """
        import json

        config_path = get_config_path("hotwords.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            output_cfg = data.get("output", {})

            # Create config with values from file (or defaults)
            config = OutputConfig(
                typewriter_mode=output_cfg.get("typewriter_mode", False),
                typewriter_delay_ms=output_cfg.get("typewriter_delay_ms", 15),
                word_native_enabled=output_cfg.get("word_native_enabled", True),
                terminal_profile=output_cfg.get("terminal_profile", "safe"),
                terminal_chunking_enabled=output_cfg.get(
                    "terminal_chunking_enabled", False
                ),
                terminal_chunk_chars=output_cfg.get("terminal_chunk_chars", 1000),
                terminal_chunk_delay_ms=output_cfg.get(
                    "terminal_chunk_delay_ms", 120
                ),
                game_chat_profiles=output_cfg.get("game_chat_profiles", {}),
                check_elevation=output_cfg.get("check_elevation", True),
                # Elevation callback will show warning via UI bridge
                elevation_callback=self._on_elevation_warning,
                paste_abort_callback=self._on_paste_abort,
            )

            if config.typewriter_mode:
                logger.info(
                    "[OUTPUT] Typewriter mode enabled (for apps without Ctrl+V support)"
                )

            return config

        except Exception as e:
            logger.warning(f"Failed to load output config: {e}, using defaults")
            return OutputConfig(
                elevation_callback=self._on_elevation_warning,
                paste_abort_callback=self._on_paste_abort,
            )

    def _on_elevation_warning(self, target_info: str) -> None:
        """Called when trying to input to an elevated (admin) window.

        Shows warning to user that they need to run Aria as admin.
        """
        warning_msg = (
            f"无法向高权限窗口输入文字。请以管理员身份运行 Aria。\n目标: {target_info}"
        )
        logger.warning(f"[ELEVATION] {warning_msg}")
        print(f"[ELEVATION] WARNING: {warning_msg}")

        # Emit error to UI if bridge available
        self._emit_error(warning_msg)

    def _on_paste_abort(self, message: str) -> None:
        """Called from the injection path when the paste failed — set-verify
        gate abort (nothing injected) or Ctrl+V SendInput failure (possibly
        nothing injected). Make the failure audible and visible so the user
        re-checks / re-dictates instead of assuming the text landed."""
        try:
            self._emit_notice(message, "error", 2600)
        except Exception:
            logger.debug("paste-abort notice failed", exc_info=True)
        self._play_sound("error")

    def _load_asr_config(self) -> dict:
        """Load ASR configuration from hotwords.json."""
        import json

        config_path = get_config_path("hotwords.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            general = data.get("general", {})
            return {
                "engine": data.get("asr_engine", "qwen3"),
                "funasr": data.get("funasr", {}),
                "qwen3": data.get("qwen3", {}),
                "qwen3_sherpa": data.get("qwen3_sherpa", {}),
                "qwen3_llamacpp": data.get("qwen3_llamacpp", {}),
                "vad": data.get("vad", {}),
                "audio": data.get("audio", {}),
                "audio_device": general.get("audio_device"),  # Device name string
                "auto_hotword": data.get("auto_hotword", {}),
                "polish": data.get("polish", {}),
                "memory": data.get("memory", {}),
                "gpu_fallback": data.get(
                    "gpu_fallback", data.get("gpu_pressure_fallback", {})
                ),
                "asr_rescue": data.get("asr_rescue", {}),
                "sound": data.get("sound", {}),
            }
        except Exception as e:
            logger.warning(f"Failed to load ASR config: {e}, using defaults")
            return {
                "engine": "qwen3",
                "funasr": {},
                "qwen3": {},
                "qwen3_sherpa": {},
                "qwen3_llamacpp": {},
                "vad": {},
                "audio": {},
                "audio_device": None,
                "auto_hotword": {},
                "polish": {},
                "memory": {},
                "gpu_fallback": {},
                "asr_rescue": {},
                "sound": {},
            }

    @staticmethod
    def _canonical_asr_engine_type(engine_type: str | None) -> str:
        """Normalize removed/unknown ASR engine names to the runtime default.

        "none" is the runtime-only sentinel for the degraded no-ASR state
        (engine load failed, no torch fallback). It is preserved — NOT
        normalized to qwen3 — so status/state readers never relabel the
        failure state as a healthy engine. Config files never legitimately
        carry it; engine creation maps it back to qwen3 explicitly.
        """
        engine = str(engine_type or "qwen3").lower()
        if engine == "none":
            return "none"
        if engine in ("whisper", "fireredasr"):
            return "qwen3"
        if engine not in ("qwen3", "funasr", "qwen3_sherpa", "qwen3_llamacpp"):
            return "qwen3"
        return engine

    def _asr_cfg_uses_cpu_runtime(self, asr_cfg: dict | None) -> bool:
        """Return True when the requested ASR runtime is explicitly CPU.

        Only explicit CPU device requests should activate the CPU-friendly
        VAD/interim policy.  The default/auto Qwen3 path is treated as GPU-capable
        so we do not accidentally reduce CUDA 1.7B responsiveness.
        """
        asr_cfg = asr_cfg or {}
        engine_type = self._canonical_asr_engine_type(asr_cfg.get("engine"))
        if engine_type == "funasr":
            funasr_cfg = asr_cfg.get("funasr", {}) or {}
            return str(funasr_cfg.get("device", "cuda") or "cuda").lower() == "cpu"
        if engine_type == "qwen3_sherpa":
            # sherpa int8 runs on the ORT provider; anything but explicit CUDA
            # is a CPU runtime and inherits the CPU-side VAD/interim policy.
            sherpa_cfg = asr_cfg.get("qwen3_sherpa", {}) or {}
            return str(sherpa_cfg.get("provider", "cpu") or "cpu").lower() != "cuda"
        if engine_type == "qwen3_llamacpp":
            # llama-server always runs -ngl on CUDA: GPU runtime by definition
            # (interim captions and the GPU-side VAD policy stay available).
            return False
        qwen3_cfg = asr_cfg.get("qwen3", {}) or {}
        return str(qwen3_cfg.get("device", "cuda") or "cuda").lower() == "cpu"

    @staticmethod
    def _asr_engine_uses_cpu_runtime(transcribe_engine) -> bool:
        """Return True when this concrete ASR engine is running on CPU."""
        if transcribe_engine is None:
            return False
        cfg = getattr(transcribe_engine, "config", None)
        device = getattr(cfg, "device", "")
        actual_device = getattr(transcribe_engine, "actual_device", None)
        if actual_device:
            device = actual_device
        return str(device or "").strip().lower() == "cpu"

    def _active_asr_uses_cpu_runtime(self) -> bool:
        """Return True when the currently loaded primary ASR engine is CPU."""
        return self._asr_engine_uses_cpu_runtime(getattr(self, "asr_engine", None))

    def _runtime_vad_uses_cpu_policy(self, asr_cfg: dict | None) -> bool:
        """CPU-vs-GPU decision for runtime VAD timing.

        Normally follows config, but when the LOADED engine family differs
        from the configured one (fallback state: e.g. config says
        qwen3_sherpa but sherpa failed to load and torch/CUDA took over),
        the loaded engine is authoritative — otherwise VAD would stay
        CPU-tightened forever while a CUDA engine is actually serving, and
        every config_reload would re-tighten it from the stale config.
        """
        asr_cfg = asr_cfg or {}
        engine = getattr(self, "asr_engine", None)
        if engine is not None and getattr(engine, "is_loaded", False):
            cfg_engine = self._canonical_asr_engine_type(asr_cfg.get("engine"))
            loaded_engine = self._canonical_asr_engine_type(
                getattr(self, "_asr_engine_type", None)
            )
            if cfg_engine != loaded_engine:
                return self._asr_engine_uses_cpu_runtime(engine)
        return self._asr_cfg_uses_cpu_runtime(asr_cfg)

    def _apply_asr_runtime_vad_timing(self, asr_cfg: dict | None, source: str) -> None:
        """Apply ASR-device-specific VAD timing without changing user config.

        CPU mode gets shorter runtime segments so a 0.6B CPU decode does not hold
        the single ASR lock for 10-20 seconds.  GPU mode restores the user's VAD
        timing (plus capture-mode caps) from config.
        """
        audio_capture = getattr(self, "audio_capture", None)
        vad = getattr(audio_capture, "_vad", None) if audio_capture else None
        if vad is None or getattr(vad, "config", None) is None:
            return

        asr_cfg = asr_cfg or self._load_asr_config()
        vad_cfg = asr_cfg.get("vad", {}) or {}
        runtime_cfg = vad.config

        base_max_speech = max(3000, min(60000, vad_cfg.get("max_speech_ms", 10000)))
        base_soft_split_min = max(
            2000, min(20000, vad_cfg.get("soft_split_min_speech_ms", 5000))
        )

        effective_max_speech = base_max_speech
        try:
            from .core.audio.dsp import MODE_PRESETS as _MP

            preset = _MP.get(getattr(self, "_capture_mode", "standard"), {}) or {}
            max_speech_override = preset.get("max_speech_ms_override")
            if max_speech_override is not None:
                max_speech_override_i = int(max_speech_override)
                if max_speech_override_i > 0:
                    effective_max_speech = min(
                        effective_max_speech, max_speech_override_i
                    )
        except Exception:
            pass

        mode_label = "gpu"
        if self._runtime_vad_uses_cpu_policy(asr_cfg):
            mode_label = "cpu"
            effective_max_speech = min(
                effective_max_speech, self._CPU_ASR_MAX_SPEECH_MS
            )
            base_soft_split_min = min(
                base_soft_split_min, self._CPU_ASR_SOFT_SPLIT_MIN_SPEECH_MS
            )

        changes = []
        if runtime_cfg.max_speech_ms != effective_max_speech:
            changes.append(
                f"max_speech {runtime_cfg.max_speech_ms}->{effective_max_speech}"
            )
            runtime_cfg.max_speech_ms = effective_max_speech
        if runtime_cfg.soft_split_min_speech_ms != base_soft_split_min:
            changes.append(
                "soft_split_min "
                f"{runtime_cfg.soft_split_min_speech_ms}->{base_soft_split_min}"
            )
            runtime_cfg.soft_split_min_speech_ms = base_soft_split_min

        if changes:
            _pipeline_log(
                "ASR",
                f"Runtime VAD timing ({source}, {mode_label}): " + ", ".join(changes),
            )

    def _apply_hallucination_gate_config(self, vad_cfg: dict) -> None:
        """Load the anti-hallucination gate keys from the vad config block.

        Called at startup and from config hot-reload so all three H2 gates
        (VAD-probability joint gate, template-sentence blacklist, decode-
        confidence gate) can be tuned/disabled without a restart. Defaults
        are the acoustic_policy constants; numeric overrides are clamped to
        sane bands so a bad hand-edit cannot disarm the energy semantics
        (ceiling above ~0.01 would start eating real quiet speech).
        """
        from .core.asr.acoustic_policy import (
            CONF_GATE_AVG_LOGPROB_FLOOR,
            CONF_GATE_MIN_LOGPROB_FLOOR,
            VAD_JOINT_GATE_ENERGY_MAX,
            VAD_JOINT_GATE_VAD_MAX_BELOW,
        )

        vad_cfg = vad_cfg or {}
        self._vad_joint_gate_enabled = bool(vad_cfg.get("vad_joint_gate", True))
        self._energy_gate_vad_exempt_enabled = bool(
            vad_cfg.get("energy_gate_vad_exempt", True)
        )
        try:
            self._vad_joint_gate_energy_max = max(
                0.0,
                min(
                    0.01,
                    float(
                        vad_cfg.get(
                            "vad_joint_gate_energy_max", VAD_JOINT_GATE_ENERGY_MAX
                        )
                    ),
                ),
            )
        except (TypeError, ValueError):
            self._vad_joint_gate_energy_max = VAD_JOINT_GATE_ENERGY_MAX
        try:
            self._vad_joint_gate_vad_max = max(
                0.0,
                min(
                    1.0,
                    float(
                        vad_cfg.get(
                            "vad_joint_gate_vad_max", VAD_JOINT_GATE_VAD_MAX_BELOW
                        )
                    ),
                ),
            )
        except (TypeError, ValueError):
            self._vad_joint_gate_vad_max = VAD_JOINT_GATE_VAD_MAX_BELOW
        self._template_blacklist_enabled = bool(
            vad_cfg.get("template_blacklist", True)
        )
        extra = vad_cfg.get("template_blacklist_extra", [])
        if isinstance(extra, (list, tuple)):
            self._template_blacklist_extra = tuple(
                str(item).strip() for item in extra if str(item).strip()
            )
        else:
            self._template_blacklist_extra = ()
        self._conf_gate_enabled = bool(vad_cfg.get("conf_gate_enabled", False))
        try:
            self._conf_gate_floor = float(
                vad_cfg.get(
                    "conf_gate_avg_logprob_floor", CONF_GATE_AVG_LOGPROB_FLOOR
                )
            )
        except (TypeError, ValueError):
            self._conf_gate_floor = CONF_GATE_AVG_LOGPROB_FLOOR
        # MIN axis (2026-07-29): default ON — field-calibrated, see the
        # CONF_GATE_MIN_LOGPROB_FLOOR block in acoustic_policy.
        self._conf_gate_min_enabled = bool(
            vad_cfg.get("conf_gate_min_enabled", True)
        )
        try:
            self._conf_gate_min_floor = float(
                vad_cfg.get(
                    "conf_gate_min_logprob_floor", CONF_GATE_MIN_LOGPROB_FLOOR
                )
            )
        except (TypeError, ValueError):
            self._conf_gate_min_floor = CONF_GATE_MIN_LOGPROB_FLOOR

    @staticmethod
    def _app_leakage_rate_cap(engine_type: str) -> int:
        """chars/s cap for the app-level post-ASR discard; 0 = engine-guarded.

        See the Check-1 comment in the transcription worker: the qwen3 family
        (torch and sherpa share the inherited transcribe() guards) runs its own
        energy-aware, retry-protected speed guard, so the cruder app-level cap
        is disabled for it.
        """
        from .core.asr.acoustic_policy import APP_LEAKAGE_CHARS_PER_SEC

        if engine_type in ("qwen3", "qwen3_sherpa", "qwen3_llamacpp"):
            return 0
        return APP_LEAKAGE_CHARS_PER_SEC

    @staticmethod
    def _preloaded_engine_matches(preloaded, engine_type: str) -> bool:
        """Exact-type claim check for the launcher-preloaded ASR engine.

        MUST stay type()-exact for the qwen3 family: SherpaQwen3Engine
        subclasses Qwen3ASREngine and its .name contains "Qwen3", so an
        isinstance()/name-substring check would let the torch branch claim a
        sherpa instance (and vice versa the subclass would satisfy the base).
        """
        if preloaded is None:
            return False
        if engine_type == "funasr":
            return isinstance(preloaded, FunASREngine)
        if engine_type == "qwen3":
            return type(preloaded) is Qwen3ASREngine
        if engine_type == "qwen3_sherpa":
            return type(preloaded) is SherpaQwen3Engine
        if engine_type == "qwen3_llamacpp":
            return type(preloaded) is LlamaCppQwen3Engine
        return False

    @staticmethod
    def _torch_available() -> bool:
        """Cheap availability probe: True when torch is importable.

        find_spec does not import torch (no 2-5s DLL load), it only checks
        that the package exists — exactly what the slim (torch-free) build
        needs to know before attempting a torch fallback.
        """
        try:
            import importlib.util

            return importlib.util.find_spec("torch") is not None
        except Exception:
            return False

    def _fallback_to_torch_qwen3(self, asr_cfg: dict, engine_label: str, exc):
        """Startup fallback when an opt-in torch-free engine fails to load.

        Returns (engine_type, engine). On installs without torch (slim
        package) there is nothing to fall back to: emit a clear error and
        return ("none", None) so startup continues WITHOUT ASR — the tray
        stays alive for settings/diagnostics/exit instead of hard-crashing.
        """
        if not self._torch_available():
            msg = f"轻量包内置引擎加载失败：{exc}。请检查模型文件或重装"
            logger.error(
                f"{engine_label} failed and torch is unavailable "
                f"(slim install), continuing without ASR: {exc}"
            )
            print(f"[ERROR] {msg}")
            self._emit_error(msg)
            return "none", None

        logger.error(
            f"{engine_label} unavailable: {exc}; falling back to torch Qwen3-ASR"
        )
        print(
            f"[WARN] {engine_label}不可用: {exc}\n"
            "[WARN] 已回退到 Qwen3-ASR (torch)"
        )
        self._emit_error(f"{engine_label}不可用，已回退: {exc}")
        try:
            asr_config = Qwen3Config.from_mapping(asr_cfg.get("qwen3", {}))
            engine = Qwen3ASREngine(asr_config)
            engine.load()
        except Exception as torch_exc:
            # find_spec said torch exists, but a broken install (missing
            # DLLs, half-upgraded wheel) can still explode on import/load.
            # Same degraded outcome as "no torch": stay alive without ASR.
            msg = (
                f"{engine_label}加载失败（{exc}），"
                f"检测到 torch 但回退引擎也加载失败：{torch_exc}。"
                "请检查模型文件或重装"
            )
            logger.error(
                f"torch fallback failed after {engine_label} failure, "
                f"continuing without ASR: {torch_exc}",
                exc_info=True,
            )
            print(f"[ERROR] {msg}")
            self._emit_error(msg)
            return "none", None
        return "qwen3", engine

    def _fallback_llamacpp_to_sherpa_then_torch(self, asr_cfg: dict, exc):
        """Startup fallback chain for a failed llamacpp (GPU GGUF) engine.

        Try the OTHER torch-free engine (sherpa CPU int8) before the torch
        path: on a slim install torch is absent, so the old direct
        llamacpp→torch fallback degraded straight to "no ASR" even though
        the bundled sherpa model was sitting right there. Failure classes
        are disjoint (missing llama-server exe / port conflict / CUDA vs
        missing onnx model dir), so one failing says nothing about the
        other. sherpa itself needs no such hop — it already IS the last
        local torch-free engine, its failure goes to torch/none directly.

        Returns (engine_type, engine) like _fallback_to_torch_qwen3.
        """
        try:
            model_dir, provider, num_threads, sherpa_qcfg = (
                self._sherpa_runtime_settings(asr_cfg)
            )
            engine = SherpaQwen3Engine(
                sherpa_qcfg,
                model_dir=model_dir,
                provider=provider,
                num_threads=num_threads,
            )
            engine.load()
        except Exception as sherpa_exc:
            logger.error(
                f"sherpa fallback after llamacpp failure also failed: {sherpa_exc}"
            )
            print(f"[WARN] 轻量引擎回退也失败: {sherpa_exc}")
            return self._fallback_to_torch_qwen3(
                asr_cfg, "Qwen3-ASR GPU 加速引擎", exc
            )

        logger.error(
            f"Qwen3-ASR GPU 加速引擎 unavailable: {exc}; "
            "fell back to sherpa CPU engine"
        )
        print(
            f"[WARN] Qwen3-ASR GPU 加速引擎不可用: {exc}\n"
            "[WARN] 已回退到 Qwen3-ASR 轻量引擎 (sherpa CPU)"
        )
        self._emit_error(f"GPU 加速引擎不可用，已回退到轻量引擎: {exc}")
        return "qwen3_sherpa", engine

    @staticmethod
    def _sherpa_runtime_settings(asr_cfg: dict) -> tuple:
        """Parse the qwen3_sherpa config block into (model_dir, provider,
        num_threads, Qwen3Config).

        Shared by the desired-signature builder and the engine factory so a
        config edit and the resulting live engine always compare equal (a
        mismatch would either miss hot-reloads or trigger reload loops).
        """
        sherpa_cfg = asr_cfg.get("qwen3_sherpa", {}) or {}
        model_dir = resolve_sherpa_model_dir(str(sherpa_cfg.get("model_dir", "") or ""))
        provider = (
            str(sherpa_cfg.get("provider", "cpu") or "cpu").strip().lower() or "cpu"
        )
        # No explicit num_threads in config → core-aware default
        # (min(16, max(4, cpu_count // 2)), see sherpa_engine). _as_int
        # returns the default for missing/invalid values, so an explicit
        # valid value always wins.
        num_threads = Qwen3Config._as_int(
            sherpa_cfg.get("num_threads"),
            default_sherpa_num_threads(),
            min_value=1,
            max_value=64,
        )
        # Drop a stray "device" key before from_mapping: the sherpa block's
        # runtime key is "provider", and a user-copied "device": "cpu" would
        # trigger from_mapping's torch-CPU normalization (max_new_tokens
        # silently clamped to 512 -> the long-sentence word-swallow returns).
        # SherpaQwen3Engine sets config.device from provider itself.
        qwen3_cfg = Qwen3Config.from_mapping(
            {k: v for k, v in sherpa_cfg.items() if k != "device"}
        )
        return model_dir, provider, num_threads, qwen3_cfg

    @staticmethod
    def _llamacpp_runtime_settings(asr_cfg: dict) -> tuple:
        """Parse the qwen3_llamacpp config block into (server_path, model_path,
        mmproj_path, port, ngl, ctx, timeout_base_s, Qwen3Config).

        Shared by the desired-signature builder and the engine factory so a
        config edit and the resulting live engine always compare equal
        (same contract as _sherpa_runtime_settings).
        """
        block = asr_cfg.get("qwen3_llamacpp", {}) or {}
        server_path = resolve_llamacpp_path(
            str(block.get("server_path", "") or ""), DEFAULT_LLAMACPP_SERVER
        )
        model_path = resolve_llamacpp_path(
            str(block.get("model_path", "") or ""), DEFAULT_LLAMACPP_MODEL
        )
        mmproj_raw = str(block.get("mmproj_path", "") or "").strip()
        if mmproj_raw:
            mmproj_path = resolve_llamacpp_path(mmproj_raw, "")
        else:
            mmproj_path = default_mmproj_for(model_path)
        port = Qwen3Config._as_int(
            block.get("port"), DEFAULT_LLAMACPP_PORT, min_value=1024, max_value=65535
        )
        ngl = Qwen3Config._as_int(
            block.get("ngl"), DEFAULT_LLAMACPP_NGL, min_value=0, max_value=999
        )
        ctx = Qwen3Config._as_int(
            block.get("ctx"), DEFAULT_LLAMACPP_CTX, min_value=1024, max_value=65536
        )
        try:
            timeout_base_s = float(block.get("request_timeout_base", 8.0) or 8.0)
        except (TypeError, ValueError):
            timeout_base_s = 8.0
        timeout_base_s = max(1.0, min(120.0, timeout_base_s))
        # Drop a stray "device" key before from_mapping, same defense as the
        # sherpa block: a user-copied "device": "cpu" would trigger the
        # torch-CPU normalization (max_new_tokens clamped to 512). The engine
        # sets config.device = "cuda" itself.
        qwen3_cfg = Qwen3Config.from_mapping(
            {k: v for k, v in block.items() if k != "device"}
        )
        return (
            server_path,
            model_path,
            mmproj_path,
            port,
            ngl,
            ctx,
            timeout_base_s,
            qwen3_cfg,
        )

    def _desired_asr_runtime_signature(self, asr_cfg: dict) -> tuple:
        """Comparable ASR runtime settings from hotwords.json."""
        engine_type = self._canonical_asr_engine_type(asr_cfg.get("engine"))
        if engine_type == "funasr":
            funasr_cfg = asr_cfg.get("funasr", {}) or {}
            return (
                "funasr",
                str(funasr_cfg.get("model_name", "paraformer-zh")),
                str(funasr_cfg.get("device", "cuda")),
                bool(funasr_cfg.get("enable_vad", False)),
                bool(funasr_cfg.get("enable_punc", False)),
            )

        if engine_type == "qwen3_sherpa":
            model_dir, provider, num_threads, sherpa_qcfg = (
                self._sherpa_runtime_settings(asr_cfg)
            )
            return (
                "qwen3_sherpa",
                model_dir,
                provider,
                num_threads,
                sherpa_qcfg.max_total_len,
                sherpa_qcfg.max_new_tokens,
                sherpa_qcfg.language,
            )

        if engine_type == "qwen3_llamacpp":
            (
                server_path,
                model_path,
                mmproj_path,
                port,
                ngl,
                ctx,
                timeout_base_s,
                llama_qcfg,
            ) = self._llamacpp_runtime_settings(asr_cfg)
            return (
                "qwen3_llamacpp",
                server_path,
                model_path,
                mmproj_path,
                port,
                ngl,
                ctx,
                timeout_base_s,
                llama_qcfg.max_new_tokens,
                llama_qcfg.language,
            )

        qwen3_cfg = Qwen3Config.from_mapping(asr_cfg.get("qwen3", {}) or {})
        return (
            "qwen3",
            qwen3_cfg.model_name,
            qwen3_cfg.device,
            qwen3_cfg.torch_dtype,
            qwen3_cfg.max_new_tokens,
            qwen3_cfg.max_inference_batch_size,
            qwen3_cfg.low_cpu_mem_usage,
            qwen3_cfg.language,
        )

    def _current_asr_runtime_signature(self) -> tuple:
        """Comparable ASR runtime settings for the loaded engine."""
        engine_type = self._canonical_asr_engine_type(self._asr_engine_type)
        engine = self.asr_engine
        cfg = getattr(engine, "config", None)
        if engine_type == "funasr":
            return (
                "funasr",
                str(getattr(cfg, "model_name", "paraformer-zh")),
                str(getattr(cfg, "device", "cuda")),
                bool(getattr(cfg, "enable_vad", False)),
                bool(getattr(cfg, "enable_punc", False)),
            )

        if engine_type == "qwen3_sherpa":
            return (
                "qwen3_sherpa",
                str(getattr(engine, "_model_dir", "")),
                str(getattr(engine, "_provider", "cpu")),
                int(getattr(engine, "_num_threads", 8)),
                int(getattr(cfg, "max_total_len", 2048)),
                int(getattr(cfg, "max_new_tokens", 1024)),
                str(getattr(cfg, "language", "Chinese")),
            )

        if engine_type == "qwen3_llamacpp":
            return (
                "qwen3_llamacpp",
                str(getattr(engine, "_server_path", "")),
                str(getattr(engine, "_model_path", "")),
                str(getattr(engine, "_mmproj_path", "")),
                int(getattr(engine, "_port", DEFAULT_LLAMACPP_PORT)),
                int(getattr(engine, "_ngl", DEFAULT_LLAMACPP_NGL)),
                int(getattr(engine, "_ctx", DEFAULT_LLAMACPP_CTX)),
                float(getattr(engine, "_request_timeout_base_s", 8.0)),
                int(getattr(cfg, "max_new_tokens", 1024)),
                str(getattr(cfg, "language", "Chinese")),
            )

        return (
            "qwen3",
            str(getattr(cfg, "model_name", "auto")),
            str(getattr(cfg, "device", "cuda")),
            str(getattr(cfg, "torch_dtype", "bfloat16")),
            int(getattr(cfg, "max_new_tokens", 1024)),
            int(getattr(cfg, "max_inference_batch_size", 32)),
            bool(getattr(cfg, "low_cpu_mem_usage", True)),
            str(getattr(cfg, "language", "Chinese")),
        )

    def _asr_runtime_config_changed(self, asr_cfg: dict) -> bool:
        """Return True when ASR engine/model/device settings need live rebuild."""
        if not self.asr_engine:
            return False
        return (
            self._desired_asr_runtime_signature(asr_cfg)
            != self._current_asr_runtime_signature()
        )

    def _create_asr_engine_from_config(self, asr_cfg: dict):
        """Create an unloaded ASR engine from current config."""
        engine_type = self._canonical_asr_engine_type(asr_cfg.get("engine"))
        if engine_type == "none":
            # Creating an engine means the caller wants a real one; "none"
            # is a runtime failure sentinel, never a buildable engine type.
            engine_type = "qwen3"
        if engine_type == "funasr":
            funasr_cfg = asr_cfg.get("funasr", {}) or {}
            asr_config = FunASRConfig(
                model_name=funasr_cfg.get("model_name", "paraformer-zh"),
                device=funasr_cfg.get("device", "cuda"),
                enable_vad=funasr_cfg.get("enable_vad", False),
                enable_punc=funasr_cfg.get("enable_punc", False),
            )
            return engine_type, FunASREngine(asr_config)

        if engine_type == "qwen3_sherpa":
            model_dir, provider, num_threads, sherpa_qcfg = (
                self._sherpa_runtime_settings(asr_cfg)
            )
            return engine_type, SherpaQwen3Engine(
                sherpa_qcfg,
                model_dir=model_dir,
                provider=provider,
                num_threads=num_threads,
            )

        if engine_type == "qwen3_llamacpp":
            (
                server_path,
                model_path,
                mmproj_path,
                port,
                ngl,
                ctx,
                timeout_base_s,
                llama_qcfg,
            ) = self._llamacpp_runtime_settings(asr_cfg)
            # Config→adapter bridge for per-token logprobs (decode-confidence
            # telemetry): the engine ctor signature is frozen, so the adapter
            # reads ARIA_LLAMACPP_LOGPROBS when it is built inside load()
            # (after this export). Default ON; qwen3_llamacpp.logprobs=false
            # switches it off.
            _lp_block = asr_cfg.get("qwen3_llamacpp", {}) or {}
            os.environ["ARIA_LLAMACPP_LOGPROBS"] = (
                "1" if _lp_block.get("logprobs", True) else "0"
            )
            return engine_type, LlamaCppQwen3Engine(
                llama_qcfg,
                server_path=server_path,
                model_path=model_path,
                mmproj_path=mmproj_path,
                port=port,
                ngl=ngl,
                ctx=ctx,
                request_timeout_base_s=timeout_base_s,
            )

        qwen3_cfg = asr_cfg.get("qwen3", {}) or {}
        asr_config = Qwen3Config.from_mapping(qwen3_cfg)
        return engine_type, Qwen3ASREngine(asr_config)

    def _apply_hotword_context_to_asr_engine(self) -> None:
        """Refresh Layer-1 hotword/context on the currently active ASR engine."""
        if not self.hotword_manager or not self.asr_engine:
            return
        self.hotword_manager.config.asr_engine_type = self._asr_engine_type
        if self._asr_engine_type == "funasr" and hasattr(
            self.asr_engine, "set_hotwords_with_score"
        ):
            hotwords_with_score = self.hotword_manager.get_asr_hotwords_with_score()
            self.asr_engine.set_hotwords_with_score(hotwords_with_score)
            print(
                f"[HOT-RELOAD] Updated FunASR hotwords: {len(hotwords_with_score)} words"
            )
        elif self._asr_engine_type in ("qwen3", "qwen3_sherpa", "qwen3_llamacpp") and hasattr(
            self.asr_engine, "set_context"
        ):
            if self._active_asr_uses_cpu_runtime():
                # CPU 0.6B keeps a BOUNDED hotword context — dropping it entirely
                # destroyed domain-term accuracy ("Fable" -> "F A B L E").  The
                # historic "CPU stuck for tens of seconds" failure was context
                # parroting triggering the retry-without-context path, which the
                # engine's CTX-STARVE + energy-gated leakage guards now prevent.
                # recent_context stays off below: it is the worst regurgitation
                # vector and its parroting retry doubles a slow CPU decode.
                context_string = self._build_cpu_asr_context()
            else:
                context_string = self._build_asr_context_for_current_mode(
                    include_screen=False
                )
            self.asr_engine.set_context(context_string or "")
            if self._active_asr_uses_cpu_runtime() and hasattr(
                self.asr_engine, "set_recent_context"
            ):
                self.asr_engine.set_recent_context("")
            print(f"[HOT-RELOAD] Updated Qwen3 context: {len(context_string)} chars")

    def _maybe_hot_reload_asr_engine(self, asr_cfg: dict) -> bool:
        """Start a background ASR engine rebuild when model/device settings changed."""
        if not self._asr_runtime_config_changed(asr_cfg):
            return False
        cfg_snapshot = json.loads(json.dumps(asr_cfg, ensure_ascii=False))
        with self._asr_hot_reload_lock:
            if self._asr_hot_reload_in_progress:
                self._asr_hot_reload_pending_cfg = cfg_snapshot
                self._asr_hot_reload_target_cfg = cfg_snapshot
                _pipeline_log(
                    "ASR",
                    "ASR hot-reload already in progress; queued latest ASR config",
                )
                return True
            self._asr_hot_reload_in_progress = True
            self._asr_hot_reload_target_cfg = cfg_snapshot
            self._asr_hot_reload_pending_cfg = None
            old_sig = self._current_asr_runtime_signature()
            new_sig = self._desired_asr_runtime_signature(cfg_snapshot)
            self._asr_hot_reload_thread = threading.Thread(
                target=self._hot_reload_asr_engine_thread,
                args=(cfg_snapshot, old_sig, new_sig),
                daemon=True,
                name="asr-hot-reload",
            )
            self._asr_hot_reload_thread.start()
        return True

    def _hot_reload_asr_engine_thread(
        self, asr_cfg: dict, old_sig: tuple, new_sig: tuple
    ) -> None:
        """Rebuild ASR engine without a process restart."""
        new_engine = None
        old_engine = None
        old_type = self._asr_engine_type
        old_restored = False
        try:
            self._stop_interim_timer()
            engine_type, new_engine = self._create_asr_engine_from_config(asr_cfg)
            with self._lock:
                deep_sleeping = self._sleep_mode == SleepMode.DEEP
                state_before = self.state

            _pipeline_log(
                "ASR",
                f"Hot-reload engine start: {old_sig} -> {new_sig}, deep={deep_sleeping}",
            )
            print(f"[HOT-RELOAD] ASR engine switching: {old_sig} -> {new_sig}")
            if self._bridge and not deep_sleeping:
                try:
                    self._bridge.emit_state("LOADING")
                except Exception:
                    pass

            with self._asr_lock:
                old_engine = self.asr_engine
                if old_engine and hasattr(old_engine, "unload"):
                    old_engine.unload()

                if not deep_sleeping:
                    new_engine.load()
                    if engine_type in ("qwen3", "qwen3_sherpa", "qwen3_llamacpp"):
                        # sherpa-CPU engines skip the CUDA warmup naturally via
                        # the _qwen3_engine_uses_cuda gate inside.
                        self._warmup_qwen3_engine(new_engine, "asr_hot_reload")

                with self._lock:
                    self.asr_engine = new_engine
                    self._asr_engine_type = engine_type

                self._apply_hotword_context_to_asr_engine()
                self._configure_gpu_pressure_fallback(asr_cfg)
                self._apply_asr_runtime_vad_timing(asr_cfg, "asr_hot_reload")

            _pipeline_log("ASR", f"Hot-reload engine done: {new_sig}")
            print("[HOT-RELOAD] ASR engine switched without restart")
            self._emit_asr_status()
            if self._bridge and not deep_sleeping:
                try:
                    self._bridge.emit_state(state_before.name)
                except Exception:
                    pass
        except Exception as exc:
            _pipeline_log(
                "ERROR", f"ASR hot-reload failed: {type(exc).__name__}: {exc}"
            )
            logger.error(f"ASR hot-reload failed: {exc}", exc_info=True)
            if new_engine is not None and new_engine is not old_engine:
                try:
                    if hasattr(new_engine, "unload"):
                        new_engine.unload()
                except Exception as unload_exc:
                    logger.warning(f"Failed to unload failed ASR engine: {unload_exc}")
            if old_engine is not None:
                try:
                    old_engine.load()
                    with self._lock:
                        self.asr_engine = old_engine
                        self._asr_engine_type = old_type
                    self._apply_hotword_context_to_asr_engine()
                    rollback_cfg = self._load_asr_config()
                    self._configure_gpu_pressure_fallback(rollback_cfg)
                    # VAD timing must follow the RESTORED engine, not the
                    # failed target config (same fallback-state rule as
                    # startup: loaded engine is authoritative). The config
                    # file still carries the failed target in its engine
                    # key here, so pass a copy pinned to the restored type.
                    restored_vad_cfg = dict(rollback_cfg)
                    restored_vad_cfg["engine"] = self._canonical_asr_engine_type(
                        old_type
                    )
                    self._apply_asr_runtime_vad_timing(
                        restored_vad_cfg, "asr_hot_reload_rollback"
                    )
                    old_restored = True
                except Exception as restore_exc:
                    logger.error(
                        f"ASR hot-reload rollback failed: {restore_exc}",
                        exc_info=True,
                    )
            msg = "语音识别模型热切换失败"
            if old_restored:
                msg += "，已恢复原模型"
            else:
                msg += "，请重启应用恢复"
            self._emit_error(f"{msg}: {exc}")
            self._emit_asr_status()
        finally:
            # Engine state just changed (success or rollback): re-probe the
            # sherpa wheel on the next status refresh instead of trusting a
            # cache taken under the previous engine.
            self._invalidate_sherpa_install_cache()
            pending_cfg = None
            with self._asr_hot_reload_lock:
                self._asr_hot_reload_in_progress = False
                pending_cfg = self._asr_hot_reload_pending_cfg
                self._asr_hot_reload_pending_cfg = None
                self._asr_hot_reload_target_cfg = pending_cfg
            self._emit_asr_status()
            if pending_cfg and self._asr_runtime_config_changed(pending_cfg):
                _pipeline_log("ASR", "Starting queued ASR hot-reload")
                self._maybe_hot_reload_asr_engine(pending_cfg)

    def _warmup_qwen3_engine(self, engine, reason: str) -> None:
        """Warm up Qwen3 after a runtime model switch."""
        try:
            if not self._qwen3_engine_uses_cuda(engine):
                _pipeline_log(
                    "WARMUP",
                    f"Qwen3 warmup skipped ({reason}): engine on "
                    f"{self._qwen3_engine_device_label(engine)}",
                )
                return

            import numpy as _np

            silence = _np.zeros(16000, dtype=_np.float32)
            _cm = getattr(self, "_capture_mode", "standard")
            _ = self._transcribe_qwen3_warmup_audio(
                engine, silence, capture_mode=_cm
            )
            noise = _np.random.randn(16000).astype(_np.float32) * 0.01
            _ = self._transcribe_qwen3_warmup_audio(
                engine, noise, capture_mode=_cm
            )
            if hasattr(engine, "trim_runtime_cache"):
                engine.trim_runtime_cache(reason)
        except Exception as exc:
            logger.warning(f"Qwen3 warmup skipped after hot-reload: {exc}")

    def _schedule_polish_prewarm(self, delay_s: float, reason: str) -> None:
        """PERF-1: schedule one polish-API prewarm call on a background timer.

        Fires a max_tokens=1 dummy polish request (see AIPolisher.prewarm) to
        warm the TLS connection and the server-side prompt prefix cache, so
        the first real utterance after startup / deep-sleep wake doesn't pay
        the fully-cold-call penalty (~+1.4s p50). Delayed by `delay_s` to
        stay out of the ASR engine's startup window. Config gate:
        polish.prewarm (default true); quality-mode API polisher only —
        fast/local polish has nothing to prewarm.
        """
        polisher = self.polisher
        if polisher is None or not hasattr(polisher, "prewarm"):
            return
        cfg = getattr(polisher, "config", None)
        if cfg is None or not getattr(cfg, "prewarm", False):
            return
        if not getattr(cfg, "enabled", False):
            return

        def _run_prewarm():
            try:
                ok = polisher.prewarm(reason=reason)
                print(f"[POLISH] Prewarm ({reason}): {'ok' if ok else 'skipped/failed'}")
            except Exception:
                pass  # prewarm is best-effort by contract

        timer = threading.Timer(delay_s, _run_prewarm)
        timer.daemon = True
        timer.start()
        _pipeline_log(
            "POLISH", f"Prewarm scheduled in {delay_s:.0f}s (reason={reason})"
        )

    @staticmethod
    def _transcribe_qwen3_warmup_audio(engine, audio, *, capture_mode="standard"):
        """Run warmup audio without ASR bias context, then restore it."""
        old_context = getattr(engine, "_context_string", None)
        old_recent = getattr(engine, "_recent_context", None)
        if hasattr(engine, "set_context"):
            engine.set_context("")
        if hasattr(engine, "set_recent_context"):
            engine.set_recent_context("")
        try:
            return engine.transcribe(audio, capture_mode=capture_mode)
        finally:
            if old_context is not None and hasattr(engine, "set_context"):
                engine.set_context(old_context)
            if old_recent is not None and hasattr(engine, "set_recent_context"):
                engine.set_recent_context(old_recent)

    @staticmethod
    def _qwen3_engine_device_label(engine) -> str:
        actual = getattr(engine, "actual_device", None)
        if actual:
            return str(actual)
        cfg = getattr(engine, "config", None)
        return str(getattr(cfg, "device", "unknown"))

    @staticmethod
    def _qwen3_engine_uses_cuda(engine) -> bool:
        """True only when the already-loaded Qwen3 engine is actually on CUDA."""
        actual = getattr(engine, "actual_device", None)
        if actual is not None:
            return str(actual).lower().startswith("cuda")
        cfg = getattr(engine, "config", None)
        return str(getattr(cfg, "device", "")).lower().startswith("cuda")

    def _configure_gpu_pressure_fallback(self, asr_cfg: dict) -> None:
        """Configure GPU-pressure ASR fallback from hotwords.json.

        The feature is deliberately opt-out for Qwen3/CUDA users and inactive
        for FunASR/CPU users.  It can be tuned by adding a `gpu_fallback` block
        to hotwords.json, but the default path requires no config mutation.
        """
        cfg_block = asr_cfg.get("gpu_fallback", {}) or {}
        qwen3_cfg = asr_cfg.get("qwen3", {}) or {}
        pressure_cfg = GpuPressureConfig.from_mapping(cfg_block)
        self._gpu_fallback_retry_on_stall = GpuPressureConfig._as_bool(
            cfg_block.get("retry_same_utterance_on_stall"), True
        )
        self._gpu_fallback_primary_stall_timeout_s = GpuPressureConfig._as_float(
            cfg_block.get("primary_stall_timeout_s"),
            8.0,
            min_value=2.0,
            max_value=25.0,
        )
        self._gpu_fallback_idle_unload_s = GpuPressureConfig._as_float(
            cfg_block.get("idle_unload_s"),
            120.0,
            min_value=30.0,
            max_value=3600.0,
        )
        self._gpu_fallback_max_segment_s = GpuPressureConfig._as_float(
            cfg_block.get("max_segment_s"),
            6.0,
            min_value=2.0,
            max_value=20.0,
        )
        primary_device = str(qwen3_cfg.get("device", "cuda") or "cuda").lower()
        enabled = (
            pressure_cfg.enabled
            and self._asr_engine_type == "qwen3"
            and primary_device == "cuda"
        )

        fallback_qwen3_cfg = dict(qwen3_cfg)
        fallback_qwen3_cfg.update(cfg_block.get("qwen3", {}) or {})
        fallback_qwen3_cfg["model_name"] = str(
            cfg_block.get("model_name", "Qwen/Qwen3-ASR-0.6B") or "Qwen/Qwen3-ASR-0.6B"
        )
        fallback_qwen3_cfg["device"] = "cpu"
        fallback_qwen3_cfg["torch_dtype"] = str(
            cfg_block.get("torch_dtype", "float32") or "float32"
        )
        # CPU fallback is a rescue path, not the quality/long-form path.
        # In field logs the inherited 1024-token budget let 0.6B CPU spend
        # minutes regurgitating old context, while the worker timed out and the
        # user's segment was swallowed.  Keep the rescue decoder short unless a
        # user explicitly overrides it in gpu_fallback.max_new_tokens/qwen3.
        nested_fb_cfg = cfg_block.get("qwen3", {}) or {}
        fallback_qwen3_cfg["max_new_tokens"] = Qwen3Config._as_int(
            cfg_block.get(
                "max_new_tokens",
                nested_fb_cfg.get("max_new_tokens", 256),
            ),
            256,
            min_value=64,
            max_value=512,
        )
        fallback_qwen3_cfg["max_inference_batch_size"] = int(
            cfg_block.get("max_inference_batch_size", 8) or 8
        )
        fallback_qwen3_cfg["low_cpu_mem_usage"] = bool(
            cfg_block.get("low_cpu_mem_usage", True)
        )
        if "language" not in fallback_qwen3_cfg:
            fallback_qwen3_cfg["language"] = qwen3_cfg.get("language", "Chinese")

        reconfigure_loaded_engine = (
            self._gpu_fallback_qwen3_cfg
            and self._gpu_fallback_qwen3_cfg != fallback_qwen3_cfg
        )
        self._gpu_fallback_qwen3_cfg = fallback_qwen3_cfg
        self._gpu_fallback_enabled = enabled
        self._gpu_pressure_monitor = (
            GpuPressureMonitor(pressure_cfg) if enabled else None
        )

        if not enabled:
            self._unload_gpu_fallback_engine("disabled")
        elif reconfigure_loaded_engine:
            self._unload_gpu_fallback_engine("reconfigured")

        if enabled:
            _pipeline_log(
                "GPU-FALLBACK",
                "Enabled: "
                f"util>={pressure_cfg.utilization_threshold}%, "
                f"free<={pressure_cfg.memory_free_mb_threshold}MB, "
                f"cooldown={pressure_cfg.cooldown_s:.0f}s, "
                f"stall_retry={self._gpu_fallback_retry_on_stall}, "
                f"stall_timeout={self._gpu_fallback_primary_stall_timeout_s:.0f}s, "
                f"idle_unload={self._gpu_fallback_idle_unload_s:.0f}s, "
                f"max_segment={self._gpu_fallback_max_segment_s:.1f}s, "
                f"max_tokens={fallback_qwen3_cfg['max_new_tokens']}, "
                f"model={fallback_qwen3_cfg['model_name']} on CPU",
            )
        else:
            _pipeline_log(
                "GPU-FALLBACK",
                f"Disabled (engine={self._asr_engine_type}, primary_device={primary_device})",
            )

    def _primary_asr_supports_gpu_fallback(self) -> bool:
        if not self._gpu_fallback_enabled:
            return False
        if self._asr_engine_type != "qwen3" or not self.asr_engine:
            return False
        cfg = getattr(self.asr_engine, "config", None)
        actual_device = str(getattr(self.asr_engine, "actual_device", "") or "").lower()
        configured_device = str(getattr(cfg, "device", "") or "").lower()
        return actual_device == "cuda" or configured_device == "cuda"

    def _loaded_gpu_fallback_engine(self):
        with self._gpu_fallback_lock:
            engine = self._gpu_fallback_engine
            if engine is not None and getattr(engine, "is_loaded", False):
                return engine
        return None

    def _cancel_gpu_fallback_unload_timer(self) -> None:
        timer = getattr(self, "_gpu_fallback_unload_timer", None)
        self._gpu_fallback_unload_timer = None
        if timer:
            try:
                timer.cancel()
            except Exception:
                pass

    def _mark_gpu_fallback_used(self, reason: str = "") -> None:
        """Record CPU fallback activity and schedule idle unload."""
        self._gpu_fallback_last_used_at = time.time()
        self._schedule_gpu_fallback_unload(reason or "used")

    def _schedule_gpu_fallback_unload(self, reason: str = "") -> None:
        """Unload CPU fallback model after it has been idle for a while.

        The fallback engine is intentionally loaded on demand for GPU-pressure
        rescue, but keeping both Qwen3 1.7B CUDA and Qwen3 0.6B CPU resident
        pushes process private memory into the 20GB+ range.  Keep it warm only
        for the short recovery window, then release it.
        """
        self._cancel_gpu_fallback_unload_timer()
        if not self._gpu_fallback_enabled:
            return
        # May be called while _gpu_fallback_lock is already held by the loader,
        # so do not call _loaded_gpu_fallback_engine() here.
        engine = getattr(self, "_gpu_fallback_engine", None)
        if engine is None or not getattr(engine, "is_loaded", False):
            return
        idle_s = max(30.0, float(self._gpu_fallback_idle_unload_s or 120.0))
        timer = threading.Timer(idle_s, self._gpu_fallback_unload_fire)
        timer.daemon = True
        self._gpu_fallback_unload_timer = timer
        timer.start()
        _pipeline_log(
            "GPU-FALLBACK",
            f"CPU fallback idle unload scheduled in {idle_s:.0f}s ({reason})",
        )

    def _gpu_fallback_unload_fire(self) -> None:
        self._gpu_fallback_unload_timer = None
        engine = self._loaded_gpu_fallback_engine()
        if engine is None:
            return
        idle_for = time.time() - float(self._gpu_fallback_last_used_at or 0.0)
        idle_s = max(30.0, float(self._gpu_fallback_idle_unload_s or 120.0))
        if idle_for < idle_s:
            self._schedule_gpu_fallback_unload("still_warm")
            return
        if self._worker_busy or not self._asr_queue.empty():
            self._schedule_gpu_fallback_unload("asr_busy")
            return
        preload = getattr(self, "_gpu_fallback_preload_thread", None)
        if preload and preload.is_alive():
            self._schedule_gpu_fallback_unload("preload_running")
            return
        self._unload_gpu_fallback_engine(f"idle {idle_for:.0f}s")

    def _ensure_gpu_fallback_engine(self, reason: str = ""):
        if not self._gpu_fallback_enabled:
            return None
        now = time.time()
        if now < self._gpu_fallback_failed_until:
            return None
        with self._gpu_fallback_lock:
            engine = self._gpu_fallback_engine
            if engine is not None and getattr(engine, "is_loaded", False):
                return engine
            try:
                _pipeline_log(
                    "GPU-FALLBACK",
                    f"Loading CPU fallback ({reason or 'on demand'})...",
                )
                asr_config = Qwen3Config.from_mapping(
                    self._gpu_fallback_qwen3_cfg,
                    model_name_default="Qwen/Qwen3-ASR-0.6B",
                )
                engine = Qwen3ASREngine(asr_config)
                engine.load()
                self._gpu_fallback_engine = engine
                self._gpu_fallback_last_used_at = time.time()
                _pipeline_log(
                    "GPU-FALLBACK",
                    f"CPU fallback ready: {engine.name} ({engine.device_info})",
                )
                self._schedule_gpu_fallback_unload(reason or "loaded")
                return engine
            except Exception as exc:
                self._gpu_fallback_failed_until = time.time() + 60.0
                _pipeline_log(
                    "GPU-FALLBACK",
                    f"CPU fallback load failed: {type(exc).__name__}: {exc}",
                )
                logger.warning(f"GPU fallback ASR load failed: {exc}")
                return None

    def _start_gpu_fallback_preload(self, reason: str) -> None:
        if not self._gpu_fallback_enabled:
            return
        if self._loaded_gpu_fallback_engine() is not None:
            return
        if (
            self._gpu_fallback_preload_thread
            and self._gpu_fallback_preload_thread.is_alive()
        ):
            return

        def _preload() -> None:
            self._ensure_gpu_fallback_engine(reason)

        self._gpu_fallback_preload_thread = threading.Thread(
            target=_preload, daemon=True, name="gpu-fallback-preload"
        )
        self._gpu_fallback_preload_thread.start()

    def _probe_gpu_pressure_and_preload(self) -> None:
        if (
            not self._primary_asr_supports_gpu_fallback()
            or not self._gpu_pressure_monitor
        ):
            return
        sample = self._gpu_pressure_monitor.sample(force=True)
        _pipeline_log(
            "GPU-FALLBACK",
            f"speech_start probe: busy={sample.busy}, reason={sample.reason}",
        )
        if sample.busy:
            self._start_gpu_fallback_preload(sample.reason)

    def _on_speech_start_gpu_probe(self) -> None:
        """Probe GPU pressure asynchronously when speech starts."""
        if not self._primary_asr_supports_gpu_fallback():
            return
        if (
            self._gpu_fallback_probe_thread
            and self._gpu_fallback_probe_thread.is_alive()
        ):
            return
        self._gpu_fallback_probe_thread = threading.Thread(
            target=self._probe_gpu_pressure_and_preload,
            daemon=True,
            name="gpu-pressure-probe",
        )
        self._gpu_fallback_probe_thread.start()

    def _mark_primary_asr_timeout(self, timeout_s: float) -> None:
        # ThreadPool timeout does not kill the underlying transcribe call.  For
        # CUDA primary we bias subsequent requests to CPU for a while so the
        # queue can recover.  Manual CPU mode has no lower fallback tier, so this
        # becomes diagnostic only.
        suppress_s = max(60.0, timeout_s * 2.0)
        self._primary_asr_suppressed_until = time.time() + suppress_s
        engine = getattr(self, "asr_engine", None)
        if self._asr_engine_uses_cpu_runtime(engine):
            _pipeline_log(
                "ASR",
                f"Primary CPU ASR timeout after {timeout_s:.0f}s; future CPU interims stay disabled",
            )
        else:
            _pipeline_log(
                "GPU-FALLBACK",
                f"Primary CUDA ASR timeout; suppressing primary for {suppress_s:.0f}s",
            )
        # Capture GPU pstate/clocks/power + compute-app list so "GPU idle yet
        # 20x slow" clusters can be diagnosed after the fact.  Throttled and
        # fully async — never blocks this path.
        from .core.asr.gpu_diag import maybe_snapshot_async

        maybe_snapshot_async("asr_timeout_suppress", _pipeline_log)

    @staticmethod
    def _gpu_pressure_indicates_stall(sample: GpuPressureSample) -> bool:
        """Return True when a pressure sample justifies abandoning CUDA ASR."""
        if sample.busy:
            return True
        error = (sample.error or "").lower()
        return "timeout" in error or "timed out" in error

    @staticmethod
    def _should_skip_buffered_final_tail(
        *,
        kind: str,
        buffered_text: str,
        duration_s: float,
        pre_dsp_audio_level_avg: float,
        post_dsp_audio_level_avg: float,
        energy_gate: float,
        capture_mode: str,
    ) -> bool:
        """Skip ASR for a near-silent final tail when soft-split text exists.

        Delegates to core.asr.acoustic_policy (thresholds live there).
        """
        from .core.asr.acoustic_policy import should_skip_buffered_final_tail

        return should_skip_buffered_final_tail(
            kind=kind,
            buffered_text=buffered_text,
            duration_s=duration_s,
            pre_dsp_audio_level_avg=pre_dsp_audio_level_avg,
            post_dsp_audio_level_avg=post_dsp_audio_level_avg,
            energy_gate=energy_gate,
            capture_mode=capture_mode,
        )

    @staticmethod
    def _should_skip_unbuffered_low_energy_final(
        *,
        kind: str,
        buffered_text: str,
        has_deferred_audio: bool,
        duration_s: float,
        pre_dsp_audio_level_avg: float,
        pre_dsp_audio_level_p95: float,
        energy_gate: float,
        capture_mode: str,
        engine_is_cpu: bool,
        vad_prob_max: float = -1.0,
        vad_voiced_ratio: float = -1.0,
    ) -> bool:
        """Skip standalone final noise before it can stall a CPU decode.

        Delegates to core.asr.acoustic_policy (thresholds live there).
        """
        from .core.asr.acoustic_policy import should_skip_unbuffered_low_energy_final

        return should_skip_unbuffered_low_energy_final(
            kind=kind,
            buffered_text=buffered_text,
            has_deferred_audio=has_deferred_audio,
            duration_s=duration_s,
            pre_dsp_audio_level_avg=pre_dsp_audio_level_avg,
            pre_dsp_audio_level_p95=pre_dsp_audio_level_p95,
            energy_gate=energy_gate,
            capture_mode=capture_mode,
            engine_is_cpu=engine_is_cpu,
            vad_prob_max=vad_prob_max,
            vad_voiced_ratio=vad_voiced_ratio,
        )

    def _prepare_asr_engine_for_segment(
        self,
        transcribe_engine,
        pre_dsp_audio_level_avg: float,
        pre_dsp_audio_level_p95: float = -1.0,
    ) -> None:
        """Refresh per-segment hints on whichever ASR engine will run."""
        if not transcribe_engine:
            return

        is_cpu_engine = self._asr_engine_uses_cpu_runtime(transcribe_engine)

        if self._asr_engine_type in ("qwen3", "qwen3_sherpa", "qwen3_llamacpp") and hasattr(
            transcribe_engine, "set_context"
        ):
            if is_cpu_engine:
                # CPU mode (manual 0.6B or GPU-pressure fallback) keeps a
                # BOUNDED hotword context.  Audio-only CPU destroyed domain
                # terms ("Fable" -> "F A B L E"); the old "20s+ stuck" failure
                # was context PARROTING triggering retry-without-context, not
                # prompt length — and the engine's CTX-STARVE (pre-DSP < 0.012
                # drops all context) plus the energy-gated leakage guards now
                # block that path.  Screen OCR and recent_context stay off on
                # CPU: weakest signals, highest regurgitation/prefill cost.
                context_string = self._build_cpu_asr_context()
            else:
                # Quality mode deliberately stays screen-free at ASR; fast mode
                # may add only already-cached OCR keywords and never waits here.
                context_string = self._build_asr_context_for_current_mode(
                    include_screen=True
                )
            transcribe_engine.set_context(context_string or "")

        if hasattr(transcribe_engine, "set_recent_context"):
            if is_cpu_engine:
                # CPU 0.6B never gets recent_context (regurgitation-prone and
                # doubles a slow decode) — same policy for the in-flight prefix.
                transcribe_engine.set_recent_context("")
            else:
                # Committed sentences (previous utterances) + the in-flight
                # prefix (earlier soft-split segments of THIS same sentence) so
                # a long sentence split across 8s chunks stays coherent instead
                # of each chunk transcribing blind to the ones before it.
                recent_parts = list(self._recent_asr_buffer)
                inflight_prefix = (
                    getattr(self, "_active_session_ctx_prefix", "") or ""
                ).strip()
                if inflight_prefix:
                    recent_parts.append(inflight_prefix)
                combined_recent = " ".join(p for p in recent_parts if p).strip()
                # Bound the tail so a very long dictation can't grow the prompt
                # without limit; keep the most recent context (nearest to what
                # is being spoken now).
                if len(combined_recent) > self._RECENT_CTX_MAX_CHARS:
                    combined_recent = combined_recent[-self._RECENT_CTX_MAX_CHARS :]
                transcribe_engine.set_recent_context(combined_recent)

        # Acoustic / capture_mode hints are passed as explicit kwargs to
        # transcribe() (see _transcribe_with_gpu_stall_fallback) so they
        # cannot race with interim transcription via lock-free setters.
        # pre_dsp_* args are kept for call-site compatibility but unused here.
        _ = (pre_dsp_audio_level_avg, pre_dsp_audio_level_p95)

    def _transcribe_with_gpu_stall_fallback(
        self,
        *,
        audio,
        transcribe_engine,
        engine_label: str,
        engine_reason: str,
        kind: str,
        pre_dsp_audio_level_avg: float,
        pre_dsp_audio_level_p95: float = -1.0,
        capture_mode: str | None = None,
        asr_timeout_s: float = 30.0,
    ):
        """Run ASR, converting engine exceptions into an empty failure result.

        A stale engine reference (e.g. unloaded old instance after a
        self-heal swap raises "Model not loaded") must flow into the
        failure/rescue chain like a timeout instead of escaping to the
        worker's generic exception handler, where the segment would be lost
        without counting toward the failure streak.
        """
        try:
            return self._transcribe_with_gpu_stall_fallback_inner(
                audio=audio,
                transcribe_engine=transcribe_engine,
                engine_label=engine_label,
                engine_reason=engine_reason,
                kind=kind,
                pre_dsp_audio_level_avg=pre_dsp_audio_level_avg,
                pre_dsp_audio_level_p95=pre_dsp_audio_level_p95,
                capture_mode=capture_mode,
                asr_timeout_s=asr_timeout_s,
            )
        except Exception as exc:
            _pipeline_log(
                "ERROR",
                f"ASR transcribe raised {type(exc).__name__}: {exc} "
                f"(converted to empty failure result)",
            )
            logger.error(f"ASR transcribe exception: {exc}", exc_info=True)
            error_label = (
                engine_label
                if str(engine_label).endswith("_error")
                else f"{engine_label}_error"
            )
            return (
                None,
                error_label,
                f"{type(exc).__name__}: {exc}",
                transcribe_engine,
            )

    def _transcribe_with_gpu_stall_fallback_inner(
        self,
        *,
        audio,
        transcribe_engine,
        engine_label: str,
        engine_reason: str,
        kind: str,
        pre_dsp_audio_level_avg: float,
        pre_dsp_audio_level_p95: float = -1.0,
        capture_mode: str | None = None,
        asr_timeout_s: float = 30.0,
    ):
        """Run ASR, retrying the same utterance on CPU if CUDA stalls under load."""
        import concurrent.futures

        if capture_mode is None:
            capture_mode = getattr(self, "_capture_mode", "standard")

        primary_cuda_candidate = (
            transcribe_engine is self.asr_engine
            and self._primary_asr_supports_gpu_fallback()
            and self._gpu_pressure_monitor is not None
            and self._gpu_fallback_retry_on_stall
        )
        try:
            audio_duration_s = len(audio) / 16000.0
        except Exception:
            audio_duration_s = 0.0
        max_cpu_s = max(2.0, float(self._gpu_fallback_max_segment_s or 6.0))
        allow_cpu_stall_retry = not (
            kind in ("soft_split", "final", "selection")
            and audio_duration_s > max_cpu_s
        )
        first_wait_s = asr_timeout_s
        if primary_cuda_candidate:
            first_wait_s = min(
                asr_timeout_s,
                max(0.05, self._gpu_fallback_primary_stall_timeout_s),
            )

        _tx_kwargs = {
            "pre_dsp_energy": pre_dsp_audio_level_avg,
            "pre_dsp_p95": pre_dsp_audio_level_p95,
            "capture_mode": capture_mode,
        }

        with self._asr_lock:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                future = executor.submit(
                    transcribe_engine.transcribe, audio, **_tx_kwargs
                )
                try:
                    return (
                        future.result(timeout=first_wait_s),
                        engine_label,
                        engine_reason,
                        transcribe_engine,
                    )
                except concurrent.futures.TimeoutError:
                    stall_switched = False
                    if primary_cuda_candidate:
                        pressure = self._gpu_pressure_monitor.sample(force=True)
                        if (
                            self._gpu_pressure_indicates_stall(pressure)
                            and allow_cpu_stall_retry
                        ):
                            stall_reason = (
                                pressure.reason
                                if pressure.busy
                                else f"{pressure.reason}: {pressure.error}"
                            )
                            _pipeline_log(
                                "GPU-FALLBACK",
                                "Primary CUDA ASR stalled; probing same "
                                f"{kind} CPU fallback ({stall_reason})",
                            )
                            print(
                                "[ASR] Primary CUDA stalled; checking CPU fallback: "
                                f"{stall_reason}"
                            )

                            fallback_engine = self._ensure_gpu_fallback_engine(
                                f"primary stall: {stall_reason}"
                            )
                            if fallback_engine is not None:
                                self._mark_primary_asr_timeout(first_wait_s)
                                self._mark_gpu_fallback_used("stall_retry")

                                # CRITICAL: do not wait for the stuck CUDA worker.
                                # It cannot be killed safely inside the process; its
                                # result is ignored, while this utterance is salvaged
                                # on the independent CPU fallback engine.
                                executor.shutdown(wait=False, cancel_futures=True)
                                executor = None

                                self._prepare_asr_engine_for_segment(
                                    fallback_engine,
                                    pre_dsp_audio_level_avg,
                                    pre_dsp_audio_level_p95,
                                )
                                fb_executor = concurrent.futures.ThreadPoolExecutor(
                                    max_workers=1
                                )
                                try:
                                    fb_future = fb_executor.submit(
                                        fallback_engine.transcribe,
                                        audio,
                                        **_tx_kwargs,
                                    )
                                    try:
                                        return (
                                            fb_future.result(timeout=asr_timeout_s),
                                            "cpu_fallback_stall_retry",
                                            stall_reason,
                                            fallback_engine,
                                        )
                                    except concurrent.futures.TimeoutError:
                                        _pipeline_log(
                                            "ERROR",
                                            "CPU fallback ASR timeout after "
                                            f"{asr_timeout_s:.0f}s",
                                        )
                                        return (
                                            None,
                                            "cpu_fallback_stall_retry_timeout",
                                            stall_reason,
                                            fallback_engine,
                                        )
                                finally:
                                    fb_executor.shutdown(
                                        wait=False, cancel_futures=True
                                    )

                            _pipeline_log(
                                "GPU-FALLBACK",
                                "CPU fallback unavailable after primary stall; "
                                "waiting for primary CUDA result",
                            )
                        elif self._gpu_pressure_indicates_stall(pressure):
                            _pipeline_log(
                                "GPU-FALLBACK",
                                "Primary CUDA stalled but segment is long "
                                f"({audio_duration_s:.1f}s > {max_cpu_s:.1f}s); "
                                "waiting for primary instead of CPU fallback",
                            )

                    if not stall_switched:
                        remaining_s = max(0.1, asr_timeout_s - first_wait_s)
                        try:
                            return (
                                future.result(timeout=remaining_s),
                                engine_label,
                                engine_reason,
                                transcribe_engine,
                            )
                        except concurrent.futures.TimeoutError:
                            print(
                                "[ASR] TIMEOUT: Transcription exceeded "
                                f"{asr_timeout_s:.0f}s, skipping"
                            )
                            _pipeline_log(
                                "ERROR",
                                f"ASR timeout after {asr_timeout_s:.0f}s",
                            )
                            if transcribe_engine is self.asr_engine:
                                self._mark_primary_asr_timeout(asr_timeout_s)
                            timeout_label = (
                                engine_label
                                if str(engine_label).endswith("_timeout")
                                else f"{engine_label}_timeout"
                            )
                            return (
                                None,
                                timeout_label,
                                engine_reason,
                                transcribe_engine,
                            )

                    return None, engine_label, engine_reason, transcribe_engine
            finally:
                # CRITICAL: shutdown(wait=False) prevents deadlock when
                # transcribe hangs — with-statement would call shutdown(wait=True)
                if executor is not None:
                    executor.shutdown(wait=False, cancel_futures=True)

    def _select_asr_engine_for_segment(
        self,
        kind: str,
        *,
        allow_sync_fallback_load: bool = True,
        audio_duration_s: float | None = None,
    ):
        """Choose the ASR engine for one segment.

        Returns (engine, label, reason).  `engine` can be None for interim
        calls when fallback is needed but still cold-loading.
        """
        if getattr(self, "_asr_hot_reload_in_progress", False):
            if kind == "interim":
                return None, "asr_hot_reloading", "ASR model is switching"
            _pipeline_log("ASR", f"Waiting for ASR hot-reload before {kind}")
            while getattr(self, "_asr_hot_reload_in_progress", False):
                thread = getattr(self, "_asr_hot_reload_thread", None)
                if thread and thread.is_alive():
                    thread.join(timeout=0.2)
                else:
                    time.sleep(0.05)

        primary = self.asr_engine
        if (
            not self._primary_asr_supports_gpu_fallback()
            or not self._gpu_pressure_monitor
        ):
            return primary, "primary", "fallback_disabled"

        now = time.time()
        force_fallback_reason = ""
        if now < self._primary_asr_suppressed_until:
            remain = self._primary_asr_suppressed_until - now
            force_fallback_reason = f"primary timeout recovery {remain:.0f}s"

        sample = None
        if not force_fallback_reason:
            sample = self._gpu_pressure_monitor.sample(force=(kind != "interim"))
            if sample.busy:
                force_fallback_reason = sample.reason

        if not force_fallback_reason:
            return primary, "primary", sample.reason if sample else "gpu_idle"

        if kind == "interim":
            # Interim subtitles are optional.  Do not run them on the CPU
            # fallback: if an interim CPU decode gets slow, it holds the small
            # model's internal lock and the real final/soft-split segment can
            # time out behind it, which looks like "swallowed" dictation.
            self._start_gpu_fallback_preload(force_fallback_reason)
            _pipeline_log(
                "GPU-FALLBACK",
                f"Skipping interim CPU fallback to reserve it for commit "
                f"segments ({force_fallback_reason})",
            )
            return None, "cpu_fallback_interim_skipped", force_fallback_reason

        # Do not route long committed chunks to the CPU rescue model.  The 0.6B
        # CPU path is useful for short rescue snippets, but field logs showed
        # 8-18s soft/final chunks can take 30s+ and then get dropped as empty.
        # For longer chunks, try the primary GPU path and let the normal stall
        # guard handle true CUDA hangs.
        try:
            duration_s = float(audio_duration_s or 0.0)
        except (TypeError, ValueError):
            duration_s = 0.0
        max_cpu_s = max(2.0, float(self._gpu_fallback_max_segment_s or 6.0))
        if kind in ("soft_split", "final", "selection") and duration_s > max_cpu_s:
            _pipeline_log(
                "GPU-FALLBACK",
                f"Long {kind} segment {duration_s:.1f}s > {max_cpu_s:.1f}s; "
                f"using primary instead of CPU fallback ({force_fallback_reason})",
            )
            return primary, "primary_long_segment", force_fallback_reason

        engine = self._loaded_gpu_fallback_engine()
        if engine is None:
            self._start_gpu_fallback_preload(force_fallback_reason)
            if allow_sync_fallback_load:
                engine = self._ensure_gpu_fallback_engine(force_fallback_reason)

        if engine is None:
            if not allow_sync_fallback_load:
                _pipeline_log(
                    "GPU-FALLBACK",
                    f"Fallback loading for {kind}; skipping primary ({force_fallback_reason})",
                )
                return None, "cpu_fallback_loading", force_fallback_reason
            _pipeline_log(
                "GPU-FALLBACK",
                f"Fallback unavailable for {kind}; using primary ({force_fallback_reason})",
            )
            return primary, "primary_fallback_unavailable", force_fallback_reason

        _pipeline_log(
            "GPU-FALLBACK",
            f"Using CPU fallback for {kind}: {force_fallback_reason}",
        )
        self._mark_gpu_fallback_used(f"{kind}: {force_fallback_reason}")
        return engine, "cpu_fallback", force_fallback_reason

    def _unload_gpu_fallback_engine(self, reason: str) -> None:
        self._cancel_gpu_fallback_unload_timer()
        lock = getattr(self, "_gpu_fallback_lock", None)
        if lock is None:
            engine = getattr(self, "_gpu_fallback_engine", None)
            self._gpu_fallback_engine = None
        else:
            with lock:
                engine = self._gpu_fallback_engine
                self._gpu_fallback_engine = None
        if engine and hasattr(engine, "unload"):
            try:
                engine.unload()
                _pipeline_log("GPU-FALLBACK", f"CPU fallback unloaded ({reason})")
            except Exception as exc:
                logger.warning(f"GPU fallback unload failed: {exc}")

    @staticmethod
    def _engine_config_value(engine, name: str, default):
        cfg = getattr(engine, "config", None)
        return getattr(cfg, name, default) if cfg is not None else default

    # ------------------------------------------------------------------
    # ASR final-segment rescue chain (self-heal reload + cloud second pass)
    # ------------------------------------------------------------------

    def _configure_asr_rescue(self, asr_cfg: dict) -> None:
        """Load the asr_rescue config block from hotwords.json."""
        block = dict((asr_cfg or {}).get("asr_rescue", {}) or {})
        try:
            from .core.utils.secrets import reveal_secret

            block["api_key"] = reveal_secret(str(block.get("api_key") or ""))
        except Exception:
            pass
        self._asr_rescue_cfg = RescueConfig.from_mapping(block)
        cfg = self._asr_rescue_cfg
        _pipeline_log(
            "RESCUE",
            f"asr_rescue: enabled={cfg.enabled}, cloud={cfg.cloud_enabled}, "
            f"key={'set' if cfg.api_key else 'empty'}, model={cfg.model}, "
            f"timeout={cfg.timeout_s:.0f}s, max_audio={cfg.max_audio_s:.0f}s",
        )

    def _gpu_probe_snapshot(self) -> dict | None:
        """Most recent GPU pressure sample as a plain dict (no new query)."""
        monitor = getattr(self, "_gpu_pressure_monitor", None)
        sample = getattr(monitor, "_last_sample", None) if monitor else None
        if sample is None:
            return None
        try:
            import dataclasses

            return dataclasses.asdict(sample)
        except Exception:
            return {"reason": str(getattr(sample, "reason", ""))}

    def _record_asr_failure(
        self,
        *,
        kind: str,
        failure: str,
        audio_duration_s: float,
        elapsed_ms: float,
        engine_label: str,
        transcribe_engine,
        rescue_outcome: str,
        session_id=None,
    ) -> None:
        """Append one diagnostic row to DebugLog/asr_failures.jsonl."""
        try:
            from .core.asr.failure_log import (
                append_failure_record,
                get_driver_version,
                get_process_private_mb,
            )

            append_failure_record(
                {
                    "event": "failure",
                    "session_id": session_id,
                    "kind": kind,
                    "failure": failure,
                    "audio_duration_s": round(float(audio_duration_s), 2),
                    "elapsed_ms": round(float(elapsed_ms), 0),
                    "engine": {
                        "label": str(engine_label),
                        "model": str(
                            self._engine_config_value(
                                transcribe_engine, "model_name", "unknown"
                            )
                        ),
                        "device": str(
                            getattr(transcribe_engine, "actual_device", None)
                            or self._engine_config_value(
                                transcribe_engine, "device", "unknown"
                            )
                        ),
                    },
                    "gpu_probe": self._gpu_probe_snapshot(),
                    "driver_version": get_driver_version(),
                    "process_private_mb": get_process_private_mb(),
                    "rescue_outcome": rescue_outcome,
                }
            )
        except Exception as exc:
            logger.warning(f"ASR failure telemetry write failed: {exc}")

    def _record_noise_gate_drop(
        self,
        *,
        gate: str,
        session_id,
        kind: str,
        duration_s: float,
        pre_dsp_avg: float,
        post_dsp_avg: float,
        pre_dsp_p95: float,
        energy_gate: float,
        vad_prob_avg: float,
        vad_prob_max: float,
        vad_voiced_ratio: float,
        capture_mode: str,
        buffered_chars: int = 0,
        event: str = "noise_gate_drop",
    ) -> None:
        """Telemetry row for a segment silently dropped by a pre-ASR gate.

        2026-07-19 slim-trial forensics found 5 gate drops with no trace
        outside pipeline_debug.log (14-day retention). asr_failures.jsonl is
        retention-exempt, so recording drops there builds the dataset needed
        to judge whether the thresholds over-kill on a given mic — observe
        first, tune later. Best-effort: must never raise into the worker.

        ``event`` distinguishes actual drops from segments the VAD exemption
        RESCUED past the energy gate ("noise_gate_exempt") so the exemption's
        hit quality can be reviewed from the same file.
        """
        try:
            from .core.asr.failure_log import append_failure_record

            append_failure_record(
                {
                    "event": str(event or "noise_gate_drop"),
                    "gate": gate,
                    "session_id": session_id,
                    "kind": kind,
                    "audio_duration_s": round(float(duration_s), 2),
                    "pre_dsp_avg": round(float(pre_dsp_avg), 5),
                    "post_dsp_avg": round(float(post_dsp_avg), 5),
                    "pre_dsp_p95": round(float(pre_dsp_p95), 5),
                    "energy_gate": float(energy_gate),
                    "vad_prob_avg": round(float(vad_prob_avg), 3),
                    "vad_prob_max": round(float(vad_prob_max), 3),
                    "vad_voiced_ratio": round(float(vad_voiced_ratio), 3),
                    "capture_mode": str(capture_mode),
                    "buffered_chars": int(buffered_chars),
                }
            )
        except Exception as exc:
            logger.warning(f"noise-gate telemetry write failed: {exc}")

    def _note_low_level_speech(self) -> None:
        """Track consecutive collapsed-mic utterances and warn the user once.

        Fires after 3 consecutive energy-gate exemptions (high-VAD speech far
        below the gate) — the signature of a system-level mic problem
        (endpoint volume knocked down by another app, device re-route). The
        segments themselves still decode via the exemption; this notice turns
        the previously silent degradation into actionable feedback. 10-minute
        cooldown so a long dictation session is not spammed.
        """
        import time

        self._low_level_speech_streak = (
            getattr(self, "_low_level_speech_streak", 0) + 1
        )
        if self._low_level_speech_streak < 3:
            return
        now = time.monotonic()
        if now - getattr(self, "_low_level_mic_warn_at", 0.0) < 600.0:
            return
        self._low_level_mic_warn_at = now
        self._emit_notice(
            "麦克风音量异常偏低，已自动增强识别；建议检查系统麦克风设置",
            "warning",
            5200,
        )

    def _record_hallucination_drop(
        self,
        *,
        reason: str,
        session_id,
        kind: str,
        duration_s: float,
        pre_dsp_avg: float,
        vad_prob_max: float,
        text_len: int,
        capture_mode: str,
        detail: str = "",
        avg_logprob: float | None = None,
        min_logprob: float | None = None,
    ) -> None:
        """Telemetry row for text dropped by a post-ASR anti-hallucination
        gate (template blacklist / decode-confidence gate).

        Same retention-exempt sink as the pre-ASR noise-gate drops so
        false-kill review reads one file. Records text LENGTH plus the gate
        detail (a blacklist template is generic, never user content) — raw
        transcript text stays out of telemetry by design. Best-effort: must
        never raise into the worker.
        """
        try:
            from .core.asr.failure_log import append_failure_record

            row = {
                "event": "hallucination_drop",
                "reason": reason,
                "session_id": session_id,
                "kind": kind,
                "audio_duration_s": round(float(duration_s), 2),
                "pre_dsp_avg": round(float(pre_dsp_avg), 5),
                "vad_prob_max": round(float(vad_prob_max), 3),
                "text_len": int(text_len),
                "capture_mode": str(capture_mode),
            }
            if detail:
                row["detail"] = str(detail)[:120]
            if avg_logprob is not None:
                row["avg_logprob"] = round(float(avg_logprob), 4)
            if min_logprob is not None:
                row["min_logprob"] = round(float(min_logprob), 4)
            append_failure_record(row)
        except Exception as exc:
            logger.warning(f"hallucination-drop telemetry write failed: {exc}")

    def _record_asr_rescue_outcome(self, session_id, outcome: str, detail: str = "") -> None:
        try:
            from .core.asr.failure_log import append_failure_record

            append_failure_record(
                {
                    "event": "rescue_result",
                    "session_id": session_id,
                    "rescue_outcome": outcome,
                    "detail": detail[:200],
                }
            )
        except Exception:
            pass

    def _emit_notice(self, message: str, level: str, duration_ms: int = 2200) -> None:
        """Leveled quiet notice near the ball; falls back to the error toast."""
        if not self._bridge:
            return
        if hasattr(self._bridge, "emit_notice"):
            self._bridge.emit_notice(message, level, duration_ms)
        else:
            self._bridge.emit_error(message)

    def _emit_draft(self, text: str, reason: str = "failed") -> None:
        """Open the local editable fallback without logging transcript text."""
        value = str(text or "")
        if not value.strip() or not self._bridge:
            return
        emitter = getattr(self._bridge, "emit_draft", None)
        if callable(emitter):
            emitter(value, str(reason or "failed"))

    def _insert_output_text(self, text: str, expected_target=None) -> tuple[bool, str]:
        """Run one output transaction and return a content-free result reason.

        Keeping exception handling and delivery-metadata lookup at this boundary
        prevents a stale previous transaction status from hiding an injector
        crash. Legacy injectors also retain their historical one-argument call
        shape when no target snapshot is available.
        """
        try:
            if expected_target is None:
                insert_ok = self.output_injector.insert_text(text)
            else:
                insert_ok = self.output_injector.insert_text(
                    text, expected_target=expected_target
                )
        except Exception as insert_exc:
            logger.error(
                f"Output transaction raised: {insert_exc}",
                exc_info=True,
            )
            return False, "transaction_exception"

        if insert_ok:
            return True, "inserted"

        delivery_reason = "failed"
        try:
            delivery_reason = (
                self.output_injector.get_last_delivery_metadata()
                .get("delivery", {})
                .get("status", "failed")
            )
        except Exception:
            pass
        return False, str(delivery_reason or "failed")

    def _refresh_explicit_correction_processor(self) -> None:
        """Publish the durable rule snapshot to Layer 2 without raw logging."""

        store = getattr(self, "_explicit_correction_store", None)
        processor = getattr(self, "hotword_processor", None)
        if store is None or processor is None:
            return
        updater = getattr(processor, "update_explicit_corrections", None)
        if not callable(updater):
            return
        rules = store.active_rules()
        lock = getattr(self, "_lock", None)
        if lock is None:
            updater(rules)
            return
        with lock:
            updater(rules)

    def get_explicit_correction_rules(self) -> list[dict]:
        """Return the local active rules for the user-owned management UI."""

        store = getattr(self, "_explicit_correction_store", None)
        if store is None:
            return []
        return [
            {
                "rule_id": rule.rule_id,
                "source": rule.source,
                "replacement": rule.replacement,
                "created_at": rule.created_at,
            }
            for rule in store.active_rules()
        ]

    def add_explicit_correction(
        self, source: str, replacement: str
    ) -> CorrectionMutationResult:
        store = getattr(self, "_explicit_correction_store", None)
        if store is None:
            return CorrectionMutationResult(False, "store_unavailable")
        result = store.add_rule(source, replacement)
        if result.success:
            self._refresh_explicit_correction_processor()
        return result

    def undo_last_explicit_correction(self) -> CorrectionMutationResult:
        store = getattr(self, "_explicit_correction_store", None)
        if store is None:
            return CorrectionMutationResult(False, "store_unavailable")
        result = store.undo_last()
        if result.success:
            self._refresh_explicit_correction_processor()
        return result

    def clear_explicit_correction(self, rule_id: str) -> CorrectionMutationResult:
        store = getattr(self, "_explicit_correction_store", None)
        if store is None:
            return CorrectionMutationResult(False, "store_unavailable")
        result = store.clear_rule(str(rule_id or ""))
        if result.success:
            self._refresh_explicit_correction_processor()
        return result

    @staticmethod
    def _voice_correction_failure_message(status: str) -> str:
        return {
            "syntax_invalid": "纠正指令不完整；请说“小助手纠正 A 为 B”",
            "syntax_ambiguous": "纠正指令有多个分隔位置，未保存；请给两边加引号后重试",
            "empty_operand": "纠正指令缺少原词或新词，未保存",
            "source_too_broad": "原词过短或过宽，未保存；请至少说两个字符的完整词语",
            "source_too_long": "原词过长，未保存；纠正规则只适合词语或短语",
            "replacement_too_long": "新词过长，未保存；纠正规则只适合词语或短语",
            "command_too_long": "纠正指令过长，未保存",
            "invalid_character": "纠正指令含不支持的控制字符，未保存",
            "outer_whitespace": "纠正词两端含不可见空白，未保存",
            "no_change": "原词与新词相同，本次未新增规则",
            "empty": "目前没有可撤销的纠正规则",
            "not_found": "这条纠正规则已不存在，请刷新后重试",
            "store_full": "本地纠正规则已达到容量上限，未保存",
            "load_failed": "本地纠正规则文件无法读取，未写入任何新规则",
            "persistence_failed": "本地纠正规则未能可靠保存，本次没有生效",
            "store_unavailable": "纠正规则组件尚未就绪，本次没有生效",
        }.get(status, "纠正规则未能保存，本次没有生效")

    def _try_execute_voice_correction(self, text: str):
        """Consume an explicit learn/undo/view command, or return None."""

        parsed = parse_voice_correction(text)
        if not parsed.recognized:
            return None
        success = False
        status = parsed.reason_code
        command = parsed.command
        if command is None:
            self._emit_notice(
                self._voice_correction_failure_message(status), "warning", 3000
            )
        elif command.action == "add":
            result = self.add_explicit_correction(
                command.source, command.replacement
            )
            success = result.success
            status = result.status
            if success:
                if status == "unchanged":
                    message = "这条纠正规则已经生效"
                elif status == "updated":
                    message = "已更新纠正规则；从下一次语音起生效"
                else:
                    message = "已记住纠正规则；从下一次语音起生效"
                self._emit_notice(message, "success", 2400)
            else:
                self._emit_notice(
                    self._voice_correction_failure_message(status), "warning", 3000
                )
        elif command.action == "undo":
            result = self.undo_last_explicit_correction()
            success = result.success
            status = result.status
            if success:
                message = "已撤销上一次纠正"
                if result.restored_rule is not None:
                    message += "；较早的同词规则已恢复"
                self._emit_notice(message, "success", 2400)
            else:
                self._emit_notice(
                    self._voice_correction_failure_message(status), "info", 2600
                )
        elif command.action == "view":
            emitter = getattr(
                getattr(self, "_bridge", None),
                "emit_correction_rules_requested",
                None,
            )
            success = callable(emitter)
            status = "opened" if success else "ui_unavailable"
            if success:
                emitter()
            else:
                self._emit_notice("请右键 Aria 打开“纠正规则”", "info", 2400)

        bridge = getattr(self, "_bridge", None)
        if bridge and hasattr(bridge, "emit_command"):
            bridge.emit_command("纠正规则", success)
        _pipeline_log("VOICE_CORRECTION", f"Result: {status}")
        return success

    def _clear_pending_voice_edit_choice(self):
        lock = getattr(self, "_session_lock", None)
        if lock is None:
            pending = getattr(self, "_pending_voice_edit_choice", None)
            self._pending_voice_edit_choice = None
            return pending
        with lock:
            pending = getattr(self, "_pending_voice_edit_choice", None)
            self._pending_voice_edit_choice = None
            return pending

    def _take_pending_voice_edit_choice(self, occurrence: int):
        """Atomically validate and consume one numbered pending edit."""

        lock = getattr(self, "_session_lock", None)
        now = time.monotonic()

        def take():
            pending = getattr(self, "_pending_voice_edit_choice", None)
            if pending is None:
                return None, "missing"
            if now > pending["expires_at"]:
                self._pending_voice_edit_choice = None
                return None, "expired"
            if occurrence > pending["match_count"]:
                return pending, "out_of_range"
            self._pending_voice_edit_choice = None
            return pending, "ready"

        if lock is None:
            return take()
        with lock:
            return take()

    def _clear_last_voice_edit_undo(self):
        lock = getattr(self, "_session_lock", None)
        if lock is None:
            receipt = getattr(self, "_last_voice_edit_undo", None)
            self._last_voice_edit_undo = None
            return receipt
        with lock:
            receipt = getattr(self, "_last_voice_edit_undo", None)
            self._last_voice_edit_undo = None
            return receipt

    def _remember_voice_edit_undo(
        self, result, source: str, replacement: str, expected_target
    ) -> None:
        """Publish one volatile undo receipt only after exact write readback."""

        if not bool(getattr(result, "success", False)):
            if bool(getattr(result, "partial_possible", False)):
                self._clear_last_voice_edit_undo()
            return
        token = getattr(result, "undo_token", None)
        if token is None or expected_target is None:
            self._clear_last_voice_edit_undo()
            return
        receipt = {
            "source": source,
            "replacement": replacement,
            "expected_target": expected_target,
            "undo_token": token,
            "expires_at": time.monotonic() + self._VOICE_EDIT_UNDO_TTL_S,
        }
        lock = getattr(self, "_session_lock", None)
        if lock is None:
            self._last_voice_edit_undo = receipt
            return
        with lock:
            self._last_voice_edit_undo = receipt

    def _take_last_voice_edit_undo(self):
        lock = getattr(self, "_session_lock", None)
        now = time.monotonic()

        def take():
            receipt = getattr(self, "_last_voice_edit_undo", None)
            self._last_voice_edit_undo = None
            if receipt is None:
                return None, "missing"
            if now > receipt["expires_at"]:
                return None, "expired"
            return receipt, "ready"

        if lock is None:
            return take()
        with lock:
            return take()

    def _restore_last_voice_edit_undo_if_empty(self, receipt) -> bool:
        if not receipt or time.monotonic() > receipt["expires_at"]:
            return False
        lock = getattr(self, "_session_lock", None)

        def restore():
            if getattr(self, "_last_voice_edit_undo", None) is not None:
                return False
            self._last_voice_edit_undo = receipt
            return True

        if lock is None:
            return restore()
        with lock:
            return restore()

    def _try_execute_voice_edit_undo(self, text: str):
        """Compensate the last still-identical native Aria edit."""

        from .core.editing import parse_voice_edit_undo
        from .system.standard_text_adapter import StandardTextEditStatus

        parsed = parse_voice_edit_undo(text)
        if not parsed.recognized:
            return None
        if parsed.command is None:
            self._emit_notice(
                "撤销指令不完整；请说“小助手撤销上一次编辑”",
                "warning",
                3000,
            )
            return False

        self._clear_pending_voice_edit_choice()
        with self._voice_edit_mutation_lock:
            receipt, receipt_status = self._take_last_voice_edit_undo()
            if receipt is None:
                message = (
                    "上一次编辑的安全撤销已过期"
                    if receipt_status == "expired"
                    else "目前没有可安全撤销的 Aria 精确编辑"
                )
                self._emit_notice(message, "warning", 2800)
                return False
            revert = getattr(
                self.output_injector, "revert_standard_text_edit", None
            )
            if not callable(revert):
                self._restore_last_voice_edit_undo_if_empty(receipt)
                self._emit_error("当前版本无法安全撤销编辑，本次未修改文字")
                return False
            result = revert(
                receipt["source"],
                receipt["replacement"],
                receipt["expected_target"],
                receipt["undo_token"],
            )

            success = bool(getattr(result, "success", False))
            status = getattr(result, "status", None)
            retryable = status in {
                StandardTextEditStatus.TARGET_UNAVAILABLE,
                StandardTextEditStatus.TARGET_CHANGED,
                StandardTextEditStatus.ELEVATION_REQUIRED,
                StandardTextEditStatus.TEXT_UNAVAILABLE,
                StandardTextEditStatus.SELECTION_UNAVAILABLE,
                StandardTextEditStatus.SELECTION_FAILED,
                StandardTextEditStatus.WRITE_REJECTED,
            }
            restored = bool(
                not success
                and retryable
                and self._restore_last_voice_edit_undo_if_empty(receipt)
            )

        if success:
            self._emit_notice(
                "已撤销上一次 Aria 精确编辑；可用 Ctrl+Z 重做",
                "success",
                2600,
            )
        else:
            messages = {
                StandardTextEditStatus.TARGET_CHANGED: (
                    "请回到原输入框后重试撤销" if restored else "目标已变化，未撤销"
                ),
                StandardTextEditStatus.CONTENT_CHANGED: (
                    "文字已在编辑后发生变化，为避免误删，本次未撤销"
                ),
                StandardTextEditStatus.WRITE_PARTIAL: (
                    "撤销结果未确认；请检查目标，可能需要 Ctrl+Z"
                ),
            }
            self._emit_error(messages.get(status, "无法安全撤销上一次编辑"))
        bridge = getattr(self, "_bridge", None)
        if bridge and hasattr(bridge, "emit_command"):
            bridge.emit_command("撤销编辑", success)
        status_value = getattr(status, "value", "unknown")
        _pipeline_log("VOICE_EDIT_UNDO", f"Result: {status_value}")
        return success

    def _try_execute_voice_edit_choice(self, text: str):
        """Consume the numbered second phase of an ambiguous exact edit."""

        from .core.editing import parse_voice_edit_choice
        from .system.standard_text_adapter import StandardTextEditStatus

        parsed = parse_voice_edit_choice(text)
        if not parsed.recognized:
            return None
        command = parsed.command
        if command is None:
            self._emit_notice(
                "编号无效；请说“小助手替换第 2 处”，最多支持 20 处",
                "warning",
                3000,
            )
            return False
        if command.action == "cancel":
            existed = self._clear_pending_voice_edit_choice() is not None
            self._emit_notice(
                "已取消这次替换" if existed else "目前没有待确认的替换",
                "success" if existed else "info",
                2200,
            )
            return existed

        pending, pending_status = self._take_pending_voice_edit_choice(
            command.occurrence
        )
        if pending is None:
            message = (
                "替换确认已过期，请重新说完整编辑指令"
                if pending_status == "expired"
                else "目前没有待确认的多处替换"
            )
            self._emit_notice(message, "warning", 2800)
            return False
        if pending_status == "out_of_range":
            self._emit_notice(
                f"只有 {pending['match_count']} 处，请重新选择编号",
                "warning",
                2600,
            )
            return False

        apply_choice = getattr(
            self.output_injector, "apply_standard_text_edit_candidate", None
        )
        if not callable(apply_choice):
            self._emit_error("当前版本无法完成编号替换，本次未修改文字")
            return False

        # The pending command was atomically consumed before the mutation edge.
        # Partial/unknown results must never be retried from that command.
        with self._voice_edit_mutation_lock:
            result = apply_choice(
                pending["source"],
                pending["replacement"],
                pending["expected_target"],
                pending["candidate_token"],
                command.occurrence,
            )
            self._remember_voice_edit_undo(
                result,
                pending["source"],
                pending["replacement"],
                pending["expected_target"],
            )
        success = bool(getattr(result, "success", False))
        status = getattr(result, "status", None)
        if success:
            self._emit_notice(
                f"已替换第 {command.occurrence} 处，可用 Ctrl+Z 撤销",
                "success",
                2400,
            )
        else:
            messages = {
                StandardTextEditStatus.TARGET_CHANGED: "目标已变化，未执行编号替换",
                StandardTextEditStatus.CONTENT_CHANGED: "文字内容已变化，未执行编号替换",
                StandardTextEditStatus.WRITE_PARTIAL: (
                    "编号替换结果未确认；请检查目标，可能需要 Ctrl+Z"
                ),
            }
            self._emit_error(messages.get(status, "无法安全完成编号替换，本次未修改"))
        bridge = getattr(self, "_bridge", None)
        if bridge and hasattr(bridge, "emit_command"):
            bridge.emit_command("编辑文本", success)
        status_value = getattr(status, "value", "unknown")
        _pipeline_log("VOICE_EDIT_CHOICE", f"Result: {status_value}")
        return success

    def _record_recent_voice_insert(
        self,
        text: str,
        expected_target,
        session_id: int,
        *,
        audio_seconds: float = 0.0,
    ) -> None:
        """Remember one successful ordinary dictation with a native range receipt."""

        now = time.monotonic()
        with self._session_lock:
            windows = getattr(self, "_session_voice_windows", {})
            window = windows.pop(session_id, None)
            getattr(self, "_session_voice_started_at", {}).pop(session_id, None)
        if window is None:
            duration = max(0.0, min(float(audio_seconds or 0.0), 180.0))
            window = (now - duration, now)

        captured_target = expected_target
        capture_status = "unsupported"
        capture = getattr(
            self.output_injector, "capture_recent_voice_insert", None
        )
        if callable(capture) and expected_target is not None:
            try:
                receipt = capture(text, expected_target)
            except Exception:
                receipt = None
            if receipt is not None:
                capture_status = str(
                    getattr(getattr(receipt, "status", None), "value", "failed")
                )
                if bool(getattr(receipt, "success", False)) and getattr(
                    receipt, "target", None
                ) is not None:
                    captured_target = receipt.target

        tracker = getattr(self, "_recent_voice_groups", None)
        if tracker is None:
            tracker = self._recent_voice_groups = RecentVoiceGroupTracker()
        tracker.record(
            text,
            captured_target,
            session_id=session_id,
            voice_start=window[0],
            voice_end=window[1],
            inserted_at=now,
        )
        _pipeline_log(
            "RECENT_VOICE",
            f"Recorded successful dictation: chars={len(text)}, "
            f"range={capture_status}",
        )

    def _try_execute_recent_voice_command(
        self, text: str, *, trace_id: str | None = None
    ):
        """Ask AI to review or safely rewrite the latest dictated passage."""

        detector = getattr(self, "wakeword_detector", None)
        active_wakewords = None
        if detector is not None:
            active_wakewords = (
                getattr(detector, "wakeword", ""),
                *tuple(getattr(detector, "wakeword_aliases", ()) or ()),
            )
        parsed = parse_recent_voice_command(text, wakewords=active_wakewords)
        if not parsed.recognized:
            return None
        command = parsed.command
        if command is None:
            if parsed.reason_code == "recent_voice_same_replacement":
                self._emit_notice(
                    "原词和新词被识别成了相同内容，本次没有修改；请重新说清楚新词",
                    "warning",
                    4600,
                )
            else:
                self._emit_notice("修改要求没有识别完整，本次没有修改", "warning", 3600)
            if self._bridge and hasattr(self._bridge, "emit_command"):
                self._bridge.emit_command("修改刚才语音", False)
            _pipeline_log(
                "RECENT_VOICE",
                f"Command refused: parser={parsed.reason_code}",
            )
            return False

        return self._execute_recent_voice_request(
            command.instruction,
            rewrite=command.mode == RecentVoiceCommandMode.REWRITE,
            trace_id=trace_id,
        )

    def _execute_recent_voice_request(
        self,
        instruction: str,
        *,
        rewrite: bool,
        trace_id: str | None = None,
    ) -> bool:
        """Apply one AI request only to the latest safely tracked dictation."""

        from .core.ai.feedback import describe_ai_error, describe_delivery_status

        tracker = getattr(self, "_recent_voice_groups", None)
        group_result = tracker.latest(now=time.monotonic()) if tracker else None
        group = getattr(group_result, "group", None)
        status = getattr(group_result, "status", "empty")
        if group is None:
            messages = {
                "expired": "刚才输入的内容已超过引用时间，请重新说一遍再修改",
                "too_large": "刚才连续输入的内容过长，未截断处理；请先选中需要修改的部分",
            }
            self._emit_notice(
                messages.get(status, "还没有可引用的最近语音输入"),
                "warning",
                4200,
            )
            if self._bridge and hasattr(self._bridge, "emit_command"):
                self._bridge.emit_command("修改刚才语音", False)
            _pipeline_log("RECENT_VOICE", f"Command refused: group={status}")
            return False

        processor = getattr(self, "selection_processor", None)
        if processor is None:
            self._emit_notice("AI 修改服务尚未就绪，本次未改动文字", "error", 3600)
            return False
        self._emit_notice(
            "正在分析刚才输入的整段文字…",
            "info",
            2600,
        )
        result = processor.process_recent_voice(
            group.text,
            str(instruction or "").strip(),
            rewrite=rewrite,
            trace_id=trace_id,
        )
        if not result.success:
            self._emit_notice(
                describe_ai_error(getattr(result, "error_category", None)),
                "error",
                3600,
            )
            _pipeline_log(
                "RECENT_VOICE",
                "AI processing failed: "
                f"category={getattr(result, 'error_category', None) or '-'}",
            )
            return False

        feedback = str(result.feedback or "").strip()
        if not rewrite:
            self._emit_draft(feedback, "recent_voice_advice")
            self._emit_notice(
                feedback[:90] or "分析完成",
                "info",
                7000,
            )
            if self._bridge and hasattr(self._bridge, "emit_command"):
                self._bridge.emit_command("分析刚才语音", True)
            _pipeline_log("RECENT_VOICE", "Advice ready")
            return True

        revised = str(result.revised_text or "").strip()
        if not revised:
            self._emit_notice("AI 没有返回可替换的完整文本", "error", 3600)
            return False
        if not group.addressable:
            # Qt/Electron/custom composers intentionally do not block the
            # dictation hot path while the clipboard's asynchronous restore is
            # settling.  Re-probe the still-focused field here, using every
            # tracked segment boundary so Ctrl+Z can later be verified one
            # exact Aria paste at a time.
            capture_group = getattr(
                self.output_injector, "capture_recent_voice_group", None
            )
            if callable(capture_group):
                try:
                    late_receipt = capture_group(
                        tuple(item.text for item in group.segments),
                        group.target,
                    )
                except Exception:
                    late_receipt = None
                if (
                    late_receipt is not None
                    and bool(getattr(late_receipt, "success", False))
                    and getattr(late_receipt, "target", None) is not None
                ):
                    group = dataclass_replace(
                        group,
                        target=late_receipt.target,
                        addressable=True,
                    )
                    _pipeline_log(
                        "RECENT_VOICE",
                        "Custom field range captured lazily before rewrite",
                    )
        if not group.addressable:
            self._emit_draft(revised, "recent_voice_unaddressable")
            self._emit_notice(
                "当前载体无法安全回读原文，本次未自动替换",
                "warning",
                5200,
            )
            _pipeline_log("RECENT_VOICE", "Rewrite drafted: unaddressable range")
            return False

        replace_range = getattr(
            self.output_injector, "replace_recent_voice_range", None
        )
        if not callable(replace_range):
            self._emit_draft(revised, "recent_voice_adapter_unavailable")
            self._emit_notice("当前应用暂不支持安全替换，本次未改动文字", "warning", 4200)
            return False

        with self._voice_edit_mutation_lock:
            delivery = replace_range(revised, group.text, group.target)
            self._remember_voice_edit_undo(
                delivery,
                group.text,
                revised,
                group.target,
            )
        success = bool(getattr(delivery, "success", False))
        delivery_status = str(
            getattr(getattr(delivery, "status", None), "value", "unknown")
        )
        delivery_transport = str(getattr(delivery, "transport", "") or "")
        undo_available = bool(getattr(delivery, "undo_available", False))
        if success:
            if delivery_status != "no_change":
                capture = getattr(
                    self.output_injector, "capture_recent_voice_insert", None
                )
                next_receipt = None
                if callable(capture):
                    try:
                        next_receipt = capture(revised, group.target)
                    except Exception:
                        next_receipt = None
                if (
                    next_receipt is not None
                    and bool(getattr(next_receipt, "success", False))
                    and getattr(next_receipt, "target", None) is not None
                ):
                    tracker.replace_group(
                        group,
                        revised,
                        next_receipt.target,
                        inserted_at=time.monotonic(),
                    )
                else:
                    tracker.clear()
            notice = feedback[:76] if feedback else "已按要求修改"
            if delivery_status == "no_change":
                suffix = "（原文无需改动）"
            elif delivery_transport == "terminal_tail_replace":
                suffix = "（已在终端中核对后直接替换；建议确认）"
            elif undo_available:
                suffix = "（整段已替换，可 Ctrl+Z 撤销）"
            else:
                suffix = "（整段已替换）"
            self._emit_notice(f"{notice}{suffix}", "success", 7000)
        else:
            self._emit_draft(revised, f"recent_voice_{delivery_status}")
            if bool(getattr(delivery, "partial_possible", False)):
                message = describe_delivery_status(
                    "write_partial",
                    default=(
                        "写入状态未确认，请先检查原文；为避免重复输入，未再次写入"
                    ),
                )
            else:
                message = describe_delivery_status(
                    getattr(delivery, "status", None),
                    default="原文范围或目标已变化，本次未自动替换",
                )
            self._emit_notice(message, "warning", 6000)
        if self._bridge and hasattr(self._bridge, "emit_command"):
            self._bridge.emit_command("修改刚才语音", success)
        _pipeline_log(
            "RECENT_VOICE",
            f"Rewrite result: {delivery_status}, success={success}, "
            f"chars={len(revised)}",
        )
        return success

    def _try_execute_wakeword_ai_fallback(
        self, question: str, *, trace_id: str | None = None
    ) -> bool:
        """Treat an unmatched wakeword request as a recent-dictation rewrite.

        The generic route never opens the AI chat window and never captures the
        selection, screen text or document metadata.  Only the latest safely
        tracked voice group is eligible for replacement.
        """

        question = str(question or "").strip()
        if not question:
            self._emit_notice("请在唤醒词后继续说你的要求", "info", 2600)
            return False

        success = self._execute_recent_voice_request(
            question, rewrite=True, trace_id=trace_id
        )
        _pipeline_log(
            "AI_ROUTE",
            f"Wakeword fallback used recent rewrite: chars={len(question)}, "
            f"success={success}",
        )
        return success

    def _emit_route_decision(self, decision: RouteDecision) -> RouteDecision:
        """Persist one always-on route summary and return the same decision."""

        try:
            write_route_decision(decision)
        except Exception:
            pass
        return decision

    def _dispatch_final_command(
        self,
        session_id,
        text: str,
        prior_buffered_text: str,
        final_only_text: str,
    ):
        """Run the final-utterance command layers; None means continue dictation.

        Behavior is byte-equivalent to the former inline block in ``_asr_worker``.
        A returned ``RouteDecision`` with ``final_text is None`` (and/or
        ``inserted is None``) means the caller must not rewrite that variable —
        matching the original bare ``continue`` in light-sleep.
        """

        text_len = len(text or "")

        fast_handled, fast_cmd_id, fast_success = (
            self._fast_wakeword_already_handled(session_id)
        )
        if fast_handled:
            if text and not self._fast_wakeword_final_is_still_command(
                text, fast_cmd_id
            ):
                _pipeline_log(
                    "WAKEWORD",
                    "Fast command was already executed, but final text is "
                    f"not command-only; allowing dictation: {fast_cmd_id} "
                    f"text='{text[:60]}'",
                )
                self._clear_fast_wakeword_state("final-normal-text")
            else:
                self._clear_fast_wakeword_state("final-consumed")
                status = "OK" if fast_success else "FAIL"
                _pipeline_log(
                    "WAKEWORD",
                    f"Final suppressed after fast command {status}: "
                    f"{fast_cmd_id} text='{text[:60]}'",
                )
                return self._emit_route_decision(
                    RouteDecision(
                        session_id=session_id,
                        stage="fast_wakeword",
                        reason="fast_consumed",
                        consumed=True,
                        inserted=fast_success,
                        final_text=f"[唤醒词] {fast_cmd_id}",
                        command_id=str(fast_cmd_id or "") or None,
                        text_len=text_len,
                    )
                )

        # Extract the configured wakeword once with the detector's normal
        # pinyin tolerance.  The canonical form lets downstream parsers
        # share one route even when ASR writes a homophonic wakeword.
        _route_invocation = None
        _route_command_text = ""
        _route_canonical_text = text
        _route_compat_text = text
        _deferred_wakeword_result = None
        if text and self.wakeword_detector:
            _extract_invocation = getattr(
                self.wakeword_detector, "extract_invocation", None
            )
            if callable(_extract_invocation):
                _route_invocation = _extract_invocation(text)
                if (
                    not _route_invocation
                    and prior_buffered_text
                    and final_only_text
                    and final_only_text != text
                ):
                    _route_invocation = _extract_invocation(final_only_text)
                    if _route_invocation:
                        _pipeline_log(
                            "WAKEWORD",
                            "Tail invocation matched for unified command routing",
                        )
            if _route_invocation:
                _route_command_text = str(_route_invocation[1] or "").strip()
                _primary_wakeword = str(
                    getattr(self.wakeword_detector, "wakeword", "")
                    or "小助手"
                ).strip()
                _route_canonical_text = (
                    f"{_primary_wakeword}，{_route_command_text}"
                )
                # The deterministic editing parsers intentionally use the
                # public compatibility prefix rather than personal config.
                _route_compat_text = f"小助手，{_route_command_text}"

        # === Layer -1: Wakeword Detection (app-level commands via "小助手") ===
        # Check for wakeword to control app settings (auto-send, etc.).
        # For multi-segment final commits, try the joined text first; if that
        # fails try the final-only tail as fallback — fillers/cross-segment
        # noise in the joined pinyin can occasionally defeat the matcher
        # while the tail alone is clean.
        if text and self.wakeword_detector and self.wakeword_executor:
            wakeword_result = self.wakeword_detector.detect(text)
            if (
                not wakeword_result
                and prior_buffered_text
                and final_only_text
                and final_only_text != text
            ):
                _tail_result = self.wakeword_detector.detect(final_only_text)
                if _tail_result:
                    _pipeline_log(
                        "WAKEWORD",
                        f"Tail fallback matched (joined miss): tail='{final_only_text[:40]}'",
                    )
                    wakeword_result = _tail_result
            if wakeword_result and str(wakeword_result[1] or "") == "ask_ai":
                if self._wakeword_result_is_explicit_ai_chat(wakeword_result):
                    success, cmd_id, response = (
                        self._execute_final_wakeword_result(
                            wakeword_result, text
                        )
                    )
                    return self._emit_route_decision(
                        RouteDecision(
                            session_id=session_id,
                            stage="wakeword",
                            reason="explicit_ai_chat",
                            consumed=True,
                            inserted=success,
                            final_text=(
                                f"[唤醒词] {response}"
                                if response
                                else f"[唤醒词] {cmd_id}"
                            ),
                            command_id=str(cmd_id or "") or None,
                            text_len=text_len,
                            detail={"invocation": 1},
                        )
                    )
                _pipeline_log(
                    "AI_ROUTE",
                    "Suppressed broad ask_ai match; trying recent rewrite",
                )
            elif wakeword_result and self._wakeword_result_uses_contextual_ai(
                wakeword_result
            ):
                # A recent-passage request is more specific than legacy
                # selected-text AI commands. Defer only those AI actions;
                # reminders, app control, launchers and other deterministic
                # side effects still execute immediately.
                _deferred_wakeword_result = wakeword_result
            elif wakeword_result:
                success, cmd_id, response = self._execute_final_wakeword_result(
                    wakeword_result, text
                )
                return self._emit_route_decision(
                    RouteDecision(
                        session_id=session_id,
                        stage="wakeword",
                        reason=str(cmd_id or "wakeword_executed"),
                        consumed=True,
                        inserted=success,
                        final_text=(
                            f"[唤醒词] {response}" if response else f"[唤醒词] {cmd_id}"
                        ),
                        command_id=str(cmd_id or "") or None,
                        text_len=text_len,
                        detail={"invocation": 1},
                    )
                )

        # === Light Sleeping Mode Check ===
        # If light sleeping, ignore all input (wakeword already handled above)
        with self._lock:
            is_light_sleeping = self._sleep_mode == SleepMode.LIGHT
        if is_light_sleeping:
            print(f"[SLEEPING] Ignoring input: '{text[:50]}...'")
            return self._emit_route_decision(
                RouteDecision(
                    session_id=session_id,
                    stage="sleep",
                    reason="light_sleep",
                    consumed=True,
                    inserted=None,
                    final_text=None,
                    text_len=text_len,
                )
            )

        # === Layer 0: Voice Command Detection (BEFORE any processing) ===
        # Check raw ASR text for commands to achieve lowest latency.
        # Tail fallback: command detector requires the text
        # to START with the prefix "小助手", so a joined multi-segment text
        # will always fail if the user spoke something before "小助手". Try
        # the final-only tail as secondary in that case.
        if text and self.command_detector and self.command_executor:
            _command_input = text
            if _route_invocation:
                _command_prefix = str(
                    getattr(self.command_detector, "prefix", "") or "小助手"
                ).strip()
                _command_input = f"{_command_prefix}，{_route_command_text}"
            cmd_id = self.command_detector.detect(_command_input)
            if (
                not cmd_id
                and not _route_invocation
                and prior_buffered_text
                and final_only_text
                and final_only_text != text
            ):
                _tail_cmd_id = self.command_detector.detect(final_only_text)
                if _tail_cmd_id:
                    _pipeline_log(
                        "CMD",
                        f"Tail fallback matched (joined miss): tail='{final_only_text[:40]}'",
                    )
                    cmd_id = _tail_cmd_id
            if cmd_id:
                # Execute command immediately, skip all processing layers
                success = self.command_executor.execute(cmd_id)
                status = "OK" if success else "FAIL"
                print(f"[CMD] {status}: {cmd_id} (raw ASR: '{text}')")
                # Notify UI about command execution
                if self._bridge and hasattr(self._bridge, "emit_command"):
                    self._bridge.emit_command(cmd_id, success)
                return self._emit_route_decision(
                    RouteDecision(
                        session_id=session_id,
                        stage="command",
                        reason=str(cmd_id),
                        consumed=True,
                        inserted=success,
                        final_text=f"[命令] {cmd_id}",
                        command_id=str(cmd_id),
                        text_len=text_len,
                    )
                )

        # Explicit correction learning is a separate raw-ASR command
        # layer.  It records a durable future-ASR rule but never edits
        # the current target.  Recognized failures are consumed so the
        # command itself cannot fall through into dictation.
        if text:
            voice_correction_result = self._try_execute_voice_correction(
                _route_compat_text
            )
            if voice_correction_result is not None:
                return self._emit_route_decision(
                    RouteDecision(
                        session_id=session_id,
                        stage="voice_correction",
                        reason="voice_correction",
                        consumed=True,
                        inserted=voice_correction_result,
                        final_text="[命令] voice_correction",
                        command_id="voice_correction",
                        text_len=text_len,
                    )
                )

        if text:
            voice_edit_undo_result = self._try_execute_voice_edit_undo(
                _route_compat_text
            )
            if voice_edit_undo_result is not None:
                return self._emit_route_decision(
                    RouteDecision(
                        session_id=session_id,
                        stage="voice_edit_undo",
                        reason="voice_edit_undo",
                        consumed=True,
                        inserted=voice_edit_undo_result,
                        final_text="[命令] voice_edit_undo",
                        command_id="voice_edit_undo",
                        text_len=text_len,
                    )
                )

        if text:
            voice_edit_choice_result = self._try_execute_voice_edit_choice(
                _route_compat_text
            )
            if voice_edit_choice_result is not None:
                return self._emit_route_decision(
                    RouteDecision(
                        session_id=session_id,
                        stage="voice_edit_choice",
                        reason="voice_edit_choice",
                        consumed=True,
                        inserted=voice_edit_choice_result,
                        final_text="[命令] voice_edit_choice",
                        command_id="voice_edit_choice",
                        text_len=text_len,
                    )
                )

        # Deterministic voice editing is intentionally checked on raw
        # ASR before hotword/fuzzy/LLM rewriting. A recognized edit
        # command is always consumed, including safe rejections, so it
        # can never fall through and dictate the command itself.
        if text:
            voice_edit_result = self._try_execute_voice_edit(
                _route_compat_text, session_id
            )
            if voice_edit_result is not None:
                from .core.editing import parse_voice_edit

                _ve_parsed = parse_voice_edit(_route_compat_text)
                _ve_reason = str(
                    getattr(_ve_parsed, "reason_code", None) or "voice_edit"
                )
                return self._emit_route_decision(
                    RouteDecision(
                        session_id=session_id,
                        stage="voice_edit",
                        reason=_ve_reason,
                        consumed=True,
                        inserted=voice_edit_result,
                        final_text="[命令] voice_edit",
                        command_id="voice_edit",
                        text_len=text_len,
                    )
                )

        # Explicit requests about the latest dictated passage use its
        # verified range as the only AI context. They run after every
        # deterministic command/edit, but before legacy selection AI.
        if text:
            recent_voice_result = self._try_execute_recent_voice_command(
                _route_canonical_text,
                trace_id=str(session_id),
            )
            if recent_voice_result is not None:
                detector = getattr(self, "wakeword_detector", None)
                active_wakewords = None
                if detector is not None:
                    active_wakewords = (
                        getattr(detector, "wakeword", ""),
                        *tuple(getattr(detector, "wakeword_aliases", ()) or ()),
                    )
                _rv_parsed = parse_recent_voice_command(
                    _route_canonical_text, wakewords=active_wakewords
                )
                _rv_reason = str(
                    getattr(_rv_parsed, "reason_code", None)
                    or "recent_voice_command"
                )
                return self._emit_route_decision(
                    RouteDecision(
                        session_id=session_id,
                        stage="recent_voice",
                        reason=_rv_reason,
                        consumed=True,
                        inserted=recent_voice_result,
                        final_text="[命令] recent_voice",
                        command_id="recent_voice",
                        text_len=text_len,
                    )
                )

        # Preserve explicit independent selected-text tools such as translation
        # or summary after the more specific recent-passage route declined.
        if _deferred_wakeword_result is not None:
            if self._wakeword_result_is_explicit_contextual_request(
                _deferred_wakeword_result
            ):
                success, cmd_id, response = self._execute_final_wakeword_result(
                    _deferred_wakeword_result, text
                )
                return self._emit_route_decision(
                    RouteDecision(
                        session_id=session_id,
                        stage="deferred_selection",
                        reason=str(cmd_id or "deferred_selection"),
                        consumed=True,
                        inserted=success,
                        final_text=(
                            f"[唤醒词] {response}"
                            if response
                            else f"[唤醒词] {cmd_id}"
                        ),
                        command_id=str(cmd_id or "") or None,
                        text_len=text_len,
                        detail={"invocation": 1},
                    )
                )
            _pipeline_log(
                "AI_ROUTE",
                "Suppressed contextual trigger embedded in natural request; "
                "trying recent rewrite",
            )

        # Product default: every remaining non-empty wakeword request is
        # an AI prompt. It is always consumed, including setup/network/UI
        # failures, so the spoken instruction cannot become dictation.
        if _route_invocation:
            inserted = self._try_execute_wakeword_ai_fallback(
                _route_command_text,
                trace_id=str(session_id),
            )
            return self._emit_route_decision(
                RouteDecision(
                    session_id=session_id,
                    stage="wakeword_ai_fallback",
                    reason="wakeword_ai_fallback",
                    consumed=True,
                    inserted=inserted,
                    final_text="[命令] wakeword_ai",
                    command_id="wakeword_ai",
                    text_len=text_len,
                    detail={"invocation": 1},
                )
            )

        return None

    def _target_surface_is_terminal(self, expected_target) -> bool:
        """Read-only terminal classification for the pending edit target."""

        if expected_target is None:
            return False
        profile = getattr(expected_target, "profile", None)
        if profile is None:
            return False
        classified = classify_target_surface(
            process_name=str(getattr(profile, "process_name", "") or ""),
            window_class=str(getattr(profile, "window_class", "") or ""),
            focused_class=str(getattr(profile, "focused_class", "") or ""),
            explicit_kind=getattr(profile, "kind", None),
        )
        return getattr(classified, "kind", None) == SurfaceKind.TERMINAL

    def _should_yield_voice_edit_to_recent_on_terminal(self, expected_target) -> bool:
        """True when a terminal A→B edit should use the recent-voice rewrite path."""

        if not self._target_surface_is_terminal(expected_target):
            return False
        tracker = getattr(self, "_recent_voice_groups", None)
        if tracker is None:
            return False
        group_result = tracker.latest(now=time.monotonic())
        group = getattr(group_result, "group", None)
        status = getattr(group_result, "status", "empty")
        return (
            status == "ready"
            and group is not None
            and bool(getattr(group, "addressable", False))
        )

    def _try_execute_voice_edit(self, text: str, session_id: int):
        """Execute an explicit deterministic edit, or return None for dictation.

        The adapter and delivery metadata remain content-free; existing ASR
        history/privacy settings still govern the raw spoken command.
        """
        from .core.editing import parse_voice_edit

        # A request scoped to the latest dictated passage is semantic context,
        # not a literal source operand.  Let the immediately following recent-
        # voice route interpret it with DeepSeek and use that passage's verified
        # range (including the terminal tail carrier).  Plain ``把 A 改成 B``
        # commands still stay on the deterministic native editor below.
        if parse_recent_voice_command(text).recognized:
            return None

        parsed = parse_voice_edit(text)
        if not parsed.recognized:
            return None

        with self._session_lock:
            expected_target = getattr(
                self, "_session_output_targets", {}
            ).get(session_id)

        # Ordinary edits are single-phase and replace all safe exact matches.
        # Any older explicit numbered workflow must not survive a new command.
        self._clear_pending_voice_edit_choice()

        if parsed.command is None:
            reject = getattr(self.output_injector, "reject_voice_edit", None)
            if callable(reject):
                reject(parsed.reason_code, expected_target)
            message = {
                "voice_correction_not_enabled": (
                    "纠正指令未通过安全解析，本次未保存规则；请说“小助手纠正 A 为 B”"
                ),
                "voice_edit_syntax_ambiguous": (
                    "编辑指令有多个可能的分隔位置，未修改；请换成“小助手把 A 改成 B”"
                ),
                "voice_edit_no_change": "原文和替换文字相同，本次未修改",
            }.get(parsed.reason_code, "编辑指令不完整，本次未修改文字")
            self._emit_error(message)
            if self._bridge and hasattr(self._bridge, "emit_command"):
                self._bridge.emit_command("编辑文本", False)
            _pipeline_log("VOICE_EDIT", f"Rejected: {parsed.reason_code}")
            return False

        # Terminal surfaces cannot host the native scalpel. When a verified
        # recent dictation receipt is ready, rewrite that passage with the
        # original A→B instruction instead of claiming unsupported_surface.
        # Same-word rejection above still wins; non-terminal behavior is unchanged.
        if self._should_yield_voice_edit_to_recent_on_terminal(expected_target):
            instruction = (
                f"把{parsed.command.source}改成{parsed.command.replacement}"
            )
            success = self._execute_recent_voice_request(
                instruction, rewrite=True, trace_id=str(session_id)
            )
            _pipeline_log(
                "VOICE_EDIT",
                "Terminal yield to recent rewrite: "
                f"success={success}, chars={len(instruction)}",
            )
            return success

        apply_edit = getattr(
            self.output_injector, "apply_standard_text_edit", None
        )
        if not callable(apply_edit):
            reject = getattr(self.output_injector, "reject_voice_edit", None)
            if callable(reject):
                reject("voice_edit_adapter_unavailable", expected_target)
            self._emit_error("当前版本无法执行精确编辑，本次未修改文字")
            if self._bridge and hasattr(self._bridge, "emit_command"):
                self._bridge.emit_command("编辑文本", False)
            return False

        with self._voice_edit_mutation_lock:
            result = apply_edit(
                parsed.command.source,
                parsed.command.replacement,
                expected_target,
            )
            self._remember_voice_edit_undo(
                result,
                parsed.command.source,
                parsed.command.replacement,
                expected_target,
            )
        success = bool(getattr(result, "success", False))
        status = getattr(result, "status", None)
        match_count = max(0, int(getattr(result, "match_count", 0) or 0))
        if not success:
            from .core.ai.feedback import describe_voice_edit_status

            self._emit_error(describe_voice_edit_status(status))
        else:
            notice = (
                f"已一次替换 {match_count} 处，可用 Ctrl+Z 撤销"
                if match_count > 1
                else "已完成精确替换，可用 Ctrl+Z 撤销"
            )
            self._emit_notice(notice, "success", 2600)
            print(f"[VOICE_EDIT] Exact replacement confirmed: count={match_count}")
        if self._bridge and hasattr(self._bridge, "emit_command"):
            self._bridge.emit_command("编辑文本", success)
        status_value = getattr(status, "value", "unknown")
        _pipeline_log(
            "VOICE_EDIT", f"Result: {status_value}, count={match_count}"
        )
        return success

    def _notify_final_asr_failure(self, rescue_launched: bool) -> None:
        """Quiet UI feedback for a lost committed segment (never for interim).

        Feedback matrix (ui_spec section 2.2): a loss with no rescue running
        gets the error cue + error-level notice; a launched cloud rescue gets
        the softer rescue cue + info-level notice ("still working"). One lost
        segment therefore never stacks two sounds — the red flash, the notice
        and the single cue all fire together, once.
        """
        try:
            if self._bridge and hasattr(self._bridge, "emit_asr_failure"):
                self._bridge.emit_asr_failure("final")
            if rescue_launched:
                self._emit_notice("语音识别暂时异常，正在自动补救…", "info", 2600)
            else:
                self._emit_notice("语音识别暂时异常，请再说一次", "error", 2600)
        except Exception as exc:
            logger.warning(f"ASR failure UI notify failed: {exc}")
        if getattr(self._asr_rescue_cfg, "beep", True):
            # Soft blip while a rescue works in the background; low double
            # pulse when the segment is definitively lost (was a 300Hz beep).
            self._play_sound("rescue" if rescue_launched else "error")

    def _maybe_self_heal_asr_engine(self, reason: str) -> bool:
        """Kick an async primary-engine rebuild when the failure streak trips."""
        cfg = self._asr_rescue_cfg
        if not cfg.enabled:
            return False
        from .core.asr.rescue_policy import should_trigger_engine_reload

        # External-process engines (llama-server) can die outright; a dead
        # backend is only fixable by a reload, so it bypasses the 600s
        # anti-storm cooldown. In-process engines always report alive.
        backend_dead = False
        engine = getattr(self, "asr_engine", None)
        if getattr(engine, "supports_sync_teardown", False):
            try:
                backend_dead = not bool(engine.is_backend_alive())
            except Exception:
                backend_dead = False
        if backend_dead:
            _pipeline_log(
                "RESCUE",
                "Self-heal: backend process dead — cooldown bypass eligible",
            )

        if not should_trigger_engine_reload(
            consecutive_failures=self._asr_failure_streak,
            now=time.time(),
            last_reload_at=self._asr_last_self_heal_at,
            backend_dead=backend_dead,
        ):
            return False
        if getattr(self, "_asr_hot_reload_in_progress", False):
            return False
        with self._lock:
            if self._sleep_mode == SleepMode.DEEP:
                return False
        thread = self._asr_self_heal_thread
        if thread and thread.is_alive():
            return False
        self._asr_last_self_heal_at = time.time()
        _pipeline_log(
            "RESCUE",
            f"Self-heal reload triggered after {self._asr_failure_streak} "
            f"consecutive failures ({reason})",
        )
        self._asr_self_heal_thread = threading.Thread(
            target=self._asr_self_heal_reload,
            args=(reason,),
            daemon=True,
            name="asr-self-heal",
        )
        self._asr_self_heal_thread.start()
        return True

    def _asr_self_heal_reload(self, reason: str) -> None:
        """Replace the (possibly poisoned) primary engine with a fresh instance.

        The stuck CUDA transcribe thread cannot be killed and may hold the old
        engine's internal lock forever, so for TORCH engines this path must
        NEVER call old_engine.unload() synchronously or join anything: build
        new, swap the reference, and hand the old object to a detached
        best-effort unloader that simply waits out the stuck thread before
        freeing VRAM.

        EXTERNAL-PROCESS engines (supports_sync_teardown, e.g. llama-server)
        invert that rule: teardown is a millisecond-fast process kill with no
        lock hazard, and the build-first ordering would DEADLOCK the heal —
        the hung-but-alive old server keeps the port bound, so every fresh
        load() fails its port pre-check and the heal loops forever while the
        old process never dies. For those engines the old instance is
        unloaded synchronously BEFORE the fresh load (the engine's own
        orphan-reclaim port check is the second line of defense).
        """
        try:
            asr_cfg = self._load_asr_config()
            engine_type, new_engine = self._create_asr_engine_from_config(asr_cfg)
            old_engine_pre = getattr(self, "asr_engine", None)
            if getattr(old_engine_pre, "supports_sync_teardown", False) and hasattr(
                old_engine_pre, "unload"
            ):
                _pipeline_log(
                    "RESCUE",
                    "Self-heal: sync-unloading external-process engine "
                    "before fresh load (frees its port)",
                )
                try:
                    old_engine_pre.unload()
                except Exception as pre_unload_exc:
                    logger.warning(
                        f"Self-heal pre-unload failed: {pre_unload_exc}"
                    )
            _pipeline_log("RESCUE", f"Self-heal: loading fresh {engine_type} engine...")
            new_engine.load()
            if engine_type in ("qwen3", "qwen3_sherpa", "qwen3_llamacpp"):
                try:
                    self._warmup_qwen3_engine(new_engine, "asr_self_heal")
                except Exception:
                    pass

            # Re-check the world before swapping: the rebuild takes 10-30s and
            # a hot-reload / deep-sleep entry meanwhile owns the engine slot.
            old_engine = self.asr_engine
            aborted_reason = ""
            with self._lock:
                if self._sleep_mode == SleepMode.DEEP:
                    aborted_reason = "deep_sleep"
                elif getattr(self, "_asr_hot_reload_in_progress", False):
                    aborted_reason = "hot_reload_in_progress"
                else:
                    self.asr_engine = new_engine
                    self._asr_engine_type = engine_type
            if aborted_reason:
                _pipeline_log(
                    "RESCUE",
                    f"Self-heal aborted before swap ({aborted_reason}); "
                    "discarding fresh engine",
                )
                self._detached_engine_unload(new_engine, "self_heal_aborted")
                # Same rollback as the failure path below: this attempt
                # healed nothing, so it must not burn the full 600s cooldown
                # — keep only the short retry window.
                from .core.asr.rescue_policy import (
                    RELOAD_COOLDOWN_S,
                    RELOAD_FAILED_RETRY_S,
                )

                self._asr_last_self_heal_at = time.time() - (
                    RELOAD_COOLDOWN_S - RELOAD_FAILED_RETRY_S
                )
                return
            self._asr_failure_streak = 0
            self._primary_asr_suppressed_until = 0.0
            try:
                self._apply_hotword_context_to_asr_engine()
            except Exception as ctx_exc:
                logger.warning(f"Self-heal context refresh failed: {ctx_exc}")
            _pipeline_log("RESCUE", "Self-heal: fresh engine swapped in")
            print("[RESCUE] ASR engine self-heal complete (fresh instance)")

            self._detached_engine_unload(old_engine, "self_heal_old_engine")
        except Exception as exc:
            _pipeline_log(
                "ERROR", f"Self-heal reload failed: {type(exc).__name__}: {exc}"
            )
            logger.error(f"ASR self-heal reload failed: {exc}", exc_info=True)
            # A failed attempt must not burn the whole cooldown: keep the
            # start-of-attempt timestamp semantics (anti-storm) but shrink the
            # wait to a short retry window; the streak stays so the very next
            # failure can re-trigger once that window passes.
            from .core.asr.rescue_policy import (
                RELOAD_COOLDOWN_S,
                RELOAD_FAILED_RETRY_S,
            )

            self._asr_last_self_heal_at = time.time() - (
                RELOAD_COOLDOWN_S - RELOAD_FAILED_RETRY_S
            )

    def _detached_engine_unload(self, engine, reason: str) -> None:
        """Best-effort unload on a detached daemon thread (never joined).

        The engine's internal lock may be held forever by a stuck CUDA
        thread; the unloader simply waits it out without blocking anyone.
        """
        if engine is None or not hasattr(engine, "unload"):
            return

        def _unload():
            try:
                engine.unload()
                _pipeline_log("RESCUE", f"Detached engine unload done ({reason})")
            except Exception as unload_exc:
                logger.warning(
                    f"Detached engine unload failed ({reason}): {unload_exc}"
                )

        threading.Thread(
            target=_unload, daemon=True, name=f"asr-engine-unload-{reason}"
        ).start()

    def _launch_cloud_asr_rescue(
        self,
        *,
        session_id,
        kind: str,
        audio,
        segment_done_at: float,
    ) -> bool:
        """Fire an async cloud second-pass transcription for a lost segment."""
        import numpy as np

        from .core.asr.rescue_policy import should_attempt_cloud_rescue

        cfg = self._asr_rescue_cfg
        try:
            duration_s = len(audio) / 16000.0 if audio is not None else 0.0
        except Exception:
            duration_s = 0.0
        if not should_attempt_cloud_rescue(
            config=cfg, kind=kind, audio_duration_s=duration_s
        ):
            return False

        # Single-flight: parallel uploads cost money and can race each other's
        # late-insert decisions.  Launches come from the single worker thread,
        # so check-then-set is safe here.
        if self._cloud_rescue_inflight.is_set():
            _pipeline_log(
                "RESCUE", "Cloud rescue skipped: another rescue is in flight"
            )
            self._record_asr_rescue_outcome(session_id, "cloud_skipped_inflight")
            return False
        self._cloud_rescue_inflight.set()

        # Everything between set() and a successful thread start must release
        # the in-flight slot on failure (audio copy can raise MemoryError,
        # Thread.start can raise RuntimeError) — otherwise the worker's
        # finally never runs and cloud rescue stays silently disabled for the
        # rest of the process lifetime.
        try:
            audio_copy = np.asarray(audio, dtype=np.float32).copy()
            context = ""
            try:
                if self.hotword_manager:
                    context = self.hotword_manager.build_initial_prompt() or ""
            except Exception:
                context = ""
            _pipeline_log(
                "RESCUE",
                f"Cloud rescue launched: {duration_s:.1f}s audio -> {cfg.model}",
            )
            threading.Thread(
                target=self._cloud_asr_rescue_worker,
                args=(session_id, audio_copy, context, segment_done_at),
                daemon=True,
                name="asr-cloud-rescue",
            ).start()
        except Exception as launch_exc:
            self._cloud_rescue_inflight.clear()
            _pipeline_log(
                "RESCUE",
                f"Cloud rescue launch failed: {type(launch_exc).__name__}: "
                f"{launch_exc}",
            )
            self._record_asr_rescue_outcome(
                session_id, "cloud_launch_failed", str(launch_exc)
            )
            return False
        return True

    def _cloud_asr_rescue_worker(
        self, session_id, audio, context: str, segment_done_at: float
    ) -> None:
        try:
            self._cloud_asr_rescue_impl(session_id, audio, context, segment_done_at)
        finally:
            self._cloud_rescue_inflight.clear()

    def _cloud_asr_rescue_impl(
        self, session_id, audio, context: str, segment_done_at: float
    ) -> None:
        from .core.asr.cloud_rescue import transcribe_via_dashscope
        from .core.asr.rescue_policy import (
            sanitize_cloud_rescue_text,
            should_insert_late_result,
        )

        cfg = self._asr_rescue_cfg
        text, error = transcribe_via_dashscope(
            audio,
            api_key=cfg.api_key,
            model=cfg.model,
            timeout_s=cfg.timeout_s,
            api_url=cfg.api_url,
            context=context,
        )
        if not text:
            _pipeline_log("RESCUE", f"Cloud rescue failed: {error}")
            self._record_asr_rescue_outcome(session_id, "cloud_fail", error)
            return

        _pipeline_log("RESCUE", f"Cloud rescue text: '{text[:60]}'")

        # Hygiene BEFORE anything reaches the caret: the input audio is
        # exactly the fuzzy/noisy material the local engine gave up on, so
        # the second pass is hallucination-prone.  Junk is dropped; anything
        # over the length cap or flagged by the local hallucination detector
        # goes to history instead of the caret.
        verdict, text = sanitize_cloud_rescue_text(text)
        if verdict == "junk":
            _pipeline_log("RESCUE", "Cloud rescue rejected: junk output")
            self._record_asr_rescue_outcome(session_id, "cloud_rejected", "junk")
            return
        hallucinated = False
        try:
            hallucinated = bool(self._is_hallucination(text))
        except Exception:
            hallucinated = False
        if verdict == "too_long" or hallucinated:
            reason = "hallucination" if hallucinated else "too_long"
            _pipeline_log("RESCUE", f"Cloud rescue not inserted: {reason}")
            self._store_rescued_text_to_history(
                text, session_id, recoverable=False
            )
            self._record_asr_rescue_outcome(session_id, "cloud_rejected", reason)
            return

        if not should_insert_late_result(
            now=time.time(),
            segment_done_at=segment_done_at,
            last_success_insert_at=self._last_success_insert_at,
            has_pending_work=self._worker_busy or not self._asr_queue.empty(),
        ):
            self._store_rescued_text_to_history(text, session_id)
            self._record_asr_rescue_outcome(session_id, "late_to_history")
            return

        status = self._commit_rescued_text(
            text,
            segment_done_at,
            session_id=session_id,
        )
        if status == "inserted":
            self._record_asr_rescue_outcome(session_id, "cloud_ok")
        else:
            self._store_rescued_text_to_history(text, session_id)
            self._record_asr_rescue_outcome(session_id, "late_to_history", status)

    def _commit_rescued_text(
        self,
        text: str,
        segment_done_at: float,
        session_id: int | None = None,
    ) -> str:
        """Polish a rescued transcription and insert it at the caret.

        Returns "inserted", "stale" (world changed during processing — caller
        stores to history) or "insert_failed".  The hotword/polish stage can
        take seconds (remote LLM), so the late-insert gates are re-checked
        atomically right before the paste under _rescue_insert_lock, which the
        ASR worker also holds when marking itself busy / refreshing the
        insert anchor.
        """
        from .core.asr.rescue_policy import should_insert_late_result

        try:
            with self._lock:
                _snap_processor = self.hotword_processor
                _snap_fuzzy = self.fuzzy_matcher
                _snap_polisher = self.polisher

            if _snap_processor:
                text, _ = _snap_processor.process_with_info(text)
            if _snap_fuzzy:
                text, _ = _snap_fuzzy.process_with_info(text)
            if _snap_polisher:
                try:
                    text = _snap_polisher.polish(text) or text
                except Exception as polish_exc:
                    logger.warning(f"Rescue polish failed: {polish_exc}")

            with self._rescue_insert_lock:
                # TOCTOU re-check: polish above may have taken seconds and a
                # newer segment may have started/committed meanwhile.
                if not should_insert_late_result(
                    now=time.time(),
                    segment_done_at=segment_done_at,
                    last_success_insert_at=self._last_success_insert_at,
                    has_pending_work=(
                        self._worker_busy or not self._asr_queue.empty()
                    ),
                ):
                    _pipeline_log(
                        "RESCUE",
                        "Rescued text became stale during polish; to history",
                    )
                    return "stale"
                expected_target = None
                if session_id is not None:
                    with self._session_lock:
                        expected_target = getattr(
                            self, "_session_output_targets", {}
                        ).get(session_id)
                    # A production OutputInjector understands target snapshots.
                    # If this late rescue has already outlived its session
                    # identity, storing it in history is safer than pasting
                    # into whichever window happens to be active now. Minimal
                    # legacy/test injectors keep their old isolated contract.
                    if expected_target is None and callable(
                        getattr(
                            self.output_injector, "capture_target_snapshot", None
                        )
                    ):
                        _pipeline_log(
                            "RESCUE",
                            "Rescued text has no surviving target snapshot; to history",
                        )
                        return "stale"
                self._emit_text(text, is_final=True)
                if expected_target is None:
                    insert_ok = self.output_injector.insert_text(text)
                else:
                    insert_ok = self.output_injector.insert_text(
                        text, expected_target=expected_target
                    )
                self._remember_last_transcript(text)
                if insert_ok:
                    self._last_success_insert_at = time.time()
            if insert_ok:
                _pipeline_log("RESCUE", f"Rescued text inserted: '{text[:60]}'")
                print(f"[RESCUE] Inserted rescued text: {text[:60]}")
                return "inserted"
            return "insert_failed"
        except Exception as exc:
            logger.error(f"Rescued text insert failed: {exc}", exc_info=True)
            return "insert_failed"

    def _store_rescued_text_to_history(
        self, text: str, session_id, *, recoverable: bool = True
    ) -> None:
        try:
            if recoverable:
                self._remember_last_transcript(text)
            if self.history_store:
                self.history_store.add(
                    record_type=RecordType.ASR,
                    input_text=text,
                    output_text="",
                    metadata={
                        "session_id": session_id,
                        "rescued": True,
                        "source": "asr_rescue_cloud",
                        "recoverable": recoverable,
                    },
                )
            # Late rescue success: success-level notice, silent by design
            # (ui_spec section 2.2 row "rescue arrived late -> history").
            self._emit_notice("救回的转写已存入历史", "success", 2600)
            _pipeline_log("RESCUE", "Rescued text stored to history (too late to insert)")
        except Exception as exc:
            logger.warning(f"Rescued text history store failed: {exc}")

    @staticmethod
    def _normalize_mic_input_gain(value, default: float = 1.0) -> float:
        try:
            gain = float(value)
        except (TypeError, ValueError):
            gain = float(default)
        if not math.isfinite(gain):
            gain = float(default)
        return float(max(0.5, min(2.0, gain)))

    @classmethod
    def _default_mic_input_gain_for_mode(cls, mode: str) -> float:
        try:
            from .core.audio.dsp import MODE_PRESETS

            preset = MODE_PRESETS.get(str(mode or "standard"), {}) or {}
            return cls._normalize_mic_input_gain(preset.get("input_gain_default", 1.0))
        except Exception:
            return 1.0

    @classmethod
    def _mic_input_gain_from_audio_cfg(cls, audio_cfg: dict, mode: str) -> float:
        """Resolve mode-aware mic gain.

        New configs use audio.input_gain_by_mode so each 收音模式 remembers its
        own slider value.  A legacy audio.input_gain is treated as the standard
        mode's value only; whisper/noisy fall back to their preset defaults
        unless they already have an explicit per-mode value.
        """
        audio_cfg = audio_cfg or {}
        mode = str(mode or "standard")
        by_mode = audio_cfg.get("input_gain_by_mode")
        if isinstance(by_mode, dict):
            if mode in by_mode:
                return cls._normalize_mic_input_gain(by_mode.get(mode))
            return cls._default_mic_input_gain_for_mode(mode)
        if mode == "standard" and "input_gain" in audio_cfg:
            return cls._normalize_mic_input_gain(audio_cfg.get("input_gain"))
        return cls._default_mic_input_gain_for_mode(mode)

    @staticmethod
    def _vad_steady_noise_from_preset(preset: dict) -> dict:
        """Map capture-mode endpoint noise knobs into VADConfig fields."""

        def _as_int(name: str, default: int, lo: int, hi: int) -> int:
            try:
                value = int(preset.get(name, default))
            except (TypeError, ValueError):
                value = default
            return max(lo, min(hi, value))

        def _as_float(name: str, default: float, lo: float, hi: float) -> float:
            try:
                value = float(preset.get(name, default))
            except (TypeError, ValueError):
                value = default
            return max(lo, min(hi, value))

        return {
            "speech_end_steady_noise_ms": _as_int(
                "endpoint_steady_noise_ms", 0, 0, 5000
            ),
            "speech_end_steady_noise_min_speech_ms": _as_int(
                "endpoint_steady_noise_min_speech_ms", 1200, 200, 10000
            ),
            "speech_end_steady_noise_max_cv": _as_float(
                "endpoint_steady_noise_max_cv", 0.18, 0.01, 1.0
            ),
            "speech_end_steady_noise_peak_ratio": _as_float(
                "endpoint_steady_noise_peak_ratio", 0.65, 0.05, 1.5
            ),
            "speech_end_steady_noise_flat_ms": _as_int(
                "endpoint_steady_noise_flat_ms", 1800, 0, 10000
            ),
            "speech_end_steady_noise_flat_max_cv": _as_float(
                "endpoint_steady_noise_flat_max_cv", 0.10, 0.01, 1.0
            ),
            "speech_end_steady_noise_min_rms": _as_float(
                "endpoint_steady_noise_min_rms", 0.0015, 0.0, 0.2
            ),
            "speech_end_rel_drop_ratio": _as_float(
                "endpoint_rel_drop_ratio", 0.0, 0.0, 1.0
            ),
            "speech_end_rel_drop_min_speech_ms": _as_int(
                "endpoint_rel_drop_min_speech_ms", 900, 200, 10000
            ),
            "speech_end_rel_drop_abs_ceiling": _as_float(
                "endpoint_rel_drop_abs_ceiling", 0.0045, 0.0005, 0.2
            ),
            "speech_end_rel_drop_prob_ceiling": _as_float(
                "endpoint_rel_drop_prob_ceiling", 0.70, 0.05, 1.0
            ),
            "speech_end_override_cancel_chunks": _as_int(
                "endpoint_override_cancel_chunks", 1, 1, 30
            ),
        }

    def _find_audio_device_id(self, device_name: str) -> int:
        """
        Find audio device ID by name.

        Args:
            device_name: Device name (e.g., "Microsoft 声音映射器 - Input")

        Returns:
            Device ID (int), or None if not found (uses default)
        """
        if not device_name:
            return None

        try:
            import sounddevice as sd

            # List all input devices
            devices = sd.query_devices()

            for i, d in enumerate(devices):
                if d["max_input_channels"] > 0:  # Input device
                    # Exact match
                    if d["name"] == device_name:
                        logger.info(f"Found audio device: {device_name} -> ID {i}")
                        return i
                    # Partial match (in case of encoding issues)
                    if device_name in d["name"] or d["name"] in device_name:
                        logger.info(
                            f"Found audio device (partial): {d['name']} -> ID {i}"
                        )
                        return i

            # Not found - log available devices for debugging
            input_devices = [
                (i, d["name"])
                for i, d in enumerate(devices)
                if d["max_input_channels"] > 0
            ]
            logger.warning(
                f"Audio device '{device_name}' not found. Available: {input_devices}"
            )
            return None

        except Exception as e:
            logger.error(f"Failed to find audio device: {e}")
            return None

    def _init_components(self) -> None:
        """Initialize audio and ASR components."""
        print("Initializing components...")

        # Load config from hotwords.json
        asr_cfg = self._load_asr_config()

        # Memory guard config. Default ON: after 5 minutes idle, unload the
        # resident ASR model via deep sleep. This keeps Qwen3 1.7B accuracy and
        # only trades the next wake for a reload delay.
        memory_cfg = asr_cfg.get("memory", {}) or {}
        self._auto_deep_sleep_enabled = bool(memory_cfg.get("auto_deep_sleep", True))
        try:
            self._auto_deep_sleep_idle_s = float(
                memory_cfg.get("auto_deep_sleep_idle_s", 300)
            )
        except (TypeError, ValueError):
            self._auto_deep_sleep_idle_s = 300.0
        self._auto_deep_sleep_idle_s = max(
            60.0, min(24 * 3600.0, self._auto_deep_sleep_idle_s)
        )
        print(
            "[MEMORY] Auto deep sleep "
            f"{'enabled' if self._auto_deep_sleep_enabled else 'disabled'} "
            f"(idle={self._auto_deep_sleep_idle_s:.0f}s)"
        )

        # VAD config with validation (clamp to valid ranges)
        vad_cfg = asr_cfg.get("vad", {})
        vad_threshold = max(0.1, min(0.9, vad_cfg.get("threshold", 0.2)))
        vad_min_speech = max(50, min(1000, vad_cfg.get("min_speech_ms", 150)))
        vad_min_silence = max(100, min(5000, vad_cfg.get("min_silence_ms", 1500)))
        vad_max_speech = max(3000, min(60000, vad_cfg.get("max_speech_ms", 10000)))

        # Soft-split: cut long utterances at natural pauses for pipeline parallelism.
        # Defaults: ON, 500ms pause, 5s accumulated speech — short utterances never split.
        vad_soft_split_enabled = bool(vad_cfg.get("soft_split_enabled", True))
        vad_soft_split_silence = max(
            200, min(1400, vad_cfg.get("soft_split_silence_ms", 500))
        )
        vad_soft_split_min_speech = max(
            2000, min(20000, vad_cfg.get("soft_split_min_speech_ms", 5000))
        )

        # Pre-ASR energy gate (configurable from settings)
        self._energy_threshold = max(
            0.0005, min(0.02, vad_cfg.get("energy_threshold", 0.003))
        )
        # Cache the user's base energy + VAD thresholds before mode overrides
        # so we can revert them when switching back to a non-overriding mode.
        self._energy_threshold_base = self._energy_threshold
        self._vad_threshold_base = float(vad_threshold)

        # Pre-ASR capture-mode DSP. Stored in config under audio.capture_mode
        # rather than vad.* because it's audio-pipeline-level, not VAD config.
        # Valid values: "standard" / "noisy" / "whisper".
        # Backwards-compat: an older "off" value (pre-1.0.5.10 schema, when
        # the bypass tier still existed) silently falls back to "standard"
        # since the standard preset's lift-only AGC is already near-
        # transparent for near-field users.
        audio_cfg = asr_cfg.get("audio", {}) or {}
        self._capture_mode = str(
            audio_cfg.get("capture_mode", "standard") or "standard"
        )
        if self._capture_mode not in ("standard", "noisy", "whisper"):
            self._capture_mode = "standard"
        self._mic_input_gain = self._mic_input_gain_from_audio_cfg(
            audio_cfg, self._capture_mode
        )
        print(f"[AUDIO] Mic input gain: {self._mic_input_gain:.2f}x")

        # Pre-ASR loudness normalization (default OFF; flip via
        # audio.asr_gain_normalize once field evidence supports it).
        from .core.audio.gain import AsrGainConfig

        self._asr_gain_cfg = AsrGainConfig.from_mapping(audio_cfg)
        if self._asr_gain_cfg.enabled:
            print(
                "[AUDIO] ASR gain normalize ON "
                f"(target={self._asr_gain_cfg.target_peak_dbfs:.1f}dBFS, "
                f"max=+{self._asr_gain_cfg.max_gain_db:.0f}dB)"
            )

        # UI sound cues: apply config (sound.enabled / volume /
        # quiet_in_whisper) and sync the capture mode for the whisper
        # auto-quiet rule.
        self._configure_sound(asr_cfg)
        # Apply mode-specific energy_gate override now (VAD override is
        # applied later, after audio_capture is constructed).
        from .core.audio.dsp import MODE_PRESETS as _MP

        _energy_override = _MP[self._capture_mode].get("energy_gate_override")
        if _energy_override is not None:
            self._energy_threshold = float(_energy_override)
            print(
                f"[CAPTURE-MODE] {self._capture_mode}: "
                f"energy_gate {self._energy_threshold_base} → {self._energy_threshold}"
            )
        _active_preset = _MP[self._capture_mode]
        _max_speech_override = _active_preset.get("max_speech_ms_override")
        if _max_speech_override is not None:
            try:
                _override_ms = int(_max_speech_override)
                if _override_ms > 0 and _override_ms < vad_max_speech:
                    print(
                        f"[CAPTURE-MODE] {self._capture_mode}: "
                        f"max_speech {vad_max_speech}ms → {_override_ms}ms"
                    )
                    vad_max_speech = _override_ms
            except (TypeError, ValueError):
                pass
        _endpoint_micro_rms = float(_active_preset.get("endpoint_micro_rms") or 0.0)
        try:
            _endpoint_micro_min_ms = int(
                _active_preset.get("endpoint_micro_min_speech_ms", 1200)
            )
        except (TypeError, ValueError):
            _endpoint_micro_min_ms = 1200
        _steady_noise_cfg = self._vad_steady_noise_from_preset(_active_preset)

        # Post-ASR noise text filter
        self._noise_filter_enabled = vad_cfg.get("noise_filter", True)

        # Anti-hallucination gates (H2): joint VAD gate, template blacklist,
        # decode-confidence gate.
        self._apply_hallucination_gate_config(vad_cfg)

        # Screen OCR switches
        self._screen_ocr_enabled = vad_cfg.get("screen_ocr", False)
        self._screen_ocr_polish_enabled = _screen_ocr_polish_opted_in(vad_cfg)
        # OCR backend switches.  DirectML runs in an isolated worker process:
        # if a driver/ORT combination native-crashes, the worker dies and Aria
        # falls back to CPU instead of losing the main pythonw.exe process.
        self._screen_ocr_use_dml = bool(vad_cfg.get("screen_ocr_use_dml", True))
        # Diagnostic: force CPU path (skip DirectML probe even when the
        # experimental DML switch is enabled).
        self._screen_ocr_force_cpu = bool(vad_cfg.get("screen_ocr_force_cpu", False))

        # Terminal polish bypass: cmd / PowerShell / Windows Terminal etc where
        # raw ASR text is sometimes preferable to polished prose. Default OFF —
        # user wants polish to run everywhere. Set `vad.polish_terminal_bypass:
        # true` to skip polish in terminals.
        self._polish_terminal_bypass = bool(
            vad_cfg.get("polish_terminal_bypass", False)
        )

        # Find audio device ID from config name
        audio_device_name = asr_cfg.get("audio_device")
        audio_device_id = self._find_audio_device_id(audio_device_name)
        if audio_device_name:
            print(
                f"[AUDIO] Configured device: '{audio_device_name}' -> ID {audio_device_id}"
            )
        else:
            print("[AUDIO] Using system default input device")

        # Audio capture with VAD
        audio_config = AudioConfig(
            sample_rate=16000,
            channels=1,
            enable_vad=True,
            device_id=audio_device_id,  # Use configured device
            vad_config=VADConfig(
                threshold=vad_threshold,
                min_speech_ms=vad_min_speech,
                min_silence_ms=vad_min_silence,
                max_speech_ms=vad_max_speech,
                soft_split_enabled=vad_soft_split_enabled,
                soft_split_silence_ms=vad_soft_split_silence,
                soft_split_min_speech_ms=vad_soft_split_min_speech,
                speech_end_micro_rms=_endpoint_micro_rms,
                speech_end_micro_min_speech_ms=_endpoint_micro_min_ms,
                **_steady_noise_cfg,
            ),
        )
        self.audio_capture = AudioCapture(audio_config)
        self._apply_asr_runtime_vad_timing(asr_cfg, "init")

        # Set up audio callbacks
        self.audio_capture.set_callbacks(
            on_speech_start=self._on_speech_start,
            on_speech_end=self._on_speech_end,
            on_audio_level=self._on_audio_level,
            on_speech_soft_split=self._on_speech_soft_split,
        )

        # Apply capture-mode VAD threshold override now that audio_capture
        # exists. Whisper mode lowers Silero's probability threshold so
        # very-quiet speech actually triggers speech_start — without this,
        # the rest of the DSP chain never sees the audio.
        _vad_override = _MP[self._capture_mode].get("vad_threshold_override")
        if _vad_override is not None and self.audio_capture._vad is not None:
            self.audio_capture._vad.config.threshold = float(_vad_override)
            print(
                f"[CAPTURE-MODE] {self._capture_mode}: "
                f"vad_threshold {self._vad_threshold_base} → {_vad_override}"
            )

        # ASR engine selection
        engine_type = asr_cfg["engine"]

        # Backward compatibility: old configs with removed engines fall back to qwen3
        if engine_type in ("whisper", "fireredasr"):
            logger.warning(
                f"ASR engine '{engine_type}' is no longer supported, falling back to Qwen3-ASR"
            )
            print(f"[WARN] ASR engine '{engine_type}' removed, using Qwen3-ASR instead")
            engine_type = "qwen3"

        if engine_type == "funasr":
            # FunASR (Paraformer/SenseVoice)
            self._asr_engine_type = "funasr"
            # Check for pre-loaded engine (loaded before Qt to avoid conflict)
            import aria

            preloaded = getattr(aria, "_preloaded_asr_engine", None)
            if self._preloaded_engine_matches(preloaded, "funasr"):
                print("Using pre-loaded FunASR engine")
                self.asr_engine = preloaded
            else:
                print("Loading FunASR model (this may take a few seconds)...")
                funasr_cfg = asr_cfg["funasr"]
                asr_config = FunASRConfig(
                    model_name=funasr_cfg.get("model_name", "paraformer-zh"),
                    device=funasr_cfg.get("device", "cuda"),
                    enable_vad=funasr_cfg.get("enable_vad", False),
                    enable_punc=funasr_cfg.get("enable_punc", False),
                )
                self.asr_engine = FunASREngine(asr_config)
                self.asr_engine.load()
            print(f"FunASR ready!")
        elif engine_type == "qwen3":
            # Qwen3-ASR (Alibaba's latest, 52 languages, context biasing)
            self._asr_engine_type = "qwen3"
            qwen3_cfg = asr_cfg.get("qwen3", {})
            # Check for pre-loaded engine. Exact type match: a sherpa engine's
            # name also contains "Qwen3" (it subclasses Qwen3ASREngine), so a
            # substring/name check would let the torch branch claim it.
            import aria

            preloaded = getattr(aria, "_preloaded_asr_engine", None)
            if self._preloaded_engine_matches(preloaded, "qwen3"):
                print("Using pre-loaded Qwen3-ASR engine")
                self.asr_engine = preloaded
            else:
                print("Loading Qwen3-ASR model (this may take a few seconds)...")
                # Use config helper so memory-related knobs stay consistent
                # with launcher preloading and fallback paths.
                asr_config = Qwen3Config.from_mapping(qwen3_cfg)
                self.asr_engine = Qwen3ASREngine(asr_config)
                self.asr_engine.load()
            print(f"Qwen3-ASR ready!")
        elif engine_type == "qwen3_sherpa":
            # Qwen3-ASR via sherpa-onnx int8 (torch-free lightweight runtime).
            # No launcher preload branch this cycle (deliberate): cold load here
            # takes a few seconds on CPU, acceptable for an opt-in engine.
            self._asr_engine_type = "qwen3_sherpa"
            import aria

            preloaded = getattr(aria, "_preloaded_asr_engine", None)
            if self._preloaded_engine_matches(preloaded, "qwen3_sherpa"):
                print("Using pre-loaded Qwen3-ASR (sherpa) engine")
                self.asr_engine = preloaded
            else:
                print("Loading Qwen3-ASR sherpa int8 model...")
                try:
                    engine_type, self.asr_engine = self._create_asr_engine_from_config(
                        asr_cfg
                    )
                    self.asr_engine.load()
                except Exception as sherpa_exc:
                    # Missing wheel / model dir: fall back to the torch path so
                    # the app still starts, and tell the user why. On slim
                    # (torch-free) installs this degrades to "no ASR" instead
                    # of crashing — the tray stays usable.
                    engine_type, self.asr_engine = self._fallback_to_torch_qwen3(
                        asr_cfg, "Qwen3-ASR 轻量引擎", sherpa_exc
                    )
                    self._asr_engine_type = engine_type
            if self.asr_engine is not None:
                print(f"ASR ready! ({self._asr_engine_type})")
        elif engine_type == "qwen3_llamacpp":
            # Qwen3-ASR GGUF via a resident llama-server subprocess (llama.cpp
            # CUDA). No launcher preload branch this cycle (deliberate): cold
            # start is 2-6s (server load + health poll), acceptable opt-in.
            self._asr_engine_type = "qwen3_llamacpp"
            import aria

            preloaded = getattr(aria, "_preloaded_asr_engine", None)
            if self._preloaded_engine_matches(preloaded, "qwen3_llamacpp"):
                print("Using pre-loaded Qwen3-ASR (llama.cpp) engine")
                self.asr_engine = preloaded
            else:
                print("Starting llama-server for Qwen3-ASR GGUF...")
                try:
                    engine_type, self.asr_engine = self._create_asr_engine_from_config(
                        asr_cfg
                    )
                    self.asr_engine.load()
                except Exception as llama_exc:
                    # Missing exe/model, port conflict, health timeout: try
                    # the sherpa CPU engine first (also torch-free, disjoint
                    # failure class), then torch, so the app still starts.
                    # On slim (torch-free) installs the final degradation is
                    # "no ASR" instead of crashing — the tray stays usable.
                    engine_type, self.asr_engine = (
                        self._fallback_llamacpp_to_sherpa_then_torch(
                            asr_cfg, llama_exc
                        )
                    )
                    self._asr_engine_type = engine_type
            if self.asr_engine is not None:
                print(f"ASR ready! ({self._asr_engine_type})")
        else:
            # Unknown engine type - fall back to Qwen3-ASR
            # NOTE: Do NOT import Qwen3ASREngine/Qwen3Config here!
            # Python scoping: any `from X import Y` inside a function makes Y a
            # LOCAL variable for the ENTIRE function, shadowing the module-level
            # import at line 135. This causes UnboundLocalError when the normal
            # qwen3 branch (line 762) tries to use Qwen3Config.
            logger.warning(
                f"Unknown ASR engine '{engine_type}', falling back to Qwen3-ASR"
            )
            engine_type = "qwen3"  # Canonicalize so hotword setup below works
            self._asr_engine_type = "qwen3"
            qwen3_cfg = asr_cfg.get("qwen3", {})
            asr_config = Qwen3Config.from_mapping(qwen3_cfg)
            self.asr_engine = Qwen3ASREngine(asr_config)
            self.asr_engine.load()
            print(f"Qwen3-ASR model loaded (fallback): {asr_config.model_name}")

        # Re-apply VAD timing now that the engine actually loaded: the "init"
        # application above followed the CONFIG (e.g. qwen3_sherpa -> CPU
        # tightening), but a load failure may have fallen back to a different
        # runtime (torch/CUDA). VAD policy must follow the loaded engine.
        self._apply_asr_runtime_vad_timing(asr_cfg, "post_engine_init")
        self._configure_gpu_pressure_fallback(asr_cfg)
        self._configure_asr_rescue(asr_cfg)

        # HotWord system initialization (warmup moved to after initial_prompt is set)
        print("Loading hotword configuration...")
        self.hotword_manager = HotWordManager.from_default()

        # Auto-hotword tracker + reviewer initialization. Always built so that
        # disabling at runtime via settings can re-enable without restart.
        self._auto_hotword_cfg = asr_cfg.get("auto_hotword", {}) or {}
        try:
            self._init_auto_hotword(asr_cfg.get("polish", {}) or {})
        except Exception as exc:
            logger.warning(f"Auto-hotword init failed: {exc}")
            self._auto_hotword_tracker = None
            self._auto_hotword_reviewer = None

        # v3.2: Set ASR engine type for polish layer optimization
        # Qwen3 handles English well at ASR layer, so we reduce English hotwords to LLM
        self.hotword_manager.config.asr_engine_type = engine_type

        # Set hotwords based on engine type
        if engine_type == "funasr" and hasattr(
            self.asr_engine, "set_hotwords_with_score"
        ):
            # FunASR: use layer-aware hotwords with score (weight->score mapping)
            # 0.3->30(hint), 0.5->60(reference), 1.0->100(critical)
            hotwords_with_score = self.hotword_manager.get_asr_hotwords_with_score()
            self.asr_engine.set_hotwords_with_score(hotwords_with_score)
            print(
                f"[HOTWORD] FunASR hotwords: {len(hotwords_with_score)} words (weight->score mapped)"
            )
        elif engine_type in ("qwen3", "qwen3_sherpa", "qwen3_llamacpp"):
            # Qwen3-ASR: use context biasing (text-based, weight->repetition)
            context_string = self._build_asr_context_for_current_mode(
                include_screen=False
            )
            self.asr_engine.set_context(context_string or "")
            print(f"[HOTWORD] Qwen3 context: {len(context_string)} chars")

        # GPU Warmup — two passes to fully prime both encoder and decoder
        # Pass 1: silence → primes CUDA kernels + audio encoder
        # Pass 2: low noise → forces decoder to generate tokens (primes text generation path)
        # See docs/DEBUG_LESSONS.md: warmup before context causes "Prompt Shock"
        try:
            import numpy as _np
            import time as _warmup_time

            skip_warmup_reason = ""
            if not self._qwen3_engine_uses_cuda(self.asr_engine):
                skip_warmup_reason = (
                    f"ASR on {self._qwen3_engine_device_label(self.asr_engine)}"
                )
            elif (
                self._gpu_pressure_monitor and self._primary_asr_supports_gpu_fallback()
            ):
                pressure = self._gpu_pressure_monitor.sample(force=True)
                if pressure.busy:
                    skip_warmup_reason = pressure.reason

            if skip_warmup_reason:
                _pipeline_log(
                    "WARMUP",
                    f"GPU warmup skipped: {skip_warmup_reason}",
                )
                print(f"[WARMUP] GPU warmup skipped: {skip_warmup_reason}")
            else:
                _pipeline_log("WARMUP", "Starting GPU warmup (2 passes)...")
                _warmup_start = _warmup_time.time()
                with self._asr_lock:
                    # Pass 1: silence (prime encoder + CUDA kernels)
                    silence = _np.zeros(16000, dtype=_np.float32)
                    _cm = getattr(self, "_capture_mode", "standard")
                    _ = self._transcribe_qwen3_warmup_audio(
                        self.asr_engine, silence, capture_mode=_cm
                    )
                    # Pass 2: low-level noise (force decoder to produce tokens)
                    noise = _np.random.randn(16000).astype(_np.float32) * 0.01
                    _ = self._transcribe_qwen3_warmup_audio(
                        self.asr_engine, noise, capture_mode=_cm
                    )
                    if hasattr(self.asr_engine, "trim_runtime_cache"):
                        self.asr_engine.trim_runtime_cache("startup_warmup")
                _warmup_ms = (_warmup_time.time() - _warmup_start) * 1000
                _pipeline_log("WARMUP", f"GPU warmup complete ({_warmup_ms:.0f}ms)")
                print(f"[WARMUP] GPU warmup complete ({_warmup_ms:.0f}ms)")
        except Exception as e:
            _pipeline_log("WARMUP", f"GPU warmup FAILED: {e}")
            print(f"[WARMUP] GPU warmup failed (non-fatal): {e}")

        layer_hotwords = self.hotword_manager.get_hotwords_by_layer()
        self.hotword_processor = HotWordProcessor(
            self.hotword_manager.get_replacements(),
            hotwords=layer_hotwords.get("layer2_regex", []),
            explicit_corrections=self._explicit_correction_store.active_rules(),
        )
        print(
            f"[HOTWORD] {len(self.hotword_manager.config.prompt_words)} words, "
            f"{len(self.hotword_manager.config.replacements)} replacements, "
            f"{self.hotword_processor.explicit_correction_count} explicit corrections"
        )

        # Layer 2.5: Pinyin fuzzy matching
        # Static hotwords: weight >= 1.0 only (lower weights too risky for fuzzy)
        # Screen keywords: handled separately by _screen_pinyin_correct (3+ chars)
        fuzzy_hotwords = layer_hotwords.get("layer2_5_pinyin", [])
        self.fuzzy_matcher = PinyinFuzzyMatcher(
            fuzzy_hotwords,
            FuzzyMatchConfig(enabled=True, threshold=0.75, min_word_length=2),
        )
        print(
            f"[FUZZY] Pinyin matcher: {len(fuzzy_hotwords)} static hotwords (weight>=1.0) + screen scan (3+ chars)"
        )

        # Layer 3: Polish (optional, mode-based)
        self.polisher = self.hotword_manager.get_active_polisher()
        if self.polisher:
            mode = self.hotword_manager.polish_mode
            if mode == "fast":
                print(f"[POLISH] Local polish enabled (Qwen, fast mode)")
            else:
                # Show actual model from config
                cfg = self.hotword_manager.config.polish_config
                model_name = cfg.model if cfg else "unknown"
                short_name = (
                    model_name.split("/")[-1] if "/" in model_name else model_name
                )
                print(f"[POLISH] AI polish enabled ({short_name}, quality mode)")

        # L1 cache-friendly path: seed polisher.session_hotwords from the tracker's
        # already-approved set so that even before the first daily review the
        # polish prompt prefix carries the slow-changing word list.
        if self.polisher and self._auto_hotword_tracker is not None:
            try:
                active_words = self._auto_hotword_tracker.get_active_hotwords()
                if hasattr(self.polisher, "config"):
                    self.polisher.config.session_hotwords = list(active_words or [])
                    if active_words:
                        print(
                            f"[POLISH] Seeded {len(active_words)} session hotwords "
                            f"from auto-hotword tracker"
                        )
            except Exception as exc:
                logger.warning(f"Failed to seed polisher.session_hotwords: {exc}")

        # PERF-1: warm the polish API (TLS + prompt prefix cache) once the
        # pipeline is up. Delay keeps it clear of the ASR/GPU startup window.
        self._schedule_polish_prewarm(delay_s=8.0, reason="startup")

        # Layer 0: Voice command system
        self.command_detector = CommandDetector()
        if self.command_detector.enabled:
            self.command_executor = CommandExecutor(
                self.output_injector,
                self.command_detector.commands,
                self.command_detector.cooldown_ms,
            )
            print(
                f"[COMMAND] Voice commands enabled: {len(self.command_detector.commands)} commands"
            )
        else:
            print("[COMMAND] Voice commands disabled")

        # Layer -1: Wakeword system (app-level commands via "小助手")
        self.wakeword_detector = WakewordDetector()
        if self.wakeword_detector.enabled:
            self.wakeword_executor = WakewordExecutor(
                self,
                self._bridge,
                self.wakeword_detector.cooldown_ms,
            )
            print(
                f"[WAKEWORD] Enabled: '{self.wakeword_detector.wakeword}' "
                f"({len(self.wakeword_detector.commands)} commands)"
            )
        else:
            print("[WAKEWORD] Disabled")

        # Insight store for voice memo recording (deprecated, kept for compatibility)
        self.insight_store = InsightStore(
            data_dir=Path(__file__).parent / "data" / "insights"
        )
        print("[INSIGHT] Voice insight store initialized")

        # Unified history store (v1.2) - reads config from hotwords.json
        _history_enabled = True
        _history_retention = 90
        _debuglog_retention = 14
        try:
            _hcfg_path = Path(__file__).parent / "config" / "hotwords.json"
            if _hcfg_path.exists():
                import json as _hjson

                with open(_hcfg_path, "r", encoding="utf-8") as _hf:
                    _hcfg = _hjson.load(_hf)
                _history_enabled = _hcfg.get("history_enabled", True)
                _history_retention = _hcfg.get("history_retention_days", 90)
                _debuglog_retention = _hcfg.get("debuglog_retention_days", 14)
        except Exception:
            pass

        self.history_store = HistoryStore(
            data_dir=Path(__file__).parent / "data" / "history",
            enabled=_history_enabled,
            retention_days=_history_retention,
        )
        print(
            f"[HISTORY] Unified history store initialized (enabled={_history_enabled}, retention={_history_retention}d)"
        )

        # Auto-cleanup old history records on startup
        try:
            cleaned = self.history_store.auto_cleanup()
            if cleaned:
                print(f"[HISTORY] Auto-cleanup: removed {cleaned} old day files")
        except Exception as e:
            print(f"[HISTORY] Auto-cleanup failed: {e}")

        # DebugLog retention: age-expire session/audio artifacts and enforce a
        # directory size cap, on startup and then every 24h. Runs on a daemon
        # thread (first run may delete tens of thousands of files; must not
        # block startup).
        try:
            from .core.debug_retention import (
                start_retention_thread,
                resolve_retention_days,
            )

            start_retention_thread(
                retention_days=resolve_retention_days(_debuglog_retention)
            )
        except Exception as e:
            print(f"[DEBUGLOG] Retention thread failed to start: {e}")

        # Run migration from legacy data (once)
        try:
            from .core.history.migrator import run_migration

            run_migration(
                config_path=Path(__file__).parent / "config" / "hotwords.json",
                debug_dir=Path(__file__).parent / "DebugLog",
                insight_dir=Path(__file__).parent / "data" / "insights",
                history_store=self.history_store,
            )
        except Exception as e:
            print(f"[HISTORY] Migration skipped: {e}")

        # Reminder system (voice-triggered alarms)
        from .core.reminder import ReminderStore, ReminderScheduler
        from .core.action.types import ReminderNotifyAction

        self.reminder_store = ReminderStore(
            data_path=Path(__file__).parent / "data" / "reminders.json"
        )
        self.reminder_store.cleanup()  # Clean old fired/cancelled on startup

        def _on_reminder_due(reminder):
            """Callback from scheduler thread — emit action via bridge."""
            batch_items = reminder.get("batch_items", []) or []
            notified_ids = tuple(
                str(item.get("id", ""))
                for item in batch_items
                if isinstance(item, dict) and item.get("id")
            )
            if not notified_ids and reminder.get("id"):
                notified_ids = (str(reminder.get("id")),)
            self._last_notified_reminder_ids = notified_ids
            self._last_notified_reminder_id = (
                notified_ids[0] if notified_ids else ""
            )
            self._last_notified_reminder_at = time.time()
            batch_count = reminder.get("batch_count", 0)
            try:
                repeat_interval_seconds = int(
                    reminder.get("repeat_interval_seconds", 0) or 0
                )
            except (TypeError, ValueError):
                repeat_interval_seconds = 0
            action = ReminderNotifyAction(
                reminder_id=reminder.get("id", ""),
                content=reminder.get("content", ""),
                created_at=reminder.get("created_at", ""),
                batch_count=batch_count,
                repeat_interval_seconds=repeat_interval_seconds,
            )
            if self._bridge:
                self._bridge.emit_action(action)

        self.reminder_scheduler = ReminderScheduler(
            store=self.reminder_store,
            on_reminder_due=_on_reminder_due,
            stop_event=self._stop_event,
        )
        self.reminder_scheduler.start()
        pending = self.reminder_store.get_pending()
        print(f"[REMINDER] Scheduler started ({len(pending)} pending reminders)")

        # Selection mode components
        self.selection_detector = SelectionDetector(self.output_injector)
        self.selection_processor = SelectionProcessor(self.polisher)
        print("[SELECTION] Selection mode initialized")

    def _start_ocr_watcher(self) -> None:
        """Start window-change OCR watcher during continuous recording.

        Monitors foreground window handle every 0.5s. Only triggers OCR when
        the window actually changes (event-driven, not polling OCR itself).
        500ms debounce prevents thrashing during fast Alt+Tab.
        """
        if not self._screen_ocr_enabled:
            return
        self._stop_ocr_watcher()

        import ctypes

        def _watch():
            self._ocr_last_hwnd = 0
            _debounce_hwnd = 0
            _debounce_time = 0.0
            import time as _t

            while self.state == AppState.RECORDING and not self._stop_event.is_set():
                try:
                    current = ctypes.windll.user32.GetForegroundWindow()
                    if current and current != self._ocr_last_hwnd:
                        # Window changed — debounce 500ms
                        if current != _debounce_hwnd:
                            _debounce_hwnd = current
                            _debounce_time = _t.time()
                        elif _t.time() - _debounce_time >= 0.5:
                            # Stable for 500ms — update title + trigger OCR
                            self._ensure_screen_ocr()
                            if self._screen_ocr:
                                # Layer 0: instant title update
                                self._screen_ocr.update_title(current)
                                # Layer 1+2: background OCR. ScreenOCR coalesces
                                # requests if a previous slow OCR run is still
                                # active, so latest-window refreshes are queued
                                # instead of silently dropped.
                                self._screen_ocr.trigger()
                                _pipeline_log(
                                    "OCR",
                                    f"Window changed, OCR triggered (hwnd={current})",
                                )
                                self._ocr_last_hwnd = current
                except Exception as _e:
                    _pipeline_log("OCR", f"Watcher error: {_e}")
                self._stop_event.wait(0.5)

        self._ocr_watcher_thread = threading.Thread(
            target=_watch, daemon=True, name="ocr-watcher"
        )
        self._ocr_watcher_thread.start()

    def _stop_ocr_watcher(self) -> None:
        """Stop window-change OCR watcher."""
        self._ocr_watcher_thread = (
            None  # Thread checks self.state, will exit on its own
        )

    def _ensure_screen_ocr(self) -> None:
        """Lazy-init ScreenOCR if not yet created."""
        if self._screen_ocr is None:
            try:
                from .core.context.screen_ocr import ScreenOCR

                # Larger cap so Polish gets a reasonable slice of the screen
                # (get_text_for_polish() further caps at ~1200).
                # Compose the OCR-text sink: tracker is the primary consumer,
                # the optional sampler (T6) gets the same payload for offline
                # debugging when enabled.
                # Resolve consumers at callback time, not construction time.
                # Otherwise an OCR object created while learning was off kept
                # a permanent ``tracker=None`` closure after the user enabled
                # "仅自动学习", so nothing was ever accumulated.
                def _ocr_sink(text: str, title: str = "") -> None:
                    tracker = self._auto_hotword_tracker
                    if tracker is not None:
                        try:
                            tracker.record(text, title)
                            # The settings page reads the on-disk pool. Persist
                            # after each deduplicated observation so a user who
                            # opens it immediately can see the new candidate,
                            # rather than waiting for the 30-second scheduler.
                            tracker.save_if_dirty()
                        except Exception as exc:
                            logger.debug(f"tracker.record failed (swallowed): {exc}")
                    sampler = getattr(self, "_ocr_sampler", None)
                    if sampler is not None and sampler.enabled:
                        try:
                            # Re-extract candidate terms cheaply for the
                            # sampler — tracker.record() doesn't return them in
                            # a structured form. Acceptable cost because sample
                            # logging only fires when explicitly enabled.
                            from .core.hotword.session_tracker import (
                                _extract_candidate_terms,
                            )

                            terms = list(_extract_candidate_terms(text))
                            sampler.record(text, title, candidate_terms=terms)
                        except Exception as exc:
                            logger.debug(f"sampler.record failed (swallowed): {exc}")

                self._screen_ocr = ScreenOCR(
                    max_text_len=2000,
                    force_cpu=self._screen_ocr_force_cpu,
                    enable_dml=self._screen_ocr_use_dml,
                    on_text_extracted=_ocr_sink,
                )
            except Exception:
                self._screen_ocr_enabled = False

    def _on_speech_start(self) -> None:
        """Called when speech is detected."""
        logger.debug("Speech detected")
        print("\n[MIC] Speaking...")
        self._emit_voice_activity(True)
        self._on_speech_start_gpu_probe()
        with self._session_lock:
            started = getattr(self, "_session_voice_started_at", None)
            if started is None:
                started = self._session_voice_started_at = {}
            started.setdefault(self._session_count, time.monotonic())

        # Layer 0: Update title keywords instantly (0ms)
        # Layer 1+2: Fire OCR at EVERY speech_start so it races the user's
        # utterance. Typical windows finish in ~2-3s after downscale; very
        # text-dense terminal windows can still take 4-6s.  The
        # Polish layer later uses ScreenOCR's timing predictor to wait only
        # when the in-flight OCR is likely to finish inside the latency budget.
        # ScreenOCR coalesces overlapping requests so this remains bounded.
        if self._screen_ocr_enabled:
            self._ensure_screen_ocr()
            if self._screen_ocr:
                self._screen_ocr.update_title()  # instant
                self._screen_ocr.trigger(force=True)

        # Start streaming ASR (interim results while speaking)
        self._last_interim_text = ""
        self._start_interim_timer()

    def _capture_session_output_target(
        self, session_id: int
    ) -> TargetSnapshot | None:
        """Capture and persist the final-commit target for one ASR session.

        Real OutputInjector capture failures become an invalid snapshot so
        the later insert fails closed. Minimal legacy/test injectors that do
        not implement snapshots return None and keep their isolated contract.
        """
        capture = getattr(self.output_injector, "capture_target_snapshot", None)
        if not callable(capture):
            return None
        try:
            target = capture()
            if not isinstance(target, TargetSnapshot):
                raise TypeError("capture_target_snapshot returned an invalid value")
        except Exception as exc:
            logger.warning(f"Failed to capture output target snapshot: {exc}")
            target = TargetSnapshot(
                hwnd=0,
                pid=0,
                focused_hwnd=0,
                profile=classify_target_surface(),
            )

        with self._session_lock:
            targets = getattr(self, "_session_output_targets", None)
            if targets is None:
                targets = self._session_output_targets = {}
            targets[session_id] = target
        return target

    def _on_speech_end(self, audio, vad_stats=None) -> None:
        """Called when speech ends (true EOS) — queue as 'final' for session commit.

        The worker will run ASR on `audio` (if it has real content), join with any
        buffered soft-split raw segments, then polish ONCE and paste ONCE.
        vad_stats is optional per-segment Silero probability telemetry.
        """
        self._stop_interim_timer()  # Stop streaming ASR
        self._emit_voice_activity(False)

        # Health check: restart worker if it died
        if self._asr_thread and not self._asr_thread.is_alive():
            print("[WARN] ASR worker thread died! Restarting...")
            _pipeline_log("ERROR", "ASR worker thread died, restarting")
            logger.error("ASR worker thread died, restarting")
            self._start_asr_worker()

        # Determine whether we have anything to commit at all.
        audio_usable = audio is not None and len(audio) >= 1600
        with self._session_lock:
            buffered = len(self._session_raw_segments.get(self._session_count, []))
            deferred = len(
                self._session_deferred_audio_segments.get(self._session_count, [])
            )

        if not audio_usable and buffered == 0 and deferred == 0:
            # Nothing to ASR, nothing buffered → truly empty session. Let UI relax.
            return

        # Capture at the commit boundary rather than recording start: users
        # may intentionally switch to the destination while speaking. From
        # here ASR/polish may be slow, so the worker must not paste if focus
        # moves again before output.
        self._capture_session_output_target(self._session_count)

        # Persist the audio-time window before the ASR queue wait.  VAD reports
        # how far the last voiced frame preceded the endpoint; when unavailable
        # we conservatively use the callback time.  The start fallback uses the
        # locally buffered audio duration and never reads transcript content.
        voice_commit_at = time.monotonic()
        trailing_s = 0.0
        if isinstance(vad_stats, dict):
            try:
                trailing_ms = float(vad_stats.get("last_voice_to_commit_ms", 0.0))
                if trailing_ms >= 0.0:
                    trailing_s = min(30.0, trailing_ms / 1000.0)
            except (TypeError, ValueError):
                trailing_s = 0.0
        with self._session_lock:
            started = getattr(self, "_session_voice_started_at", None)
            if started is None:
                started = self._session_voice_started_at = {}
            voice_start = started.get(self._session_count)
            if voice_start is None:
                buffered_seconds = self._session_soft_seg_seconds.get(
                    self._session_count, 0.0
                )
                tail_seconds = (len(audio) / 16000.0) if audio is not None else 0.0
                voice_start = voice_commit_at - buffered_seconds - tail_seconds
            windows = getattr(self, "_session_voice_windows", None)
            if windows is None:
                windows = self._session_voice_windows = {}
            windows[self._session_count] = (
                float(voice_start),
                max(float(voice_start), voice_commit_at - trailing_s),
            )

        pending = self._asr_queue.qsize()
        audio_len = len(audio) if audio is not None else 0
        logger.debug(
            f"Speech ended, {audio_len} samples, {buffered} buffered segments, queuing as 'final'"
        )
        print(
            f"[QUEUE] Final segment queued ({audio_len / 16000:.1f}s, +{buffered} buffered)"
            f" [pending={pending}, deferred_audio={deferred}, worker_busy={self._worker_busy}]"
        )

        # Always enqueue 'final' so the worker runs the commit path exactly once.
        # `audio` may be None or len<1600 — the worker skips ASR for that case
        # but still drains the session buffer and emits insert_complete.
        try:
            self._asr_queue.put_nowait(
                (self._session_count, audio, "final", vad_stats)
            )
        except queue.Full:
            print("[WARN] ASR queue full, dropping final segment")
            # This is a real problem: we'd strand buffered segments. Emit the
            # insert_complete so UI doesn't hang in TRANSCRIBING forever.
            self._emit_insert_complete()

    def _on_speech_soft_split(self, audio, vad_stats=None) -> None:
        """Mid-utterance soft split — queue segment for hidden ASR precompute.

        The worker transcribes this segment and appends raw text to
        `_session_raw_segments`. It does NOT polish, paste, or emit insert_complete.
        Those fire only once per recording session, when 'final' is processed.
        """
        if audio is None or len(audio) < 1600:  # < 0.1s, nothing useful
            return

        if self._asr_thread and not self._asr_thread.is_alive():
            print("[WARN] ASR worker thread died! Restarting...")
            _pipeline_log("ERROR", "ASR worker thread died, restarting")
            logger.error("ASR worker thread died, restarting")
            self._start_asr_worker()

        pending = self._asr_queue.qsize()
        duration = len(audio) / 16000
        print(
            f"[QUEUE] Soft-split segment queued ({duration:.1f}s) [pending={pending}]"
        )
        _pipeline_log(
            "VAD",
            f"Soft-split segment queued ({duration:.1f}s, pending={pending})",
        )

        try:
            self._asr_queue.put_nowait(
                (self._session_count, audio, "soft_split", vad_stats)
            )
        except queue.Full:
            # Soft-split segments are non-critical — dropping one means that chunk's
            # ASR won't be included in the final commit. Log but don't block.
            print("[WARN] ASR queue full, dropping soft-split segment")
            _pipeline_log(
                "VAD",
                f"ASR queue full, soft-split segment LOST ({duration:.1f}s)",
            )

    # ========== Streaming ASR (interim results) ==========

    def _start_interim_timer(self) -> None:
        """启动中间识别定时器"""
        if not self._streaming_config.enabled:
            print("[STREAM] Disabled, skipping timer")
            return

        # Cancel existing timer (but don't increment generation)
        if self._interim_timer:
            self._interim_timer.cancel()
            self._interim_timer = None

        # Capture current generation for the callback
        current_gen = self._interim_generation
        print(
            f"[STREAM] Starting timer (gen={current_gen}, interval={self._streaming_config.chunk_interval_ms}ms)",
            flush=True,
        )
        self._interim_timer = threading.Timer(
            self._streaming_config.chunk_interval_ms / 1000,
            self._do_interim_transcription,
            args=(current_gen,),
        )
        self._interim_timer.daemon = True
        self._interim_timer.start()

    def _stop_interim_timer(self) -> None:
        """停止中间识别定时器"""
        self._interim_generation += 1  # Invalidate any running/pending callbacks
        if self._interim_timer:
            self._interim_timer.cancel()
            self._interim_timer = None

    def _interim_low_confidence(
        self,
        min_logprob: float | None,
        pre_dsp_avg: float,
        vad_prob_max: float,
    ) -> bool:
        """MIN-axis decode-confidence suppress for interim captions.

        Same predicate as the final path's confidence gate (min axis only —
        interim never arms the avg axis), so a fabrication suppressed here
        is the same fabrication the final gate would drop. Misfire cost is
        one caption tick; the final commit re-decides with full stats.
        """
        if min_logprob is None:
            return False
        if not getattr(self, "_conf_gate_min_enabled", True):
            return False
        from .core.asr.acoustic_policy import (
            CONF_GATE_MIN_LOGPROB_FLOOR,
            should_drop_low_confidence,
        )

        return should_drop_low_confidence(
            avg_logprob=None,
            pre_dsp_audio_level_avg=float(pre_dsp_avg),
            vad_prob_max=float(vad_prob_max),
            min_logprob=float(min_logprob),
            min_floor=getattr(
                self, "_conf_gate_min_floor", CONF_GATE_MIN_LOGPROB_FLOOR
            ),
        )

    def _do_interim_transcription(self, generation: int) -> None:
        """执行中间识别（在定时器线程中运行）"""
        try:
            # Check generation token - if mismatched, this callback is stale
            if generation != self._interim_generation:
                print(
                    f"[STREAM] Stale callback (gen={generation}, current={self._interim_generation})"
                )
                return

            # Check if still recording
            if self.state != AppState.RECORDING:
                print(f"[STREAM] Not recording (state={self.state})")
                return

            # Get current speech buffer from VAD
            if not self.audio_capture or not self.audio_capture._vad:
                print("[STREAM] No audio capture or VAD")
                return

            # Check ASR engine is ready
            if not self.asr_engine:
                print("[STREAM] No ASR engine")
                return

            vad = self.audio_capture._vad
            speech_duration_ms = vad.get_speech_duration_ms()
            print(
                f"[STREAM] Check: duration={speech_duration_ms:.0f}ms, min={self._streaming_config.min_speech_ms}ms"
            )

            # Only process if minimum duration reached
            if speech_duration_ms < self._streaming_config.min_speech_ms:
                # Not enough audio yet, schedule next check
                if (
                    self.state == AppState.RECORDING
                    and generation == self._interim_generation
                ):
                    self._start_interim_timer()
                return

            audio = vad.get_current_speech_buffer()
            if audio is None or len(audio) < self._streaming_config.min_chunk_samples:
                if (
                    self.state == AppState.RECORDING
                    and generation == self._interim_generation
                ):
                    self._start_interim_timer()
                return

            cpu_primary_interim = self._active_asr_uses_cpu_runtime()
            asr_queue = getattr(self, "_asr_queue", None)
            asr_queue_pending = bool(asr_queue is not None and not asr_queue.empty())
            if cpu_primary_interim:
                # CPU interim subtitles are optional, but CPU transcribe cannot
                # be cancelled safely.  A timed-out interim thread keeps holding
                # Qwen3ASREngine._lock, so the later final segment appears to
                # “hang” even when CPU/GPU utilization is low.  In CPU mode,
                # reserve the model for final/soft-split commit work only.
                _pipeline_log(
                    "STREAM",
                    "CPU interim skipped: CPU mode reserves ASR for final text",
                )
                if (
                    self.state == AppState.RECORDING
                    and generation == self._interim_generation
                ):
                    self._start_interim_timer()
                return

            # Limit audio length to avoid O(n²) performance degradation
            # GPU can preview a longer rolling buffer. CPU 0.6B only gets a
            # short tail preview so the right-corner text never blocks final ASR.
            max_interim_audio_s = (
                self._CPU_INTERIM_MAX_AUDIO_S
                if cpu_primary_interim
                else self._GPU_INTERIM_MAX_AUDIO_S
            )
            MAX_INTERIM_SAMPLES = int(16000 * max_interim_audio_s)
            if len(audio) > MAX_INTERIM_SAMPLES:
                audio = audio[-MAX_INTERIM_SAMPLES:]

            # Interim recognition is UI-only, but it shares the ASR lock/model
            # with the final commit path.  Recent logs showed short near-silence
            # chunks ("嗯。" / breath) spending 7-16s in Qwen3 before being
            # filtered, which kept the blue processing indicator alive and made
            # the real final segment wait.  Keep the final path permissive, but
            # aggressively skip low-energy interim chunks because dropping an
            # interim subtitle cannot lose committed user text.
            _interim_energy = -1.0
            _interim_p95 = -1.0
            _interim_duration_s = len(audio) / 16000.0
            try:
                import numpy as _np_interim

                _interim_abs = _np_interim.abs(audio)
                _interim_energy = float(_interim_abs.mean())
                _interim_p95 = (
                    float(_np_interim.percentile(_interim_abs, 95))
                    if _interim_abs.size
                    else -1.0
                )
                # Interim text is UI-only. On the CPU runtime stay stricter
                # than the final path so sustained environmental noise
                # (field logs: airplane-like 0.008-0.010 pre-DSP avg) cannot
                # flash/regurgitate old text. On GPU the model judges noise
                # itself (2026-07-04 philosophy) — a raw-energy floor here
                # killed every interim of a quiet-mic session and with it
                # the fast wakeword path (2026-07-26 field incident).
                from .core.asr.acoustic_policy import interim_energy_gate

                _interim_is_cpu = self._active_asr_uses_cpu_runtime()
                _interim_gate = interim_energy_gate(
                    float(self._energy_threshold), _interim_is_cpu
                )
                if _interim_energy < _interim_gate:
                    _pipeline_log(
                        "STREAM",
                        (
                            "Interim skipped: low energy "
                            f"(avg={_interim_energy:.5f} < {_interim_gate:.5f}, "
                            f"duration={_interim_duration_s:.2f}s, "
                            f"runtime={'cpu' if _interim_is_cpu else 'gpu'})"
                        ),
                    )
                    if (
                        self.state == AppState.RECORDING
                        and generation == self._interim_generation
                    ):
                        self._start_interim_timer()
                    return
            except Exception as _energy_exc:
                logger.debug(f"Interim energy gate failed: {_energy_exc}")

            # GPU interim Silero corroboration (2026-07-29 field incident):
            # the 0.0002 floor alone let every ambient-noise chunk in the
            # 0.0002-0.008 band reach the decoder once per second, and Qwen3
            # renders sustained faint noise as fluent encyclopedia lines
            # ("《小王子》是法国作家…") straight into the caption bubble — this
            # path has no VAD joint gate and the text filters never match a
            # 5-6 cps generative sentence. Silero separates the bands (real
            # speech vad_max >= 0.94, fabrication fodder <= 0.67); sentinel
            # (no stats yet) fails open. Faint music (vad 0.7-0.9) still
            # passes — the decode-confidence suppress below is its layer.
            _interim_vad_max = -1.0
            try:
                from .core.asr.acoustic_policy import (
                    INTERIM_VAD_MAX_FLOOR_GPU,
                    should_skip_interim_low_vad,
                )

                _interim_vad_max = float(vad.peek_vad_prob_max())
                if should_skip_interim_low_vad(
                    engine_is_cpu=cpu_primary_interim,
                    vad_prob_max=_interim_vad_max,
                ):
                    _pipeline_log(
                        "STREAM",
                        (
                            "Interim skipped: low VAD "
                            f"(vad_max={_interim_vad_max:.3f} < "
                            f"{INTERIM_VAD_MAX_FLOOR_GPU}, "
                            f"avg={_interim_energy:.5f}, "
                            f"duration={_interim_duration_s:.2f}s)"
                        ),
                    )
                    if (
                        self.state == AppState.RECORDING
                        and generation == self._interim_generation
                    ):
                        self._start_interim_timer()
                    return
            except Exception as _vad_exc:
                logger.debug(f"Interim VAD gate failed: {_vad_exc}")

            # Try to acquire ASR lock (non-blocking) - skip if ASR is busy
            if not self._asr_lock.acquire(blocking=False):
                # ASR busy (likely final transcription), skip this interim
                if (
                    self.state == AppState.RECORDING
                    and generation == self._interim_generation
                ):
                    self._start_interim_timer()
                return

            text_to_emit = None
            try:
                # Double-check after acquiring lock
                if (
                    generation != self._interim_generation
                    or self.state != AppState.RECORDING
                ):
                    return

                # Quick transcription with timeout (no hotword processing for interim)
                # CRITICAL: Must have timeout to prevent deadlock.
                # Without it, a GPU hang holds _asr_lock forever,
                # blocking all final transcriptions.
                import concurrent.futures

                transcribe_engine, engine_label, engine_reason = (
                    self._select_asr_engine_for_segment(
                        "interim",
                        allow_sync_fallback_load=False,
                        audio_duration_s=_interim_duration_s,
                    )
                )
                if transcribe_engine is None:
                    _pipeline_log(
                        "STREAM",
                        f"Interim skipped while ASR route prepares ({engine_label}: {engine_reason})",
                    )
                    if (
                        self.state == AppState.RECORDING
                        and generation == self._interim_generation
                    ):
                        self._start_interim_timer()
                    return

                if engine_label != "primary":
                    _pipeline_log(
                        "STREAM",
                        f"Interim using {engine_label}: {engine_reason}",
                    )

                _interim_capture_mode = getattr(self, "_capture_mode", "standard")

                engine_uses_cpu = cpu_primary_interim
                try:
                    engine_device = self._engine_config_value(
                        transcribe_engine, "device", ""
                    )
                    engine_uses_cpu = (
                        engine_uses_cpu or str(engine_device).lower() == "cpu"
                    )
                except Exception:
                    pass
                INTERIM_TIMEOUT_S = (
                    self._CPU_INTERIM_TIMEOUT_S
                    if engine_uses_cpu
                    else self._GPU_INTERIM_TIMEOUT_S
                )
                _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                try:
                    _future = _executor.submit(
                        transcribe_engine.transcribe,
                        audio,
                        pre_dsp_energy=_interim_energy,
                        pre_dsp_p95=_interim_p95,
                        capture_mode=_interim_capture_mode,
                    )
                    result = _future.result(timeout=INTERIM_TIMEOUT_S)
                except concurrent.futures.TimeoutError:
                    print(
                        f"[STREAM] TIMEOUT: interim transcription exceeded {INTERIM_TIMEOUT_S}s, skipping"
                    )
                    _pipeline_log(
                        "STREAM",
                        f"Interim timeout after {INTERIM_TIMEOUT_S}s",
                    )
                    if transcribe_engine is self.asr_engine:
                        self._mark_primary_asr_timeout(INTERIM_TIMEOUT_S)
                    result = None
                finally:
                    _executor.shutdown(wait=False, cancel_futures=True)

                text = result.text.strip() if result and result.text else ""

                # Snapshot decode confidence under the lock (2026-07-29):
                # last_confidence is per-request state on the llamacpp
                # adapter; a queued final decode would overwrite it the
                # moment we release. Torch/sherpa engines have none → None.
                _interim_min_logprob = None
                _interim_conf = getattr(
                    getattr(transcribe_engine, "_model", None),
                    "last_confidence",
                    None,
                )
                if isinstance(_interim_conf, dict):
                    try:
                        _interim_min_logprob = float(
                            _interim_conf["min_logprob"]
                        )
                    except (TypeError, ValueError, KeyError):
                        _interim_min_logprob = None

                # Final check before emitting
                if generation != self._interim_generation:
                    return

                # Only emit if text changed (avoid flickering)
                if text and text != self._last_interim_text:
                    self._last_interim_text = text
                    text_to_emit = text  # Defer emit until after releasing lock
            finally:
                self._asr_lock.release()

            # Suppress interim emit for noise/hallucination so the subtitle bubble
            # doesn't flash filler words ("嗯") or random regurgitation.
            # Final ASR path (_asr_worker) already has equivalent filters;
            # interim was the remaining source of bubble pollution.
            if text_to_emit:
                import re as _re

                _filler_set = {
                    "嗯",
                    "啊",
                    "哦",
                    "呃",
                    "额",
                    "噢",
                    "唔",
                    "嘶",
                    "哼",
                    "啧",
                    "就",
                    "嗯嗯",
                    "啊啊",
                    "哦哦",
                    "呃呃",
                    "嗯哼",
                    "嗯啊",
                    "嘶嘶",
                    "咚咚",
                }
                _stripped = _re.sub(r"[，。！？、,\.!\?\s]", "", text_to_emit)
                if _stripped in _filler_set:
                    _pipeline_log(
                        "STREAM", f"Interim suppressed (filler): '{text_to_emit}'"
                    )
                    text_to_emit = None
                elif self._is_hallucination(text_to_emit):
                    _pipeline_log(
                        "STREAM",
                        f"Interim suppressed (hallucination): '{text_to_emit}'",
                    )
                    text_to_emit = None
                elif self._interim_low_confidence(
                    _interim_min_logprob, _interim_energy, _interim_vad_max
                ):
                    _pipeline_log(
                        "STREAM",
                        (
                            "Interim suppressed (low confidence: "
                            f"min_logprob={_interim_min_logprob:.3f}, "
                            f"avg={_interim_energy:.5f}, "
                            f"vad_max={_interim_vad_max:.3f}): "
                            f"'{text_to_emit[:60]}'"
                        ),
                    )
                    text_to_emit = None
                else:
                    # Rate guard — same as qwen3_engine: >15 chars/s with >20 chars = regurgitation
                    _audio_dur_s = len(audio) / 16000
                    if _audio_dur_s > 0 and len(text_to_emit) > 20:
                        _cps = len(text_to_emit) / _audio_dur_s
                        if _cps > 15:
                            _pipeline_log(
                                "STREAM",
                                f"Interim suppressed (rate {_cps:.1f}cps): '{text_to_emit[:60]}'",
                            )
                            text_to_emit = None

            # Emit outside lock to avoid blocking final transcription
            if text_to_emit:
                self._try_fast_wakeword_command(text_to_emit, "interim")
                self._emit_text(text_to_emit, is_final=False)
                print(f"[INTERIM] {text_to_emit}")

            # Schedule next interim transcription if still recording
            if (
                self.state == AppState.RECORDING
                and generation == self._interim_generation
            ):
                self._start_interim_timer()

        except Exception as e:
            logger.warning(f"Interim transcription error: {e}")
            # Continue anyway, schedule next attempt
            if (
                self.state == AppState.RECORDING
                and generation == self._interim_generation
            ):
                self._start_interim_timer()

    # ========== End Streaming ASR ==========

    def _maybe_normalize_asr_audio(
        self, audio, *, kind: str = "final", force: bool = False
    ):
        """Opt-in per-utterance peak normalization right before ASR.

        Runs AFTER every VAD/energy gate (their thresholds keep reading the
        raw / capture-mode-DSP signal) and right before the engine transcribe
        call, so all engines — primary, GPU-stall fallback, CPU rescue,
        hallucination retry — receive the same normalized segment.

        Config: audio.asr_gain_normalize / target_peak_dbfs / max_gain_db in
        hotwords.json (default OFF). Hot-reloaded like other audio settings.
        Returns the input unchanged when disabled or on any failure.

        ``force=True`` (energy-gate VAD exemption path) normalizes even when
        the config switch is off AND drops the near-silence RMS floor: a
        collapsed-mic segment sits below MIN_RMS_FOR_GAIN by definition, so
        without the override BOTH gain stages skip it and the engine decodes
        near-silence — the 2026-07-23 empty-transcription failure mode.
        Silero has already vouched for the segment being sustained speech,
        which is what makes the boost safe.
        """
        cfg = getattr(self, "_asr_gain_cfg", None)
        enabled = cfg is not None and cfg.enabled
        if not enabled and not force:
            return audio
        if audio is None or getattr(audio, "size", 0) == 0:
            return audio
        try:
            from .core.audio.gain import (
                DEFAULT_MAX_GAIN_DB,
                DEFAULT_TARGET_PEAK_DBFS,
                apply_peak_normalize,
            )

            target_peak_dbfs = (
                cfg.target_peak_dbfs if cfg is not None else DEFAULT_TARGET_PEAK_DBFS
            )
            max_gain_db = cfg.max_gain_db if cfg is not None else DEFAULT_MAX_GAIN_DB
            extra_kwargs = {}
            if force:
                extra_kwargs["min_rms"] = 0.0
            normalized, gain_db = apply_peak_normalize(
                audio,
                target_peak_dbfs=target_peak_dbfs,
                max_gain_db=max_gain_db,
                **extra_kwargs,
            )
            if gain_db != 0.0:
                _pipeline_log(
                    "GAIN",
                    f"ASR peak-normalize ({kind}{', forced' if force else ''}): "
                    f"{gain_db:+.1f}dB "
                    f"(target={target_peak_dbfs:.1f}dBFS, "
                    f"max=+{max_gain_db:.0f}dB)",
                )
            return normalized
        except Exception as exc:
            logger.warning(f"ASR gain normalize failed: {exc}")
            return audio

    def _asr_worker(self) -> None:
        """Worker thread for ASR transcription (runs in background)."""
        import wave
        import os
        import numpy as np

        logger.info("ASR worker thread started")
        _pipeline_log("ASR", "Worker thread started, waiting for audio...")

        while not self._stop_event.is_set():
            try:
                # Wait for data with timeout to allow checking stop event
                queue_item = self._asr_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            # Unpack 4-tuple (session_id, audio, kind, vad_stats). Older 3-tuple
            # / 2-tuple formats default vad_stats=None for safety.
            vad_stats = None
            if len(queue_item) >= 4:
                session_id, data, kind, vad_stats = (
                    queue_item[0],
                    queue_item[1],
                    queue_item[2],
                    queue_item[3],
                )
            elif len(queue_item) == 3:
                session_id, data, kind = queue_item
            else:
                session_id, data = queue_item[0], queue_item[1]
                kind = "final"

            audio = data
            # Under _rescue_insert_lock so a cloud rescue's atomic pre-insert
            # re-check can never observe "idle" while this segment is starting.
            with self._rescue_insert_lock:
                self._worker_busy = True

            # 'soft_split' iterations must NOT emit insert_complete — it fires
            # exactly once per recording session when the 'final' iteration drains
            # the buffer and completes paste. Set early so `finally` sees it.
            _suppress_insert_complete = kind == "soft_split"
            # 'final' iterations drain THIS session's bucket only; other sessions'
            # buckets (stale in-flight items) are left intact for their own 'final'
            # to drain. Keyed by session_id to prevent cross-session
            # contamination from rapid F11 toggles.
            _prior_buffered_text = ""
            _prior_buffered_count = 0
            _prior_buffered_seconds = 0.0
            _deferred_audio_chunks = []
            if kind == "final":
                with self._session_lock:
                    _popped_segments = self._session_raw_segments.pop(session_id, [])
                    _prior_buffered_count = len(_popped_segments)
                    _prior_buffered_text = "".join(_popped_segments)
                    _prior_buffered_seconds = self._session_soft_seg_seconds.pop(
                        session_id, 0.0
                    )
                    _deferred_audio_chunks = self._session_deferred_audio_segments.pop(
                        session_id, []
                    )
            # Snapshot tail usability BEFORE deferred-audio concat below mutates
            # `audio` — used only by the speech_segments legacy fallback.
            _tail_audio_usable = data is not None and len(data) >= 1600
            # skip_asr short-circuits transcription (for empty/silent audio) but
            # still allows the commit flow to polish+insert the buffered text.
            skip_asr = False

            _pipeline_log(
                "ASR",
                f">>> Got segment: session={session_id}, samples={len(audio) if audio is not None else 0}, "
                f"kind={kind}, buffered_prefix={len(_prior_buffered_text)}chars, pending={self._asr_queue.qsize()}",
            )
            print(
                f"[...] Transcribing... (queue: {self._asr_queue.qsize()} pending, kind={kind})"
            )

            # === Safety guards for corrupted/empty audio ===
            if audio is None or len(audio) == 0:
                _pipeline_log("ASR", "Empty/None audio segment")
                if kind == "soft_split":
                    # Nothing to transcribe, nothing to buffer — silently drop.
                    self._worker_busy = False
                    self._asr_queue.task_done()
                    continue
                # kind == 'final': continue into commit path with empty text.
                audio = np.zeros(0, dtype=np.float32)
                skip_asr = True

            if _deferred_audio_chunks:
                usable_chunks = [
                    np.asarray(chunk, dtype=np.float32)
                    for chunk in _deferred_audio_chunks
                    if chunk is not None and len(chunk) > 0
                ]
                if audio is not None and len(audio) > 0:
                    usable_chunks.append(np.asarray(audio, dtype=np.float32))
                if usable_chunks:
                    try:
                        before_samples = len(audio) if audio is not None else 0
                        audio = np.concatenate(usable_chunks)
                        skip_asr = False
                        _pipeline_log(
                            "SESSION",
                            f"Final retry includes {len(_deferred_audio_chunks)} "
                            f"deferred soft-split audio chunks "
                            f"({before_samples} -> {len(audio)} samples)",
                        )
                    except Exception as concat_exc:
                        _pipeline_log(
                            "ERROR",
                            f"Deferred audio concat failed: {concat_exc}",
                        )

            if not skip_asr and not np.isfinite(audio).all():
                _pipeline_log("ASR", "Audio contains NaN/Inf, sanitizing")
                audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)

            raw_audio_for_retry = (
                np.asarray(audio, dtype=np.float32).copy()
                if audio is not None and len(audio) > 0
                else np.zeros(0, dtype=np.float32)
            )

            # Create debug session
            debug = DebugSession(session_id=session_id, enabled=DebugConfig.enabled)

            # Debug: save audio for inspection (only when debug enabled)
            debug_dir = os.path.join(os.path.dirname(__file__), "DebugLog")
            debug_path = ""

            # Audio level stats (always compute for logging). These are
            # PRE-DSP: this runs before the capture-mode DSP block mutates
            # `audio` below, so the JSON reflects the raw mic envelope.
            audio_level_avg = (
                float(np.abs(audio).mean()) if audio.size else 0.0
            )
            audio_level_max = (
                float(np.abs(audio).max()) if audio.size else 0.0
            )
            audio_level_p95 = (
                float(np.percentile(np.abs(audio), 95)) if audio.size else 0.0
            )
            _vad_avg = -1.0
            _vad_max = -1.0
            _vad_voiced = -1.0
            _vad_endpoint_reason = ""
            _vad_tail_silence_ms = -1.0
            _vad_last_voice_to_commit_ms = -1.0
            _vad_pause_histogram: list = []
            if isinstance(vad_stats, dict):
                try:
                    _vad_avg = float(vad_stats.get("avg", -1.0))
                    _vad_max = float(vad_stats.get("max", -1.0))
                    _vad_voiced = float(vad_stats.get("voiced_ratio", -1.0))
                    # Endpoint telemetry (mining/report.md section C): absent on
                    # older tuple formats / stats-less paths, defaults above.
                    _vad_endpoint_reason = str(
                        vad_stats.get("endpoint_reason", "") or ""
                    )
                    _vad_tail_silence_ms = float(
                        vad_stats.get("tail_silence_ms", -1.0)
                    )
                    _vad_last_voice_to_commit_ms = float(
                        vad_stats.get("last_voice_to_commit_ms", -1.0)
                    )
                    _ph = vad_stats.get("pause_histogram")
                    if isinstance(_ph, (list, tuple)):
                        _vad_pause_histogram = [int(x) for x in _ph]
                except (TypeError, ValueError):
                    pass
            _speech_segments = _session_speech_segment_count(
                vad_stats,
                buffered_count=_prior_buffered_count,
                deferred_count=len(
                    [
                        c
                        for c in _deferred_audio_chunks
                        if c is not None and len(c) > 0
                    ]
                ),
                tail_audio_usable=_tail_audio_usable,
            )

            try:
                if DebugConfig.enabled and DebugConfig.save_audio:
                    os.makedirs(debug_dir, exist_ok=True)
                    debug_path = os.path.join(debug_dir, f"audio_{session_id}.wav")
                    audio_int16 = (audio * 32767).astype("int16")
                    with wave.open(debug_path, "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(16000)
                        wf.writeframes(audio_int16.tobytes())

                # Log audio debug info
                debug.log_audio(
                    duration_seconds=len(audio) / 16000,
                    sample_count=len(audio),
                    sample_rate=16000,
                    channels=1,
                    vad_enabled=(
                        self.audio_capture.config.enable_vad
                        if self.audio_capture
                        else True
                    ),
                    vad_threshold=(
                        self.audio_capture.config.vad_config.threshold
                        if self.audio_capture
                        else 0.5
                    ),
                    speech_segments=_speech_segments,
                    audio_level_avg=audio_level_avg,
                    audio_level_max=audio_level_max,
                    audio_level_p95=audio_level_p95,
                    vad_prob_avg=_vad_avg,
                    vad_prob_max=_vad_max,
                    vad_voiced_ratio=_vad_voiced,
                    endpoint_reason=_vad_endpoint_reason,
                    tail_silence_ms=_vad_tail_silence_ms,
                    last_voice_to_commit_ms=_vad_last_voice_to_commit_ms,
                    pause_histogram=_vad_pause_histogram,
                    audio_file_path=debug_path,
                )
                print(
                    f"[DEBUG] Audio: {len(audio) / 16000:.1f}s, level_avg={audio_level_avg:.4f}, level_max={audio_level_max:.4f}"
                )

            except Exception as e:
                logger.warning(f"Failed to save debug audio: {e}")
                debug.log_error(f"Audio save failed: {e}")

            inserted = False
            final_text = ""
            asr_start = None  # Will be set when transcription starts

            # Snapshot the raw acoustic energy BEFORE DSP so downstream
            # leakage detection (Qwen3) and the whisper-mode UI suppressor
            # below can tell "actual silence + 30dB AGC boost" apart from
            # "real quiet speech". post-DSP audio_level_avg is recomputed
            # right after the chain runs so the energy gate sees the lifted
            # signal — but we MUST keep the pre-DSP value for hallucination
            # gating, otherwise boosted noise looks indistinguishable from
            # normal speech.
            pre_dsp_audio_level_avg = audio_level_avg
            # Reuse the pre-DSP p95 computed above (same raw `audio`); the DSP
            # block below mutates `audio`, so recomputing here would be post-DSP.
            pre_dsp_audio_level_p95 = audio_level_p95

            # === Pre-ASR DSP (capture mode) ===
            # Apply the HPF→Gate→AGC→Limiter chain BEFORE the energy gate so
            # far-field whisper signals get lifted into the gate's pass band
            # rather than being killed for being too quiet. The debug WAV
            # above already captured the original signal — that wav is the
            # raw mic, this point onward is the processed signal that ASR
            # actually sees.
            if (
                not skip_asr
                and audio.size >= 160  # < 10ms = nothing to process
                and getattr(self, "_capture_mode", "standard")
                in ("standard", "noisy", "whisper")
            ):
                try:
                    from .core.audio.dsp import process_with_preset

                    processed = process_with_preset(
                        audio,
                        self._capture_mode,
                        sample_rate=16000,
                        input_gain=getattr(self, "_mic_input_gain", 1.0),
                    )
                    if processed is not None and processed.size == audio.size:
                        audio = processed
                        # Recompute levels so the energy gate below sees the
                        # DSP-lifted signal — otherwise far-field whisper
                        # gets killed for being below the raw amplitude
                        # threshold even though DSP made it audible.
                        audio_level_avg = float(np.abs(audio).mean())
                        audio_level_max = float(np.abs(audio).max())
                        _pipeline_log(
                            "DSP",
                            f"capture_mode={self._capture_mode}: "
                            f"pre={pre_dsp_audio_level_avg:.5f} "
                            f"pre_p95={pre_dsp_audio_level_p95:.5f} "
                            f"post={audio_level_avg:.4f} "
                            f"gain={getattr(self, '_mic_input_gain', 1.0):.2f}x "
                            f"vad_avg={_vad_avg:.3f} "
                            f"vad_max={_vad_max:.3f} "
                            f"voiced={_vad_voiced:.3f}",
                        )
                except Exception as exc:
                    logger.warning(f"capture-mode DSP failed: {exc}")

            # === Pre-ASR Acoustic Gate ===
            # Skip ASR entirely if audio energy is near-silence (saves 1-15s of transcription time).
            # VAD can false-trigger on keyboard clicks, mouse clicks, etc. — this catches those.
            # Configurable via settings: vad.energy_threshold (default 0.003)
            energy_gate = self._energy_threshold
            audio_duration_s = (len(audio) / 16000.0) if audio is not None else 0.0
            if not skip_asr and self._should_skip_buffered_final_tail(
                kind=kind,
                buffered_text=_prior_buffered_text,
                duration_s=audio_duration_s,
                pre_dsp_audio_level_avg=pre_dsp_audio_level_avg,
                post_dsp_audio_level_avg=audio_level_avg,
                energy_gate=energy_gate,
                capture_mode=getattr(self, "_capture_mode", "standard"),
            ):
                _pipeline_log(
                    "ASR",
                    "Buffered-final tail gate: "
                    f"duration={audio_duration_s:.2f}s, "
                    f"pre={pre_dsp_audio_level_avg:.5f}, "
                    f"post={audio_level_avg:.5f}, "
                    f"buffered={len(_prior_buffered_text)}chars; skipping ASR",
                )
                print(
                    "[ASR] Skipped: buffered final tail is near-silence "
                    f"({audio_duration_s:.2f}s, avg={audio_level_avg:.5f})"
                )
                self._record_noise_gate_drop(
                    gate="buffered_final_tail",
                    session_id=session_id,
                    kind=kind,
                    duration_s=audio_duration_s,
                    pre_dsp_avg=pre_dsp_audio_level_avg,
                    post_dsp_avg=audio_level_avg,
                    pre_dsp_p95=pre_dsp_audio_level_p95,
                    energy_gate=energy_gate,
                    vad_prob_avg=_vad_avg,
                    vad_prob_max=_vad_max,
                    vad_voiced_ratio=_vad_voiced,
                    capture_mode=getattr(self, "_capture_mode", "standard"),
                    buffered_chars=len(_prior_buffered_text),
                )
                skip_asr = True

            if not skip_asr and self._should_skip_unbuffered_low_energy_final(
                kind=kind,
                buffered_text=_prior_buffered_text,
                has_deferred_audio=bool(_deferred_audio_chunks),
                duration_s=audio_duration_s,
                pre_dsp_audio_level_avg=pre_dsp_audio_level_avg,
                pre_dsp_audio_level_p95=pre_dsp_audio_level_p95,
                energy_gate=energy_gate,
                capture_mode=getattr(self, "_capture_mode", "standard"),
                engine_is_cpu=self._active_asr_uses_cpu_runtime(),
                vad_prob_max=_vad_max,
                vad_voiced_ratio=_vad_voiced,
            ):
                _pipeline_log(
                    "ASR",
                    "Unbuffered final noise gate (cpu runtime): "
                    f"duration={audio_duration_s:.2f}s, "
                    f"pre={pre_dsp_audio_level_avg:.5f}, "
                    f"post={audio_level_avg:.5f}; skipping ASR",
                )
                print(
                    "[ASR] Skipped: low-energy final noise "
                    f"({audio_duration_s:.2f}s, raw_avg={pre_dsp_audio_level_avg:.5f})"
                )
                self._record_noise_gate_drop(
                    gate="unbuffered_low_energy_final",
                    session_id=session_id,
                    kind=kind,
                    duration_s=audio_duration_s,
                    pre_dsp_avg=pre_dsp_audio_level_avg,
                    post_dsp_avg=audio_level_avg,
                    pre_dsp_p95=pre_dsp_audio_level_p95,
                    energy_gate=energy_gate,
                    vad_prob_avg=_vad_avg,
                    vad_prob_max=_vad_max,
                    vad_voiced_ratio=_vad_voiced,
                    capture_mode=getattr(self, "_capture_mode", "standard"),
                )
                # Fall through with skip_asr=True rather than `continue` so the
                # UI still receives insert_complete promptly for this final.
                skip_asr = True

            _energy_gate_exempted = False
            if not skip_asr and audio_level_avg < energy_gate:
                from .core.asr.acoustic_policy import (
                    should_exempt_energy_gate_high_vad,
                )

                if getattr(
                    self, "_energy_gate_vad_exempt_enabled", True
                ) and should_exempt_energy_gate_high_vad(
                    kind=kind,
                    duration_s=audio_duration_s,
                    vad_prob_max=_vad_max,
                    vad_voiced_ratio=_vad_voiced,
                ):
                    _energy_gate_exempted = True
                    # Collapsed-mic rescue (2026-07-23): Silero is confident
                    # this is sustained speech, so decode it anyway — the
                    # peak-normalize stage lifts the level before the engine.
                    _pipeline_log(
                        "ASR",
                        "Energy gate EXEMPT: high-VAD speech below gate "
                        f"(avg={audio_level_avg:.5f} < {energy_gate}, "
                        f"vad_max={_vad_max:.3f}, voiced={_vad_voiced:.3f}, "
                        f"duration={audio_duration_s:.2f}s); decoding",
                    )
                    print(
                        "[ASR] Energy gate exempt: high-VAD quiet speech "
                        f"(avg={audio_level_avg:.5f}, vad_max={_vad_max:.2f})"
                    )
                    self._record_noise_gate_drop(
                        event="noise_gate_exempt",
                        gate="energy_threshold",
                        session_id=session_id,
                        kind=kind,
                        duration_s=audio_duration_s,
                        pre_dsp_avg=pre_dsp_audio_level_avg,
                        post_dsp_avg=audio_level_avg,
                        pre_dsp_p95=pre_dsp_audio_level_p95,
                        energy_gate=energy_gate,
                        vad_prob_avg=_vad_avg,
                        vad_prob_max=_vad_max,
                        vad_voiced_ratio=_vad_voiced,
                        capture_mode=getattr(self, "_capture_mode", "standard"),
                        buffered_chars=len(_prior_buffered_text),
                    )
                    self._note_low_level_speech()
                else:
                    _pipeline_log(
                        "ASR",
                        f"Pre-ASR gate: audio too quiet (avg={audio_level_avg:.5f} < {energy_gate}), skipping ASR",
                    )
                    print(
                        f"[ASR] Skipped: audio too quiet (avg={audio_level_avg:.5f})"
                    )
                    self._record_noise_gate_drop(
                        gate="energy_threshold",
                        session_id=session_id,
                        kind=kind,
                        duration_s=audio_duration_s,
                        pre_dsp_avg=pre_dsp_audio_level_avg,
                        post_dsp_avg=audio_level_avg,
                        pre_dsp_p95=pre_dsp_audio_level_p95,
                        energy_gate=energy_gate,
                        vad_prob_avg=_vad_avg,
                        vad_prob_max=_vad_max,
                        vad_voiced_ratio=_vad_voiced,
                        capture_mode=getattr(self, "_capture_mode", "standard"),
                        buffered_chars=len(_prior_buffered_text),
                    )
                    if kind == "soft_split" or not _prior_buffered_text:
                        # Nothing to commit → drop entirely.
                        self._worker_busy = False
                        self._asr_queue.task_done()
                        continue
                    # 'final' with buffered segments: fall through so commit runs.
                    skip_asr = True
            elif not skip_asr:
                # Level is back above the gate — the mic path is healthy, so
                # any collapsed-mic warning streak is over.
                self._low_level_speech_streak = 0

            # === VAD-probability joint gate (anti-hallucination, H2) ===
            # The energy gate passed, but both energies are near-silent AND
            # Silero never scored the segment as confident speech — the exact
            # profile of the "light noise → invented encyclopedia sentence"
            # decodes (cluster_20260720/hallucination checkpoint_H1). Refuse
            # to decode; dispatch mirrors the energy gate above.
            if not skip_asr and getattr(self, "_vad_joint_gate_enabled", True):
                from .core.asr.acoustic_policy import (
                    VAD_JOINT_GATE_ENERGY_MAX,
                    VAD_JOINT_GATE_VAD_MAX_BELOW,
                    should_skip_low_vad_joint,
                )

                _joint_energy_max = getattr(
                    self, "_vad_joint_gate_energy_max", VAD_JOINT_GATE_ENERGY_MAX
                )
                _joint_vad_max = getattr(
                    self, "_vad_joint_gate_vad_max", VAD_JOINT_GATE_VAD_MAX_BELOW
                )
                if should_skip_low_vad_joint(
                    kind=kind,
                    pre_dsp_audio_level_avg=pre_dsp_audio_level_avg,
                    post_dsp_audio_level_avg=audio_level_avg,
                    vad_prob_max=_vad_max,
                    capture_mode=getattr(self, "_capture_mode", "standard"),
                    energy_ceiling=_joint_energy_max,
                    vad_max_ceiling=_joint_vad_max,
                ):
                    _pipeline_log(
                        "ASR",
                        "VAD joint gate: near-silence with weak VAD "
                        f"(pre={pre_dsp_audio_level_avg:.5f}, "
                        f"post={audio_level_avg:.5f} < "
                        f"{_joint_energy_max}, "
                        f"vad_max={_vad_max:.3f} < "
                        f"{_joint_vad_max}); skipping ASR",
                    )
                    print(
                        "[ASR] Skipped: low VAD confidence on near-silence "
                        f"(vad_max={_vad_max:.2f}, "
                        f"avg={audio_level_avg:.5f})"
                    )
                    self._record_noise_gate_drop(
                        gate="vad_joint",
                        session_id=session_id,
                        kind=kind,
                        duration_s=audio_duration_s,
                        pre_dsp_avg=pre_dsp_audio_level_avg,
                        post_dsp_avg=audio_level_avg,
                        pre_dsp_p95=pre_dsp_audio_level_p95,
                        energy_gate=energy_gate,
                        vad_prob_avg=_vad_avg,
                        vad_prob_max=_vad_max,
                        vad_voiced_ratio=_vad_voiced,
                        capture_mode=getattr(self, "_capture_mode", "standard"),
                        buffered_chars=len(_prior_buffered_text),
                    )
                    if kind == "soft_split" or not _prior_buffered_text:
                        # Nothing to commit → drop entirely.
                        self._worker_busy = False
                        self._asr_queue.task_done()
                        continue
                    # 'final' with buffered segments: fall through to commit.
                    skip_asr = True

            # === Deep Sleep Guard (BEFORE transcribe) ===
            # Model may have been unloaded — skip to avoid crash
            with self._lock:
                _is_deep_sleep = self._sleep_mode == SleepMode.DEEP
            if _is_deep_sleep:
                print("[ASR] Deep sleep: skipping transcription")
                if kind == "soft_split" or not _prior_buffered_text:
                    self._worker_busy = False
                    self._asr_queue.task_done()
                    continue
                skip_asr = True

            try:
                # Transcribe (Layer 1: initial_prompt already set)
                import time as time_module

                asr_start = time_module.time()
                transcribe_engine = self.asr_engine
                engine_label = "primary"
                engine_reason = "not_selected"

                if skip_asr:
                    # No usable audio for this iteration. For kind=='final' with buffered
                    # text we still need to run polish+insert on the accumulated session
                    # raw text; just short-circuit ASR with empty text.
                    _pipeline_log(
                        "ASR",
                        f"skip_asr=True ({kind}), falling through with empty this-segment text",
                    )
                    text = ""
                    asr_time = 0.0
                    initial_prompt = (
                        self.hotword_manager.build_initial_prompt()
                        if self.hotword_manager
                        else ""
                    )
                    initial_prompt_enabled = (
                        self.hotword_manager.config.enable_initial_prompt
                        if self.hotword_manager
                        else False
                    )
                    debug.log_asr(
                        model_name=(
                            self.asr_engine.config.model_name
                            if self.asr_engine
                            else "unknown"
                        ),
                        device=(
                            self.asr_engine.config.device
                            if self.asr_engine
                            else "unknown"
                        ),
                        language=(
                            self.asr_engine.config.language if self.asr_engine else "zh"
                        ),
                        audio_duration=0.0,
                        initial_prompt=initial_prompt,
                        initial_prompt_enabled=initial_prompt_enabled,
                        raw_text="",
                        transcribe_time_ms=0,
                    )
                else:
                    _pipeline_log("ASR", "Starting transcription...")

                # Keep title context fresh (0ms) regardless — Polish layer
                # reads the OCR result later via get_text_for_polish(), and
                # title is that method's fallback when OCR is stale.
                if not skip_asr and self._screen_ocr:
                    self._screen_ocr.update_title()

                if not skip_asr:
                    # Loudness normalization LAST before ASR: all gates above
                    # already ran on the un-normalized signal, and `audio` is
                    # reused by the fallback/rescue/retry transcribe calls
                    # below, so one application covers every engine path.
                    # Exempted collapsed-mic segments force the boost (their
                    # RMS sits below the normal near-silence floor).
                    audio = self._maybe_normalize_asr_audio(
                        audio, kind=kind, force=_energy_gate_exempted
                    )

                    transcribe_engine, engine_label, engine_reason = (
                        self._select_asr_engine_for_segment(
                            kind,
                            allow_sync_fallback_load=True,
                            audio_duration_s=audio_duration_s,
                        )
                    )
                    if transcribe_engine is None:
                        transcribe_engine = self.asr_engine
                        engine_label = "primary"
                        engine_reason = "fallback_unavailable"

                    if not engine_label.startswith("primary"):
                        print(f"[ASR] Using {engine_label}: {engine_reason}")

                    # In-flight prefix: earlier soft-split segments of THIS same
                    # utterance so the current chunk transcribes with continuity.
                    # final popped its bucket into _prior_buffered_text already;
                    # soft_split peeks the still-growing bucket (segments 1..N-1).
                    if kind == "final":
                        self._active_session_ctx_prefix = _prior_buffered_text or ""
                    elif kind == "soft_split":
                        with self._session_lock:
                            self._active_session_ctx_prefix = "".join(
                                self._session_raw_segments.get(session_id, [])
                            )
                    else:
                        self._active_session_ctx_prefix = ""

                    self._prepare_asr_engine_for_segment(
                        transcribe_engine,
                        pre_dsp_audio_level_avg,
                        pre_dsp_audio_level_p95,
                    )

                    # Slow-stage indicator: after 3s, tell ball to show GPU-slow glow
                    _slow_hint_timer = threading.Timer(
                        3.0,
                        lambda: (
                            self._bridge.emit_slow_stage("gpu")
                            if self._bridge
                            else None
                        ),
                    )
                    _slow_hint_timer.daemon = True
                    _slow_hint_timer.start()

                    ASR_TIMEOUT_S = 30
                    (
                        result,
                        engine_label,
                        engine_reason,
                        transcribe_engine,
                    ) = self._transcribe_with_gpu_stall_fallback(
                        audio=audio,
                        transcribe_engine=transcribe_engine,
                        engine_label=engine_label,
                        engine_reason=engine_reason,
                        kind=kind,
                        pre_dsp_audio_level_avg=pre_dsp_audio_level_avg,
                        pre_dsp_audio_level_p95=pre_dsp_audio_level_p95,
                        capture_mode=getattr(self, "_capture_mode", "standard"),
                        asr_timeout_s=ASR_TIMEOUT_S,
                    )
                    _slow_hint_timer.cancel()  # Cancel ASR slow hint
                    asr_time = (time_module.time() - asr_start) * 1000
                    text = result.text.strip() if result and result.text else ""
                    _pipeline_log(
                        "ASR",
                        f"Transcription done via {engine_label}: '{text}' ({asr_time:.0f}ms)",
                    )

                    # Slow-but-completed sample: snapshot GPU pstate/clocks/
                    # power to explain "idle GPU yet way over realtime" runs.
                    # Throttled + async in gpu_diag; adds no latency here.
                    slow_threshold_ms = max(8000.0, audio_duration_s * 3000.0)
                    if asr_time > slow_threshold_ms:
                        from .core.asr.gpu_diag import maybe_snapshot_async

                        maybe_snapshot_async("slow_transcription", _pipeline_log)

                    from .core.asr.acoustic_policy import RESCUE_DEFER_ENERGY_FLOOR

                    if (
                        not text
                        and kind == "final"
                        and not str(engine_label).startswith("cpu_fallback")
                        and raw_audio_for_retry.size >= 1600
                        and pre_dsp_audio_level_avg
                        >= max(float(energy_gate), RESCUE_DEFER_ENERGY_FLOOR)
                    ):
                        fallback_engine = self._loaded_gpu_fallback_engine()
                        if fallback_engine is None:
                            fallback_engine = self._ensure_gpu_fallback_engine(
                                f"final empty rescue after {engine_label}"
                            )
                        if (
                            fallback_engine is not None
                            and fallback_engine is not transcribe_engine
                        ):
                            rescue_timeout_s = max(
                                30.0, min(60.0, 15.0 + audio_duration_s * 3.0)
                            )
                            _pipeline_log(
                                "GPU-FALLBACK",
                                "Final primary path returned empty; retrying "
                                f"audio-only CPU rescue ({engine_label}, "
                                f"duration={audio_duration_s:.1f}s, "
                                f"timeout={rescue_timeout_s:.0f}s)",
                            )
                            self._prepare_asr_engine_for_segment(
                                fallback_engine,
                                pre_dsp_audio_level_avg,
                                pre_dsp_audio_level_p95,
                            )
                            (
                                rescue_result,
                                rescue_label,
                                rescue_reason,
                                rescue_engine,
                            ) = self._transcribe_with_gpu_stall_fallback(
                                audio=audio,
                                transcribe_engine=fallback_engine,
                                engine_label="cpu_fallback_final_rescue",
                                engine_reason=engine_reason,
                                kind=kind,
                                pre_dsp_audio_level_avg=pre_dsp_audio_level_avg,
                                pre_dsp_audio_level_p95=pre_dsp_audio_level_p95,
                                capture_mode=getattr(
                                    self, "_capture_mode", "standard"
                                ),
                                asr_timeout_s=rescue_timeout_s,
                            )
                            rescue_text = (
                                rescue_result.text.strip()
                                if rescue_result and rescue_result.text
                                else ""
                            )
                            if rescue_text:
                                text = rescue_text
                                engine_label = rescue_label
                                engine_reason = rescue_reason
                                transcribe_engine = rescue_engine
                                asr_time = (time_module.time() - asr_start) * 1000
                                _pipeline_log(
                                    "ASR",
                                    f"Final CPU rescue recovered: '{text}' "
                                    f"({asr_time:.0f}ms)",
                                )
                            else:
                                _pipeline_log(
                                    "ASR",
                                    "Final CPU rescue returned empty; preserving "
                                    "empty result",
                                )

                    # === Final-segment failure accounting + rescue chain ===
                    # Timeouts surface as result=None; engine exceptions are
                    # swallowed into empty text upstream (qwen3_engine returns
                    # text="" on its top-level except).  Genuine silence is
                    # excluded via the energy floor inside the classifier.
                    from .core.asr.rescue_policy import (
                        classify_final_failure,
                        resolve_failure_action,
                        should_notify_failure,
                        will_defer_soft_split_audio,
                    )

                    _failure_kind = classify_final_failure(
                        kind=kind,
                        skip_asr=skip_asr,
                        text=text,
                        timed_out=(result is None),
                        pre_dsp_audio_level_avg=pre_dsp_audio_level_avg,
                        energy_gate=energy_gate,
                        engine_error=str(engine_label).endswith("_error"),
                    )
                    # A deferred soft_split rides the final retry (not a loss);
                    # a final tail with buffered text still commits (partial
                    # success).  Both are telemetry-only: no streak, no rescue.
                    # A full deferred-audio bucket means the chunk gets DROPPED
                    # by the dispatch below, so it must route as a real loss.
                    # Same worker thread reads the bucket here and appends in
                    # the dispatch — no interleaving writer, so both sites see
                    # the same occupancy.
                    _defer_bucket_full = False
                    if _failure_kind and kind == "soft_split":
                        from .core.asr.rescue_policy import DEFER_BUCKET_MAX

                        with self._session_lock:
                            _defer_bucket_full = (
                                len(
                                    self._session_deferred_audio_segments.get(
                                        session_id, []
                                    )
                                )
                                >= DEFER_BUCKET_MAX
                            )
                    _failure_action = resolve_failure_action(
                        failure_kind=_failure_kind,
                        kind=kind,
                        has_prior_buffered_text=bool(_prior_buffered_text),
                        will_defer_audio=will_defer_soft_split_audio(
                            skip_asr=skip_asr,
                            audio_samples=raw_audio_for_retry.size,
                            pre_dsp_audio_level_avg=pre_dsp_audio_level_avg,
                            energy_gate=energy_gate,
                            asr_time_ms=asr_time,
                            engine_label=engine_label,
                        ),
                        defer_bucket_full=_defer_bucket_full,
                    )
                    if _failure_action in ("deferred", "partial"):
                        _pipeline_log(
                            "RESCUE",
                            f"Final-segment failure ({_failure_kind}) absorbed "
                            f"as {_failure_action}: {kind}, "
                            f"{audio_duration_s:.1f}s, {engine_label}",
                        )
                        self._record_asr_failure(
                            kind=kind,
                            failure=_failure_kind,
                            audio_duration_s=audio_duration_s,
                            elapsed_ms=asr_time,
                            engine_label=engine_label,
                            transcribe_engine=transcribe_engine,
                            rescue_outcome=_failure_action,
                            session_id=session_id,
                        )
                    elif _failure_action in ("count", "count_bucket_full"):
                        self._asr_failure_streak += 1
                        _segment_done_at = time_module.time()
                        _pipeline_log(
                            "RESCUE",
                            f"Final-segment failure #{self._asr_failure_streak}: "
                            f"{_failure_kind} ({kind}, {audio_duration_s:.1f}s, "
                            f"{asr_time:.0f}ms, {engine_label})",
                        )
                        _cloud_launched = False
                        _reload_launched = False
                        try:
                            _reload_launched = self._maybe_self_heal_asr_engine(
                                _failure_kind
                            )
                            # Cloud rescue only covers 'final' (audio here
                            # already includes any deferred chunks); deferred
                            # soft_splits never reach this branch.
                            _cloud_launched = self._launch_cloud_asr_rescue(
                                session_id=session_id,
                                kind=kind,
                                audio=audio,
                                segment_done_at=_segment_done_at,
                            )
                        except Exception as _rescue_exc:
                            logger.warning(f"Rescue chain error: {_rescue_exc}")
                        if _reload_launched:
                            _rescue_outcome = "reload"
                        elif _cloud_launched:
                            _rescue_outcome = "cloud_pending"
                        else:
                            _rescue_outcome = "none"
                        if _failure_action == "count_bucket_full":
                            # Chunk dropped by the full deferred-audio bucket:
                            # keep both facts in the outcome.
                            _rescue_outcome = (
                                "dropped_bucket_full"
                                if _rescue_outcome == "none"
                                else f"dropped_bucket_full+{_rescue_outcome}"
                            )
                        self._record_asr_failure(
                            kind=kind,
                            failure=_failure_kind,
                            audio_duration_s=audio_duration_s,
                            elapsed_ms=asr_time,
                            engine_label=engine_label,
                            transcribe_engine=transcribe_engine,
                            rescue_outcome=_rescue_outcome,
                            session_id=session_id,
                        )
                        # Toast only for a whole-commit, confirmed engine
                        # fault. Ambiguous empty decodes remain diagnostic-only
                        # so background sound and short pauses do not create
                        # false red warnings. "正在补救" is only promised when
                        # a cloud second pass was actually launched.
                        if kind in ("final", "selection") and should_notify_failure(
                            failure_kind=_failure_kind,
                            voiced_ratio=_vad_voiced,
                            consecutive_failures=self._asr_failure_streak,
                        ):
                            self._notify_final_asr_failure(_cloud_launched)
                    elif kind in ("final", "soft_split", "selection") and text:
                        self._asr_failure_streak = 0

                    # Get initial prompt info
                    initial_prompt = (
                        self.hotword_manager.build_initial_prompt()
                        if self.hotword_manager
                        else ""
                    )
                    initial_prompt_enabled = (
                        self.hotword_manager.config.enable_initial_prompt
                        if self.hotword_manager
                        else False
                    )

                    # Log ASR debug info
                    debug.log_asr(
                        model_name=self._engine_config_value(
                            transcribe_engine, "model_name", "unknown"
                        ),
                        device=self._engine_config_value(
                            transcribe_engine, "device", "unknown"
                        ),
                        language=self._engine_config_value(
                            transcribe_engine, "language", "zh"
                        ),
                        audio_duration=len(audio) / 16000,
                        initial_prompt=initial_prompt,
                        initial_prompt_enabled=initial_prompt_enabled,
                        raw_text=text,
                        transcribe_time_ms=asr_time,
                    )
                    print(f"[ASR] raw via {engine_label}: '{text}' ({asr_time:.0f}ms)")

                # === Decode-confidence telemetry + opt-in gate (H2) ===
                # llamacpp adapter exposes per-token logprob stats of the last
                # request on model.last_confidence; snapshot NOW, before the
                # hallucination-retry paths below overwrite it with a retry's
                # stats. Torch/sherpa models have no such attribute → None.
                _avg_logprob = None
                _min_logprob = None
                if not skip_asr:
                    _decode_conf = getattr(
                        getattr(transcribe_engine, "_model", None),
                        "last_confidence",
                        None,
                    )
                    if isinstance(_decode_conf, dict):
                        try:
                            _avg_logprob = float(_decode_conf["avg_logprob"])
                            _min_logprob = float(
                                _decode_conf.get("min_logprob", 0.0)
                            )
                            _pipeline_log(
                                "ASR",
                                "decode confidence: "
                                f"avg_logprob={_avg_logprob:.4f} "
                                f"min={_min_logprob:.4f} "
                                f"tokens={int(_decode_conf.get('tokens', 0))}",
                            )
                        except (TypeError, ValueError, KeyError):
                            _avg_logprob = None
                            _min_logprob = None
                _conf_gate_avg_armed = bool(
                    _avg_logprob is not None
                    and getattr(self, "_conf_gate_enabled", False)
                )
                _conf_gate_min_armed = bool(
                    _min_logprob is not None
                    and getattr(self, "_conf_gate_min_enabled", True)
                )
                if text and (_conf_gate_avg_armed or _conf_gate_min_armed):
                    from .core.asr.acoustic_policy import (
                        CONF_GATE_AVG_LOGPROB_FLOOR,
                        CONF_GATE_MIN_LOGPROB_FLOOR,
                        should_drop_low_confidence,
                    )

                    _conf_floor = getattr(
                        self, "_conf_gate_floor", CONF_GATE_AVG_LOGPROB_FLOOR
                    )
                    _conf_min_floor = getattr(
                        self, "_conf_gate_min_floor", CONF_GATE_MIN_LOGPROB_FLOOR
                    )
                    if should_drop_low_confidence(
                        avg_logprob=(
                            _avg_logprob if _conf_gate_avg_armed else None
                        ),
                        pre_dsp_audio_level_avg=pre_dsp_audio_level_avg,
                        vad_prob_max=_vad_max,
                        floor=_conf_floor,
                        min_logprob=(
                            _min_logprob if _conf_gate_min_armed else None
                        ),
                        min_floor=_conf_min_floor,
                    ):
                        print(
                            "[ASR] Dropped: low decode confidence "
                            f"(avg_logprob={_avg_logprob:.3f}, "
                            f"min_logprob={_min_logprob:.3f}) "
                            "on weak acoustics"
                        )
                        _pipeline_log(
                            "NOISE",
                            "Confidence gate drop: "
                            f"avg_logprob={_avg_logprob:.4f}, "
                            f"min_logprob={_min_logprob:.4f}, "
                            f"pre={pre_dsp_audio_level_avg:.5f}, "
                            f"vad_max={_vad_max:.3f}, {len(text)} chars",
                        )
                        self._record_hallucination_drop(
                            reason="low_confidence",
                            session_id=session_id,
                            kind=kind,
                            duration_s=audio_duration_s,
                            pre_dsp_avg=pre_dsp_audio_level_avg,
                            vad_prob_max=_vad_max,
                            text_len=len(text),
                            capture_mode=getattr(
                                self, "_capture_mode", "standard"
                            ),
                            avg_logprob=_avg_logprob,
                            min_logprob=_min_logprob,
                        )
                        debug.log_error(
                            f"Confidence gate dropped {len(text)} chars "
                            f"(avg_logprob={_avg_logprob:.3f}, "
                            f"min_logprob={_min_logprob:.3f})"
                        )
                        text = ""

                # Fix ASR sentence repetition bug (before hallucination check)
                if text:
                    deduped = self._deduplicate_sentences(text)
                    if deduped != text:
                        print(f"[ASR] Deduplicated: '{text}' -> '{deduped}'")
                        text = deduped

                # === Template-sentence blacklist (anti-hallucination, H2) ===
                # Whole segment exactly equals a classic subtitle-corpus
                # hallucination ("谢谢观看" family / config extras) AND the
                # raw mic was near-silent → certain fabrication; drop without
                # the retry dance (re-decoding noise just rolls new dice).
                if text and getattr(self, "_template_blacklist_enabled", True):
                    from .core.asr.acoustic_policy import find_template_sentence

                    _tpl_hit = find_template_sentence(
                        text,
                        pre_dsp_audio_level_avg=pre_dsp_audio_level_avg,
                        capture_mode=getattr(self, "_capture_mode", "standard"),
                        extra=getattr(self, "_template_blacklist_extra", ()),
                    )
                    if _tpl_hit:
                        print(
                            f"[ASR] Filtered template hallucination: '{text}' "
                            f"(pre_dsp={pre_dsp_audio_level_avg:.5f})"
                        )
                        _pipeline_log(
                            "NOISE",
                            f"Template blacklist hit '{_tpl_hit}' at "
                            f"near-silence (pre={pre_dsp_audio_level_avg:.5f}, "
                            f"{len(text)} chars)",
                        )
                        self._record_hallucination_drop(
                            reason="template_sentence",
                            session_id=session_id,
                            kind=kind,
                            duration_s=audio_duration_s,
                            pre_dsp_avg=pre_dsp_audio_level_avg,
                            vad_prob_max=_vad_max,
                            text_len=len(text),
                            capture_mode=getattr(
                                self, "_capture_mode", "standard"
                            ),
                            detail=_tpl_hit,
                        )
                        debug.log_error(
                            f"Template hallucination filtered: '{_tpl_hit}'"
                        )
                        text = ""

                # Filter ASR hallucinations (applies to all engines)
                # Enhanced: retry once on hallucination, then fallback to interim result
                if text and self._is_hallucination(text):
                    print(f"[ASR] Detected hallucination: '{text}'")

                    # Strategy 1: Retry transcription once (cold-start hallucinations often clear on retry)
                    print("[ASR] Retrying transcription...")
                    retry_start = time_module.time()
                    with self._asr_lock:
                        retry_result = transcribe_engine.transcribe(
                            audio,
                            pre_dsp_energy=pre_dsp_audio_level_avg,
                            pre_dsp_p95=pre_dsp_audio_level_p95,
                            capture_mode=getattr(self, "_capture_mode", "standard"),
                        )
                    retry_time = (time_module.time() - retry_start) * 1000
                    retry_text = (
                        retry_result.text.strip()
                        if retry_result and retry_result.text
                        else ""
                    )
                    print(f"[ASR] retry: '{retry_text}' ({retry_time:.0f}ms)")

                    if retry_text and not self._is_hallucination(retry_text):
                        # Retry succeeded - use retry result
                        print(f"[ASR] Retry succeeded, using: '{retry_text}'")
                        text = retry_text
                        debug.log_error(
                            f"Hallucination recovered via retry: '{retry_text}'"
                        )
                    elif kind == "final":
                        # Strategy 2: Fallback to interim result — only safe for 'final'
                        # kind. For 'soft_split', _last_interim_text tracks the ongoing
                        # recording and would contain later-segment text, causing silent
                        # cross-segment contamination. Drop instead.
                        interim_text = self._last_interim_text
                        if (
                            interim_text
                            and len(interim_text) > 3
                            and not self._is_hallucination(interim_text)
                        ):
                            print(f"[ASR] Using interim fallback: '{interim_text}'")
                            text = interim_text
                            debug.log_error(
                                f"Hallucination recovered via interim: '{interim_text}'"
                            )
                        else:
                            # Both strategies failed
                            print(
                                f"[ASR] Filtered hallucination (no recovery): '{text}'"
                            )
                            debug.log_error(f"Hallucination filtered: '{text}'")
                            text = ""
                    else:
                        # soft_split + retry failed → drop without interim fallback
                        print(
                            f"[ASR] soft_split: filtered hallucination (no interim fallback): '{text}'"
                        )
                        debug.log_error(
                            f"Hallucination filtered (soft_split, no interim): '{text}'"
                        )
                        text = ""

                # Post-ASR: context leakage detection
                # Checks: 1) text impossibly long for audio duration (any length)
                #          2) text is verbatim substring of recent context
                #          3) whisper-mode regurgitation (token-overlap fuzzy match)
                if text:
                    audio_duration_s = len(audio) / 16000
                    is_leakage = False
                    leakage_detail = ""

                    # Check 1: text length vs audio duration ratio
                    # Chinese: ~4-8 chars/sec | English: ~15-20 chars/sec
                    # APP_LEAKAGE_CHARS_PER_SEC is the safe upper bound for mixed language
                    from .core.asr.acoustic_policy import (
                        APP_LEAKAGE_MIN_CHARS,
                        WHISPER_CHECK3_MIN_CHARS,
                        WHISPER_CHECK3_OVERLAP,
                        WHISPER_CHECK3_PRE_DSP_MAX,
                    )

                    # The qwen3 family engines (torch and sherpa) already run an
                    # energy-aware, retry-protected speed guard internally
                    # (35 chars/s at clear-speech energy, 15 at low — and they
                    # KEEP a coherent original when only the speed heuristic
                    # fires). Re-applying a cruder fixed 25 chars/s here has no
                    # retry protection and a lower bound, so it silently drops
                    # fast dense real speech the engine deliberately allowed (a
                    # dense Chinese run-on at 25-35 chars/s). Defer rate-based
                    # leakage to the engine for qwen3; keep a last-resort net
                    # only for engines without one.
                    rate_cap = self._app_leakage_rate_cap(self._asr_engine_type)
                    max_reasonable_chars = int(audio_duration_s * rate_cap)
                    if rate_cap and len(text) > max(
                        max_reasonable_chars, APP_LEAKAGE_MIN_CHARS
                    ):
                        is_leakage = True
                        leakage_detail = (
                            f"impossible rate: {len(text)}ch/{audio_duration_s:.1f}s"
                        )

                    # Check 2: output is verbatim substring of recent context buffer
                    if not is_leakage and len(text) > 30 and self._recent_asr_buffer:
                        recent_combined = " ".join(self._recent_asr_buffer)
                        if text in recent_combined:
                            is_leakage = True
                            leakage_detail = "verbatim substring of recent buffer"

                    # Check 3 (whisper-mode UI suppressor):
                    # Verbatim substring matching above is brittle — Qwen3 can
                    # regurgitate recent_context with reordered punctuation or
                    # a few changed tokens and slip through. At the same time
                    # we can't blanket fuzzy-match in standard/noisy mode
                    # because that would kill legitimate user repetition.
                    #
                    # Triple-gated: capture_mode == whisper (user already
                    # opted into aggressive suppression by switching) + raw
                    # mic energy near silence (not a real-speech-just-quiet
                    # situation, since whisper-AGC already lifted that) +
                    # token overlap with the LAST entry of the buffer is
                    # high (the actual regurgitation pattern).
                    #
                    # Real user repetition will fail gate 2 (real speech has
                    # actual energy) so this can't kill legitimate restated
                    # sentences.
                    if (
                        not is_leakage
                        and getattr(self, "_capture_mode", "standard") == "whisper"
                        and pre_dsp_audio_level_avg < WHISPER_CHECK3_PRE_DSP_MAX
                        and self._recent_asr_buffer
                        and len(text) >= WHISPER_CHECK3_MIN_CHARS
                    ):
                        last_recent = self._recent_asr_buffer[-1] or ""
                        if len(last_recent) >= WHISPER_CHECK3_MIN_CHARS:
                            import re as _re_overlap

                            # Token overlap on character n-grams. Plain word
                            # tokenization fails on Chinese (no spaces) and
                            # mixing punctuation gives noisy results.
                            def _ngrams(s: str, n: int = 2) -> set:
                                stripped = _re_overlap.sub(
                                    r"[\s，。！？、,\.!\?;；:：]+", "", s
                                )
                                if len(stripped) < n:
                                    return {stripped} if stripped else set()
                                return {
                                    stripped[i : i + n]
                                    for i in range(len(stripped) - n + 1)
                                }

                            text_ngrams = _ngrams(text)
                            recent_ngrams = _ngrams(last_recent)
                            if text_ngrams and recent_ngrams:
                                overlap = len(text_ngrams & recent_ngrams) / len(
                                    text_ngrams
                                )
                                if overlap >= WHISPER_CHECK3_OVERLAP:
                                    is_leakage = True
                                    leakage_detail = (
                                        f"whisper regurgitation: "
                                        f"overlap={overlap:.0%} "
                                        f"pre_dsp={pre_dsp_audio_level_avg:.5f}"
                                    )

                    if is_leakage:
                        print(
                            f"[LEAKAGE] Context leakage detected ({leakage_detail}): "
                            f"{len(text)} chars / {audio_duration_s:.1f}s, dropping"
                        )
                        _pipeline_log(
                            "NOISE",
                            f"Context leakage [{leakage_detail}]: "
                            f"{len(text)} chars from {audio_duration_s:.1f}s audio",
                        )
                        text = ""

                # Post-ASR noise text filter: drop filler-only outputs
                # Safe: only drops known filler sounds, never meaningful words like 好的/行/可以
                if text and self._noise_filter_enabled:
                    _filler_set = {
                        "嗯",
                        "啊",
                        "哦",
                        "呃",
                        "额",
                        "噢",
                        "唔",
                        "嘶",
                        "哼",
                        "啧",
                        "就",
                        "嗯嗯",
                        "啊啊",
                        "哦哦",
                        "呃呃",
                        "嗯哼",
                        "嗯啊",
                        "嘶嘶",
                        "咚咚",
                    }
                    import re

                    _stripped = re.sub(r"[，。！？、,\.!\?\s]", "", text)
                    if _stripped in _filler_set:
                        print(f"[NOISE] Filtered filler text: '{text}'")
                        _pipeline_log("NOISE", f"Filtered: '{text}'")
                        text = ""

                # Short audio + single char = likely noise (random ASR artifact)
                # Only block single-character outputs if they are NOT valid Chinese characters;
                # 2-3 char phrases are legitimate in Chinese. Single valid CJK chars like '好', '行' must be kept.
                if text and self._noise_filter_enabled:
                    audio_dur = len(audio) / 16000
                    text_len = len(re.sub(r"[，。！？、,\.!\?\s]", "", text))
                    if audio_dur < 1.5 and text_len <= 1:
                        # Check if the single character is a valid CJK character
                        _is_cjk = bool(re.search(r"[\u4e00-\u9fff]", text))
                        if not _is_cjk:
                            print(
                                f"[NOISE] Short audio noise: '{text}' ({audio_dur:.1f}s/{text_len}chars)"
                            )
                            _pipeline_log(
                                "NOISE", f"Short noise: '{text}' ({audio_dur:.1f}s)"
                            )
                            text = ""

                # Buffer write moved to AFTER Polish (search "RECENT-BUFFER-WRITE").
                # Writing raw ASR text here would re-bias the next ASR call with
                # the very mistakes Polish just corrected, locking errors in for
                # the rest of the session. The post-Polish write also still skips
                # soft_split because soft_split segments never reach the Polish
                # stage; only `kind == "final"` does, and that path is the only
                # one that arrives at the buffer-write site below.

                # Emit interim text to UI (before polish)
                if text:
                    self._emit_text(text, is_final=False)

                # === Session-level dispatch: soft_split accumulates, final commits ===
                if kind == "soft_split":
                    # Precompute path: append raw text to THIS segment's session
                    # bucket (keyed by the session_id that originated the segment,
                    # so a stale in-flight segment can't contaminate a newly-started
                    # session's bucket). Update UI bubble with
                    # cumulative content for the ACTIVE session only.
                    if text:
                        with self._session_lock:
                            self._session_raw_segments.setdefault(
                                session_id, []
                            ).append(text)
                            # Chain-duration bookkeeping for the history
                            # record committed by this session's 'final'
                            # iteration (audio itself is discarded here).
                            self._session_soft_seg_seconds[session_id] = (
                                self._session_soft_seg_seconds.get(session_id, 0.0)
                                + (len(audio) / 16000 if audio is not None else 0.0)
                            )
                            _seg_count = len(self._session_raw_segments[session_id])
                            _cumulative = "".join(
                                self._session_raw_segments[session_id]
                            )
                            # Only echo to UI when this segment belongs to the
                            # currently-active recording session.
                            _is_current = session_id == self._session_count
                        if _is_current:
                            self._emit_text(_cumulative, is_final=False)
                            self._last_interim_text = _cumulative
                        _pipeline_log(
                            "SESSION",
                            f"Soft-split buffered (session={session_id}, active={_is_current}): "
                            f"'{text[:40]}' (bucket total={len(_cumulative)} chars, {_seg_count} segments)",
                        )
                    else:
                        # Shared predicate: the failure-accounting hook above
                        # uses the same function to decide "deferred (no
                        # streak)" vs "real loss", so the two must agree.
                        from .core.asr.rescue_policy import (
                            will_defer_soft_split_audio,
                        )

                        should_defer_audio = will_defer_soft_split_audio(
                            skip_asr=skip_asr,
                            audio_samples=raw_audio_for_retry.size,
                            pre_dsp_audio_level_avg=pre_dsp_audio_level_avg,
                            energy_gate=energy_gate,
                            asr_time_ms=asr_time,
                            engine_label=engine_label,
                        )
                        if should_defer_audio:
                            asr_ms_text = (
                                f"{asr_time:.0f}ms"
                                if isinstance(asr_time, (int, float))
                                else "unknown"
                            )
                            from .core.asr.rescue_policy import DEFER_BUCKET_MAX

                            with self._session_lock:
                                bucket = (
                                    self._session_deferred_audio_segments.setdefault(
                                        session_id, []
                                    )
                                )
                                _bucket_had_room = len(bucket) < DEFER_BUCKET_MAX
                                if _bucket_had_room:
                                    bucket.append(raw_audio_for_retry.copy())
                                _deferred_count = len(bucket)
                            if _bucket_had_room:
                                _pipeline_log(
                                    "SESSION",
                                    "Soft-split text empty; deferred audio for final retry "
                                    f"(session={session_id}, chunks={_deferred_count}, "
                                    f"duration={raw_audio_for_retry.size / 16000:.1f}s, "
                                    f"engine={engine_label}, asr_time={asr_ms_text})",
                                )
                            else:
                                # Real loss: the failure hook above already
                                # routed this chunk as count_bucket_full.
                                _pipeline_log(
                                    "SESSION",
                                    "Soft-split text empty; deferred-audio bucket "
                                    f"FULL ({_deferred_count}) — chunk dropped "
                                    f"(session={session_id}, "
                                    f"duration={raw_audio_for_retry.size / 16000:.1f}s, "
                                    f"engine={engine_label}, asr_time={asr_ms_text})",
                                )
                        else:
                            _pipeline_log(
                                "SESSION",
                                "Soft-split produced empty text — nothing appended",
                            )
                            # Route to finally → task_done is invoked at the finally tail,
                            # then the while-loop pulls the next queue item.
                            continue

                        _pipeline_log(
                            "SESSION",
                            "Soft-split produced empty text — audio "
                            + ("deferred" if _bucket_had_room else "dropped (bucket full)"),
                        )
                    # Route to finally → task_done is invoked at the finally tail,
                    # then the while-loop pulls the next queue item.
                    continue

                # kind == 'final': join prior buffered segments with this segment's text.
                # Preserve _final_only_text (before prepend) for the wakeword/command
                # tail-fallback path below.
                _final_only_text = text
                if _prior_buffered_text:
                    if text:
                        _pipeline_log(
                            "SESSION",
                            f"Final commit: prepend {len(_prior_buffered_text)} chars of buffered + {len(text)} chars of final",
                        )
                        text = _prior_buffered_text + text
                    else:
                        _pipeline_log(
                            "SESSION",
                            f"Final commit: no final text, using {len(_prior_buffered_text)} chars buffered only",
                        )
                        text = _prior_buffered_text

                    # Boundary-punctuation cleanup on the concatenated string.
                    # ASR per-segment outputs usually end with
                    # '。' — once concatenated, adjacent punctuation at segment
                    # boundaries can produce noise like '。，' / '。。' / leading
                    # '，' / trailing duplicates. Normalize only obvious errors;
                    # never rewrite sentence content.
                    if text:
                        import re as _re

                        _before_clean = text
                        # 1) Collapse runs of CJK sentence-end punctuation
                        text = _re.sub(r"[。]{2,}", "。", text)
                        text = _re.sub(r"[？]{2,}", "？", text)
                        text = _re.sub(r"[！]{2,}", "！", text)
                        text = _re.sub(r"[，]{2,}", "，", text)
                        # 2) Mixed sentence+clause punctuation at a boundary →
                        #    keep the stronger (sentence-end) one
                        text = _re.sub(r"。[，、]", "。", text)
                        text = _re.sub(r"[，、]。", "。", text)
                        text = _re.sub(r"？[，、]", "？", text)
                        text = _re.sub(r"！[，、]", "！", text)
                        # 3) Strip leading punctuation that shouldn't start a paste
                        text = _re.sub(r"^[，。、！？,\.!\?]+", "", text).lstrip()
                        if text != _before_clean:
                            _pipeline_log(
                                "SESSION",
                                f"Boundary cleanup: {len(_before_clean)} → {len(text)} chars",
                            )

                # === Selection Command Detection ===
                # REMOVED: Automatic selection detection based on ASR keywords
                # Selection processing is now ONLY triggered via wakeword (小助手润色, etc.)
                # This prevents accidental Ctrl+C during normal dictation
                # See: wakeword/executor.py -> _selection_process()

                _route_decision = self._dispatch_final_command(
                    session_id,
                    text,
                    _prior_buffered_text,
                    _final_only_text,
                )
                if _route_decision is not None:
                    if _route_decision.inserted is not None:
                        inserted = _route_decision.inserted
                    if _route_decision.final_text is not None:
                        final_text = _route_decision.final_text
                    continue

                if text:
                    # Snapshot post-processing references under lock to prevent
                    # race with reload_config() closing/replacing them mid-use.
                    with self._lock:
                        _snap_processor = self.hotword_processor
                        _snap_fuzzy = self.fuzzy_matcher
                        _snap_polisher = self.polisher
                        _snap_manager = self.hotword_manager

                    # Layer 2: Apply regex corrections
                    _pipeline_log("POST", "Layer 2: HotWord regex starting...")
                    original_text = text
                    layer2_replacements = []
                    import time as time_module

                    layer2_start = time_module.time()

                    if _snap_processor:
                        text, changes = _snap_processor.process_with_info(original_text)
                        layer2_replacements = [{"change": c} for c in changes]
                        if text != original_text:
                            print(f"[HOTWORD] '{original_text}' -> '{text}'")

                    layer2_time = (time_module.time() - layer2_start) * 1000

                    # Log HotWord debug info
                    debug.log_hotword(
                        layer1_enabled=initial_prompt_enabled,
                        layer1_prompt_words=(
                            _snap_manager.config.prompt_words if _snap_manager else []
                        ),
                        layer1_domain_context=(
                            _snap_manager.config.domain_context if _snap_manager else ""
                        ),
                        layer2_input=original_text,
                        layer2_output=text,
                        layer2_replacements_applied=layer2_replacements,
                        layer2_rules_count=(
                            _snap_processor.total_rule_count if _snap_processor else 0
                        ),
                        layer2_time_ms=layer2_time,
                    )

                    _pipeline_log(
                        "POST", f"Layer 2: HotWord done ({layer2_time:.0f}ms)"
                    )

                    # Layer 2.5: Hotword fuzzy matching only.
                    # (Screen-based homophone correction moved to Polish layer —
                    # LLM does softer/safer nudging with the raw OCR text in
                    # <screen_context>. The old sliding-pinyin scanner would
                    # aggressively rewrite ASR intent whenever a title string
                    # happened to match phonetically — a short spoken token
                    # could get coerced into a long unrelated window title.)
                    _pipeline_log("POST", "Layer 2.5: Fuzzy matching starting...")
                    if _snap_fuzzy:
                        text, fuzzy_corrections = _snap_fuzzy.process_with_info(text)
                        if fuzzy_corrections:
                            for corr in fuzzy_corrections:
                                print(
                                    f"[FUZZY] '{corr['original']}' -> '{corr['corrected']}' (score: {corr['score']})"
                                )

                    _pipeline_log("POST", "Layer 2.5: Fuzzy matching done")

                    # Layer 3: AI Polish (optional)
                    # Skip polish for very short text (≤4 meaningful chars) —
                    # saves API cost (each Polish call costs ~3500 input tokens
                    # of fixed prompt overhead regardless of input length).
                    # Punctuation/whitespace doesn't count toward the threshold,
                    # so "嗯。" = 1 char, "好的。" = 2 chars, etc.
                    _pipeline_log("POST", "Layer 3: Polish starting...")
                    polish_debug = None
                    import re as _re_short

                    _meaningful = _re_short.sub(r"[，。！？、,\.!\?\s　 ]+", "", text)
                    _skip_polish = _snap_polisher and len(_meaningful) <= 4
                    if _skip_polish:
                        _pipeline_log(
                            "POST",
                            f"Polish skipped: text too short "
                            f"({len(_meaningful)} meaningful chars: '{text}')",
                        )

                    # Terminal bypass: skip polish when the foreground window is
                    # a terminal emulator (Windows Terminal / cmd / PowerShell /
                    # etc). Raw ASR text is preferable in those contexts — the
                    # receiver is usually a model or a developer, both of which
                    # handle unpolished text better than post-hoc prose cleanup.
                    # Saves ~3-6s of remote API latency per utterance.
                    if (
                        not _skip_polish
                        and _snap_polisher
                        and self._polish_terminal_bypass
                        and self.output_injector.is_terminal_target()
                    ):
                        _skip_polish = True
                        _pipeline_log(
                            "POST",
                            "Polish skipped: terminal target (polish_terminal_bypass=True)",
                        )
                        print("[POLISH] Skipped: terminal target")

                    # PERF-2: short-text bypass — <10 meaningful chars with no
                    # leading filler word and no spoken numbers skips the cloud
                    # LLM entirely (measured change rate 11%, all low-risk;
                    # saves the ~760ms fixed round-trip). Local Layer2/2.5
                    # replacements already ran above. Config gate:
                    # polish.skip_short_text (default true) — only present on
                    # the quality-mode AIPolisher config, so fast/local polish
                    # is never skipped by this branch.
                    if (
                        not _skip_polish
                        and _snap_polisher
                        and getattr(
                            getattr(_snap_polisher, "config", None),
                            "skip_short_text",
                            False,
                        )
                    ):
                        from .core.hotword.polish import (
                            should_skip_short_text_polish,
                        )

                        if should_skip_short_text_polish(text):
                            _skip_polish = True
                            _pipeline_log(
                                "POST",
                                "Polish skipped: short text fast path "
                                f"(<10 meaningful chars, no leading filler): "
                                f"'{text}'",
                            )
                            print("[POLISH] Skipped: short text fast path")
                    if _snap_polisher and not _skip_polish:
                        api_status_at_start = (
                            _snap_polisher.get_api_status()
                            if hasattr(_snap_polisher, "get_api_status")
                            else {}
                        )
                        try:
                            _slow_hint_delay_s = max(
                                1.0,
                                float(
                                    getattr(
                                        _snap_polisher.config,
                                        "slow_threshold_ms",
                                        3000.0,
                                    )
                                )
                                / 1000.0,
                            )
                        except Exception:
                            _slow_hint_delay_s = 3.0

                        def _emit_polish_slow_hint():
                            if self._bridge:
                                self._bridge.emit_slow_stage("api")
                            if api_status_at_start:
                                hint_status = dict(api_status_at_start)
                                hint_status["last_was_slow"] = True
                                hint_status["last_response_ms"] = (
                                    _slow_hint_delay_s * 1000
                                )
                                hint_status["status_message"] = (
                                    f"当前{hint_status.get('current_api', 'API')}"
                                    f"请求已等待超过{_slow_hint_delay_s:.0f}秒，"
                                    "可能是服务器或网络响应慢"
                                )
                                self._emit_api_status(hint_status)

                        # Slow-stage indicator: after threshold, show API-slow glow/status.
                        _polish_hint_timer = threading.Timer(
                            _slow_hint_delay_s,
                            _emit_polish_slow_hint,
                        )
                        _polish_hint_timer.daemon = True
                        _polish_hint_timer.start()

                        # v1.2: Build screen context string (runtime, not persisted)
                        screen_ctx_str = ""
                        try:
                            if (
                                _snap_manager
                                and _snap_manager.config.screen_context_enabled
                            ):
                                from aria.system.output import (
                                    get_foreground_window_info,
                                )
                                from aria.core.context import AppCategoryDetector

                                ctx_info = get_foreground_window_info()
                                proc = ctx_info.get("process_name", "")
                                if proc:
                                    cat = AppCategoryDetector.detect(
                                        proc,
                                        user_overrides=_snap_manager.config.app_categories,
                                    )
                                    app_name = proc.replace(".exe", "").replace(
                                        ".EXE", ""
                                    )
                                    screen_ctx_str = (
                                        f"用户当前在{app_name}中（{cat}场景）"
                                    )
                                    _record_app_context_debug(app_name, cat)
                        except Exception as e:
                            _pipeline_log("POST", f"Screen context failed: {e}")

                        # Screen-aware Polish: pass OCR text as a separate
                        # system-message <screen_context> block (structured,
                        # cache-friendly, with anti-echo prompting baked in).
                        # screen_ctx_str (the app-category string) continues to
                        # feed the user-prompt's tone guidance.
                        screen_text_for_polish = ""
                        if self._screen_ocr_polish_enabled and self._screen_ocr:
                            try:
                                _fast_polish_mode = bool(
                                    _snap_manager
                                    and getattr(_snap_manager, "polish_mode", "")
                                    == "fast"
                                )
                                if _fast_polish_mode:
                                    _pipeline_log(
                                        "POST",
                                        "Screen OCR wait skipped in fast polish mode; using title/cache fallback",
                                    )
                                elif not self._screen_ocr.has_cached_for_current_hwnd():
                                    # Accuracy-first cold/window-switch path,
                                    # but do not tax very short everyday
                                    # utterances.  Earlier live testing defined
                                    # the intended tradeoff: short first
                                    # utterances may miss screen context, while
                                    # 3-4s+ / name-test / term-test utterances
                                    # should wait for fresh OCR when possible.
                                    _audio_duration_s = len(audio) / 16000
                                    _plain_len = len(text.strip())
                                    _screen_need_markers = (
                                        "名字",
                                        "人名",
                                        "角色名",
                                        "角色",
                                        "人设",
                                        "设定",
                                        "主角",
                                        "女主",
                                        "女主角",
                                        "专名",
                                        "目标词",
                                        "叫做",
                                        "叫作",
                                        "名叫",
                                        "称为",
                                        "术语",
                                        "化学式",
                                        "医学",
                                        "药名",
                                        "识别",
                                    )
                                    _wait_timeout = 0.0
                                    _has_screen_need = (
                                        _audio_duration_s >= 3.8
                                        or _plain_len >= 24
                                        or any(
                                            marker in text
                                            for marker in _screen_need_markers
                                        )
                                    )
                                    if _has_screen_need:
                                        _wait_timeout = 1.2
                                        try:
                                            # Isolated v5_dml is fast enough that
                                            # an extra bounded wait often catches
                                            # the very first screen OCR result.
                                            # Keep CPU tiers on the old budget so
                                            # low-end machines do not feel stuck.
                                            if self._screen_ocr.ocr_tier() == "v5_dml":
                                                _wait_timeout = 1.8
                                        except Exception:
                                            pass
                                    elif _plain_len >= 12:
                                        _wait_timeout = 0.45

                                    if _wait_timeout > 0:
                                        _planned_wait, _wait_info = (
                                            self._screen_ocr.plan_wait_for_pending(
                                                max_wait=_wait_timeout,
                                                allow_queued_latest=_has_screen_need,
                                                allow_overdue=_has_screen_need,
                                            )
                                        )
                                        if _planned_wait > 0:
                                            _pipeline_log(
                                                "POST",
                                                "Screen OCR wait planned: "
                                                f"wait={_planned_wait:.2f}s/"
                                                f"cap={_wait_timeout:.2f}s, "
                                                f"elapsed={_wait_info.get('elapsed', 0.0):.2f}s, "
                                                f"estimate={_wait_info.get('estimate', 0.0):.2f}s, "
                                                f"remaining={_wait_info.get('remaining', 0.0):.2f}s, "
                                                f"reason={_wait_info.get('reason', '')}",
                                            )
                                            if self._screen_ocr.wait_for_current_cache(
                                                timeout=_planned_wait
                                            ):
                                                _pipeline_log(
                                                    "POST",
                                                    "Screen OCR current cache became available "
                                                    f"within {_planned_wait:.2f}s",
                                                )
                                            elif not self._screen_ocr.wait_for_pending(
                                                timeout=0.05
                                            ):
                                                _pipeline_log(
                                                    "POST",
                                                    "Screen OCR predicted wait missed "
                                                    f"after {_planned_wait:.2f}s; "
                                                    "using title/cache fallback",
                                                )
                                        else:
                                            _pipeline_log(
                                                "POST",
                                                "Screen OCR wait skipped by predictor: "
                                                f"cap={_wait_timeout:.2f}s, "
                                                f"elapsed={_wait_info.get('elapsed', 0.0):.2f}s, "
                                                f"estimate={_wait_info.get('estimate', 0.0):.2f}s, "
                                                f"remaining={_wait_info.get('remaining', 0.0):.2f}s, "
                                                f"reason={_wait_info.get('reason', '')}",
                                            )
                                    else:
                                        _pipeline_log(
                                            "POST",
                                            "Screen OCR wait skipped for short/plain utterance; using title/cache fallback",
                                        )
                                # Then take whatever OCR cache is available.
                                # Cached paths remain non-blocking; no-cache
                                # cold/window-switch paths get only the bounded
                                # wait above and then fall back to the
                                # explicitly-labeled title/recent context.
                                screen_text_for_polish = (
                                    self._screen_ocr.get_text_for_polish(max_chars=1200)
                                )
                                if screen_text_for_polish:
                                    import hashlib as _hl

                                    _s_hash = _hl.md5(
                                        screen_text_for_polish.encode()
                                    ).hexdigest()[:8]
                                    _record_screen_text_debug(
                                        screen_text_for_polish, _s_hash
                                    )
                            except Exception as _e:
                                _pipeline_log(
                                    "POST", f"get_text_for_polish failed: {_e}"
                                )
                                screen_text_for_polish = ""

                        before_polish = text
                        # Pre-flight: pick output mode. Typewriter targets may
                        # use the streaming API for transport, but insertion is
                        # still unified after validation below.
                        _output_mode = self.output_injector.detect_output_mode()
                        polish_debug = None
                        # Terminal targets are where the user usually dictates
                        # instructions to an agent/shell.  In that context a
                        # structured/document-polish prompt feels like "漏字":
                        # it may remove fillers, merge clauses or shorten the
                        # command intent before paste.  Force the conservative
                        # loose prompt here; the output layer still handles
                        # terminal-safe paste/newline stripping separately.
                        try:
                            _terminal_target = self.output_injector.is_terminal_target()
                        except Exception:
                            _terminal_target = False
                        _force_loose_polish = bool(_terminal_target)
                        if _terminal_target:
                            _pipeline_log(
                                "POST",
                                "Polish: terminal target → force loose prompt "
                                "(preserve dictated content)",
                            )
                        try:
                            # Streaming polish is used only as a transport for
                            # receiving chunks. We still wait for the complete
                            # result before inserting, so the same length and
                            # content-addition guards protect every output mode.
                            if (
                                _output_mode == "typewriter"
                                and not screen_text_for_polish.strip()
                            ):
                                import time as _time_mod

                                _stream_start = _time_mod.time()
                                _stream_api_status_before = (
                                    _snap_polisher.get_api_status()
                                    if hasattr(_snap_polisher, "get_api_status")
                                    else {}
                                )
                                _accumulated = ""
                                try:
                                    for _chunk in _snap_polisher.polish_stream(
                                        text,
                                        screen_context=screen_ctx_str,
                                        screen_text=screen_text_for_polish,
                                        force_loose=_force_loose_polish,
                                    ):
                                        if not _chunk:
                                            continue
                                        _accumulated += _chunk
                                except PolishStreamError as _stream_err:
                                    # Pre-first-chunk failure → fall back to classic polish
                                    _pipeline_log(
                                        "POST",
                                        f"Polish stream failed pre-first-chunk: {_stream_err} — falling back",
                                    )
                                    _accumulated = ""

                                _stream_ms = (_time_mod.time() - _stream_start) * 1000

                                if _accumulated:
                                    text = _accumulated
                                    polish_debug = {
                                        "enabled": True,
                                        "error": "",
                                        "api_time_ms": _stream_ms,
                                        "changed": _accumulated != before_polish,
                                        "output_text": _accumulated,
                                        "input_text": before_polish,
                                        "api_url": _stream_api_status_before.get(
                                            "current_url",
                                            _snap_polisher.config.api_url,
                                        ),
                                        "model": _stream_api_status_before.get(
                                            "model", _snap_polisher.config.model
                                        ),
                                        "timeout": _snap_polisher.config.timeout,
                                        "prompt_template": "",
                                        "full_prompt": "",
                                        "http_status": 200,
                                        "using_backup": _stream_api_status_before.get(
                                            "using_backup", False
                                        ),
                                        "request_api_label": _stream_api_status_before.get(
                                            "current_api", "主 API"
                                        ),
                                        "api_status": (
                                            _snap_polisher.get_api_status()
                                            if hasattr(_snap_polisher, "get_api_status")
                                            else {}
                                        ),
                                        "streamed": True,
                                    }

                            if polish_debug is None:
                                # Clipboard target OR streaming aborted before first chunk
                                polish_debug = _snap_polisher.polish_with_debug(
                                    text,
                                    screen_context=screen_ctx_str,
                                    screen_text=screen_text_for_polish,
                                    force_loose=_force_loose_polish,
                                )
                                text = polish_debug["output_text"]
                        except Exception as polish_err:
                            # Polish is optional — never block text insertion
                            print(f"[POLISH] EXCEPTION (degraded to raw): {polish_err}")
                            _pipeline_log("POST", f"Polish exception: {polish_err}")
                            polish_debug = {
                                "enabled": True,
                                "error": str(polish_err),
                                "api_time_ms": 0,
                                "changed": False,
                                "output_text": text,
                                "input_text": text,
                                "api_url": "",
                                "model": "",
                                "timeout": 0,
                                "prompt_template": "",
                                "full_prompt": "",
                                "http_status": 0,
                            }
                        finally:
                            _polish_hint_timer.cancel()

                        # === LENGTH SAFETY NET ===
                        # Reject polish output that's much longer than ASR raw
                        # input — defense against OCR/screen-context bleed-in
                        # on short / noise-induced inputs (e.g. ambient noise
                        # transcribed as "嗯。" → polish writes a 90-char
                        # paragraph from screen context). When triggered we
                        # fall back to the raw ASR text so the user types what
                        # they actually said (or just "嗯。") instead of an
                        # invented paragraph.
                        if text and before_polish:
                            _raw_len = len(before_polish.strip())
                            _polished_len = len(text.strip())
                            # Two-tier rejection:
                            #   - very short input (≤3 chars): polished output
                            #     never legitimately exceeds 5 chars
                            #   - longer input: cap expansion at ratio 1.5 +
                            #     5-char absolute slack for punctuation
                            _expanded = (_raw_len <= 3 and _polished_len > 5) or (
                                _raw_len > 3 and _polished_len > _raw_len * 1.5 + 5
                            )
                            if _expanded:
                                _pipeline_log(
                                    "POST",
                                    f"Polish output rejected (length "
                                    f"{_raw_len}->{_polished_len}): "
                                    f"'{before_polish[:30]}' -> "
                                    f"'{text[:60]}' (using raw)",
                                )
                                text = before_polish
                                if polish_debug:
                                    polish_debug["changed"] = False
                                    polish_debug["output_text"] = before_polish
                                    polish_debug["rejected_reason"] = "length_explosion"

                        if text and before_polish:
                            _addition_rejection = (
                                self._polish_content_addition_rejection(
                                    before_polish, text
                                )
                            )
                            if _addition_rejection:
                                _pipeline_log(
                                    "POST",
                                    f"Polish output rejected ({_addition_rejection}): "
                                    f"'{before_polish[:40]}' -> '{text[:40]}' "
                                    "(using raw)",
                                )
                                text = before_polish
                                if polish_debug:
                                    polish_debug["changed"] = False
                                    polish_debug["output_text"] = before_polish
                                    polish_debug["rejected_reason"] = (
                                        _addition_rejection
                                    )

                        # Log Polish debug info
                        debug.log_polish(
                            enabled=polish_debug.get("enabled", True),
                            api_url=polish_debug.get("api_url", ""),
                            model=polish_debug.get("model", ""),
                            timeout=polish_debug.get("timeout", 0),
                            input_text=polish_debug.get("input_text", text),
                            prompt_template=polish_debug.get("prompt_template", ""),
                            full_prompt=polish_debug.get("full_prompt", ""),
                            output_text=polish_debug.get("output_text", text),
                            changed=polish_debug.get("changed", False),
                            api_time_ms=polish_debug.get("api_time_ms", 0),
                            error=polish_debug.get("error", ""),
                            http_status=polish_debug.get("http_status", 0),
                        )
                        api_status = polish_debug.get("api_status")
                        self._emit_api_status(
                            api_status if isinstance(api_status, dict) else None
                        )

                        # 显示 API 状态（主/备用）
                        api_tag = (
                            "[备用]" if polish_debug.get("using_backup") else "[主]"
                        )
                        if polish_debug.get("changed"):
                            print(
                                f"[POLISH]{api_tag} '{before_polish}' -> '{text}' ({polish_debug['api_time_ms']:.0f}ms)"
                            )
                        elif polish_debug.get("error"):
                            print(f"[POLISH]{api_tag} ERROR: {polish_debug['error']}")
                    else:
                        # Log that polish is disabled
                        debug.log_polish(enabled=False)

                    # 记录 Polish 完成时间和 API 状态
                    if _snap_polisher and polish_debug:
                        api_status = (
                            "备用" if polish_debug.get("using_backup") else "主"
                        )
                        _pipeline_log(
                            "POST",
                            f"Layer 3: Polish done ({polish_debug['api_time_ms']:.0f}ms, {api_status}API)",
                        )
                    else:
                        _pipeline_log("POST", "Layer 3: Polish done (disabled)")

                    # Custom instructions get a second, narrow detection pass
                    # after Polish. This is important for screen-aware launch
                    # commands such as "小助手启动项目": raw ASR may hear
                    # near-homophone variants, then the Polish layer corrects
                    # it using OCR / domain hints.
                    #
                    # Guardrails:
                    # - only custom_instruction_launch is allowed here; built-in
                    #   sleep/auto-send/selection commands remain raw-ASR only;
                    # - the detector still requires an explicit configured
                    #   wakeword + configured custom instruction phrase.
                    if text and self.wakeword_detector and self.wakeword_executor:
                        polished_wakeword_result = self.wakeword_detector.detect(text)
                        if polished_wakeword_result:
                            (
                                cmd_id,
                                action,
                                value,
                                response,
                                following_text,
                                command_text,
                            ) = polished_wakeword_result
                            is_custom_instruction = (
                                action == "custom_instruction_launch"
                                or str(cmd_id).startswith("custom_instruction:")
                            )
                            if is_custom_instruction:
                                self.wakeword_executor._pending_command_text = (
                                    command_text
                                )
                                success = self.wakeword_executor.execute(
                                    cmd_id, action, value, response, following_text
                                )
                                status = "OK" if success else "FAIL"
                                print(
                                    f"[WAKEWORD] {status}: {cmd_id} "
                                    f"(polished text: '{text}')"
                                )
                                _pipeline_log(
                                    "WAKEWORD",
                                    f"Post-polish custom instruction {status}: "
                                    f"{cmd_id} text='{text[:60]}'",
                                )
                                if self._bridge and hasattr(
                                    self._bridge, "emit_command"
                                ):
                                    self._bridge.emit_command(
                                        f"小助手:{cmd_id}", success
                                    )
                                inserted = success
                                final_text = (
                                    f"[唤醒词] {response}"
                                    if response
                                    else f"[唤醒词] {cmd_id}"
                                )
                                self._emit_route_decision(
                                    RouteDecision(
                                        session_id=session_id,
                                        stage="wakeword",
                                        reason="post_polish_custom_instruction",
                                        consumed=True,
                                        inserted=success,
                                        final_text=final_text,
                                        command_id=str(cmd_id or "") or None,
                                        text_len=len(text or ""),
                                        detail={"invocation": 1},
                                    )
                                )
                                continue
                            _pipeline_log(
                                "WAKEWORD",
                                f"Post-polish matched non-custom command ignored: {cmd_id}",
                            )
                    # RECENT-BUFFER-WRITE: feed Polish-corrected text (not raw
                    # ASR) back into the recent_context buffer so the next
                    # transcription is biased toward the *corrected* spelling.
                    # Writing raw ASR here was the root cause of "first error
                    # locks in for the whole session": ASR's own miss-recognition
                    # was silently re-biasing every later call.
                    #
                    # Insurance: if Polish changed the text but introduced new
                    # CJK terms that have NO evidence anywhere on screen or in
                    # user hotwords, treat the change as ungrounded LLM guess
                    # and write the raw ASR text instead. This stops Polish's
                    # over-correction (e.g. forcing a less-common name into a
                    # more-common homophone) from poisoning the buffer in the
                    # opposite direction.
                    if text:
                        write_text = text
                        if polish_debug and polish_debug.get("changed", False):
                            raw_input = polish_debug.get("input_text") or text
                            evidence_pool = screen_text_for_polish or ""
                            if (
                                self.hotword_manager
                                and self.hotword_manager.config.prompt_words
                            ):
                                evidence_pool = (
                                    evidence_pool
                                    + "\n"
                                    + "\n".join(
                                        self.hotword_manager.config.prompt_words
                                    )
                                )
                            if not self._polish_change_supported_by_evidence(
                                raw_input, text, evidence_pool
                            ):
                                write_text = raw_input
                                _pipeline_log(
                                    "POST",
                                    f"Buffer write: rejected ungrounded polish change "
                                    f"'{raw_input[:40]}' -> '{text[:40]}' (using raw)",
                                )
                        if (
                            not self._recent_asr_buffer
                            or self._recent_asr_buffer[-1] != write_text
                        ):
                            self._recent_asr_buffer.append(write_text)
                            if len(self._recent_asr_buffer) > self._recent_context_max:
                                self._recent_asr_buffer = self._recent_asr_buffer[
                                    -self._recent_context_max :
                                ]

                    final_text = text
                    self._remember_last_transcript(text)
                    print(f"[TEXT] {text}")
                    _pipeline_log("OUTPUT", f"Final text: '{text}'")

                    # Emit final text to UI
                    self._emit_text(text, is_final=True)

                    # Insert into active application. Even streamed Polish is
                    # accumulated first, then inserted here after safety checks.
                    _pipeline_log("OUTPUT", "Calling output_injector.insert_text()...")
                    with self._session_lock:
                        expected_target = getattr(
                            self, "_session_output_targets", {}
                        ).get(session_id)
                    insert_ok, delivery_reason = self._insert_output_text(
                        text, expected_target
                    )
                    if delivery_reason == "transaction_exception":
                        _pipeline_log(
                            "OUTPUT",
                            "Output transaction raised; text moved to Draft Box",
                        )
                    inserted = insert_ok
                    if insert_ok:
                        # Late-rescue ordering anchor: an async cloud rescue
                        # result older than this insert goes to history.
                        with self._rescue_insert_lock:
                            self._last_success_insert_at = time.time()
                        tail_seconds = (
                            len(audio) / 16000.0 if audio is not None else 0.0
                        )
                        self._record_recent_voice_insert(
                            text,
                            expected_target,
                            session_id,
                            audio_seconds=_prior_buffered_seconds + tail_seconds,
                        )
                        _pipeline_log("OUTPUT", ">>> Text inserted successfully!")
                        print("[OK] Inserted!")
                    else:
                        _pipeline_log("OUTPUT", ">>> Text insertion FAILED!")
                        print("[FAIL] Insert failed! (clipboard/paste error)")
                        self._emit_draft(text, delivery_reason)
                    try:
                        self._emit_route_decision(
                            make_dictation_decision(
                                session_id,
                                delivery="insert_ok" if insert_ok else "fail",
                                text_len=len(text or ""),
                                inserted=insert_ok,
                            )
                        )
                    except Exception:
                        pass

                    # Auto-send is part of the output transaction. Never press
                    # Enter after a failed paste or a changed target.
                    self._maybe_auto_send_after_insert(insert_ok, expected_target)

                    # Note: UI notification moved to finally block to ensure it always fires
                else:
                    print("[WARN] No speech recognized")
                    _pipeline_log("ASR", "No speech recognized (empty result)")
                    debug.log_error("No speech recognized")

            except Exception as e:
                logger.error(f"Transcription error: {e}", exc_info=True)
                _pipeline_log(
                    "ERROR", f"Transcription exception: {type(e).__name__}: {e}"
                )
                print(f"[ERR] {type(e).__name__}: {e}")
                debug.log_error(f"Transcription error: {e}")

            finally:
                self._worker_busy = False
                if kind == "final":
                    with self._session_lock:
                        getattr(self, "_session_voice_windows", {}).pop(
                            session_id, None
                        )
                        getattr(self, "_session_voice_started_at", {}).pop(
                            session_id, None
                        )
                # Clear the in-flight same-utterance prefix so it can never
                # bleed into a later segment that skips ASR (and thus never
                # refreshes it) — each transcribed segment sets it fresh.
                self._active_session_ctx_prefix = ""

                # Log total processing time for this segment
                import time as _fin_time

                total_time = (_fin_time.time() - asr_start) * 1000 if asr_start else -1
                remaining = self._asr_queue.qsize()
                _pipeline_log(
                    "ASR",
                    f"<<< Segment done: total={total_time:.0f}ms, "
                    f"result={'OK: ' + repr(final_text[:40]) if final_text else 'EMPTY'}, "
                    f"inserted={inserted}, queue_remaining={remaining}",
                )
                print(
                    f"[DONE] total={total_time:.0f}ms, inserted={inserted}, "
                    f"queue_remaining={remaining}"
                )

                # Notify UI that processing is complete (success or failure).
                # Gated: soft_split iterations must NOT emit — insert_complete fires
                # exactly once per recording session, when the 'final' iteration ends.
                # The _stop_recording() UI contract depends on this (TRANSCRIBING →
                # IDLE transition is driven by insert_complete firing once).
                if not _suppress_insert_complete:
                    self._emit_insert_complete()
                else:
                    _pipeline_log(
                        "ASR",
                        f"Soft-split iteration done, suppressing insert_complete (kind={kind})",
                    )

                # Finalize and save debug session
                debug.finalize(final_text=final_text, inserted=inserted)

                # NOTE: insight_store hot-path add removed (deprecated; data lives in
                # history_store). insight_store itself stays alive for the wakeword
                # highlight command (core/wakeword/executor.py).

                # Save to unified history store (v1.2). The debug session only
                # covers the tail segment of a soft-split chain: its raw_text
                # is logged before the buffered prefix is prepended, and its
                # audio duration excludes the earlier (already discarded)
                # segments. Re-join the chain so input_text truly pairs with
                # output_text (BACKLOG DATA-1).
                if final_text and final_text.strip() and self.history_store:
                    raw_text = (
                        debug.info.asr.raw_text
                        if debug.info.asr and hasattr(debug.info.asr, "raw_text")
                        else ""
                    )
                    tail_duration_s = (
                        debug.info.audio.duration_seconds if debug.info.audio else 0.0
                    )
                    entry = compose_asr_history_entry(
                        buffered_raw_text=_prior_buffered_text,
                        tail_raw_text=raw_text,
                        final_text=final_text,
                        tail_duration_s=tail_duration_s,
                        buffered_duration_s=_prior_buffered_seconds,
                    )
                    history_metadata = {
                        "session_id": session_id,
                        "duration_s": entry.duration_s,
                    }
                    try:
                        history_metadata.update(
                            self.output_injector.get_last_delivery_metadata()
                        )
                    except Exception:
                        pass
                    self.history_store.add(
                        record_type=RecordType.ASR,
                        input_text=entry.input_text,
                        output_text=entry.output_text,
                        timestamp=debug.info.start_time,
                        metadata=history_metadata,
                    )

                if DebugConfig.print_summary:
                    debug.print_summary()

                if DebugConfig.save_to_file:
                    saved_path = debug.save()
                    if saved_path:
                        print(f"[DEBUG] Saved to: {saved_path}")

                self._asr_queue.task_done()

        logger.info("ASR worker thread stopped")

    def _start_asr_worker(self) -> None:
        """Start the ASR worker thread."""
        if self._asr_thread is None or not self._asr_thread.is_alive():
            self._stop_event.clear()
            self._asr_thread = threading.Thread(target=self._asr_worker, daemon=True)
            self._asr_thread.start()
            logger.info("ASR worker thread started")

    def _stop_asr_worker(self) -> None:
        """Stop the ASR worker thread."""
        self._stop_event.set()
        if self._asr_thread and self._asr_thread.is_alive():
            self._asr_thread.join(timeout=2.0)
            if self._asr_thread.is_alive():
                logger.warning("ASR thread did not stop in 2s")
            else:
                logger.info("ASR worker thread joined")

    def _on_audio_level(self, level: float) -> None:
        """Called with audio level updates."""
        self._last_audio_callback_time = time.time()
        self._emit_level(level)

    def _on_hotkey(self) -> None:
        """Called when hotkey is pressed - NON-BLOCKING.

        This runs on the hotkey thread (Windows message loop).
        Must return ASAP to keep the message loop responsive.
        Actual work is offloaded to _hotkey_action_worker thread.
        """
        _pipeline_log("HOTKEY", ">>> Hotkey pressed (on hotkey thread)")

        # Health check: restart action worker if it died
        if self._hotkey_action_thread and not self._hotkey_action_thread.is_alive():
            _pipeline_log("HOTKEY", "Action worker died! Restarting...")
            self._start_hotkey_action_worker()

        try:
            self._hotkey_action_queue.put_nowait("toggle")
        except queue.Full:
            _pipeline_log("HOTKEY", "Action queue full (4 pending), dropping press")

    def _load_trigger_mode(self) -> str:
        """Read general.trigger_mode from hotwords.json ("toggle" default).

        Any read/parse failure or unknown value falls back to "toggle" so the
        legacy behavior is never at risk from a bad config.
        """
        import json

        mode = "toggle"
        try:
            with open(get_config_path("hotwords.json"), "r", encoding="utf-8") as f:
                mode = json.load(f).get("general", {}).get("trigger_mode", "toggle")
        except Exception:
            mode = "toggle"
        if mode not in ("toggle", "hold_to_talk"):
            logger.warning(f"Unknown trigger_mode '{mode}', falling back to 'toggle'")
            mode = "toggle"
        return mode

    def _register_recording_hotkey(self) -> None:
        """Register the recording hotkey according to trigger_mode.

        toggle (default): legacy press-only registration — byte-for-byte the
        same call as before trigger modes existed.
        hold_to_talk: press+release registration; events feed the trigger
        state machine via the hotkey action queue.
        """
        if self._trigger_mode == "hold_to_talk":
            self.hotkey_manager.register_press_release(
                self.hotkey,
                self._on_hotkey_press,
                self._on_hotkey_release,
                "Hold-to-talk voice trigger",
            )
        else:
            self.hotkey_manager.register(
                self.hotkey, self._on_hotkey, "Toggle voice recording"
            )

    def _start_hotkey_retry(self) -> None:
        """Kick the background hotkey re-registration loop (idempotent)."""
        if self._hotkey_retry_thread and self._hotkey_retry_thread.is_alive():
            return
        self._hotkey_retry_thread = threading.Thread(
            target=self._hotkey_retry_loop, daemon=True, name="hotkey-retry"
        )
        self._hotkey_retry_thread.start()

    def _hotkey_retry_loop(self) -> None:
        """Retry hotkey registration every HOTKEY_RETRY_INTERVAL_S.

        Runs only after an initial registration failure (key grabbed by
        another instance / other software). The floating ball keeps working
        the whole time — this loop only restores the keyboard path. Exits
        cleanly when: registration succeeds, someone else registered a
        binding meanwhile (e.g. a settings hotkey change succeeded), or
        _stop_event is set (app stop()).
        """
        while not self._stop_event.wait(HOTKEY_RETRY_INTERVAL_S):
            # A successful registration elsewhere (set_hotkey from settings)
            # makes this loop obsolete — retrying would fight our own binding.
            if getattr(self.hotkey_manager, "_bindings", None):
                _pipeline_log("HOTKEY", "Retry loop: binding already present, exiting")
                return
            try:
                self._register_recording_hotkey()
            except RuntimeError as e:
                _pipeline_log("HOTKEY", f"Retry registration failed: {e}")
                continue
            except Exception as e:
                logger.error(f"Hotkey retry unexpected error: {e}")
                continue
            # register() already spins up the message loop via
            # _run_on_hotkey_thread; explicit start() kept for parity with
            # the start() success path (no-op when already running).
            try:
                self.hotkey_manager.start()
            except Exception as e:
                logger.warning(f"Hotkey manager start after retry failed: {e}")
            logger.info(f"Hotkey recovered after retry: {self.hotkey}")
            print(f"[HOTKEY] Recovered after retry: {self.hotkey}")
            self._emit_error(f"热键已恢复：[{self.hotkey.upper()}] 可以使用了")
            return
        _pipeline_log("HOTKEY", "Retry loop: stop event set, exiting")

    def _on_hotkey_press(self, ts_s: float) -> None:
        """Hold-to-talk press (hotkey thread) - NON-BLOCKING, just enqueue."""
        _pipeline_log("HOTKEY", ">>> Hotkey press (hold_to_talk)")
        if self._hotkey_action_thread and not self._hotkey_action_thread.is_alive():
            _pipeline_log("HOTKEY", "Action worker died! Restarting...")
            self._start_hotkey_action_worker()
        try:
            self._hotkey_action_queue.put_nowait(("press", ts_s * 1000.0))
        except queue.Full:
            _pipeline_log("HOTKEY", "Action queue full, dropping press event")

    def _on_hotkey_release(self, ts_s: float) -> None:
        """Hold-to-talk release (hotkey thread) - NON-BLOCKING, just enqueue."""
        _pipeline_log("HOTKEY", ">>> Hotkey release (hold_to_talk)")
        try:
            self._hotkey_action_queue.put_nowait(("release", ts_s * 1000.0))
        except queue.Full:
            _pipeline_log("HOTKEY", "Action queue full, dropping release event")

    def _start_hotkey_action_worker(self) -> None:
        """Start the hotkey action processing thread."""
        if (
            self._hotkey_action_thread is None
            or not self._hotkey_action_thread.is_alive()
        ):
            self._hotkey_action_thread = threading.Thread(
                target=self._hotkey_action_worker, daemon=True, name="hotkey-action"
            )
            self._hotkey_action_thread.start()

    def _hotkey_action_worker(self) -> None:
        """Process hotkey actions sequentially, OFF the hotkey thread."""
        while not self._stop_event.is_set():
            try:
                action = self._hotkey_action_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            try:
                self._dispatch_hotkey_action(action)
            except Exception as e:
                _pipeline_log("HOTKEY", f"Action error: {e}")
                logger.error(f"Hotkey action error: {e}", exc_info=True)
            finally:
                self._hotkey_action_queue.task_done()

        logger.info("Hotkey action worker stopped")

    def _dispatch_hotkey_action(self, action) -> None:
        """Route one queued hotkey action (action worker thread).

        "toggle" (legacy hotkey press / UI button) goes down the unchanged
        toggle path; ("press"|"release", t_ms) tuples from the hold-to-talk
        event source feed the trigger state machine.
        """
        if action == "toggle":
            self._handle_hotkey_action()
        elif action == "wake_start_locked":
            self._handle_wake_start_locked()
        else:
            kind, t_ms = action
            self._handle_trigger_event(kind, t_ms)

    def _handle_trigger_event(self, kind: str, t_ms: float) -> None:
        """Feed one press/release event to the state machine and apply the action.

        NOTE(trigger-ui-hook): this is the reserved bridge notification point
        for the floating-ball / sound-effect lane — every non-ignore action
        (start_recording / toggle_lock / stop_and_commit) surfaces here with
        the machine's new state available as self._trigger_sm.state.
        """
        action = self._trigger_sm.on_event(kind, t_ms)
        _pipeline_log(
            "HOTKEY",
            f"Trigger {kind} -> {action} (sm_state={self._trigger_sm.state})",
        )

        if action == ACTION_START_RECORDING:
            if not self._trigger_start_recording():
                # Recording did not actually start (disabled / sleeping /
                # not idle): resync the machine so the next press is fresh.
                self._trigger_sm.reset()
        elif action == ACTION_STOP_AND_COMMIT:
            with self._lock:
                if self.state == AppState.RECORDING:
                    self._stop_recording()
                else:
                    _pipeline_log(
                        "HOTKEY",
                        f"Trigger stop ignored (state={self.state.name})",
                    )
        elif action == ACTION_TOGGLE_LOCK:
            # Recording simply continues; lock is a semantic marker. Play the
            # latch cue on entry only — exit goes through stop_and_commit,
            # which already plays the stop cue (no sound stacking). Ball
            # visuals (accent ring) belong to the UI lane (spec 1-D).
            self._play_sound("lock")

    def _handle_wake_start_locked(self) -> None:
        """Deep-sleep wake auto-start for hold_to_talk mode.

        Starts a recording session down the legacy toggle path, then syncs
        the trigger state machine to LOCKED so the wake-started session
        behaves like a locked dictation: the next press stops and commits.
        If the start did not happen (disabled / state raced away), reset the
        machine instead so the next press is a fresh start.
        """
        with self._lock:
            if self.state == AppState.RECORDING:
                # A press already started a session while this wake action
                # sat in the queue; the machine is consistent — running the
                # toggle path now would wrongly STOP that session.
                _pipeline_log("HOTKEY", "Wake auto-start skipped: already recording")
                return
        self._handle_hotkey_action()
        with self._lock:
            recording = self.state == AppState.RECORDING
        if recording:
            self._trigger_sm.sync_external_start(time.monotonic() * 1000.0)
        else:
            self._trigger_sm.reset()
        _pipeline_log(
            "HOTKEY",
            f"Wake auto-start (hold_to_talk): recording={recording}, "
            f"sm_state={self._trigger_sm.state}",
        )

    def _trigger_start_recording(self) -> bool:
        """Start recording for the hold-to-talk path; True if it started.

        Mirrors the guard prologue of _handle_hotkey_action (disabled state,
        sleep modes) without duplicating its toggle semantics.
        """
        if self._is_disabled:
            print("[HOTKEY] Re-enabling from disabled state")
            _pipeline_log("HOTKEY", "Re-enabling from disabled state")
            self._is_disabled = False
            try:
                if self._bridge:
                    self._bridge.emit_setting_changed("enabled", True)
            except Exception:
                pass
            return False

        with self._lock:
            sleep_mode = self._sleep_mode
        if sleep_mode == SleepMode.DEEP:
            # Auto-wake; engine reload auto-starts a recording session, but
            # this press itself did not start one synchronously.
            print("[HOTKEY] Pressed in deep sleep - auto-waking engine...")
            _pipeline_log("HOTKEY", "Deep sleep: auto-waking engine")
            self.set_deep_sleep(False)
            return False
        if sleep_mode == SleepMode.LIGHT:
            print("[HOTKEY] Pressed in light sleep - wakeword detection enabled")
            _pipeline_log("HOTKEY", "In light sleep, wakeword detection enabled")

        with self._lock:
            if self.state == AppState.IDLE:
                _pipeline_log("HOTKEY", "Trigger: starting recording...")
                self._start_recording()
                return True
        _pipeline_log(
            "HOTKEY", f"Trigger start ignored (state={self.state.name})"
        )
        return False

    def _handle_hotkey_action(self) -> None:
        """Handle hotkey toggle - runs on dedicated action thread (not hotkey thread)."""
        _pipeline_log("HOTKEY", ">>> Processing hotkey action")

        # If disabled, re-enable on hotkey press
        if self._is_disabled:
            print("[HOTKEY] Re-enabling from disabled state")
            _pipeline_log("HOTKEY", "Re-enabling from disabled state")
            self._is_disabled = False
            # Notify UI to update toggle
            try:
                if self._bridge:
                    self._bridge.emit_setting_changed("enabled", True)
            except Exception:
                pass
            return

        # Handle sleep modes
        with self._lock:
            sleep_mode = self._sleep_mode
        if sleep_mode == SleepMode.DEEP:
            # Auto-wake from deep sleep, then start recording
            print("[HOTKEY] Pressed in deep sleep - auto-waking engine...")
            _pipeline_log("HOTKEY", "Deep sleep: auto-waking engine")
            self.set_deep_sleep(
                False
            )  # Non-blocking: spawns _reload_thread (auto-starts recording)
            return
        if sleep_mode == SleepMode.LIGHT:
            # Light sleep: allow recording for wakeword detection
            print("[HOTKEY] Pressed in light sleep - wakeword detection enabled")
            _pipeline_log("HOTKEY", "In light sleep, wakeword detection enabled")

        worker_alive = self._asr_thread.is_alive() if self._asr_thread else False
        print(
            f"[HOTKEY] Pressed! state={self.state.name}, "
            f"queue={self._asr_queue.qsize()}, worker_alive={worker_alive}, "
            f"worker_busy={self._worker_busy}"
        )
        _pipeline_log(
            "HOTKEY",
            f"Current state: {self.state.name}, queue={self._asr_queue.qsize()}, "
            f"worker_alive={worker_alive}, worker_busy={self._worker_busy}",
        )

        with self._lock:
            if self.state == AppState.IDLE:
                # Normal recording start
                _pipeline_log("HOTKEY", "Starting recording...")
                self._start_recording()
            elif self.state == AppState.RECORDING:
                # Stop recording (toggle mode)
                _pipeline_log("HOTKEY", "Stopping recording...")
                self._stop_recording()
            else:
                print(
                    f"[HOTKEY] Ignored! state={self.state.name} (only IDLE/RECORDING accepted)"
                )
                _pipeline_log("HOTKEY", f"Ignored (state={self.state.name})")

    def _start_recording(self) -> None:
        """Start recording."""
        _pipeline_log("RECORD", ">>> _start_recording called")
        self._cancel_auto_deep_sleep_timer()

        # Cue: rising two-tone = recording starts (start.wav, async)
        self._play_sound("start_recording")

        self._session_count += 1
        with self._fast_wakeword_lock:
            self._fast_wakeword_session = None
            self._fast_wakeword_cmd_id = ""
            self._fast_wakeword_success = False
            self._fast_wakeword_started_at = 0.0

        # Set up this session's raw-text bucket; GC buckets older than 5 sessions
        # (enough slack for in-flight worker items while preventing unbounded growth
        # if a 'final' was somehow dropped without draining its bucket).
        with self._session_lock:
            self._session_raw_segments[self._session_count] = []
            self._session_soft_seg_seconds[self._session_count] = 0.0
            self._session_deferred_audio_segments[self._session_count] = []
            targets = getattr(self, "_session_output_targets", None)
            if targets is None:
                targets = self._session_output_targets = {}
            targets.pop(self._session_count, None)
            voice_starts = getattr(self, "_session_voice_started_at", None)
            if voice_starts is None:
                voice_starts = self._session_voice_started_at = {}
            voice_starts.pop(self._session_count, None)
            voice_windows = getattr(self, "_session_voice_windows", None)
            if voice_windows is None:
                voice_windows = self._session_voice_windows = {}
            voice_windows.pop(self._session_count, None)
            _stale_sids = [
                sid
                for sid in set(self._session_raw_segments.keys())
                | set(self._session_soft_seg_seconds.keys())
                | set(self._session_deferred_audio_segments.keys())
                | set(targets.keys())
                | set(voice_starts.keys())
                | set(voice_windows.keys())
                if sid < self._session_count - 5
            ]
            for _sid in _stale_sids:
                _dropped = self._session_raw_segments.pop(_sid, [])
                self._session_soft_seg_seconds.pop(_sid, None)
                _dropped_audio = self._session_deferred_audio_segments.pop(_sid, [])
                targets.pop(_sid, None)
                voice_starts.pop(_sid, None)
                voice_windows.pop(_sid, None)
                if _dropped or _dropped_audio:
                    _pipeline_log(
                        "SESSION",
                        f"GC'd {len(_dropped)} stranded text segments and "
                        f"{len(_dropped_audio)} audio chunks from old session {_sid}",
                    )

        print(f"\n{'=' * 50}")
        print(f"Recording Session #{self._session_count}")
        print(f"{'=' * 50}")
        print("[REC] Recording started")

        self.state = AppState.RECORDING
        self._emit_state("RECORDING")

        # Start window-change OCR watcher AFTER state is RECORDING
        # (watcher thread checks self.state == RECORDING to stay alive)
        self._start_ocr_watcher()
        _pipeline_log(
            "RECORD", f"Session #{self._session_count}, starting audio capture..."
        )
        if not self.audio_capture.start():
            logger.error("Failed to start audio capture")
            self._emit_error("麦克风启动失败，请检查音频设备")
            self.state = AppState.IDLE
            self._emit_state("IDLE")
            return
        self._last_audio_callback_time = time.time()
        self._start_audio_watchdog()
        _pipeline_log("RECORD", "Audio capture started")

    def _start_audio_watchdog(self) -> None:
        """Start watchdog that detects audio stream death during recording."""
        self._audio_watchdog_thread = threading.Thread(
            target=self._audio_watchdog_loop, daemon=True, name="audio-watchdog"
        )
        self._audio_watchdog_thread.start()

    def _audio_watchdog_loop(self) -> None:
        """Periodically check if audio stream is still alive."""
        while self.state == AppState.RECORDING and not self._stop_event.is_set():
            self._stop_event.wait(3.0)  # Check every 3 seconds
            if self.state != AppState.RECORDING:
                break
            if self._last_audio_callback_time <= 0:
                continue
            stale_s = time.time() - self._last_audio_callback_time
            if stale_s > self._audio_stale_threshold_s:
                print(
                    f"[WATCHDOG] Audio stream dead ({stale_s:.1f}s silent), restarting..."
                )
                _pipeline_log(
                    "WATCHDOG",
                    f"Audio stream stale ({stale_s:.1f}s), restarting capture",
                )
                try:
                    self.audio_capture.stop()
                    if self.audio_capture._vad:
                        self.audio_capture._vad.reset()
                    if self.audio_capture.start():
                        self._last_audio_callback_time = time.time()
                        print("[WATCHDOG] Audio capture restarted OK")
                        _pipeline_log("WATCHDOG", "Audio capture restarted OK")
                    else:
                        print("[WATCHDOG] Audio restart FAILED")
                        _pipeline_log("WATCHDOG", "Audio restart failed")
                except Exception as e:
                    print(f"[WATCHDOG] Audio restart error: {e}")
                    _pipeline_log("WATCHDOG", f"Audio restart error: {e}")

    def _stop_recording(self) -> None:
        """Stop recording."""
        _pipeline_log("RECORD", ">>> _stop_recording called")

        # Stop streaming ASR timer first
        self._stop_interim_timer()

        # Stop window-change OCR watcher
        self._stop_ocr_watcher()

        # Cue: falling two-tone = recording stops (stop.wav, async)
        self._play_sound("stop_recording")
        print("[STOP] Recording stopped")

        # Emit TRANSCRIBING state while processing
        self._emit_state("TRANSCRIBING")

        # Stop capture and get any remaining audio
        import time as _stop_time

        stop_start = _stop_time.time()
        _pipeline_log("RECORD", "Stopping audio capture...")
        final_audio = self.audio_capture.stop()
        # Manual-stop endpoint telemetry: stop() snapshots the in-flight VAD
        # segment stats (endpoint_reason='manual_stop') before draining its
        # buffers; None when no segment was in flight. Observation only.
        stop_vad_stats = self.audio_capture.consume_stop_vad_stats()
        stop_ms = (_stop_time.time() - stop_start) * 1000
        audio_len = len(final_audio) if final_audio is not None else 0
        audio_dur = audio_len / 16000 if audio_len > 0 else 0
        _pipeline_log(
            "RECORD",
            f"Audio captured: {audio_len} samples ({audio_dur:.2f}s), stop took {stop_ms:.0f}ms",
        )
        print(
            f"[STOP] Audio: {audio_dur:.2f}s ({audio_len} samples), capture.stop() took {stop_ms:.0f}ms"
        )

        # Minimum duration check: 0.3 seconds = 4800 samples at 16kHz
        # (Filter accidental clicks, but allow short words like "好"/"嗯")
        # BUT: if soft-split segments are already buffered, we MUST still run the
        # final commit path — otherwise the buffered raw text is stranded.
        MIN_SAMPLES = 4800  # 0.3 seconds

        with self._session_lock:
            _has_buffered = bool(
                self._session_raw_segments.get(self._session_count, [])
                or self._session_deferred_audio_segments.get(self._session_count, [])
            )

        if final_audio is not None:
            duration_s = len(final_audio) / 16000
            if len(final_audio) < MIN_SAMPLES and not _has_buffered:
                print(
                    f"[WARN] Recording too short ({duration_s:.2f}s < 0.3s) - accidental click?"
                )
                _pipeline_log("RECORD", f"Too short ({duration_s:.2f}s), skipping")
                self.state = AppState.IDLE
                self._emit_state("IDLE")
                self._emit_insert_complete()  # Must notify UI to shrink ball!
                print("[STATE] -> IDLE (skipped)")
                return
            if len(final_audio) < MIN_SAMPLES and _has_buffered:
                _pipeline_log(
                    "RECORD",
                    f"Final audio short ({duration_s:.2f}s) but session buffer has content — committing anyway",
                )
            else:
                _pipeline_log("RECORD", f"Queuing audio for ASR ({duration_s:.2f}s)")
            # Hotkey-stop bypasses VAD's on_speech_end; the manual-stop stats
            # snapshot (endpoint_reason='manual_stop', None if nothing was in
            # flight) stands in for the segment-level VAD telemetry.
            self._on_speech_end(final_audio, stop_vad_stats)
        else:
            # No audio captured - still run commit if we have buffered segments.
            if _has_buffered:
                _pipeline_log(
                    "RECORD", "No tail audio but session buffer non-empty — committing"
                )
                self._on_speech_end(None, stop_vad_stats)
            else:
                _pipeline_log("RECORD", "No audio captured!")
                self.state = AppState.IDLE
                self._emit_state("IDLE")
                self._emit_insert_complete()  # Must notify UI to shrink ball!
                print("[STATE] -> IDLE (no audio)")
                return

        # Internal state returns to IDLE, but DON'T emit to UI yet!
        # UI should stay in TRANSCRIBING until on_insert_complete() is called
        # This allows the loading animation to display properly
        self.state = AppState.IDLE
        # REMOVED: self._emit_state("IDLE") - moved to on_insert_complete flow
        print("[STATE] -> IDLE (internal, UI stays TRANSCRIBING)")

    # =========================================================================
    # Selection Mode Methods
    # =========================================================================

    def _try_enter_selection_mode(self) -> bool:
        """
        Try to enter selection mode by detecting selected text.

        Returns:
            True if entered selection mode, False otherwise (no selection)
        """
        # Debug logging to file
        debug_file = Path(__file__).parent / "DebugLog" / "selection_debug.log"

        def dbg(msg):
            with open(debug_file, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

        dbg("[SELECTION] Trying to detect selected text...")
        if not self.selection_detector:
            dbg("[SELECTION] No detector available!")
            return False

        try:
            selection_start_target = self.output_injector.capture_target_snapshot()
        except Exception:
            selection_start_target = None

        # Detect selection (sends Ctrl+C and checks clipboard)
        result = self.selection_detector.detect()
        dbg(
            f"[SELECTION] Detection result: has_selection={result.has_selection}, "
            f"selected_chars={len(result.selected_text or '')}, "
            f"original_clipboard_chars={len(result.original_clipboard or '')}, "
            f"original_format_count={len(getattr(result, 'original_clipboard_formats', None) or {})}"
        )

        if result.has_selection and result.selected_text:
            try:
                capture = self.output_injector.capture_selection_transaction(
                    result.selected_text,
                    selection_start_target,
                )
            except Exception:
                capture = None
            recording_started = False
            # Lock for thread-safe state modification
            with self._lock:
                # Store selection info
                self._selection_mode = True
                self._selected_text = result.selected_text
                self._selection_target = (
                    capture.target
                    if capture is not None and capture.success
                    else None
                )
                self._selection_capture_reason = str(
                    getattr(
                        getattr(capture, "status", None),
                        "value",
                        "target_unavailable",
                    )
                )
                self._original_clipboard = result.original_clipboard
                self._original_clipboard_formats = getattr(
                    result, "original_clipboard_formats", None
                )

                # Enter selection listening mode (silent, seamless experience)
                self.state = AppState.SELECTION_LISTENING
                self._emit_state("SELECTION_LISTENING")

                # Same cue as normal recording start
                self._play_sound("start_recording")

                print(f"[SELECTION] Text selected ({len(self._selected_text)} chars)")

                # Start recording for command
                self._session_count += 1
                self.state = AppState.RECORDING
                self._emit_state("RECORDING")
                recording_started = bool(self.audio_capture.start())

            if not recording_started:
                logger.error("Failed to start audio capture for selection mode")
                self._emit_error("麦克风启动失败，请检查音频设备")
                self._cleanup_selection_mode()
                return False

            return True

        return False

    def _stop_selection_recording(self) -> None:
        """Stop recording in selection mode and process the command."""
        # Same cue as normal recording stop
        self._play_sound("stop_recording")
        print("[SELECTION] Recording stopped, processing command...")

        self.state = AppState.SELECTION_PROCESSING
        self._emit_state("SELECTION_PROCESSING")

        # Stop capture and get audio
        final_audio = self.audio_capture.stop()

        if final_audio is None or len(final_audio) < 4000:  # < 0.25s
            print("[SELECTION] Recording too short, canceling")
            self._cancel_selection_mode()
            return

        # Queue for ASR (will be processed by _asr_worker_selection)
        # For now, process synchronously to keep it simple
        self._process_selection_audio(final_audio)

    def _process_selection_audio(self, audio) -> None:
        """Process audio in selection mode - transcribe and execute command."""
        try:
            # Transcribe command (with lock for thread safety)
            transcribe_engine, engine_label, engine_reason = (
                self._select_asr_engine_for_segment(
                    "selection",
                    allow_sync_fallback_load=True,
                    audio_duration_s=len(audio) / 16000.0,
                )
            )
            if transcribe_engine is None:
                transcribe_engine = self.asr_engine
                engine_label = "primary"
                engine_reason = "fallback_unavailable"
            if transcribe_engine is None:
                # Degraded no-ASR state: nothing can transcribe the spoken
                # command — tell the user instead of failing on None below.
                print("[SELECTION] No ASR engine available, canceling")
                self._emit_error("识别引擎不可用，无法执行选区指令")
                self._cancel_selection_mode()
                return
            if not engine_label.startswith("primary"):
                print(f"[SELECTION] Using {engine_label}: {engine_reason}")
            with self._asr_lock:
                result = transcribe_engine.transcribe(
                    audio,
                    capture_mode=getattr(self, "_capture_mode", "standard"),
                )
            command_text = result.text.strip()
            print(f"[SELECTION] Command ASR: '{command_text}'")

            if not command_text:
                print("[SELECTION] No command recognized, canceling")
                self._cancel_selection_mode()
                return

            # Parse command
            command = SelectionCommand.parse(command_text)
            if not command:
                print(
                    f"[SELECTION] Unknown command: '{command_text}', treating as custom"
                )
                # SelectionCommand.parse already handles custom commands
                self._cancel_selection_mode()
                return

            print(f"[SELECTION] Command type: {command.command_type.name}")

            # Process with LLM
            if self.selection_processor:
                from .core.ai.feedback import (
                    describe_ai_error,
                    describe_delivery_status,
                )

                result = self.selection_processor.process(
                    self._selected_text,
                    command,
                    trace_id="selection_mode",
                )

                if result.success and result.output_text:
                    if self._selection_target is None:
                        reason = self._selection_capture_reason
                        self._emit_draft(result.output_text, reason)
                        self._emit_error(
                            "当前应用无法安全锁定原选区，本次未自动替换"
                        )
                        print(f"[SELECTION] Safe replacement unavailable: {reason}")
                    else:
                        delivery = self.output_injector.replace_captured_selection(
                            result.output_text,
                            self._selected_text,
                            self._selection_target,
                        )
                        if delivery.success:
                            print(
                                f"[SELECTION] OK! Replaced with {len(result.output_text)} chars "
                                f"({result.processing_time_ms:.0f}ms)"
                            )
                        else:
                            reason = str(
                                getattr(delivery.status, "value", delivery.status)
                            )
                            self._emit_draft(result.output_text, reason)
                            if delivery.partial_possible:
                                self._emit_error(
                                    describe_delivery_status("write_partial")
                                )
                            else:
                                self._emit_error(
                                    describe_delivery_status(
                                        getattr(delivery, "status", None)
                                    )
                                )
                            print(f"[SELECTION] Guarded replacement refused: {reason}")
                else:
                    print(f"[SELECTION] Processing failed: {result.error}")
                    self._emit_error(
                        describe_ai_error(getattr(result, "error_category", None))
                    )
                    # Error - no beep (silent operation)
            else:
                print("[SELECTION] No processor available")
                # No processor - no beep (silent operation)

        except Exception as e:
            print(f"[SELECTION] Error: {e}")
            logger.error(f"Selection processing error: {e}")
            self._emit_error(str(e))
            # Exception - no beep (silent operation)

        finally:
            # Always cleanup and return to IDLE
            self._cleanup_selection_mode()

    def _cancel_selection_mode(self) -> None:
        """Cancel selection mode and restore state."""
        print("[SELECTION] Canceled")
        # Cancel - no beep (silent operation)

        # Stop recording if active
        if self.audio_capture and self.audio_capture.is_recording:
            self.audio_capture.stop()

        self._cleanup_selection_mode()

    def _cleanup_selection_mode(self) -> None:
        """Cleanup selection mode state."""
        # Restore original clipboard if we have it (full-format snapshot
        # covers image/file clipboards; text is the legacy fallback)
        if self.selection_detector and (
            self._original_clipboard is not None or self._original_clipboard_formats
        ):
            self.selection_detector.restore_clipboard(
                self._original_clipboard, self._original_clipboard_formats
            )

        # Reset state
        self._selection_mode = False
        self._selected_text = None
        self._selection_target = None
        self._selection_capture_reason = "target_unavailable"
        self._original_clipboard = None
        self._original_clipboard_formats = None

        self.state = AppState.IDLE
        self._emit_state("IDLE")
        self._emit_insert_complete()
        print("[SELECTION] Cleanup done, back to IDLE")

    def start(self) -> None:
        """
        Start the application (non-blocking mode for Qt frontend).

        This initializes components and starts listening for hotkeys,
        but does not block. Use with Qt event loop.
        """
        if self._running:
            logger.warning("AriaApp already running")
            return

        print("=" * 60)
        print("  Aria - Starting...")
        print("=" * 60)
        print()

        try:
            # Initialize components
            self._init_components()

            # Start ASR worker thread
            self._start_asr_worker()
            print("ASR worker thread started")

            # Start hotkey action worker (processes hotkey presses off the hotkey thread)
            self._start_hotkey_action_worker()
            print("Hotkey action worker started")

            # Start config file watcher for hot-reload
            self._start_config_watcher()

            # Register hotkey (non-fatal - can still use UI to toggle)
            print(f"\nRegistering hotkey: {self.hotkey}")
            hotkey_ok = False
            try:
                self._register_recording_hotkey()
                hotkey_ok = True
            except RuntimeError as e:
                # 热键注册失败不是致命错误，用户仍可通过点击悬浮窗使用
                error_msg = str(e)
                print(f"[WARN] Hotkey registration failed: {error_msg}")
                self._emit_error(f"{error_msg}（每 {HOTKEY_RETRY_INTERVAL_S:.0f} 秒自动重试）")
                # 不 raise，继续启动；后台每 30s 重试注册，占用方退出后自动恢复
                self._start_hotkey_retry()

            # Start hotkey listener (only if registration succeeded)
            if hotkey_ok:
                self.hotkey_manager.start()

            self._running = True
            self._schedule_auto_deep_sleep("startup_ready")
            self._emit_asr_status()

            # Always-on startup fingerprint in route_decisions.log (packaged/pythonw safe).
            try:
                from . import __version__ as _startup_version
            except Exception:
                try:
                    from aria import __version__ as _startup_version
                except Exception:
                    _startup_version = "unknown"
            try:
                from .core.utils.paths import get_base_path as _startup_base_path
            except Exception:
                from aria.core.utils.paths import get_base_path as _startup_base_path
            write_startup_fingerprint(
                version=str(_startup_version),
                base=_startup_base_path(),
                pid=os.getpid(),
            )

            # === Auto-update v1.0.5: write .app_ready.json ack after 3s ===
            # If we get here, app init succeeded. Let the rest of Qt event loop
            # run briefly, then write the ack so launcher knows swap succeeded.
            def _emit_app_ready_ack():
                try:
                    self._write_app_ready_ack()
                except Exception as e:
                    logger.warning(f"app_ready ack failed: {e}")

            _ack_timer = threading.Timer(3.0, _emit_app_ready_ack)
            _ack_timer.daemon = True
            _ack_timer.start()

            # Background update check (non-blocking)
            try:
                import json

                with open(self._config_path, "r", encoding="utf-8") as f:
                    _cfg = json.load(f)
                _auto_update = _cfg.get("general", {}).get("auto_check_update", True)
            except Exception:
                _auto_update = True
            # Personal builds (refresh_release.py output, marked by
            # PERSONAL_BUILD.txt at the package root) must never consume the
            # public update channel: an update swap would replace the owner's
            # dev-tree code with the sanitized public source. Until now only
            # the version-number coincidence (personal > public) blocked
            # this; make the exemption explicit.
            if _auto_update and _is_personal_build():
                _auto_update = False
                print(
                    "[UPDATE] Personal build (PERSONAL_BUILD.txt) — "
                    "public update channel disabled"
                )
            if _auto_update:
                threading.Thread(
                    target=self._check_update_background, daemon=True
                ).start()

            print()
            print("=" * 60)
            if hotkey_ok:
                print(f"  Press [{self.hotkey.upper()}] to start/stop recording")
            else:
                print(
                    f"  Hotkey [{self.hotkey.upper()}] unavailable - use UI to toggle"
                )
            print("=" * 60)
            print()
            print("Ready! Waiting for input...")

        except Exception as e:
            logger.error(f"Failed to start AriaApp: {e}")
            self._emit_error(str(e))
            raise

    def stop(self, *, unload_asr: bool = True) -> None:
        """
        Stop the application and cleanup resources.

        Args:
            unload_asr: Free the ASR model explicitly. Keep True for in-process
                shutdown/deep maintenance paths. GUI process exit passes False:
                Windows/Python will reclaim the process and CUDA resources, and
                skipping explicit model unload avoids a multi-second invisible
                shutdown window after the tray icon disappears.
        """
        if not self._running:
            return

        print("\nStopping AriaApp...")

        # Stop streaming ASR timer first (prevent stale callbacks)
        self._stop_interim_timer()
        self._cancel_auto_deep_sleep_timer()

        # Stop recording if active
        if self.audio_capture and self.audio_capture.is_recording:
            self.audio_capture.stop()

        # Stop ASR worker
        self._stop_asr_worker()

        # A deferred post-paste clipboard restore may still be parked in its
        # settle delay (daemon thread) — flush it now so quitting right after
        # a dictation doesn't drop the user's clipboard backup.
        # getattr: test shells build AriaApp via __new__ without __init__.
        _injector = getattr(self, "output_injector", None)
        if _injector is not None:
            try:
                _injector.flush_pending_restore()
            except Exception:
                logger.debug("flush_pending_restore failed", exc_info=True)

        # Wait for engine reload thread if running (prevent GPU leak)
        if self._reload_thread and self._reload_thread.is_alive():
            print("[CLEANUP] Waiting for engine reload to finish...")
            self._reload_thread.join(timeout=5.0)
            if self._reload_thread.is_alive():
                print(
                    "[CLEANUP] Reload thread did not stop in 5s (daemon, will be killed)"
                )

        # Stop hotkey listener
        self.hotkey_manager.stop()

        # Retry loop waits on _stop_event (set by _stop_asr_worker above);
        # join briefly so shutdown leaves no registration attempt in flight.
        # getattr: test shells build AriaApp via __new__ without __init__.
        _retry_thread = getattr(self, "_hotkey_retry_thread", None)
        if _retry_thread and _retry_thread.is_alive():
            _retry_thread.join(timeout=1.0)

        # Release ASR model (free GPU memory) unless this is full process exit.
        # External-process engines (supports_sync_teardown, e.g. llama-server)
        # ignore the unload_asr flag: the flag exists to skip the multi-second
        # torch unload at GUI exit, but skipping a millisecond process kill
        # would orphan the server (4GB VRAM + a bound port). The Job Object
        # binding would reap it when this process dies anyway; this keeps the
        # cooperative path deterministic.
        engine_needs_teardown = self.asr_engine and hasattr(self.asr_engine, "unload")
        if engine_needs_teardown and (
            unload_asr or getattr(self.asr_engine, "supports_sync_teardown", False)
        ):
            try:
                self.asr_engine.unload()
                print(
                    "[CLEANUP] ASR engine unloaded"
                    if unload_asr
                    else "[CLEANUP] External ASR server stopped (process exit)"
                )
            except Exception as e:
                logger.warning(f"ASR engine unload failed: {e}")
        elif self.asr_engine:
            print("[CLEANUP] ASR engine unload skipped (process exit)")

        if unload_asr:
            self._unload_gpu_fallback_engine("stop")
        elif getattr(self, "_gpu_fallback_engine", None):
            print("[CLEANUP] GPU fallback unload skipped (process exit)")

        # Close AI polisher HTTP client
        if self.polisher and hasattr(self.polisher, "close"):
            try:
                self.polisher.close()
                print("[CLEANUP] Polisher client closed")
            except Exception as e:
                logger.warning(f"Polisher close failed: {e}")

        # T1 belt-and-braces: stop the daily-loop and force-flush any unsaved
        # tracker state. The 30s heartbeat would normally have caught it, but
        # if Aria is stopped between two ticks we'd otherwise lose up to 30s
        # of newly-recorded auto-hotword candidates.
        try:
            self._auto_hotword_review_stop.set()
        except Exception:
            pass
        if self._auto_hotword_tracker is not None:
            try:
                self._auto_hotword_tracker.save_if_dirty()
                print("[CLEANUP] Auto-hotword tracker flushed")
            except Exception as e:
                logger.warning(f"Auto-hotword final save failed: {e}")

        self._running = False
        self._emit_state("IDLE")
        print("AriaApp stopped.")

    def toggle_recording(self) -> None:
        """
        Programmatically toggle recording (for UI buttons).
        Non-blocking: enqueues action for the hotkey worker thread.
        """
        try:
            self._hotkey_action_queue.put_nowait("toggle")
        except queue.Full:
            print("[TOGGLE] Dropped: action queue full")

    def is_running(self) -> bool:
        """Check if the application is running."""
        return self._running

    _RELOAD_DEBOUNCE_S = 0.5  # Minimum interval between reloads

    def reload_config(self, *, force: bool = False) -> None:
        """Reload configuration from hotwords.json (hot-reload support).

        Thread-safe: Uses self._lock to prevent race conditions with ASR worker.
        Debounced: ignores rapid successive calls within 500ms.
        """
        try:
            with self._lock:
                if not self.hotword_manager:
                    return

                # Debounce: skip if last reload was too recent
                now = time.time()
                last = getattr(self, "_last_reload_time", 0.0)
                if not force and now - last < self._RELOAD_DEBOUNCE_S:
                    print("[RELOAD] Debounced (too soon after last reload)")
                    return
                self._last_reload_time = now

                self.hotword_manager.reload()

                asr_cfg = self._load_asr_config()
                asr_reload_started = self._maybe_hot_reload_asr_engine(asr_cfg)
                if not asr_reload_started:
                    # v3.2: Preserve ASR engine type after reload (for polish layer optimization)
                    self.hotword_manager.config.asr_engine_type = self._asr_engine_type
                    self._configure_gpu_pressure_fallback(asr_cfg)
                    # Update Layer 1: ASR engine hotwords/context.
                    self._apply_hotword_context_to_asr_engine()
                self._configure_asr_rescue(asr_cfg)
                self._configure_sound(asr_cfg)
                self._apply_asr_runtime_vad_timing(asr_cfg, "config_reload")
                self._emit_asr_status()

                # Update Layer 2: Regex replacements
                if self.hotword_processor:
                    new_replacements = self.hotword_manager.get_replacements()
                    self.hotword_processor.replacements = new_replacements
                    layer_hotwords = self.hotword_manager.get_hotwords_by_layer()
                    self.hotword_processor.update_hotwords(
                        layer_hotwords.get("layer2_regex", [])
                    )
                    self.hotword_processor.update_explicit_corrections(
                        self._explicit_correction_store.active_rules()
                    )
                    print(
                        f"[HOT-RELOAD] Updated {len(new_replacements)} static and "
                        f"{self.hotword_processor.explicit_correction_count} explicit rules"
                    )

                # Update Layer 2.5: Fuzzy matcher (weight >= 1.0 only)
                if self.fuzzy_matcher:
                    layer_hotwords = self.hotword_manager.get_hotwords_by_layer()
                    fuzzy_hotwords = layer_hotwords.get("layer2_5_pinyin", [])
                    self.fuzzy_matcher.update_hotwords(fuzzy_hotwords)
                    print(
                        f"[HOT-RELOAD] Updated fuzzy matcher: {len(fuzzy_hotwords)} hotwords (weight>=1.0)"
                    )

                # T5: tell the auto-hotword tracker about the new user-curated
                # set so it stops accumulating words the user just added by
                # hand. Without this, freshly-added user hotwords keep getting
                # double-tracked until the next Aria restart.
                if self._auto_hotword_tracker is not None:
                    try:
                        new_user_words = set(
                            getattr(self.hotword_manager.config, "prompt_words", [])
                            or []
                        )
                        self._auto_hotword_tracker.update_user_hotwords(new_user_words)
                        print(
                            f"[HOT-RELOAD] Synced {len(new_user_words)} user hotwords "
                            f"to auto-hotword tracker"
                        )
                    except Exception as exc:
                        logger.warning(
                            f"Failed to sync user hotwords to tracker: {exc}"
                        )

                # Update Layer 3: Polisher (close old one first to free GPU/HTTP resources)
                old_polisher = self.polisher
                self.polisher = self.hotword_manager.get_active_polisher()
                if (
                    old_polisher
                    and old_polisher is not self.polisher
                    and hasattr(old_polisher, "close")
                ):
                    try:
                        old_polisher.close()
                        print("[HOT-RELOAD] Closed previous polisher")
                    except Exception as e:
                        logger.warning(f"Failed to close old polisher: {e}")

                # Update selection processor's polisher reference
                if self.selection_processor:
                    self.selection_processor.polisher = self.polisher
                    print("[HOT-RELOAD] Updated selection processor polisher")

                # Update wakeword detector
                if self.wakeword_detector:
                    self.wakeword_detector.reload()
                    print(
                        f"[HOT-RELOAD] Updated wakeword: '{self.wakeword_detector.wakeword}'"
                    )

                # Update command detector (prefix may have changed with wakeword)
                if self.command_detector:
                    self.command_detector.reload()
                    print(
                        f"[HOT-RELOAD] Updated commands: prefix='{self.command_detector.prefix}', "
                        f"{len(self.command_detector.commands)} commands"
                    )

                # Update VAD settings + energy gate
                try:
                    import json

                    with open(self._config_path, "r", encoding="utf-8") as f:
                        config = json.load(f)
                    vad_cfg = config.get("vad", {})
                    new_threshold = max(0.1, min(0.9, vad_cfg.get("threshold", 0.2)))
                    new_min_silence = max(
                        100, min(5000, vad_cfg.get("min_silence_ms", 1200))
                    )
                    new_max_speech = max(
                        3000, min(60000, vad_cfg.get("max_speech_ms", 10000))
                    )
                    new_energy = max(
                        0.0005,
                        min(0.02, vad_cfg.get("energy_threshold", 0.003)),
                    )

                    # Hot-reload capture mode (pre-ASR DSP). Read this BEFORE
                    # applying VAD/energy values so we can layer mode
                    # overrides on top of the user's base values.
                    audio_cfg = config.get("audio", {}) or {}
                    new_capture_mode = str(
                        audio_cfg.get("capture_mode", "standard") or "standard"
                    )
                    if new_capture_mode not in ("standard", "noisy", "whisper"):
                        new_capture_mode = "standard"
                    if new_capture_mode != getattr(self, "_capture_mode", "standard"):
                        self._capture_mode = new_capture_mode
                        print(f"[HOT-RELOAD] capture_mode: {self._capture_mode}")
                    new_mic_input_gain = self._mic_input_gain_from_audio_cfg(
                        audio_cfg, new_capture_mode
                    )
                    if new_mic_input_gain != getattr(self, "_mic_input_gain", 1.0):
                        self._mic_input_gain = new_mic_input_gain
                        print(
                            f"[HOT-RELOAD] mic_input_gain: "
                            f"{new_mic_input_gain:.2f}x"
                        )

                    # Hot-reload pre-ASR loudness normalization settings.
                    from .core.audio.gain import AsrGainConfig

                    new_gain_cfg = AsrGainConfig.from_mapping(audio_cfg)
                    if new_gain_cfg != getattr(self, "_asr_gain_cfg", None):
                        self._asr_gain_cfg = new_gain_cfg
                        print(
                            "[HOT-RELOAD] asr_gain_normalize: "
                            f"{'on' if new_gain_cfg.enabled else 'off'} "
                            f"(target={new_gain_cfg.target_peak_dbfs:.1f}dBFS, "
                            f"max=+{new_gain_cfg.max_gain_db:.0f}dB)"
                        )

                    # User-configured base values (always update so revert
                    # works if user later switches mode back to standard).
                    self._vad_threshold_base = new_threshold
                    self._energy_threshold_base = new_energy

                    # Layer the active mode's overrides on top.
                    from .core.audio.dsp import MODE_PRESETS as _MP

                    _preset = _MP[self._capture_mode]
                    _vad_ov = _preset.get("vad_threshold_override")
                    _energy_ov = _preset.get("energy_gate_override")
                    _max_speech_ov = _preset.get("max_speech_ms_override")
                    effective_vad_threshold = (
                        float(_vad_ov) if _vad_ov is not None else new_threshold
                    )
                    effective_energy = (
                        float(_energy_ov) if _energy_ov is not None else new_energy
                    )
                    effective_max_speech = new_max_speech
                    if _max_speech_ov is not None:
                        try:
                            _max_speech_ov_i = int(_max_speech_ov)
                            if _max_speech_ov_i > 0:
                                effective_max_speech = min(
                                    effective_max_speech, _max_speech_ov_i
                                )
                        except (TypeError, ValueError):
                            pass
                    effective_micro_rms = float(
                        _preset.get("endpoint_micro_rms") or 0.0
                    )
                    try:
                        effective_micro_min_ms = int(
                            _preset.get("endpoint_micro_min_speech_ms", 1200)
                        )
                    except (TypeError, ValueError):
                        effective_micro_min_ms = 1200
                    effective_steady_noise = self._vad_steady_noise_from_preset(_preset)

                    # Update VAD config in place
                    if self.audio_capture and self.audio_capture._vad:
                        _vad_runtime_cfg = self.audio_capture._vad.config
                        self.audio_capture._vad.config.threshold = (
                            effective_vad_threshold
                        )
                        _vad_runtime_cfg.min_silence_ms = new_min_silence
                        _vad_runtime_cfg.max_speech_ms = effective_max_speech
                        _vad_runtime_cfg.speech_end_micro_rms = effective_micro_rms
                        _vad_runtime_cfg.speech_end_micro_min_speech_ms = (
                            effective_micro_min_ms
                        )
                        for _k, _v in effective_steady_noise.items():
                            setattr(_vad_runtime_cfg, _k, _v)
                        self._apply_asr_runtime_vad_timing(
                            {
                                "engine": config.get("asr_engine", "qwen3"),
                                "qwen3": config.get("qwen3", {}) or {},
                                "funasr": config.get("funasr", {}) or {},
                                "vad": vad_cfg,
                            },
                            "config_reload",
                        )

                    # Update energy gate (used by ASR worker)
                    self._energy_threshold = effective_energy

                    # Update noise filter and screen OCR
                    self._noise_filter_enabled = vad_cfg.get("noise_filter", True)
                    self._apply_hallucination_gate_config(vad_cfg)
                    self._screen_ocr_enabled = vad_cfg.get("screen_ocr", False)
                    self._screen_ocr_polish_enabled = _screen_ocr_polish_opted_in(
                        vad_cfg
                    )
                    new_screen_ocr_force_cpu = bool(
                        vad_cfg.get("screen_ocr_force_cpu", False)
                    )
                    new_screen_ocr_use_dml = bool(
                        vad_cfg.get("screen_ocr_use_dml", True)
                    )
                    if (
                        new_screen_ocr_force_cpu != self._screen_ocr_force_cpu
                        or new_screen_ocr_use_dml != self._screen_ocr_use_dml
                    ):
                        old_force_cpu = self._screen_ocr_force_cpu
                        old_use_dml = self._screen_ocr_use_dml
                        self._screen_ocr_force_cpu = new_screen_ocr_force_cpu
                        self._screen_ocr_use_dml = new_screen_ocr_use_dml
                        # ScreenOCR keeps the selected RapidOCR engine in a
                        # module-level cache.  If the user toggles the
                        # diagnostic CPU switch or experimental DML switch while
                        # Aria is running, recreate the ScreenOCR object and
                        # clear the cached backend so the next speech_start
                        # really re-probes the requested path instead of
                        # silently continuing with the old tier.
                        try:
                            from .core.context.screen_ocr import (
                                reset_rapidocr_backend,
                            )

                            reset_rapidocr_backend(
                                "hot-reload OCR backend switches "
                                f"force_cpu {old_force_cpu}->{new_screen_ocr_force_cpu}, "
                                f"use_dml {old_use_dml}->{new_screen_ocr_use_dml}"
                            )
                        except Exception as _ocr_reset_error:
                            print(
                                "[HOT-RELOAD] OCR backend reset failed: "
                                f"{_ocr_reset_error}"
                            )
                        self._screen_ocr = None
                        print(
                            "[HOT-RELOAD] Screen OCR backend will reinitialize "
                            f"(force_cpu={self._screen_ocr_force_cpu}, "
                            f"use_dml={self._screen_ocr_use_dml})"
                        )
                    self._polish_terminal_bypass = bool(
                        vad_cfg.get("polish_terminal_bypass", False)
                    )

                    print(
                        f"[HOT-RELOAD] Updated VAD: threshold={new_threshold}, "
                        f"min_silence={new_min_silence}ms, "
                        f"max_speech={effective_max_speech}ms, "
                        f"micro_rms={effective_micro_rms:.4f}, "
                        f"steady_noise={effective_steady_noise['speech_end_steady_noise_ms']}ms, "
                        f"energy_gate={new_energy}, effective_energy={effective_energy}, "
                        f"noise_filter={self._noise_filter_enabled}, "
                        f"screen_ocr={self._screen_ocr_enabled}, "
                        f"screen_ocr_use_dml={self._screen_ocr_use_dml}, "
                        f"screen_ocr_force_cpu={self._screen_ocr_force_cpu}"
                    )

                    # Hot-reload auto_hotword.* scheduler settings. Previously
                    # these were read only at startup, so saving the settings
                    # page could still leave the live reviewer on the legacy
                    # once-per-day cadence until Aria was restarted.
                    new_auto_hotword_cfg = config.get("auto_hotword", {}) or {}
                    self._auto_hotword_cfg = new_auto_hotword_cfg
                    want_auto_hotword = _auto_hotword_opted_in(
                        new_auto_hotword_cfg
                    )
                    if not want_auto_hotword and self._auto_hotword_tracker is not None:
                        self._auto_hotword_review_stop.set()
                        self._auto_hotword_tracker.save_if_dirty()
                        self._auto_hotword_tracker = None
                        self._auto_hotword_reviewer = None
                        if self.polisher and hasattr(self.polisher, "config"):
                            self.polisher.config.session_hotwords = []
                        print("[HOT-RELOAD] Auto-hotword tracker disabled")
                    elif want_auto_hotword and self._auto_hotword_tracker is None:
                        self._init_auto_hotword(config.get("polish", {}) or {})
                        print("[HOT-RELOAD] Auto-hotword tracker enabled")
                    elif self._auto_hotword_reviewer is not None:
                        reviewer_cfg = self._auto_hotword_reviewer.config
                        polish_block = config.get("polish", {}) or {}
                        reviewer_cfg.api_url = (
                            new_auto_hotword_cfg.get("api_url")
                            or polish_block.get("api_url", "")
                            or "https://api.deepseek.com"
                        ).strip()
                        from .core.utils.secrets import reveal_secret

                        reviewer_cfg.api_key = reveal_secret(
                            str(
                                new_auto_hotword_cfg.get("api_key")
                                or polish_block.get("api_key", "")
                                or ""
                            ).strip()
                        )
                        reviewer_cfg.model = (
                            new_auto_hotword_cfg.get("model")
                            or polish_block.get("model", "")
                            or "deepseek-v4-flash"
                        ).strip()
                        reviewer_cfg.timeout = int(
                            new_auto_hotword_cfg.get("timeout")
                            or max(
                                int(polish_block.get("timeout", 60) or 0),
                                60,
                            )
                        )
                        reviewer_cfg.max_terms_per_call = int(
                            new_auto_hotword_cfg.get("max_terms_per_review", 50) or 50
                        )
                        reviewer_cfg.review_interval_hours = int(
                            new_auto_hotword_cfg.get("review_interval_hours", 6) or 6
                        )
                        reviewer_cfg.min_batch_size = int(
                            new_auto_hotword_cfg.get("min_batch_size", 8) or 8
                        )
                        if self._auto_hotword_tracker is not None:
                            self._auto_hotword_tracker.MIN_COUNT_FOR_REVIEW = int(
                                new_auto_hotword_cfg.get("min_count_for_review", 3) or 3
                            )
                        print(
                            "[HOT-RELOAD] Auto-hotword reviewer: "
                            f"interval={reviewer_cfg.review_interval_hours}h, "
                            f"min_batch={reviewer_cfg.min_batch_size}, "
                            f"max_terms={reviewer_cfg.max_terms_per_call}, "
                            f"available={self._auto_hotword_reviewer.is_available()}"
                        )
                except Exception as e:
                    print(f"[HOT-RELOAD] VAD update failed: {e}")

                # Update output settings (typewriter mode, elevation check)
                if self.output_injector:
                    try:
                        new_output_config = self._load_output_config()
                        self.output_injector.config = new_output_config
                        mode_str = (
                            "typewriter"
                            if new_output_config.typewriter_mode
                            else "clipboard"
                        )
                        print(f"[HOT-RELOAD] Updated output: mode={mode_str}")
                    except Exception as e:
                        print(f"[HOT-RELOAD] Output config update failed: {e}")

                # Sync watcher mtime to prevent double-reload
                # (settings save triggers both signal + mtime change; without this,
                # watcher would fire again ~2s later, bypassing the 0.5s debounce)
                try:
                    if self._config_path.exists():
                        self._config_mtime = self._config_path.stat().st_mtime
                except Exception:
                    pass

                logger.info("Configuration hot-reloaded (all 4 layers + VAD + output)")
                print("[HOT-RELOAD] Config reloaded successfully!")
                self._emit_api_status()
        except Exception as e:
            # Catch all exceptions to prevent config watcher from crashing the app
            logger.error(f"Config reload failed: {e}", exc_info=True)
            print(f"[HOT-RELOAD] Error: {e}")

    def _get_install_root(self):
        """Derive install_root for portable builds. None for dev."""
        script = Path(__file__).resolve()
        if "dist_portable" in str(script) or "_internal" in str(script):
            # <install_root>/_internal/app/aria/app.py → 4 levels up
            return script.parent.parent.parent.parent
        return None

    def _write_app_ready_ack(self) -> None:
        """v1.0.5 spec: one-shot ack that app booted successfully.

        Writes <install_root>/.app_ready.json and marks state=confirmed.
        Any prior failed_boots counter is reset to 0.
        """
        install_root = self._get_install_root()
        if not install_root:
            return  # Dev mode: no-op
        try:
            from . import __version__
            from .update_tool import set_update_state, get_update_state

            ready_path = install_root / ".app_ready.json"
            ts = (
                datetime.datetime.now(datetime.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
            payload = {"version": __version__, "pid": os.getpid(), "at": ts}
            tmp = ready_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, ready_path)
            # Confirm state (resets failed_boots). Only confirm if we were in swapped state.
            state = get_update_state()
            # Always arm boot_acked — it's the failure signal for launcher.
            # Separate from status transition (only flip status when we came
            # from a swap, otherwise leave idle/confirmed untouched).
            patch = {"boot_acked": True}
            if state.get("status") in ("swapped", "launched"):
                patch.update({"status": "confirmed", "failed_boots": 0, "error": ""})
                print(f"[UPDATE] Confirmed boot at v{__version__}")
            set_update_state(**patch)
        except Exception as e:
            logger.warning(f"_write_app_ready_ack: {e}")

    def _check_update_background(self, force_stage: bool = False) -> None:
        """Background thread: fetch manifest, if newer → download+stage → notify UI.

        Args:
            force_stage: When True, ignores the auto_download_update config flag
                         (used by explicit "检查更新" menu action).
        """
        # Choke-point guard: personal builds must never consume the public
        # update channel — an update swap would replace the owner's dev-tree
        # code with the sanitized public source. Guarding only the startup
        # call site missed the tray-menu "检查更新" path (ui/qt/main.py),
        # which calls this method directly with force_stage=True.
        if _is_personal_build():
            print(
                "[UPDATE] Personal build (PERSONAL_BUILD.txt) — public "
                "update channel disabled, check skipped"
            )
            return
        try:
            import time as _time

            if not force_stage:
                _time.sleep(3)  # Wait for UI to be ready (auto-check path)

            from . import __version__
            from .update_tool import (
                check_for_update,
                download_and_stage,
                get_update_state,
            )

            # If a stage is already ready from a previous run, just surface it
            prior = get_update_state()
            if prior.get("status") == "ready":
                to_ver = prior.get("to_version", "")
                print(f"[UPDATE] Stage already ready: v{to_ver}")
                if self._bridge and to_ver:
                    self._bridge.emit_update_available(__version__, to_ver)
                return

            result = check_for_update(local_version=__version__)
            if not result["available"]:
                if result["error"]:
                    print(f"[UPDATE] Check failed: {result['error']}")
                else:
                    print(f"[UPDATE] Already latest ({result['local']})")
                return

            remote = result["remote"]
            manifest = result.get("manifest") or {}
            print(f"[UPDATE] New version: {remote}, downloading...")

            # Check auto_download pref
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                auto_dl = cfg.get("general", {}).get("auto_download_update", True)
            except Exception:
                auto_dl = True

            if not auto_dl and not force_stage:
                # Auto-download disabled: don't emit 'available' (UI treats that
                # as 'staged & ready to apply'). Just log — user can force via
                # 托盘菜单 → 检查更新.
                print(f"[UPDATE] auto_download disabled; v{remote} not staged")
                return

            stage_result = download_and_stage(manifest)
            if stage_result["ok"]:
                print(f"[UPDATE] Stage ready for v{remote}")
                if self._bridge:
                    self._bridge.emit_update_available(result["local"], remote)
            else:
                print(f"[UPDATE] Stage failed: {stage_result.get('error')}")
        except Exception as e:
            print(f"[UPDATE] Check error: {e}")

    def apply_staged_update(self) -> bool:
        """Called from UI when user clicks "立即重启并更新".

        Copies updater_runner.py/.bat to install_root, spawns BAT detached,
        then initiates Aria shutdown. Returns True if spawn succeeded.
        """
        install_root = self._get_install_root()
        if not install_root:
            print("[UPDATE] apply_staged_update: dev mode, no-op")
            return False
        try:
            from .update_tool import get_update_state

            state = get_update_state()
            if state.get("status") != "ready":
                print(f"[UPDATE] apply_staged: state not ready ({state.get('status')})")
                return False

            aria_root = Path(__file__).resolve().parent
            stage_dir = install_root / "_internal" / "app" / "aria.new"
            # Locate updater_runner files in priority: install_root (already present) →
            # aria.new/ (newer, guaranteed by download_and_stage post-copy) → aria/ (live).
            # Backward compatibility: older installs may lack these files in aria/.
            import shutil as _sh

            for fname in ("updater_runner.py", "updater_runner.bat"):
                dst = install_root / fname
                if dst.exists():
                    continue  # already bootstrapped by download_and_stage
                src = None
                for candidate in (stage_dir / fname, aria_root / fname):
                    if candidate.exists():
                        src = candidate
                        break
                if src is None:
                    print(f"[UPDATE] missing {fname} in stage/ and aria/")
                    return False
                _sh.copy2(src, dst)

            # Spawn updater_runner.bat detached
            bat = install_root / "updater_runner.bat"
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                ["cmd.exe", "/d", "/c", str(bat), str(os.getpid())],
                cwd=str(install_root),
                creationflags=DETACHED_PROCESS
                | CREATE_NEW_PROCESS_GROUP
                | CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            print(f"[UPDATE] Spawned updater_runner.bat (pid={os.getpid()})")
            return True
        except Exception as e:
            logger.error(f"apply_staged_update failed: {e}")
            return False

    def _config_watcher(self) -> None:
        """Watch config file for changes and auto-reload (polling every 2s)."""
        logger.info("Config file watcher started")

        # Initialize mtime
        if self._config_path.exists():
            self._config_mtime = self._config_path.stat().st_mtime

        while not self._stop_event.is_set():
            try:
                if self._config_path.exists():
                    current_mtime = self._config_path.stat().st_mtime
                    if current_mtime > self._config_mtime:
                        self._config_mtime = current_mtime
                        print(f"\n[WATCHER] Detected config change, reloading...")
                        self.reload_config()
            except Exception as e:
                logger.warning(f"Config watcher error: {e}")

            # Poll every 2 seconds
            self._stop_event.wait(2.0)

        logger.info("Config file watcher stopped")

    def _start_config_watcher(self) -> None:
        """Start config file watcher thread."""
        if self._watcher_thread is None or not self._watcher_thread.is_alive():
            # Initialize mtime BEFORE starting thread to prevent false trigger
            if self._config_path.exists():
                self._config_mtime = self._config_path.stat().st_mtime
            self._watcher_thread = threading.Thread(
                target=self._config_watcher, daemon=True
            )
            self._watcher_thread.start()
            print("[WATCHER] Config file watcher started (hot-reload enabled)")

    def _stop_config_watcher(self) -> None:
        """Stop config file watcher thread."""
        # Uses same stop_event as ASR worker, will stop when app stops
        pass

    def set_enabled(self, enabled: bool) -> None:
        """
        Enable or disable Aria (hotkey listening).

        When disabled, hotkey still works but only to re-enable.
        This allows users to press hotkey to resume after elevation dialog.

        Args:
            enabled: True to enable, False to disable
        """
        if enabled:
            self._is_disabled = False
            if not self._running:
                # Not running at all, full start
                self.start()
            print("[Aria] Enabled")
            logger.info("Aria enabled")
        else:
            if self._running:
                # Set disabled flag but keep hotkey listening (to allow re-enable)
                self._is_disabled = True
                print("[Aria] Disabled (press hotkey to re-enable)")
                logger.info("Aria disabled (hotkey can re-enable)")

    def set_polish_mode(self, mode: str) -> None:
        """
        Set polish mode from UI.

        Args:
            mode: "off" (disabled), "fast" (local Qwen), or "quality" (API)
        """
        if self.hotword_manager:
            try:
                self.hotword_manager.set_polish_mode(mode)
                # Update active polisher
                with self._lock:
                    old_polisher = self.polisher
                    self.polisher = self.hotword_manager.get_active_polisher()
                    # Close old polisher to free HTTP client resources
                    if (
                        old_polisher
                        and old_polisher is not self.polisher
                        and hasattr(old_polisher, "close")
                    ):
                        try:
                            old_polisher.close()
                        except Exception:
                            pass
                    # Sync selection_processor polisher reference
                    if self.selection_processor:
                        self.selection_processor.polisher = self.polisher
                logger.info(
                    f"Polish mode set to: {mode}, polisher: {type(self.polisher).__name__ if self.polisher else 'None'}"
                )
                self._emit_api_status()
            except Exception as e:
                logger.error(f"Failed to set polish mode: {e}", exc_info=True)
                # Keep existing polisher on error

    def get_polish_mode(self) -> str:
        """
        Get current polish mode.

        Returns:
            "off", "fast", or "quality"
        """
        if self.hotword_manager:
            return self.hotword_manager.polish_mode
        return "quality"  # Default matches template

    def get_api_status(self) -> dict:
        """Return current Polish API status for UI refresh."""
        if self.polisher and hasattr(self.polisher, "get_api_status"):
            status = self.polisher.get_api_status()
        else:
            status = {
                "enabled": False,
                "backup_enabled": False,
                "using_backup": False,
                "current_api": "未启用",
                "current_url": "",
                "current_host": "未启用",
                "model": "",
                "status_message": "高质量 API 润色未启用",
                "can_switch_primary": False,
            }
        return self._decorate_api_status(status)

    _DEEPSEEK_DEFAULT_API_URL = "https://api.deepseek.com"
    _DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"

    def _deepseek_setup_mutex(self) -> threading.Lock:
        """Return the setup mutex, including for lightweight test instances."""
        lock = getattr(self, "_deepseek_setup_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._deepseek_setup_lock = lock
        return lock

    def _has_configured_polish_api(self) -> bool:
        """Check for a decryptable API key without exposing it to the UI/logs."""
        from .core.utils.secrets import reveal_secret

        manager = getattr(self, "hotword_manager", None)
        live_cfg = getattr(getattr(manager, "config", None), "polish_config", None)
        if live_cfg is not None and reveal_secret(
            str(getattr(live_cfg, "api_key", "") or "")
        ):
            return True

        try:
            config_path = Path(self._config_path)
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            polish = data.get("polish", {})
            return isinstance(polish, dict) and bool(
                reveal_secret(str(polish.get("api_key") or "").strip())
            )
        except Exception:
            return False

    def _decorate_api_status(self, status: dict | None) -> dict:
        """Attach guided-setup state to an existing failover status snapshot."""
        decorated = dict(status or {})
        configured = self._has_configured_polish_api()
        setup_in_progress = bool(
            getattr(self, "_deepseek_setup_in_progress", False)
        )
        setup_error = str(getattr(self, "_deepseek_setup_error", "") or "")
        decorated["configured"] = configured
        decorated["setup_in_progress"] = setup_in_progress
        decorated["setup_error"] = setup_error
        if setup_in_progress:
            decorated["status_message"] = "正在验证 DeepSeek API"
        elif setup_error and not configured:
            decorated["status_message"] = setup_error
        return decorated

    def _validate_deepseek_api_key(self, api_key: str) -> bool:
        """Validate the key with one real minimal DeepSeek Flash request."""
        from .core.hotword.polish import PolishConfig

        candidate = AIPolisher(
            PolishConfig(
                enabled=True,
                api_url=self._DEEPSEEK_DEFAULT_API_URL,
                api_key=api_key,
                model=self._DEEPSEEK_DEFAULT_MODEL,
                timeout=20.0,
            )
        )
        try:
            return bool(candidate.prewarm(reason="guided_deepseek_setup"))
        finally:
            candidate.close()

    def _persist_deepseek_api_config(self, api_key: str) -> None:
        """Atomically store the verified key and default DeepSeek Flash route."""
        from .core.utils.secrets import is_encrypted, protect_secret

        config_path = Path(self._config_path)
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        stored_key = protect_secret(api_key)
        if not is_encrypted(stored_key):
            # This app is Windows-only and project policy requires DPAPI at
            # rest. Never silently fall back to plaintext from this easy path.
            raise RuntimeError("Windows API Key 加密不可用")

        polish = data.get("polish")
        if not isinstance(polish, dict):
            polish = {}
            data["polish"] = polish
        polish.update(
            {
                "enabled": True,
                "api_url": self._DEEPSEEK_DEFAULT_API_URL,
                "api_key": stored_key,
                "model": self._DEEPSEEK_DEFAULT_MODEL,
            }
        )
        data["polish_mode"] = "quality"

        tmp_path = config_path.with_suffix(config_path.suffix + ".deepseek.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, config_path)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _run_deepseek_setup(self, api_key: str) -> None:
        """Background validation + save + live reload transaction."""
        success = False
        saved_but_not_live = False
        setup_error = ""
        try:
            if not self._validate_deepseek_api_key(api_key):
                setup_error = "验证失败，请检查 API Key 或网络后重试"
                return

            self._persist_deepseek_api_config(api_key)
            self.reload_config(force=True)
            active = (
                self.get_polish_mode() == "quality"
                and self.polisher is not None
                and hasattr(self.polisher, "get_api_status")
            )
            if not active:
                saved_but_not_live = True
                setup_error = "API 已保存，但即时启用失败，请重启 Aria"
                return
            success = True
        except Exception:
            setup_error = "配置失败，请稍后重试"
            _pipeline_log("POLISH", "Guided DeepSeek setup failed")
        finally:
            # Clear the worker-local name before notifying UI; the thread's
            # argument tuple is released as soon as this method returns.
            api_key = ""
            with self._deepseek_setup_mutex():
                self._deepseek_setup_in_progress = False
                self._deepseek_setup_error = setup_error
                self._deepseek_setup_thread = None
            self._emit_api_status()
            if success:
                self._emit_notice(
                    "DeepSeek Flash 已配置并验证成功，已开启高质量润色",
                    "success",
                    4200,
                )
            elif saved_but_not_live:
                self._emit_notice(setup_error, "warning", 5200)
            else:
                self._emit_notice(setup_error or "DeepSeek API 配置失败", "error", 4200)

    def configure_deepseek_api(self, api_key: str) -> bool:
        """Start guided DeepSeek setup without blocking the Qt event loop."""
        value = str(api_key or "").strip()
        if not value:
            return False

        with self._deepseek_setup_mutex():
            if getattr(self, "_deepseek_setup_in_progress", False):
                return False
            self._deepseek_setup_in_progress = True
            self._deepseek_setup_error = ""
            worker = threading.Thread(
                target=self._run_deepseek_setup,
                args=(value,),
                name="AriaDeepSeekSetup",
                daemon=True,
            )
            self._deepseek_setup_thread = worker
        self._emit_api_status()
        try:
            worker.start()
        except Exception:
            with self._deepseek_setup_mutex():
                self._deepseek_setup_in_progress = False
                self._deepseek_setup_error = "无法启动验证，请稍后重试"
                self._deepseek_setup_thread = None
            self._emit_api_status()
            return False
        return True

    @classmethod
    def _wakeword_result_uses_contextual_ai(cls, wakeword_result) -> bool:
        """Return whether a detected legacy action may need recent-text routing."""

        try:
            action = str(wakeword_result[1] or "")
        except Exception:
            return False
        return action in cls._CONTEXTUAL_AI_WAKEWORD_ACTIONS

    def _wakeword_result_is_explicit_contextual_request(
        self, wakeword_result
    ) -> bool:
        """Distinguish an independent selected-text tool from natural feedback.

        The detector intentionally uses substring recall, which is useful for
        short commands but too broad as the final routing decision. Selection
        processing therefore requires either an exact configured phrase or an
        explicit reference to selected text. Popup actions keep request-prefix
        matching because translation/summary/reply are intentionally explicit
        modal workflows; generic editing instructions use recent rewrite.
        """

        if not self._wakeword_result_uses_contextual_ai(wakeword_result):
            return False
        try:
            command_id = str(wakeword_result[0] or "")
            action = str(wakeword_result[1] or "")
            command_text = str(wakeword_result[5] or "")
        except Exception:
            return False

        if action == "ask_ai":
            return self._wakeword_result_is_explicit_ai_chat(wakeword_result)

        detector = getattr(self, "wakeword_detector", None)
        commands = getattr(detector, "commands", None)
        config = commands.get(command_id) if isinstance(commands, dict) else None
        triggers = config.get("triggers", ()) if isinstance(config, dict) else ()
        normalize = getattr(detector, "_normalize_for_match", None)
        if not callable(normalize) or not isinstance(triggers, (list, tuple)):
            return False

        normalized = normalize(command_text)
        trigger_values = tuple(
            value
            for value in (normalize(str(trigger or "")) for trigger in triggers)
            if value
        )
        if not normalized or not trigger_values:
            return False

        if action == "selection_process":
            if normalized in trigger_values:
                return True
            has_selection_reference = any(
                normalize(reference) in normalized
                for reference in self._EXPLICIT_SELECTION_REFERENCES
            )
            return has_selection_reference and any(
                trigger in normalized for trigger in trigger_values
            )

        request_body = normalized
        for lead_in in self._CONTEXTUAL_REQUEST_LEAD_INS:
            lead = normalize(lead_in)
            if request_body.startswith(lead) and len(request_body) > len(lead):
                request_body = request_body[len(lead) :]
                break
        return any(request_body.startswith(trigger) for trigger in trigger_values)

    @classmethod
    def _wakeword_result_is_explicit_ai_chat(cls, wakeword_result) -> bool:
        """Allow the general chat popup only for an explicit AI-chat request."""

        try:
            action = str(wakeword_result[1] or "")
            command_text = str(wakeword_result[5] or "")
        except Exception:
            return False
        if action != "ask_ai":
            return False

        import re
        import unicodedata

        normalized = unicodedata.normalize("NFKC", command_text).casefold()
        normalized = re.sub(r"[\s，。！？、,.!?：:；;‘’“”\"'（）()]+", "", normalized)
        return any(term in normalized for term in cls._EXPLICIT_AI_CHAT_TERMS)

    def _execute_final_wakeword_result(self, wakeword_result, raw_text: str):
        """Execute one final-ASR wakeword result and publish its UI receipt."""

        (
            cmd_id,
            action,
            value,
            response,
            following_text,
            command_text,
        ) = wakeword_result
        self.wakeword_executor._pending_command_text = command_text
        success = self.wakeword_executor.execute(
            cmd_id, action, value, response, following_text
        )
        status = "OK" if success else "FAIL"
        print(f"[WAKEWORD] {status}: {cmd_id} (raw ASR: '{raw_text}')")
        if self._bridge and hasattr(self._bridge, "emit_command"):
            self._bridge.emit_command(f"小助手:{cmd_id}", success)
        return bool(success), str(cmd_id), str(response or "")

    @staticmethod
    def _asr_short_model_name(model_name: str) -> str:
        """Compact model label for floating UI."""
        name = str(model_name or "").replace("\\", "/").rstrip("/")
        if not name:
            return "未知模型"
        leaf = name.split("/")[-1]
        if leaf.startswith("sherpa-onnx-qwen3-asr"):
            return "0.6B·轻量"
        if "Qwen3-ASR-" in leaf:
            return leaf.replace("Qwen3-ASR-", "")
        if leaf.startswith("Qwen3-ASR"):
            return leaf
        return leaf

    # Popup engine-switcher targets: llamacpp (GPU) on the left button,
    # sherpa (CPU) on the right — mirrors the card's 显卡/CPU button order.
    _ASR_ENGINE_SWITCH_TARGETS = (
        ("qwen3_llamacpp", "GPU 加速"),
        ("qwen3_sherpa", "CPU 轻量"),
    )
    _GPU_RUNTIME_CONFIG_PATHS = {
        "server_path": "models/llamacpp_runtime/bin/llama-server.exe",
        "model_path": (
            "models/llamacpp_runtime/models/Qwen3-ASR-1.7B-Q8_0.gguf"
        ),
        "mmproj_path": (
            "models/llamacpp_runtime/models/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf"
        ),
    }
    _GPU_VERIFIED_MARKER = "models/llamacpp_runtime/.gpu_verified_v1"
    _GPU_PROGRESS_PREFIX = "ARIA_GPU_PROGRESS "

    def _sherpa_wheel_available(self) -> bool:
        """Cached check_sherpa_installation probe for status refreshes.

        Python does not cache FAILED imports, so in a llamacpp-without-sherpa
        environment every _emit_asr_status would re-walk sys.path for the
        missing wheel (tens-of-ms stall each time). Both outcomes are cached
        on the app instance; the cache is dropped when the user attempts an
        engine switch (set_asr_engine_mode) and when a hot-reload finishes,
        so a freshly installed wheel still gets picked up.
        """
        cached = getattr(self, "_sherpa_install_cache", None)
        if cached is None:
            from .core.asr.sherpa_engine import check_sherpa_installation

            cached = bool(check_sherpa_installation())
            self._sherpa_install_cache = cached
        return cached

    def _invalidate_sherpa_install_cache(self) -> None:
        self._sherpa_install_cache = None

    def _gpu_installer_command(self) -> tuple[list[str], Path]:
        """Return the bundled/source GPU installer command and working dir.

        Portable builds must use their embedded Python so installation never
        depends on a system Python. Development runs use the repository venv
        when present. The helper owns download, pinned-hash verification and
        the real NVIDIA smoke test; the live app only owns the final hot
        switch after that process succeeds.
        """
        app_root = Path(__file__).resolve().parent
        install_root = self._get_install_root()
        if install_root:
            helper = install_root / "fetch_gpu_pack.py"
            python_exe = install_root / "_internal" / "python.exe"
            workdir = install_root
        else:
            helper = app_root / "scripts" / "fetch_gpu_pack.py"
            python_exe = app_root / ".venv" / "Scripts" / "python.exe"
            if not python_exe.is_file():
                python_exe = Path(sys.executable)
            workdir = app_root
        if not helper.is_file():
            raise RuntimeError("安装组件缺失，请重新解压官方标准版")
        if not python_exe.is_file():
            raise RuntimeError("内置运行环境缺失，请重新解压官方标准版")
        return (
            [
                str(python_exe),
                "-s",
                "-u",
                str(helper),
                "--verify-gpu",
                "--target",
                str(app_root),
            ],
            workdir,
        )

    def _gpu_installer_support_problem(self) -> str:
        try:
            self._gpu_installer_command()
            return ""
        except Exception as exc:
            return str(exc)

    def _gpu_config_with_installed_cache(self, cfg: dict) -> dict:
        """Adopt the verified installer's standard cache when it is present.

        Existing valid custom paths always win. A standard CPU package starts
        with legacy/default llama.cpp paths, while the on-demand installer
        writes into models/llamacpp_runtime. This in-memory normalization lets
        status notice the freshly installed assets and makes the subsequent
        engine switch persist all three canonical paths atomically.
        """
        if not self._asr_engine_asset_problem("qwen3_llamacpp", cfg):
            return cfg
        marker = Path(__file__).resolve().parent / self._GPU_VERIFIED_MARKER
        if not marker.is_file():
            return cfg
        candidate = dict(cfg)
        section = dict(candidate.get("qwen3_llamacpp", {}) or {})
        section.update(self._GPU_RUNTIME_CONFIG_PATHS)
        candidate["qwen3_llamacpp"] = section
        if not self._asr_engine_asset_problem("qwen3_llamacpp", candidate):
            return candidate
        return cfg

    @staticmethod
    def _gpu_installer_log_tail(path: Path, limit: int = 2400) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")[-limit:].strip()
        except OSError:
            return ""

    def _apply_gpu_install_progress_line(self, line: str) -> bool:
        """Consume one trusted installer progress record.

        The helper's human-readable output is still written to the install
        log. Only prefixed JSON records reach the UI, and percentage is kept
        monotonic so a resumed download or noisy stdout cannot make the
        floating indicator jump backwards.
        """
        raw = str(line or "").strip()
        marker_at = raw.rfind(self._GPU_PROGRESS_PREFIX)
        if marker_at < 0:
            return False
        try:
            payload = json.loads(raw[marker_at + len(self._GPU_PROGRESS_PREFIX) :])
            percent = max(0, min(100, int(payload.get("percent", 0))))
            phase = str(payload.get("phase") or "install").strip()
            message = str(payload.get("message") or "").strip()
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.debug("Ignored malformed GPU installer progress record")
            return False

        previous = int(getattr(self, "_gpu_install_progress", 0) or 0)
        percent = max(previous, percent)
        changed = (
            percent != previous
            or phase != getattr(self, "_gpu_install_phase", "")
            or message != getattr(self, "_gpu_install_message", "")
        )
        self._gpu_install_progress = percent
        self._gpu_install_phase = phase
        self._gpu_install_message = message
        if changed:
            self._emit_asr_status()
        return True

    def _run_gpu_installer(self, command: list[str], workdir: Path) -> tuple[int, str]:
        """Run the helper in the worker thread and stream progress to Qt."""
        log_dir = Path(__file__).resolve().parent / "DebugLog"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "gpu_install.log"
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                command,
                cwd=str(workdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                log.flush()
                self._apply_gpu_install_progress_line(line)
            return_code = process.wait()
        return int(return_code), self._gpu_installer_log_tail(log_path)

    def _gpu_install_worker(self, command: list[str], workdir: Path) -> None:
        """Background install, verification and same-session GPU activation."""
        try:
            rc, tail = self._run_gpu_installer(command, workdir)
            if rc != 0:
                last_line = next(
                    (line.strip() for line in reversed(tail.splitlines()) if line.strip()),
                    f"安装程序退出码 {rc}",
                )
                raise RuntimeError(last_line)

            # Clear the install flag before set_asr_engine_mode emits status:
            # the card should transition from "安装中" to the existing
            # "正在切换" state rather than stay stuck on the installer state.
            self._gpu_install_in_progress = False
            if self._bridge and hasattr(self._bridge, "emit_notice"):
                self._bridge.emit_notice(
                    "GPU 加速安装并验证完成，正在自动切换…", "success", 3600
                )
            self.set_asr_engine_mode("qwen3_llamacpp")
            _pipeline_log("ASR", "In-app GPU install verified; hot switch requested")
            return
        except Exception as exc:
            logger.error(f"In-app GPU install failed: {exc}", exc_info=True)
            _pipeline_log("ASR", f"In-app GPU install failed: {exc}")
            self._emit_error(f"GPU 加速安装失败，已继续使用 CPU：{exc}")
        finally:
            self._gpu_install_in_progress = False
            self._gpu_install_thread = None
            self._emit_asr_status()

    def install_gpu_engine(self) -> dict:
        """Start a verified on-demand GPU install and return immediately.

        The CPU engine remains usable while the helper downloads roughly 3 GB
        of assets. Duplicate clicks are idempotent. If assets already exist,
        this falls through to the normal hot switch with no installer launch.
        """
        if bool(getattr(self, "_gpu_install_in_progress", False)):
            return self.get_asr_runtime_status()

        with open(self._config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        effective_cfg = self._gpu_config_with_installed_cache(cfg)
        if not self._asr_engine_asset_problem("qwen3_llamacpp", effective_cfg):
            return self.set_asr_engine_mode("qwen3_llamacpp")

        command, workdir = self._gpu_installer_command()
        self._gpu_install_in_progress = True
        self._gpu_install_progress = 0
        self._gpu_install_phase = "prepare"
        self._gpu_install_message = "正在准备 GPU 加速安装"
        self._emit_asr_status()
        worker = threading.Thread(
            target=self._gpu_install_worker,
            args=(command, workdir),
            name="AriaGPUInstaller",
            daemon=True,
        )
        self._gpu_install_thread = worker
        worker.start()
        return self.get_asr_runtime_status()

    def _asr_engine_asset_problem(self, target_engine: str, cfg: dict) -> str:
        """Local-asset availability probe for a cross-engine switch target.

        Returns "" when the target engine's on-disk assets look usable, else a
        short user-facing reason (shown as the disabled button's tooltip and
        as the refusal message in set_asr_engine_mode). Mirrors the settings
        page's pre-save probe, minus the llama-server port check: a foreign
        port is only knowable at spawn time and routes through the hot-reload
        failure -> rollback chain instead.
        """
        try:
            if target_engine == "qwen3_sherpa":
                if not self._sherpa_wheel_available():
                    return "未安装 sherpa-onnx 依赖"
                sherpa_cfg = cfg.get("qwen3_sherpa", {}) or {}
                model_dir = resolve_sherpa_model_dir(
                    str(sherpa_cfg.get("model_dir", "") or "")
                )
                if not Path(model_dir).is_dir():
                    return "缺少轻量识别模型"
                return ""
            if target_engine == "qwen3_llamacpp":
                # resolve_llamacpp_path routes through the pack-aware
                # resolve_llamacpp_asset chain (root -> models/llamacpp_runtime
                # cache / ARIA_LLAMACPP_DIR), so the pre-flight sees the same
                # assets load() would.
                llamacpp_cfg = cfg.get("qwen3_llamacpp", {}) or {}
                server_path = resolve_llamacpp_path(
                    str(llamacpp_cfg.get("server_path", "") or ""),
                    DEFAULT_LLAMACPP_SERVER,
                )
                model_path = resolve_llamacpp_path(
                    str(llamacpp_cfg.get("model_path", "") or ""),
                    DEFAULT_LLAMACPP_MODEL,
                )
                mmproj_raw = str(llamacpp_cfg.get("mmproj_path", "") or "").strip()
                mmproj_path = (
                    resolve_llamacpp_path(mmproj_raw, "")
                    if mmproj_raw
                    else default_mmproj_for(model_path)
                )
                if not Path(server_path).is_file():
                    return "缺少 llama-server 程序"
                if not Path(model_path).is_file():
                    return "缺少 GGUF 识别模型"
                if not Path(mmproj_path).is_file():
                    return "缺少 mmproj 文件"
                return ""
            return ""
        except Exception as exc:
            return f"检测失败: {exc}"

    def _asr_engine_switch_targets(self, engine_type: str) -> tuple[str, list[dict]]:
        """Classify the popup 识别方式 card's semantics for the current engine.

        Returns (switch_kind, engine_targets):
          * "device" — torch qwen3 / funasr: the legacy GPU/CPU device quick
            switch, byte-for-byte unchanged.
          * "engine" — sherpa/llamacpp: the card becomes a cross-engine
            switcher between the GPU (llamacpp) and lightweight-CPU (sherpa)
            runtimes; each target carries local-asset availability so the UI
            can gray out + explain a missing target.
          * "none"   — degraded no-ASR state: card fully disabled.
        """
        if engine_type == "none":
            return "none", []
        if engine_type not in ("qwen3_sherpa", "qwen3_llamacpp"):
            return "device", []
        cfg: dict = {}
        config_path = getattr(self, "_config_path", None)
        if config_path:
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
        targets = []
        gpu_installing = bool(getattr(self, "_gpu_install_in_progress", False))
        for target, label in self._ASR_ENGINE_SWITCH_TARGETS:
            if target == engine_type:
                # The currently running engine is trivially "available".
                targets.append(
                    {
                        "engine": target,
                        "label": label,
                        "available": True,
                        "installed": True,
                        "installable": False,
                        "installing": False,
                        "reason": "",
                    }
                )
                continue
            effective_cfg = (
                self._gpu_config_with_installed_cache(cfg)
                if target == "qwen3_llamacpp"
                else cfg
            )
            problem = self._asr_engine_asset_problem(target, effective_cfg)
            install_problem = (
                self._gpu_installer_support_problem()
                if target == "qwen3_llamacpp" and problem
                else ""
            )
            targets.append(
                {
                    "engine": target,
                    "label": label,
                    "available": not problem,
                    "installed": not problem,
                    "installable": bool(
                        target == "qwen3_llamacpp" and problem and not install_problem
                    ),
                    "installing": bool(
                        target == "qwen3_llamacpp" and gpu_installing
                    ),
                    "reason": install_problem or problem,
                }
            )
        return "engine", targets

    def get_asr_runtime_status(self) -> dict:
        """Return current ASR runtime/config status for floating UI.

        This deliberately reports both configured and actual device.  On RTX
        driver/PyTorch failures, config can say "cuda" while Qwen3 safely
        falls back to CPU; the UI must show that truth to avoid misleading the
        user.
        """
        engine = self.asr_engine
        hot_reloading = bool(getattr(self, "_asr_hot_reload_in_progress", False))
        if engine is None:
            runtime_type = str(getattr(self, "_asr_engine_type", "") or "").lower()
            if runtime_type == "none":
                # Degraded no-ASR state (engine failed to load, no torch to
                # fall back to). A permanent failure must not masquerade as
                # "loading": say so plainly and disable the GPU/CPU switch.
                return {
                    "engine": "none",
                    "engine_label": "识别引擎",
                    "model_name": "",
                    "model_short": "不可用",
                    "configured_device": "unknown",
                    "actual_device": "none",
                    "requested_mode": "gpu",
                    "active_mode": "unavailable",
                    "fallback_active": False,
                    "hot_reloading": hot_reloading,
                    "status_message": "识别引擎不可用",
                    "detail": "引擎加载失败，请检查模型文件或重装",
                    "device_reason": "engine_load_failed",
                    "can_request_gpu": False,
                    "switch_kind": "none",
                    "engine_targets": [],
                }
            loading_engine_type = self._canonical_asr_engine_type(
                getattr(self, "_asr_engine_type", "qwen3")
            )
            loading_switch_kind, loading_targets = self._asr_engine_switch_targets(
                loading_engine_type
            )
            return {
                "engine": loading_engine_type,
                "engine_label": "ASR",
                "model_name": "",
                "model_short": "加载中",
                "configured_device": "unknown",
                "actual_device": "loading",
                "requested_mode": "gpu",
                "active_mode": "loading",
                "fallback_active": False,
                "hot_reloading": hot_reloading,
                "status_message": "语音输入加载中",
                "detail": "请稍等",
                "device_reason": "",
                "can_request_gpu": True,
                "switch_kind": loading_switch_kind,
                "engine_targets": loading_targets,
            }

        cfg = getattr(engine, "config", None)
        engine_type = self._canonical_asr_engine_type(
            getattr(self, "_asr_engine_type", "qwen3")
        )
        configured_device = str(getattr(cfg, "device", "cuda") or "cuda").lower()
        configured_model = str(getattr(cfg, "model_name", "") or "")
        target_cfg = None
        if hot_reloading:
            target_cfg = getattr(self, "_asr_hot_reload_target_cfg", None) or getattr(
                self, "_asr_hot_reload_pending_cfg", None
            )
        if isinstance(target_cfg, dict):
            target_engine_type = self._canonical_asr_engine_type(
                target_cfg.get("engine", engine_type)
            )
            target_section = target_cfg.get(target_engine_type, {}) or {}
            if target_section:
                engine_type = target_engine_type
                # The sherpa block names its runtime key "provider" (ORT
                # terminology, default cpu); torch/funasr blocks use "device";
                # the llamacpp block has neither (llama-server is CUDA-only).
                if target_engine_type == "qwen3_sherpa":
                    configured_device = str(
                        target_section.get("provider", "cpu") or "cpu"
                    ).lower()
                elif target_engine_type == "qwen3_llamacpp":
                    configured_device = "cuda"
                else:
                    configured_device = str(
                        target_section.get("device", configured_device)
                        or configured_device
                    ).lower()
                configured_model = str(
                    target_section.get("model_name", configured_model)
                    or configured_model
                )
        actual_device = str(
            getattr(engine, "actual_device", configured_device) or configured_device
        ).lower()
        requested_mode = "gpu" if configured_device.startswith("cuda") else "cpu"
        active_mode = "gpu" if actual_device.startswith("cuda") else "cpu"
        device_reason = str(getattr(engine, "_device_reason", "") or "")
        loaded_model = str(
            getattr(engine, "loaded_model_name", configured_model) or configured_model
        )
        model_name = (
            configured_model
            if hot_reloading and configured_model
            else (loaded_model or configured_model)
        )
        model_short = self._asr_short_model_name(model_name)
        if engine_type == "qwen3_sherpa":
            engine_label = "Qwen3-ASR·轻量"
        elif engine_type == "qwen3_llamacpp":
            engine_label = "Qwen3-ASR·GPU 加速"
        elif engine_type == "qwen3":
            engine_label = "Qwen3-ASR"
        else:
            engine_label = "FunASR"
        fallback_active = requested_mode == "gpu" and active_mode == "cpu"

        gpu_installing = bool(getattr(self, "_gpu_install_in_progress", False))
        gpu_install_progress = max(
            0, min(100, int(getattr(self, "_gpu_install_progress", 0) or 0))
        )
        gpu_install_phase = str(
            getattr(self, "_gpu_install_phase", "prepare") or "prepare"
        )
        gpu_install_message = str(
            getattr(self, "_gpu_install_message", "") or ""
        )
        if gpu_installing:
            status_message = f"正在安装 GPU 加速 {gpu_install_progress}%"
            detail = gpu_install_message or "CPU 仍可继续使用，完成后会自动切换"
        elif hot_reloading:
            status_message = "正在切换识别方式…"
            detail = "切换完成后会自动生效"
        elif active_mode == "gpu":
            status_message = "显卡加速"
            detail = "识别会更快"
        elif fallback_active:
            status_message = "CPU加速"
            detail = "显卡暂不可用，已自动切换"
        else:
            status_message = "CPU加速"
            detail = "不占用显卡"

        switch_kind, engine_targets = self._asr_engine_switch_targets(engine_type)

        return {
            "engine": engine_type,
            "engine_label": engine_label,
            "model_name": model_name,
            "model_short": model_short,
            "configured_model": configured_model,
            "configured_device": configured_device,
            "actual_device": actual_device,
            "requested_mode": requested_mode,
            "active_mode": "loading" if hot_reloading else active_mode,
            "fallback_active": fallback_active,
            "hot_reloading": hot_reloading,
            "gpu_installing": gpu_installing,
            "gpu_install_progress": gpu_install_progress,
            "gpu_install_phase": gpu_install_phase,
            "gpu_install_message": gpu_install_message,
            "status_message": status_message,
            "detail": detail,
            "device_reason": device_reason,
            # The GPU/CPU quick switch rewrites torch model profiles; it is a
            # no-op for the sherpa and llamacpp runtimes, so hide the affordance.
            "can_request_gpu": engine_type not in ("qwen3_sherpa", "qwen3_llamacpp"),
            # Popup card semantics: "device" = legacy torch/funasr GPU-CPU
            # quick switch; "engine" = sherpa/llamacpp cross-engine switcher
            # (targets carry asset availability); "none" = fully disabled.
            "switch_kind": switch_kind,
            "engine_targets": engine_targets,
        }

    def set_asr_device_mode(self, mode: str) -> dict:
        """Switch the current ASR engine between GPU and CPU runtime modes.

        The change is persisted atomically and then routed through the existing
        ASR hot-reload path, so the user does not need to restart.  For Qwen3,
        CPU mode intentionally uses the lighter 0.6B/float32 profile and GPU
        mode restores the 1.7B/float16 profile.  For FunASR, the quick switch
        only changes FunASR's device and never changes the engine family.
        """
        normalized = str(mode or "").strip().lower()
        if normalized in ("gpu", "cuda"):
            target_device = "cuda"
        elif normalized == "cpu":
            target_device = "cpu"
        else:
            raise ValueError(f"unknown ASR device mode: {mode!r}")

        if (
            self.asr_engine is None
            and str(getattr(self, "_asr_engine_type", "") or "").lower() == "none"
        ):
            # Degraded no-ASR state: nothing to switch, and rewriting config
            # profiles here would only mask the load failure.
            _pipeline_log(
                "ASR",
                f"Device mode switch ({normalized}) ignored: no ASR engine",
            )
            if self._bridge:
                try:
                    self._bridge.emit_error("识别引擎不可用，无法切换")
                except Exception:
                    pass
            return self.get_asr_runtime_status()

        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            engine_type = self._canonical_asr_engine_type(
                cfg.get("asr_engine", getattr(self, "_asr_engine_type", "qwen3"))
            )
            if engine_type in ("qwen3_sherpa", "qwen3_llamacpp"):
                # The quick switch is torch-specific: it rewrites qwen3 model/
                # dtype profiles. Rewriting config here would silently kick the
                # user off the opted-in sherpa/llamacpp runtime, so refuse as a
                # no-op (llamacpp is CUDA-only anyway; its GPU-contention story
                # is the failure chain, not a torch-CPU profile swap).
                _pipeline_log(
                    "ASR",
                    f"Device mode switch ({normalized}) ignored: "
                    f"{engine_type} runtime does not support it",
                )
                if self._bridge:
                    try:
                        hint = (
                            "轻量引擎模式下暂不支持此切换"
                            if engine_type == "qwen3_sherpa"
                            else "GPU 加速引擎模式下暂不支持此切换"
                        )
                        self._bridge.emit_error(hint)
                    except Exception:
                        pass
                return self.get_asr_runtime_status()
            if engine_type == "funasr":
                funasr = cfg.setdefault("funasr", {})
                funasr["device"] = target_device
                target_model = str(funasr.get("model_name", "paraformer-zh"))
            else:
                cfg["asr_engine"] = "qwen3"
                qwen3 = cfg.setdefault("qwen3", {})
                qwen3["device"] = target_device
                if target_device == "cpu":
                    target_model = "Qwen/Qwen3-ASR-0.6B"
                    qwen3["model_name"] = target_model
                    qwen3["torch_dtype"] = "float32"
                    qwen3["max_new_tokens"] = min(
                        int(
                            qwen3.get("max_new_tokens", self._CPU_ASR_MAX_NEW_TOKENS)
                            or self._CPU_ASR_MAX_NEW_TOKENS
                        ),
                        self._CPU_ASR_MAX_NEW_TOKENS,
                    )
                    qwen3["max_inference_batch_size"] = min(
                        int(qwen3.get("max_inference_batch_size", 8) or 8), 8
                    )
                else:
                    target_model = "Qwen/Qwen3-ASR-1.7B"
                    qwen3["model_name"] = target_model
                    # CPU mode writes float32 for compatibility; make GPU mode
                    # explicitly restore the fast half-precision path.
                    qwen3["torch_dtype"] = "float16"
                    qwen3["max_new_tokens"] = max(
                        int(
                            qwen3.get("max_new_tokens", self._GPU_ASR_MAX_NEW_TOKENS)
                            or self._GPU_ASR_MAX_NEW_TOKENS
                        ),
                        self._GPU_ASR_MAX_NEW_TOKENS,
                    )
                    qwen3["max_inference_batch_size"] = max(
                        int(qwen3.get("max_inference_batch_size", 32) or 32), 32
                    )

            tmp = self._config_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self._config_path)
            try:
                self._config_mtime = self._config_path.stat().st_mtime
            except Exception:
                pass

            _pipeline_log(
                "ASR",
                f"User requested ASR device mode: {normalized} "
                f"({target_model} on {target_device})",
            )

            asr_cfg = self._load_asr_config()
            started = self._maybe_hot_reload_asr_engine(asr_cfg)
            if not started:
                self._asr_engine_type = self._canonical_asr_engine_type(
                    asr_cfg.get("engine")
                )
                self._configure_gpu_pressure_fallback(asr_cfg)
                self._apply_hotword_context_to_asr_engine()
            self._apply_asr_runtime_vad_timing(asr_cfg, "device_mode")
            self._emit_asr_status()
            return self.get_asr_runtime_status()
        except Exception as exc:
            logger.error(f"Failed to switch ASR device mode: {exc}", exc_info=True)
            self._emit_error(f"切换语音识别模式失败: {exc}")
            self._emit_asr_status()
            raise

    def set_asr_engine_mode(self, engine: str) -> dict:
        """Cross-engine hot switch between the llamacpp (GPU) and sherpa (CPU)
        runtimes, triggered from the popup 识别方式 card.

        Deliberately a separate method from set_asr_device_mode: that one is
        the torch/funasr GPU-CPU *device* quick switch (it rewrites qwen3
        model/dtype profiles) and keeps its legacy semantics untouched. This
        one only flips ``asr_engine`` between the two non-torch runtimes and
        rides the existing hot-reload chain; on load failure the hot-reload
        thread rolls back to the previous engine and re-emits status, so the
        UI re-highlights the restored engine automatically.
        """
        target = str(engine or "").strip().lower()
        if target not in ("qwen3_sherpa", "qwen3_llamacpp"):
            raise ValueError(f"unknown ASR engine mode: {engine!r}")
        # The user is actively trying to switch: drop the cached sherpa wheel
        # probe so a freshly installed wheel is picked up by this attempt.
        self._invalidate_sherpa_install_cache()
        target_label = (
            "GPU 加速引擎" if target == "qwen3_llamacpp" else "轻量引擎"
        )

        if (
            self.asr_engine is None
            and str(getattr(self, "_asr_engine_type", "") or "").lower() == "none"
        ):
            _pipeline_log(
                "ASR",
                f"Engine mode switch ({target}) ignored: no ASR engine",
            )
            if self._bridge:
                try:
                    self._bridge.emit_error("识别引擎不可用，无法切换")
                except Exception:
                    pass
            return self.get_asr_runtime_status()

        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if target == "qwen3_llamacpp":
                cfg = self._gpu_config_with_installed_cache(cfg)
            current = self._canonical_asr_engine_type(
                cfg.get("asr_engine", getattr(self, "_asr_engine_type", "qwen3"))
            )
            # "Already on target" must hold for BOTH config and runtime, with
            # no hot-reload in flight. After a failed hot switch the rollback
            # only restores the in-memory engine (config keeps the failed
            # target), so a config-only check would swallow the user's retry
            # of the same target as a silent no-op. The runtime engine feeds
            # _current_asr_runtime_signature, so in that residue state the
            # signatures differ and _maybe_hot_reload_asr_engine below really
            # relaunches the reload.
            runtime_engine = self._canonical_asr_engine_type(
                getattr(self, "_asr_engine_type", "")
            )
            hot_reloading = bool(
                getattr(self, "_asr_hot_reload_in_progress", False)
            )
            if current == target and runtime_engine == target and not hot_reloading:
                _pipeline_log(
                    "ASR", f"Engine mode switch ignored: already on {target}"
                )
                return self.get_asr_runtime_status()
            if current not in ("qwen3_sherpa", "qwen3_llamacpp"):
                # The popup switcher only mediates between the two non-torch
                # runtimes; torch/funasr keep the device quick switch and
                # change engines via the settings page. Refuse instead of
                # silently rewriting a torch user's engine choice.
                _pipeline_log(
                    "ASR",
                    f"Engine mode switch ({target}) ignored: "
                    f"current engine {current} uses the device switch",
                )
                if self._bridge:
                    try:
                        self._bridge.emit_error("当前引擎不支持此切换")
                    except Exception:
                        pass
                return self.get_asr_runtime_status()

            problem = self._asr_engine_asset_problem(target, cfg)
            if problem:
                # Pre-flight refusal: nothing was written, so the UI snaps
                # back to the current engine on the returned status.
                _pipeline_log(
                    "ASR",
                    f"Engine mode switch to {target} refused: {problem}",
                )
                if self._bridge:
                    try:
                        self._bridge.emit_error(
                            f"无法切换到{target_label}：{problem}"
                        )
                    except Exception:
                        pass
                return self.get_asr_runtime_status()

            cfg["asr_engine"] = target
            tmp = self._config_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self._config_path)
            try:
                self._config_mtime = self._config_path.stat().st_mtime
            except Exception:
                pass

            _pipeline_log(
                "ASR",
                f"User requested ASR engine mode: {current} -> {target}",
            )

            asr_cfg = self._load_asr_config()
            started = self._maybe_hot_reload_asr_engine(asr_cfg)
            if not started:
                self._asr_engine_type = self._canonical_asr_engine_type(
                    asr_cfg.get("engine")
                )
                self._configure_gpu_pressure_fallback(asr_cfg)
                self._apply_hotword_context_to_asr_engine()
            self._apply_asr_runtime_vad_timing(asr_cfg, "engine_mode")
            self._emit_asr_status()
            return self.get_asr_runtime_status()
        except Exception as exc:
            logger.error(f"Failed to switch ASR engine mode: {exc}", exc_info=True)
            self._emit_error(f"切换识别引擎失败: {exc}")
            self._emit_asr_status()
            raise

    def switch_api_to_primary(self) -> dict:
        """User-triggered switch back to the primary Polish API."""
        if self.polisher and hasattr(self.polisher, "force_primary_api"):
            status = self.polisher.force_primary_api()
        else:
            status = self.get_api_status()
        self._emit_api_status(status)
        return status

    # ── Three-tier OCR mode (cache-friendly screen-context strategy) ──
    #
    # Goal: collapse three independent flags (vad.screen_ocr,
    # vad.screen_ocr_polish, auto_hotword.enabled) into ONE user choice so
    # users don't have to reason about prefix-cache mechanics.
    #
    #   off   — fully disable: no screen reads, no polish injection,
    #           no auto-hotword learning. Lowest cost, lowest privacy risk.
    #   auto  — Read screen for the auto-hotword tracker only;
    #           do NOT inject screen text into the polish prompt. Slow-changing
    #           approved hotword list rides in polisher.session_hotwords →
    #           prefix-cache stays stable → hit rate ~85-95%.
    #   full  — Old behavior: every utterance feeds fresh screen text into the
    #           polish prompt. Best correction quality, kills the prefix cache.
    def set_ocr_mode(self, mode: str) -> None:
        """Switch between off / auto / full OCR tiers.

        Mutates in-memory flags AND persists to hotwords.json so the choice
        survives restart (and so the settings UI's hot-reload path picks it
        up on the next mtime tick).
        """
        if mode not in ("off", "auto", "full"):
            logger.warning(f"Unknown ocr_mode '{mode}', falling back to 'auto'")
            mode = "auto"

        if mode == "off":
            new_screen_ocr = False
            new_screen_polish = False
            new_auto_hotword = False
        elif mode == "auto":
            new_screen_ocr = True
            new_screen_polish = False
            new_auto_hotword = True
        else:  # "full"
            new_screen_ocr = True
            new_screen_polish = True
            new_auto_hotword = True

        # 1. In-memory flags (these are the flags actually consulted by the
        #    speech pipeline; persistence below is just so hot-reload + restart
        #    see the same thing).
        self._screen_ocr_enabled = new_screen_ocr
        self._screen_ocr_polish_enabled = new_screen_polish

        prev_auto_hotword_enabled = _auto_hotword_opted_in(
            self._auto_hotword_cfg
        )
        self._auto_hotword_cfg["enabled"] = new_auto_hotword

        # 2. Tear down or spin up the auto-hotword tracker if its enabled
        #    state changed. We re-read polish_block from disk to keep parity
        #    with how _init_auto_hotword was first called in start().
        if prev_auto_hotword_enabled != new_auto_hotword:
            try:
                if not new_auto_hotword:
                    # Stop the daily-loop thread cleanly + drop tracker refs
                    # so screen OCR (still on for "auto") doesn't feed into a
                    # disabled tracker by accident.
                    try:
                        self._auto_hotword_review_stop.set()
                    except Exception:
                        pass
                    self._auto_hotword_tracker = None
                    self._auto_hotword_reviewer = None
                    if self.polisher and hasattr(self.polisher, "config"):
                        # Clear any previously-pushed approvals so the polish
                        # prompt prefix matches the new no-tracker reality.
                        self.polisher.config.session_hotwords = []
                else:
                    polish_block: dict = {}
                    try:
                        with open(self._config_path, "r", encoding="utf-8") as f:
                            _cfg = json.load(f)
                        polish_block = _cfg.get("polish", {}) or {}
                    except Exception:
                        polish_block = {}
                    self._init_auto_hotword(polish_block)
                    # Re-seed polisher with whatever the tracker recovered from
                    # disk (preserves cross-session learned vocab).
                    if self.polisher and self._auto_hotword_tracker is not None:
                        try:
                            active_words = (
                                self._auto_hotword_tracker.get_active_hotwords()
                            )
                            if hasattr(self.polisher, "config"):
                                self.polisher.config.session_hotwords = list(
                                    active_words or []
                                )
                        except Exception:
                            pass
            except Exception as exc:
                logger.warning(
                    f"set_ocr_mode: auto-hotword tracker reinit failed: {exc}"
                )

        # 3. If screen OCR itself was just turned off, drop the cached engine
        #    so we don't keep a GPU/DML backend warm while it's unused.
        if not new_screen_ocr:
            self._screen_ocr = None

        # 4. Persist all three keys atomically.
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            vad_block = cfg.setdefault("vad", {})
            vad_block["screen_ocr"] = new_screen_ocr
            vad_block["screen_ocr_polish"] = new_screen_polish
            ah_block = cfg.setdefault("auto_hotword", {})
            ah_block["enabled"] = new_auto_hotword
            tmp = self._config_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self._config_path)
            try:
                self._config_mtime = self._config_path.stat().st_mtime
            except Exception:
                pass
        except Exception as exc:
            logger.warning(f"set_ocr_mode: failed to persist hotwords.json: {exc}")

        print(
            f"[OCR-MODE] -> {mode} "
            f"(screen_ocr={new_screen_ocr}, "
            f"screen_ocr_polish={new_screen_polish}, "
            f"auto_hotword={new_auto_hotword})"
        )
        logger.info(f"OCR mode switched to: {mode}")

    def get_ocr_mode(self) -> str:
        """Inverse mapping of set_ocr_mode."""
        ocr = bool(self._screen_ocr_enabled)
        polish = bool(self._screen_ocr_polish_enabled)
        ah = _auto_hotword_opted_in(self._auto_hotword_cfg)
        if not ocr and not polish and not ah:
            return "off"
        if ocr and not polish and ah:
            return "auto"
        if ocr and polish and ah:
            return "full"
        # Mixed legacy state — only report capabilities that are really on.
        # In particular, screen_ocr=true with auto_hotword=false used to be
        # shown as "仅自动学习" even though no tracker existed.
        resolved = (
            "off"
            if not ocr
            else ("full" if polish else ("auto" if ah else "off"))
        )
        logger.info(
            f"OCR flags in mixed state (ocr={ocr}, polish={polish}, "
            f"auto_hotword={ah}); reporting {resolved!r}"
        )
        return resolved

    # ── Capture mode (pre-ASR DSP) ──
    #
    # Three tiers — every tier runs DSP (no bypass option), they differ
    # only in aggression. The lift-only AGC means "standard" is near-
    # transparent for near-field users, so a separate bypass tier is
    # redundant. See core/audio/dsp.py MODE_PRESETS for parameter values.
    #   standard — daily default, near-transparent for near-field
    #   noisy    — strong env noise, aggressive gate to reject steady noise
    #   whisper  — quiet env + soft voice, aggressive AGC to lift weak speech
    def set_capture_mode(self, mode: str) -> None:
        """Set capture-mode DSP tier from UI; persists to hotwords.json.

        Also applies the preset's VAD/energy gate overrides if any. When
        switching between modes, overrides from the previous mode are
        reverted to the user's base values before the new overrides apply,
        so two consecutive whisper→standard switches don't leave the VAD
        threshold stuck at 0.05.
        """
        if mode not in ("standard", "noisy", "whisper"):
            logger.warning(f"Unknown capture_mode {mode!r}; falling back to 'standard'")
            mode = "standard"

        self._capture_mode = mode
        # Keep the sound cues' whisper auto-quiet rule in sync.
        try:
            from .ui.qt.sound import get_sound_manager

            get_sound_manager().set_capture_mode(mode)
        except Exception:
            pass
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                _cfg_for_gain = json.load(f)
            _audio_for_gain = _cfg_for_gain.get("audio", {}) or {}
        except Exception:
            _audio_for_gain = {}
        new_mic_gain = self._mic_input_gain_from_audio_cfg(_audio_for_gain, mode)
        if new_mic_gain != getattr(self, "_mic_input_gain", 1.0):
            self._mic_input_gain = new_mic_gain
            print(f"[CAPTURE-MODE] mic_input_gain → {new_mic_gain:.2f}x")

        # Apply / revert energy_gate + VAD threshold overrides.
        from .core.audio.dsp import MODE_PRESETS as _MP

        preset = _MP[mode]

        energy_override = preset.get("energy_gate_override")
        new_energy = (
            float(energy_override)
            if energy_override is not None
            else float(self._energy_threshold_base)
        )
        if new_energy != self._energy_threshold:
            self._energy_threshold = new_energy
            print(f"[CAPTURE-MODE] energy_gate → {new_energy}")

        vad_override = preset.get("vad_threshold_override")
        new_vad_thr = (
            float(vad_override)
            if vad_override is not None
            else float(self._vad_threshold_base)
        )
        if (
            self.audio_capture is not None
            and self.audio_capture._vad is not None
            and new_vad_thr != self.audio_capture._vad.config.threshold
        ):
            self.audio_capture._vad.config.threshold = new_vad_thr
            print(f"[CAPTURE-MODE] vad_threshold → {new_vad_thr}")

        if self.audio_capture is not None and self.audio_capture._vad is not None:
            vad_cfg = self.audio_capture._vad.config
            max_speech_override = preset.get("max_speech_ms_override")
            if max_speech_override is not None:
                try:
                    max_speech_override_i = int(max_speech_override)
                    if max_speech_override_i > 0:
                        vad_cfg.max_speech_ms = min(
                            vad_cfg.max_speech_ms, max_speech_override_i
                        )
                        print(f"[CAPTURE-MODE] max_speech_ms → {vad_cfg.max_speech_ms}")
                except (TypeError, ValueError):
                    pass
            else:
                # Restore user's base max_speech_ms from config when leaving
                # a mode that capped it (e.g. whisper).
                try:
                    with open(self._config_path, "r", encoding="utf-8") as f:
                        _cfg_for_vad = json.load(f)
                    _vad_block = _cfg_for_vad.get("vad", {}) or {}
                    vad_cfg.max_speech_ms = max(
                        3000, min(60000, _vad_block.get("max_speech_ms", 10000))
                    )
                except Exception:
                    vad_cfg.max_speech_ms = 10000

            vad_cfg.speech_end_micro_rms = float(
                preset.get("endpoint_micro_rms") or 0.0
            )
            try:
                vad_cfg.speech_end_micro_min_speech_ms = int(
                    preset.get("endpoint_micro_min_speech_ms", 1200)
                )
            except (TypeError, ValueError):
                vad_cfg.speech_end_micro_min_speech_ms = 1200
            for _k, _v in self._vad_steady_noise_from_preset(preset).items():
                setattr(vad_cfg, _k, _v)
            self._apply_asr_runtime_vad_timing(self._load_asr_config(), "capture_mode")

        # Persist atomically (tmp + replace).
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            audio_block = cfg.setdefault("audio", {})
            audio_block["capture_mode"] = mode
            gain_value = round(float(self._mic_input_gain), 2)
            audio_block["input_gain"] = gain_value
            by_mode = audio_block.get("input_gain_by_mode")
            if not isinstance(by_mode, dict):
                by_mode = {}
                audio_block["input_gain_by_mode"] = by_mode
            by_mode.setdefault(mode, gain_value)
            tmp = self._config_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self._config_path)
            try:
                self._config_mtime = self._config_path.stat().st_mtime
            except Exception:
                pass
        except Exception as exc:
            logger.warning(f"set_capture_mode: failed to persist: {exc}")

        print(f"[CAPTURE-MODE] -> {mode}")
        logger.info(f"Capture mode switched to: {mode}")

    def get_capture_mode(self) -> str:
        return getattr(self, "_capture_mode", "standard")

    def set_mic_input_gain(self, gain: float) -> float:
        """Set software microphone receive volume from the popup slider."""
        safe_gain = self._normalize_mic_input_gain(gain)
        self._mic_input_gain = safe_gain

        # Persist atomically (tmp + replace), same pattern as set_capture_mode.
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            audio_block = cfg.setdefault("audio", {})
            gain_value = round(float(safe_gain), 2)
            audio_block["input_gain"] = gain_value
            mode = getattr(self, "_capture_mode", "standard")
            by_mode = audio_block.get("input_gain_by_mode")
            if not isinstance(by_mode, dict):
                by_mode = {}
                audio_block["input_gain_by_mode"] = by_mode
            by_mode[mode] = gain_value
            tmp = self._config_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self._config_path)
            try:
                self._config_mtime = self._config_path.stat().st_mtime
            except Exception:
                pass
        except Exception as exc:
            logger.warning(f"set_mic_input_gain: failed to persist: {exc}")

        print(f"[AUDIO] mic_input_gain -> {safe_gain:.2f}x")
        return safe_gain

    def get_mic_input_gain(self) -> float:
        return self._normalize_mic_input_gain(getattr(self, "_mic_input_gain", 1.0))

    # ── Output mode (clipboard paste vs typewriter injection) ──
    #
    # Popup-facing wrapper over the same output.typewriter_mode flag the
    # settings page edits, so both UIs stay consistent by construction.
    # Unlike the ASR engine switch there is no async reload: flip the live
    # injector flag, persist, done — the next transcription uses it.
    def set_output_mode(self, mode: str) -> str:
        """Switch text output between clipboard (Ctrl+V) and typewriter.

        Args:
            mode: "clipboard" or "typewriter"

        Returns:
            The normalized mode that was applied.
        """
        normalized = str(mode or "").strip().lower()
        if normalized not in ("clipboard", "typewriter"):
            raise ValueError(f"unknown output mode: {mode!r}")
        use_typewriter = normalized == "typewriter"

        # Immediate effect: insert_text() consults config.typewriter_mode on
        # every utterance, so the very next transcription uses the new mode.
        injector = getattr(self, "output_injector", None)
        if injector is not None and getattr(injector, "config", None) is not None:
            injector.config.typewriter_mode = use_typewriter

        # Persist atomically (tmp + replace), same pattern as set_capture_mode.
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            output_block = cfg.setdefault("output", {})
            output_block["typewriter_mode"] = use_typewriter
            tmp = self._config_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self._config_path)
            try:
                self._config_mtime = self._config_path.stat().st_mtime
            except Exception:
                pass
        except Exception as exc:
            logger.warning(f"set_output_mode: failed to persist: {exc}")

        print(f"[OUTPUT-MODE] -> {normalized}")
        logger.info(f"Output mode switched to: {normalized}")
        return normalized

    def get_output_mode(self) -> str:
        """Current output mode; prefers the live injector flag over disk."""
        injector = getattr(self, "output_injector", None)
        if injector is not None and getattr(injector, "config", None) is not None:
            return "typewriter" if injector.config.typewriter_mode else "clipboard"
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            output_block = cfg.get("output", {}) or {}
            return (
                "typewriter"
                if bool(output_block.get("typewriter_mode", False))
                else "clipboard"
            )
        except Exception:
            return "clipboard"

    def set_wakeword(self, wakeword: str) -> None:
        """
        Set wakeword from UI.

        Args:
            wakeword: New wakeword (e.g., "小助手", "小朋友", "小溪")
        """
        if self.wakeword_detector:
            self.wakeword_detector.set_wakeword(wakeword)
            logger.info(f"Wakeword set to: {wakeword}")

    def get_wakeword(self) -> str:
        """Get current wakeword."""
        if self.wakeword_detector:
            return self.wakeword_detector.wakeword
        return "小助手"

    def get_available_wakewords(self) -> list:
        """Get list of available wakeword options."""
        if self.wakeword_detector:
            return self.wakeword_detector.get_available_wakewords()
        return ["小助手", "小朋友", "小溪", "助手"]

    def get_command_hints(self) -> list:
        """Get list of command hints for UI display."""
        if self.wakeword_detector:
            return self.wakeword_detector.get_command_hints()
        return []

    def set_hotkey(self, hotkey: str) -> bool:
        """
        Change the recording hotkey dynamically.

        Args:
            hotkey: New hotkey string (e.g., "grave", "capslock", "ctrl+shift+space")

        Returns:
            True if hotkey was changed successfully, False otherwise
        """
        if hotkey == self.hotkey:
            return True  # No change needed

        try:
            # Unregister current hotkey
            self.hotkey_manager.unregister_all()

            # Update hotkey
            old_hotkey = self.hotkey
            self.hotkey = hotkey

            # Register new hotkey
            self._register_recording_hotkey()

            logger.info(f"Hotkey changed: {old_hotkey} -> {hotkey}")
            print(f"[Aria] Hotkey changed to: {hotkey}")
            return True

        except Exception as e:
            logger.error(f"Failed to change hotkey: {e}")
            print(f"[Aria] Failed to change hotkey: {e}")
            # Try to restore old hotkey
            try:
                self._register_recording_hotkey()
            except Exception:
                pass
            return False

    def get_hotkey(self) -> str:
        """Get current hotkey."""
        return self.hotkey

    def run(self) -> None:
        """Run the application (blocking mode for CLI)."""
        print("=" * 60)
        print("  Aria - Local AI Voice Dictation")
        print("=" * 60)
        print()

        try:
            # Initialize components
            self._init_components()

            # Start ASR worker thread
            self._start_asr_worker()
            print("ASR worker thread started")

            # Start hotkey action worker (processes hotkey presses off the hotkey thread)
            self._start_hotkey_action_worker()
            print("Hotkey action worker started")

            # Start config file watcher for hot-reload
            print("[DEBUG] Starting config watcher...")
            self._start_config_watcher()
            print("[DEBUG] Config watcher thread launched")

            # Register hotkey
            print(f"\n[DEBUG] Registering hotkey: {self.hotkey}")
            sys.stdout.flush()
            try:
                self._register_recording_hotkey()
                print("[DEBUG] Hotkey registered successfully")
                sys.stdout.flush()
            except RuntimeError as e:
                print(f"[ERR] Failed to register hotkey: {e}")
                print("   Try using a different hotkey (e.g., 'ctrl+shift+space')")
                return

            # Note: hotkey_manager.start() is already called implicitly by register()
            # via _run_on_hotkey_thread(), so no explicit start() needed here
            print("[DEBUG] Hotkey manager running (started by register)")
            sys.stdout.flush()

            print()
            print("=" * 60)
            print(f"  Press [{self.hotkey.upper()}] to start/stop recording")
            print("  Press [Ctrl+C] to exit")
            print("=" * 60)
            print()
            print("Ready! Waiting for hotkey...")
            sys.stdout.flush()

            # Wait for Ctrl+C
            try:
                while True:
                    time.sleep(0.1)
            except KeyboardInterrupt:
                print("\n\nShutting down...")

        finally:
            # Cleanup
            print("Stopping components...")
            if self.audio_capture and self.audio_capture.is_recording:
                self.audio_capture.stop()
            self._stop_asr_worker()
            self.hotkey_manager.stop()
            print("Goodbye!")


def main():
    """Entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Aria - Local AI Voice Dictation")
    # Read hotkey from config file, fallback to grave (backtick key)
    default_hotkey = "grave"
    try:
        import json
        from pathlib import Path

        config_path = Path(__file__).parent / "config" / "hotwords.json"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            default_hotkey = cfg.get("general", {}).get("hotkey", "grave")
    except Exception:
        pass
    parser.add_argument(
        "--hotkey",
        "-k",
        default=default_hotkey,
        help=f"Hotkey to toggle recording (default from config: {default_hotkey})",
    )
    parser.add_argument(
        "--list-devices",
        "-l",
        action="store_true",
        help="List available audio input devices and exit",
    )
    parser.add_argument(
        "--get-last-log",
        action="store_true",
        help="Print the latest debug log JSON and exit (for automated analysis)",
    )

    args = parser.parse_args()

    if args.get_last_log:
        from .core.debug import DEBUG_DIR
        import glob

        if not DEBUG_DIR.exists():
            print('{"error": "DebugLog directory not found"}')
            return

        log_files = glob.glob(str(DEBUG_DIR / "session_*.json"))
        if not log_files:
            print('{"error": "No debug logs found"}')
            return

        import os

        latest_file = max(log_files, key=os.path.getctime)
        with open(latest_file, "r", encoding="utf-8") as f:
            print(f.read())
        return

    if args.list_devices:
        print("Available audio input devices:")
        print("-" * 40)
        devices = AudioCapture.list_devices()
        for d in devices:
            default = " [DEFAULT]" if d["is_default"] else ""
            print(f"  {d['id']}: {d['name']}{default}")
        return

    app = AriaApp(hotkey=args.hotkey)
    app.run()


if __name__ == "__main__":
    main()
