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
from dataclasses import dataclass
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
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    try:
        _DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{stage}] {msg}\n")
    except Exception:
        pass  # Silent fail


# === Global Exception Hooks (catch crashes in all threads) ===
def _global_excepthook(exc_type, exc_value, exc_tb):
    """Catch uncaught exceptions in main thread and log to crash file."""
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        _CRASH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_CRASH_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n[{ts}] UNCAUGHT EXCEPTION (main thread)\n{msg}\n")
    except Exception:
        pass
    _pipeline_log("CRASH", f"Uncaught exception: {exc_type.__name__}: {exc_value}")


def _thread_excepthook(args):
    """Catch uncaught exceptions in worker threads."""
    msg = "".join(
        traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
    )
    try:
        _CRASH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        thread_name = args.thread.name if args.thread else "unknown"
        with open(_CRASH_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(
                f"\n{'='*60}\n[{ts}] UNCAUGHT EXCEPTION (thread: {thread_name})\n{msg}\n"
            )
    except Exception:
        pass
    _pipeline_log(
        "CRASH",
        f"Thread exception ({args.thread}): {args.exc_type.__name__}: {args.exc_value}",
    )


sys.excepthook = _global_excepthook
threading.excepthook = _thread_excepthook

# Enable faulthandler for segfaults/aborts (writes to crash log)
try:
    _CRASH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _faulthandler_file = open(_CRASH_LOG_PATH, "a", encoding="utf-8")
    faulthandler.enable(file=_faulthandler_file)
except Exception:
    faulthandler.enable()  # Fallback to stderr


from .core.audio.capture import AudioCapture, AudioConfig
from .core.audio.vad import VADConfig
from .core.asr.funasr_engine import FunASREngine, FunASRConfig
from .core.asr.qwen3_engine import Qwen3ASREngine, Qwen3Config
from .core.hotword import (
    HotWordManager,
    HotWordProcessor,
    AIPolisher,
    PinyinFuzzyMatcher,
    FuzzyMatchConfig,
)
from .core.command import CommandDetector, CommandExecutor
from .core.wakeword import WakewordDetector, WakewordExecutor
from .core.debug import DebugSession, DebugConfig
from .core.insight_store import InsightStore
from .core.history import HistoryStore, RecordType
from .core.selection import (
    SelectionDetector,
    SelectionProcessor,
    SelectionCommand,
    CommandType,
)
from .core.action import TranslationAction, ChatAction
from .system.hotkey import HotkeyManager
from .system.output import OutputInjector, OutputConfig
from .ui.streaming_display import DisplayBuffer, DisplayState
from .core.logging import get_system_logger
from .core.utils import get_config_path, get_models_path

logger = get_system_logger()


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

    def __init__(self, hotkey: str = "grave"):
        self.hotkey = hotkey
        self.state = AppState.IDLE
        self._lock = threading.Lock()
        self._running = False

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

        # Voice command system (Layer 0: Command detection before text insertion)
        self.command_detector: CommandDetector = None
        self.command_executor: CommandExecutor = None

        # Wakeword system (Layer -1: App-level commands via "小助手")
        self.wakeword_detector: WakewordDetector = None
        self.wakeword_executor: WakewordExecutor = None

        # ASR worker thread (non-blocking transcription)
        self._asr_queue: queue.Queue = queue.Queue(maxsize=5)
        self._asr_thread: threading.Thread = None
        self._stop_event = threading.Event()
        self._worker_busy = False  # True while worker is processing a segment

        # Hotkey action queue (non-blocking hotkey callback → dedicated action thread)
        self._hotkey_action_queue: queue.Queue = queue.Queue(maxsize=4)
        self._hotkey_action_thread: threading.Thread = None

        # Stats
        self._session_count = 0

        # Sound control
        self._sound_enabled = True

        # Selection mode (smart detection: same hotkey, auto-detect if text selected)
        self._selection_mode = False
        self._selected_text: str = None
        self._original_clipboard: str = None
        self.selection_detector: SelectionDetector = None
        self.selection_processor: SelectionProcessor = None

        # Auto-send control (press Enter after text insertion)
        self._auto_send_enabled = False

        # Pre-ASR energy gate (configurable from settings, updated by hot-reload)
        self._energy_threshold = 0.003

        # Noise text filter (post-ASR, drops filler-only outputs like 嗯/啊/呃)
        self._noise_filter_enabled = True

        # Recent ASR context buffer for continuity across speech segments
        self._recent_asr_buffer: list = []  # [text, text, ...]
        self._recent_context_max = 10  # keep last N entries

        # Screen OCR for ASR context (triggered on speech start)
        self._screen_ocr = None  # Lazy init
        self._screen_ocr_enabled = True
        self._screen_ocr_polish_enabled = False  # OCR → polish layer (off by default)

        # Sleep mode: AWAKE (normal), LIGHT (voice-triggered), DEEP (model unloaded)
        self._sleep_mode = SleepMode.AWAKE
        self._deep_sleep_lock = threading.Lock()  # Prevent concurrent reload
        self._reload_thread: threading.Thread | None = None

        # Disabled mode: hotkey toggles back to enabled (for elevation dialog)
        self._is_disabled = False

        # Config file watcher (hot-reload)
        self._config_path = get_config_path("hotwords.json")
        self._config_mtime = 0.0
        self._watcher_thread: threading.Thread = None

        # Streaming ASR (interim results while speaking)
        self._streaming_config = StreamingConfig()
        self._interim_timer: threading.Timer = None
        self._last_interim_text: str = ""
        self._interim_generation: int = 0  # Generation token to prevent stale updates
        self._asr_lock = threading.Lock()  # Prevent concurrent ASR calls

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
            line = f"[{ts}] [SLEEPING] {msg}\n"
            print(line.strip())
            log_path = Path(__file__).parent / "DebugLog" / "wakeword_debug.log"
            log_path.parent.mkdir(exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)

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

    def _enter_deep_sleep(self) -> None:
        """Enter deep sleep: stop recording, drain queue, unload model."""
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

        # Wait for any active transcription to complete, then unload
        with self._asr_lock:
            # Drain pending audio from queue
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

        # Notify UI
        bridge = self._bridge
        print(f"[DEEP_SLEEP] Notifying UI... bridge={bridge is not None}")
        if bridge:
            bridge.emit_state("DEEP_SLEEPING")
            bridge.emit_setting_changed("deep_sleeping", True)
        print(
            f"[DEEP_SLEEP] Deep sleep active — GPU idle, model={self.asr_engine._model is None}"
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
                _ = self.asr_engine.transcribe(silence)
                noise = _np.random.randn(16000).astype(_np.float32) * 0.01
                _ = self.asr_engine.transcribe(noise)
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

            # Auto-start recording: user woke the engine = they want to use voice
            # F11 is a toggle (ON/OFF), not push-to-talk. Wake = resume listening.
            try:
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
        """
        self._bridge = bridge
        # Also update wakeword executor's bridge reference
        # (it was initialized with None before set_bridge was called)
        if self.wakeword_executor:
            self.wakeword_executor.bridge = bridge
            print(f"[BRIDGE] Updated wakeword executor bridge: {bridge is not None}")

    def _emit_state(self, state: str) -> None:
        """Emit state change to UI bridge if available."""
        _pipeline_log("STATE", f"emit_state('{state}') internal={self.state.name}")
        if self._bridge:
            self._bridge.emit_state(state)

    def _emit_text(self, text: str, is_final: bool) -> None:
        """Emit text update to UI bridge if available."""
        if self._bridge:
            self._bridge.emit_text(text, is_final)

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

    def _emit_voice_activity(self, is_speaking: bool) -> None:
        """Emit voice activity (VAD) to UI bridge."""
        if self._bridge:
            self._bridge.emit_voice_activity(is_speaking)

    def _emit_action(self, action) -> None:
        """Emit UI action to bridge (v1.1 action-driven architecture)."""
        if self._bridge:
            self._bridge.emit_action(action)

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

    def _screen_pinyin_correct(self, text: str, screen_keywords: str) -> tuple:
        """Unified L2.5 Phonetic Matcher — screen-aware homophone correction.

        Design (三方会谈 consensus 2026-03-23):
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
                check_elevation=output_cfg.get("check_elevation", True),
                # Elevation callback will show warning via UI bridge
                elevation_callback=self._on_elevation_warning,
            )

            if config.typewriter_mode:
                logger.info(
                    "[OUTPUT] Typewriter mode enabled (for apps without Ctrl+V support)"
                )

            return config

        except Exception as e:
            logger.warning(f"Failed to load output config: {e}, using defaults")
            return OutputConfig(elevation_callback=self._on_elevation_warning)

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
                "vad": data.get("vad", {}),
                "audio_device": general.get("audio_device"),  # Device name string
            }
        except Exception as e:
            logger.warning(f"Failed to load ASR config: {e}, using defaults")
            return {
                "engine": "qwen3",
                "funasr": {},
                "qwen3": {},
                "vad": {},
                "audio_device": None,
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

        # VAD config with validation (clamp to valid ranges)
        vad_cfg = asr_cfg.get("vad", {})
        vad_threshold = max(0.1, min(0.9, vad_cfg.get("threshold", 0.2)))
        vad_min_speech = max(50, min(1000, vad_cfg.get("min_speech_ms", 150)))
        vad_min_silence = max(100, min(5000, vad_cfg.get("min_silence_ms", 1500)))
        vad_max_speech = max(3000, min(60000, vad_cfg.get("max_speech_ms", 10000)))

        # Pre-ASR energy gate (configurable from settings)
        self._energy_threshold = max(
            0.0005, min(0.02, vad_cfg.get("energy_threshold", 0.003))
        )

        # Post-ASR noise text filter
        self._noise_filter_enabled = vad_cfg.get("noise_filter", True)

        # Screen OCR switches
        self._screen_ocr_enabled = vad_cfg.get("screen_ocr", True)
        self._screen_ocr_polish_enabled = vad_cfg.get("screen_ocr_polish", False)

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
            ),
        )
        self.audio_capture = AudioCapture(audio_config)

        # Set up audio callbacks
        self.audio_capture.set_callbacks(
            on_speech_start=self._on_speech_start,
            on_speech_end=self._on_speech_end,
            on_audio_level=self._on_audio_level,
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
            if preloaded is not None and isinstance(preloaded, FunASREngine):
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
            # Check for pre-loaded engine
            import aria

            preloaded = getattr(aria, "_preloaded_asr_engine", None)
            if (
                preloaded is not None
                and hasattr(preloaded, "name")
                and "Qwen3" in preloaded.name
            ):
                print("Using pre-loaded Qwen3-ASR engine")
                self.asr_engine = preloaded
            else:
                print("Loading Qwen3-ASR model (this may take a few seconds)...")
                # Use "auto" defaults - let Qwen3ASREngine detect optimal settings
                # based on GPU capabilities (VRAM, bfloat16 support, etc.)
                asr_config = Qwen3Config(
                    model_name=qwen3_cfg.get(
                        "model_name", "auto"
                    ),  # auto-select based on VRAM
                    device=qwen3_cfg.get("device", "cuda"),
                    # Don't override torch_dtype - let engine auto-detect bfloat16 support
                    language=qwen3_cfg.get("language", "Chinese"),
                )
                # Only override torch_dtype if explicitly set in config
                if "torch_dtype" in qwen3_cfg:
                    asr_config.torch_dtype = qwen3_cfg["torch_dtype"]
                self.asr_engine = Qwen3ASREngine(asr_config)
                self.asr_engine.load()
            print(f"Qwen3-ASR ready!")
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
            asr_config = Qwen3Config(
                model_name=qwen3_cfg.get("model_name", "auto"),
                device=qwen3_cfg.get("device", "cuda"),
                language=qwen3_cfg.get("language", "Chinese"),
            )
            # Only override torch_dtype if explicitly set (match normal path)
            if "torch_dtype" in qwen3_cfg:
                asr_config.torch_dtype = qwen3_cfg["torch_dtype"]
            self.asr_engine = Qwen3ASREngine(asr_config)
            self.asr_engine.load()
            print(f"Qwen3-ASR model loaded (fallback): {asr_config.model_name}")

        # HotWord system initialization (warmup moved to after initial_prompt is set)
        print("Loading hotword configuration...")
        self.hotword_manager = HotWordManager.from_default()

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
        elif engine_type == "qwen3":
            # Qwen3-ASR: use context biasing (text-based, weight->repetition)
            context_string = self.hotword_manager.to_qwen3_context()
            self.asr_engine.set_context(context_string or "")
            print(f"[HOTWORD] Qwen3 context: {len(context_string)} chars")

        # GPU Warmup — two passes to fully prime both encoder and decoder
        # Pass 1: silence → primes CUDA kernels + audio encoder
        # Pass 2: low noise → forces decoder to generate tokens (primes text generation path)
        # See docs/DEBUG_LESSONS.md: warmup before context causes "Prompt Shock"
        try:
            import numpy as _np
            import time as _warmup_time

            _pipeline_log("WARMUP", "Starting GPU warmup (2 passes)...")
            _warmup_start = _warmup_time.time()
            with self._asr_lock:
                # Pass 1: silence (prime encoder + CUDA kernels)
                silence = _np.zeros(16000, dtype=_np.float32)
                _ = self.asr_engine.transcribe(silence)
                # Pass 2: low-level noise (force decoder to produce tokens)
                noise = _np.random.randn(16000).astype(_np.float32) * 0.01
                _ = self.asr_engine.transcribe(noise)
            _warmup_ms = (_warmup_time.time() - _warmup_start) * 1000
            _pipeline_log("WARMUP", f"GPU warmup complete ({_warmup_ms:.0f}ms)")
            print(f"[WARMUP] GPU warmup complete ({_warmup_ms:.0f}ms)")
        except Exception as e:
            _pipeline_log("WARMUP", f"GPU warmup FAILED: {e}")
            print(f"[WARMUP] GPU warmup failed (non-fatal): {e}")

        self.hotword_processor = HotWordProcessor(
            self.hotword_manager.get_replacements()
        )
        print(
            f"[HOTWORD] {len(self.hotword_manager.config.prompt_words)} words, {len(self.hotword_manager.config.replacements)} replacements"
        )

        # Layer 2.5: Pinyin fuzzy matching
        # Static hotwords: weight >= 1.0 only (lower weights too risky for fuzzy)
        # Screen keywords: handled separately by _screen_pinyin_correct (3+ chars)
        layer_hotwords = self.hotword_manager.get_hotwords_by_layer()
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
        try:
            _hcfg_path = Path(__file__).parent / "config" / "hotwords.json"
            if _hcfg_path.exists():
                import json as _hjson

                with open(_hcfg_path, "r", encoding="utf-8") as _hf:
                    _hcfg = _hjson.load(_hf)
                _history_enabled = _hcfg.get("history_enabled", True)
                _history_retention = _hcfg.get("history_retention_days", 90)
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
            batch_count = reminder.get("batch_count", 0)
            action = ReminderNotifyAction(
                reminder_id=reminder.get("id", ""),
                content=reminder.get("content", ""),
                created_at=reminder.get("created_at", ""),
                batch_count=batch_count,
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
                                # Layer 1+2: background OCR (skips if busy)
                                if not self._screen_ocr._running:
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

                self._screen_ocr = ScreenOCR(max_text_len=1000)
            except Exception:
                self._screen_ocr_enabled = False

    def _on_speech_start(self) -> None:
        """Called when speech is detected."""
        logger.debug("Speech detected")
        print("\n[MIC] Speaking...")
        self._emit_voice_activity(True)

        # Layer 0: Update title keywords instantly (0ms)
        # Layer 1+2: Trigger UIA/OCR in background
        if self._screen_ocr_enabled:
            self._ensure_screen_ocr()
            if self._screen_ocr:
                self._screen_ocr.update_title()  # instant
                self._screen_ocr.trigger()  # background

        # Start streaming ASR (interim results while speaking)
        self._last_interim_text = ""
        self._start_interim_timer()

    def _on_speech_end(self, audio) -> None:
        """Called when speech ends - queue for transcription (non-blocking)."""
        self._stop_interim_timer()  # Stop streaming ASR
        self._emit_voice_activity(False)

        if audio is None or len(audio) < 1600:  # < 0.1s
            return

        # Health check: restart worker if it died (GPU error, uncaught exception, etc.)
        if self._asr_thread and not self._asr_thread.is_alive():
            print("[WARN] ASR worker thread died! Restarting...")
            _pipeline_log("ERROR", "ASR worker thread died, restarting")
            logger.error("ASR worker thread died, restarting")
            self._start_asr_worker()

        pending = self._asr_queue.qsize()
        logger.debug(f"Speech ended, {len(audio)} samples, queuing for ASR")
        print(
            f"[QUEUE] Audio segment queued ({len(audio)/16000:.1f}s)"
            f" [pending={pending}, worker_busy={self._worker_busy}]"
        )

        # Non-blocking: just put in queue, let worker thread handle it
        try:
            self._asr_queue.put_nowait((self._session_count, audio))
        except queue.Full:
            print("[WARN] ASR queue full, dropping segment")

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

            # Limit audio length to avoid O(n²) performance degradation
            # Only process last 10 seconds for interim (160000 samples @ 16kHz)
            MAX_INTERIM_SAMPLES = 160000
            if len(audio) > MAX_INTERIM_SAMPLES:
                audio = audio[-MAX_INTERIM_SAMPLES:]

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

                INTERIM_TIMEOUT_S = 10
                _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                try:
                    _future = _executor.submit(self.asr_engine.transcribe, audio)
                    result = _future.result(timeout=INTERIM_TIMEOUT_S)
                except concurrent.futures.TimeoutError:
                    print(
                        f"[STREAM] TIMEOUT: interim transcription exceeded {INTERIM_TIMEOUT_S}s, skipping"
                    )
                    _pipeline_log(
                        "STREAM",
                        f"Interim timeout after {INTERIM_TIMEOUT_S}s",
                    )
                    result = None
                finally:
                    _executor.shutdown(wait=False, cancel_futures=True)

                text = result.text.strip() if result and result.text else ""

                # Final check before emitting
                if generation != self._interim_generation:
                    return

                # Only emit if text changed (avoid flickering)
                if text and text != self._last_interim_text:
                    self._last_interim_text = text
                    text_to_emit = text  # Defer emit until after releasing lock
            finally:
                self._asr_lock.release()

            # Emit outside lock to avoid blocking final transcription
            if text_to_emit:
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
                session_id, data = self._asr_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            audio = data
            self._worker_busy = True

            _pipeline_log(
                "ASR",
                f">>> Got audio from queue: session={session_id}, samples={len(audio)}, pending={self._asr_queue.qsize()}",
            )
            print(f"[...] Transcribing... (queue: {self._asr_queue.qsize()} pending)")

            # === Safety guards for corrupted/empty audio ===
            if len(audio) == 0:
                _pipeline_log("ASR", "Empty audio segment, skipping")
                self._worker_busy = False
                self._asr_queue.task_done()
                continue

            if not np.isfinite(audio).all():
                _pipeline_log("ASR", "Audio contains NaN/Inf, sanitizing")
                audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)

            # Create debug session
            debug = DebugSession(session_id=session_id, enabled=DebugConfig.enabled)

            # Debug: save audio for inspection (only when debug enabled)
            debug_dir = os.path.join(os.path.dirname(__file__), "DebugLog")
            debug_path = ""

            # Audio level stats (always compute for logging)
            audio_level_avg = float(np.abs(audio).mean())
            audio_level_max = float(np.abs(audio).max())

            try:
                if DebugConfig.enabled and DebugConfig.save_to_file:
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
                    audio_level_avg=audio_level_avg,
                    audio_level_max=audio_level_max,
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

            # === Pre-ASR Acoustic Gate ===
            # Skip ASR entirely if audio energy is near-silence (saves 1-15s of transcription time).
            # VAD can false-trigger on keyboard clicks, mouse clicks, etc. — this catches those.
            # Configurable via settings: vad.energy_threshold (default 0.003)
            energy_gate = self._energy_threshold
            if audio_level_avg < energy_gate:
                _pipeline_log(
                    "ASR",
                    f"Pre-ASR gate: audio too quiet (avg={audio_level_avg:.5f} < {energy_gate}), skipping ASR",
                )
                print(f"[ASR] Skipped: audio too quiet (avg={audio_level_avg:.5f})")
                self._worker_busy = False
                self._asr_queue.task_done()
                continue

            # === Deep Sleep Guard (BEFORE transcribe) ===
            # Model may have been unloaded — skip to avoid crash
            with self._lock:
                if self._sleep_mode == SleepMode.DEEP:
                    print("[ASR] Deep sleep: skipping transcription")
                    self._worker_busy = False
                    self._asr_queue.task_done()
                    continue

            try:
                # Transcribe (Layer 1: initial_prompt already set)
                import time as time_module
                import concurrent.futures

                _pipeline_log("ASR", "Starting transcription...")
                asr_start = time_module.time()
                # Update screen keywords (injected at hotword level for strong bias)
                if self._screen_ocr and hasattr(self.asr_engine, "set_screen_keywords"):
                    # Layer 0: refresh title (0ms), then get combined context
                    self._screen_ocr.update_title()
                    ocr_text = self._screen_ocr.get_text()
                    _pipeline_log(
                        "ASR",
                        f"OCR get_text: {len(ocr_text)} chars"
                        + (
                            f" = '{ocr_text[:80]}...'" if ocr_text else " (empty/stale)"
                        ),
                    )
                    if ocr_text:
                        ocr_keywords = self._extract_ocr_keywords(ocr_text)
                        self.asr_engine.set_screen_keywords(ocr_keywords)
                        if ocr_keywords:
                            _pipeline_log("ASR", f"OCR keywords: '{ocr_keywords[:80]}'")
                    else:
                        self.asr_engine.set_screen_keywords("")

                # Update recent ASR context (for continuity, weaker position)
                if hasattr(self.asr_engine, "set_recent_context"):
                    if self._recent_asr_buffer:
                        self.asr_engine.set_recent_context(
                            " ".join(self._recent_asr_buffer)
                        )
                    else:
                        self.asr_engine.set_recent_context("")

                # Slow-stage indicator: after 3s, tell ball to show GPU-slow glow
                _slow_hint_timer = threading.Timer(
                    3.0,
                    lambda: (
                        self._bridge.emit_slow_stage("gpu") if self._bridge else None
                    ),
                )
                _slow_hint_timer.daemon = True
                _slow_hint_timer.start()

                ASR_TIMEOUT_S = 30
                with self._asr_lock:
                    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    try:
                        future = executor.submit(self.asr_engine.transcribe, audio)
                        try:
                            result = future.result(timeout=ASR_TIMEOUT_S)
                        except concurrent.futures.TimeoutError:
                            print(
                                f"[ASR] TIMEOUT: Transcription exceeded {ASR_TIMEOUT_S}s, skipping"
                            )
                            _pipeline_log(
                                "ERROR",
                                f"ASR timeout after {ASR_TIMEOUT_S}s",
                            )
                            result = None
                    finally:
                        # CRITICAL: shutdown(wait=False) prevents deadlock when
                        # transcribe hangs — with-statement would call shutdown(wait=True)
                        executor.shutdown(wait=False, cancel_futures=True)
                _slow_hint_timer.cancel()  # Cancel ASR slow hint
                asr_time = (time_module.time() - asr_start) * 1000
                text = result.text.strip() if result and result.text else ""
                _pipeline_log("ASR", f"Transcription done: '{text}' ({asr_time:.0f}ms)")

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
                    model_name=(
                        self.asr_engine.config.model_name
                        if self.asr_engine
                        else "unknown"
                    ),
                    device=(
                        self.asr_engine.config.device if self.asr_engine else "unknown"
                    ),
                    language=(
                        self.asr_engine.config.language if self.asr_engine else "zh"
                    ),
                    audio_duration=len(audio) / 16000,
                    initial_prompt=initial_prompt,
                    initial_prompt_enabled=initial_prompt_enabled,
                    raw_text=text,
                    transcribe_time_ms=asr_time,
                )
                print(f"[ASR] raw: '{text}' ({asr_time:.0f}ms)")

                # Fix ASR sentence repetition bug (before hallucination check)
                if text:
                    deduped = self._deduplicate_sentences(text)
                    if deduped != text:
                        print(f"[ASR] Deduplicated: '{text}' -> '{deduped}'")
                        text = deduped

                # Filter ASR hallucinations (applies to all engines)
                # Enhanced: retry once on hallucination, then fallback to interim result
                if text and self._is_hallucination(text):
                    print(f"[ASR] Detected hallucination: '{text}'")

                    # Strategy 1: Retry transcription once (cold-start hallucinations often clear on retry)
                    print("[ASR] Retrying transcription...")
                    retry_start = time_module.time()
                    with self._asr_lock:
                        retry_result = self.asr_engine.transcribe(audio)
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
                    else:
                        # Strategy 2: Fallback to interim result if available
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

                # Post-ASR: context leakage detection
                # Checks: 1) text impossibly long for audio duration (any length)
                #          2) text is verbatim substring of recent context
                if text:
                    audio_duration_s = len(audio) / 16000
                    is_leakage = False

                    # Check 1: text length vs audio duration ratio
                    # Chinese: ~4-8 chars/sec | English: ~15-20 chars/sec
                    # Use 25 chars/sec as safe upper bound for mixed language
                    max_reasonable_chars = int(audio_duration_s * 25)
                    if len(text) > max(max_reasonable_chars, 30):
                        is_leakage = True

                    # Check 2: output is substring of recent context buffer
                    if not is_leakage and len(text) > 30 and self._recent_asr_buffer:
                        recent_combined = " ".join(self._recent_asr_buffer)
                        if text in recent_combined:
                            is_leakage = True

                    if is_leakage:
                        print(
                            f"[LEAKAGE] Context leakage detected: "
                            f"{len(text)} chars / {audio_duration_s:.1f}s, dropping"
                        )
                        _pipeline_log(
                            "NOISE",
                            f"Context leakage: {len(text)} chars from {audio_duration_s:.1f}s audio",
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

                # Add successful ASR result to recent context buffer (deduplicated)
                if text:
                    if (
                        not self._recent_asr_buffer
                        or self._recent_asr_buffer[-1] != text
                    ):
                        self._recent_asr_buffer.append(text)
                        if len(self._recent_asr_buffer) > self._recent_context_max:
                            self._recent_asr_buffer = self._recent_asr_buffer[
                                -self._recent_context_max :
                            ]

                # Emit interim text to UI (before polish)
                if text:
                    self._emit_text(text, is_final=False)

                # === Selection Command Detection ===
                # REMOVED: Automatic selection detection based on ASR keywords
                # Selection processing is now ONLY triggered via wakeword (小助手润色, etc.)
                # This prevents accidental Ctrl+C during normal dictation
                # See: wakeword/executor.py -> _selection_process()

                # === Layer -1: Wakeword Detection (app-level commands via "小助手") ===
                # Check for wakeword to control app settings (auto-send, etc.)
                if text and self.wakeword_detector and self.wakeword_executor:
                    wakeword_result = self.wakeword_detector.detect(text)
                    if wakeword_result:
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
                        print(f"[WAKEWORD] {status}: {cmd_id} (raw ASR: '{text}')")
                        # Notify UI about wakeword command
                        if self._bridge and hasattr(self._bridge, "emit_command"):
                            self._bridge.emit_command(f"小助手:{cmd_id}", success)
                        inserted = success
                        final_text = (
                            f"[唤醒词] {response}" if response else f"[唤醒词] {cmd_id}"
                        )
                        continue  # Skip all processing layers

                # === Light Sleeping Mode Check ===
                # If light sleeping, ignore all input (wakeword already handled above)
                with self._lock:
                    is_light_sleeping = self._sleep_mode == SleepMode.LIGHT
                if is_light_sleeping:
                    print(f"[SLEEPING] Ignoring input: '{text[:50]}...'")
                    continue  # Skip all processing layers

                # === Layer 0: Voice Command Detection (BEFORE any processing) ===
                # Check raw ASR text for commands to achieve lowest latency
                if text and self.command_detector and self.command_executor:
                    cmd_id = self.command_detector.detect(text)
                    if cmd_id:
                        # Execute command immediately, skip all processing layers
                        success = self.command_executor.execute(cmd_id)
                        status = "OK" if success else "FAIL"
                        print(f"[CMD] {status}: {cmd_id} (raw ASR: '{text}')")
                        # Notify UI about command execution
                        if self._bridge and hasattr(self._bridge, "emit_command"):
                            self._bridge.emit_command(cmd_id, success)
                        inserted = success
                        final_text = f"[命令] {cmd_id}"
                        continue  # Skip all processing layers (HotWord, Polish, Insert)

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
                            len(_snap_processor.replacements) if _snap_processor else 0
                        ),
                        layer2_time_ms=layer2_time,
                    )

                    _pipeline_log(
                        "POST", f"Layer 2: HotWord done ({layer2_time:.0f}ms)"
                    )

                    # Layer 2.5: Screen-aware homophone correction
                    # Sliding pinyin scan: find exact homophones (same pinyin+tone)
                    # in ASR output that match screen keywords, and replace them.
                    # Uses toned pinyin to avoid false positives (大学≠大雪).
                    _pipeline_log("POST", "Layer 2.5: Fuzzy matching starting...")
                    screen_kw = (
                        self.asr_engine._screen_keywords
                        if hasattr(self.asr_engine, "_screen_keywords")
                        else ""
                    )
                    if screen_kw and text:
                        text, n_fixes = self._screen_pinyin_correct(text, screen_kw)
                        if n_fixes:
                            _pipeline_log(
                                "POST",
                                f"Layer 2.5: {n_fixes} screen homophone fix(es)",
                            )

                    # Also run static hotword fuzzy matching
                    if _snap_fuzzy:
                        text, fuzzy_corrections = _snap_fuzzy.process_with_info(text)
                        if fuzzy_corrections:
                            for corr in fuzzy_corrections:
                                print(
                                    f"[FUZZY] '{corr['original']}' -> '{corr['corrected']}' (score: {corr['score']})"
                                )

                    _pipeline_log("POST", "Layer 2.5: Fuzzy matching done")

                    # Layer 3: AI Polish (optional)
                    # Skip polish for very short text (≤3 chars) — saves API cost,
                    # short text rarely benefits from polish
                    _pipeline_log("POST", "Layer 3: Polish starting...")
                    polish_debug = None
                    _skip_polish = _snap_polisher and len(text.strip()) <= 3
                    if _skip_polish:
                        _pipeline_log(
                            "POST",
                            f"Polish skipped: text too short ({len(text.strip())} chars)",
                        )
                    if _snap_polisher and not _skip_polish:
                        # Slow-stage indicator: after 3s, tell ball to show API-slow glow
                        _polish_hint_timer = threading.Timer(
                            3.0,
                            lambda: (
                                self._bridge.emit_slow_stage("api")
                                if self._bridge
                                else None
                            ),
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
                                    _pipeline_log(
                                        "POST", f"Screen context: {screen_ctx_str}"
                                    )
                        except Exception as e:
                            _pipeline_log("POST", f"Screen context failed: {e}")

                        # Pass OCR screen text to polish for context-aware correction
                        # Controlled by separate toggle (default off, may cause LLM echo)
                        if self._screen_ocr_polish_enabled and self._screen_ocr:
                            ocr_text = self._screen_ocr.get_text()
                            if ocr_text:
                                ocr_hint = f"屏幕上的文字：{ocr_text}"
                                screen_ctx_str = (
                                    f"{screen_ctx_str}。{ocr_hint}"
                                    if screen_ctx_str
                                    else ocr_hint
                                )

                        before_polish = text
                        try:
                            polish_debug = _snap_polisher.polish_with_debug(
                                text, screen_context=screen_ctx_str
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
                    final_text = text
                    print(f"[TEXT] {text}")
                    _pipeline_log("OUTPUT", f"Final text: '{text}'")

                    # Emit final text to UI
                    self._emit_text(text, is_final=True)

                    # Insert into active application
                    _pipeline_log("OUTPUT", "Calling output_injector.insert_text()...")
                    insert_ok = self.output_injector.insert_text(text)
                    inserted = insert_ok
                    if insert_ok:
                        _pipeline_log("OUTPUT", ">>> Text inserted successfully!")
                        print("[OK] Inserted!")
                    else:
                        _pipeline_log("OUTPUT", ">>> Text insertion FAILED!")
                        print("[FAIL] Insert failed! (clipboard/paste error)")

                    # Auto-send: press Enter after text insertion if enabled
                    if self._auto_send_enabled:
                        import time as time_mod

                        time_mod.sleep(0.05)  # Small delay to ensure paste completes
                        if self.output_injector.send_key("enter"):
                            print("[AUTO-SEND] Enter pressed")
                        else:
                            print("[AUTO-SEND] Failed to send Enter")

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

                # Always notify UI that processing is complete (success or failure)
                self._emit_insert_complete()

                # Finalize and save debug session
                debug.finalize(final_text=final_text, inserted=inserted)

                # Save to insight store for AI retrieval (deprecated)
                if final_text and final_text.strip() and self.insight_store:
                    duration_s = (
                        debug.info.audio.duration_seconds if debug.info.audio else 0.0
                    )
                    self.insight_store.add(
                        text=final_text,
                        timestamp=debug.info.start_time,
                        duration_s=duration_s,
                        session_id=session_id,
                    )

                # Save to unified history store (v1.2)
                if final_text and final_text.strip() and self.history_store:
                    raw_text = (
                        debug.info.asr.raw_text
                        if debug.info.asr and hasattr(debug.info.asr, "raw_text")
                        else ""
                    )
                    duration_s = (
                        debug.info.audio.duration_seconds if debug.info.audio else 0.0
                    )
                    self.history_store.add(
                        record_type=RecordType.ASR,
                        input_text=raw_text or final_text,
                        output_text=final_text if raw_text else "",
                        timestamp=debug.info.start_time,
                        metadata={
                            "session_id": session_id,
                            "duration_s": round(duration_s, 2),
                        },
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
                self._handle_hotkey_action()
            except Exception as e:
                _pipeline_log("HOTKEY", f"Action error: {e}")
                logger.error(f"Hotkey action error: {e}", exc_info=True)
            finally:
                self._hotkey_action_queue.task_done()

        logger.info("Hotkey action worker stopped")

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

        # Beep: high pitch = start recording (short, subtle)
        self._beep(800, 50)

        self._session_count += 1
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

        # Beep: low pitch = stop recording (short, subtle)
        self._beep(400, 50)
        print("[STOP] Recording stopped")

        # Emit TRANSCRIBING state while processing
        self._emit_state("TRANSCRIBING")

        # Stop capture and get any remaining audio
        import time as _stop_time

        stop_start = _stop_time.time()
        _pipeline_log("RECORD", "Stopping audio capture...")
        final_audio = self.audio_capture.stop()
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
        MIN_SAMPLES = 4800  # 0.3 seconds
        if final_audio is not None:
            duration_s = len(final_audio) / 16000
            if len(final_audio) < MIN_SAMPLES:
                print(
                    f"[WARN] Recording too short ({duration_s:.2f}s < 0.3s) - accidental click?"
                )
                _pipeline_log("RECORD", f"Too short ({duration_s:.2f}s), skipping")
                # Warning beep disabled
                self.state = AppState.IDLE
                self._emit_state("IDLE")
                self._emit_insert_complete()  # Must notify UI to shrink ball!
                print("[STATE] -> IDLE (skipped)")
                return
            _pipeline_log("RECORD", f"Queuing audio for ASR ({duration_s:.2f}s)")
            self._on_speech_end(final_audio)
        else:
            # No audio captured - still need to notify UI
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

        # Detect selection (sends Ctrl+C and checks clipboard)
        result = self.selection_detector.detect()
        dbg(
            f"[SELECTION] Detection result: has_selection={result.has_selection}, "
            f"text='{result.selected_text[:50] if result.selected_text else None}...', "
            f"orig_clip='{result.original_clipboard[:30] if result.original_clipboard else None}...'"
        )

        if result.has_selection and result.selected_text:
            # Lock for thread-safe state modification
            with self._lock:
                # Store selection info
                self._selection_mode = True
                self._selected_text = result.selected_text
                self._original_clipboard = result.original_clipboard

                # Enter selection listening mode (silent, seamless experience)
                self.state = AppState.SELECTION_LISTENING
                self._emit_state("SELECTION_LISTENING")

                # Same beep as normal recording (short, subtle)
                self._beep(800, 50)

                print(f"[SELECTION] Text selected ({len(self._selected_text)} chars)")

                # Start recording for command
                self._session_count += 1
                self.state = AppState.RECORDING
                self._emit_state("RECORDING")
                if not self.audio_capture.start():
                    logger.error("Failed to start audio capture for selection mode")
                    self._emit_error("麦克风启动失败，请检查音频设备")
                    self.state = AppState.IDLE
                    self._emit_state("IDLE")
                    return False

            return True

        return False

    def _stop_selection_recording(self) -> None:
        """Stop recording in selection mode and process the command."""
        # Beep: low pitch = stop recording (short, subtle)
        self._beep(400, 50)
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
            with self._asr_lock:
                result = self.asr_engine.transcribe(audio)
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
                result = self.selection_processor.process(self._selected_text, command)

                if result.success and result.output_text:
                    # Replace selected text with processed result
                    # The text should still be selected, so just paste
                    self.output_injector.insert_text(result.output_text)
                    print(
                        f"[SELECTION] OK! Replaced with {len(result.output_text)} chars ({result.processing_time_ms:.0f}ms)"
                    )
                    # Success - no beep (silent operation)
                else:
                    print(f"[SELECTION] Processing failed: {result.error}")
                    self._emit_error(f"Selection processing failed: {result.error}")
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
        # Restore original clipboard if we have it
        if self._original_clipboard is not None and self.selection_detector:
            self.selection_detector.restore_clipboard(self._original_clipboard)

        # Reset state
        self._selection_mode = False
        self._selected_text = None
        self._original_clipboard = None

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
                self.hotkey_manager.register(
                    self.hotkey, self._on_hotkey, "Toggle voice recording"
                )
                hotkey_ok = True
            except RuntimeError as e:
                # 热键注册失败不是致命错误，用户仍可通过点击悬浮窗使用
                error_msg = str(e)
                print(f"[WARN] Hotkey registration failed: {error_msg}")
                self._emit_error(error_msg)
                # 不 raise，继续启动

            # Start hotkey listener (only if registration succeeded)
            if hotkey_ok:
                self.hotkey_manager.start()

            self._running = True

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

    def stop(self) -> None:
        """
        Stop the application and cleanup resources.
        """
        if not self._running:
            return

        print("\nStopping AriaApp...")

        # Stop streaming ASR timer first (prevent stale callbacks)
        self._stop_interim_timer()

        # Stop recording if active
        if self.audio_capture and self.audio_capture.is_recording:
            self.audio_capture.stop()

        # Stop ASR worker
        self._stop_asr_worker()

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

        # Release ASR model (free GPU memory)
        if self.asr_engine and hasattr(self.asr_engine, "unload"):
            try:
                self.asr_engine.unload()
                print("[CLEANUP] ASR engine unloaded")
            except Exception as e:
                logger.warning(f"ASR engine unload failed: {e}")

        # Close AI polisher HTTP client
        if self.polisher and hasattr(self.polisher, "close"):
            try:
                self.polisher.close()
                print("[CLEANUP] Polisher client closed")
            except Exception as e:
                logger.warning(f"Polisher close failed: {e}")

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

    def reload_config(self) -> None:
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
                if now - last < self._RELOAD_DEBOUNCE_S:
                    print("[RELOAD] Debounced (too soon after last reload)")
                    return
                self._last_reload_time = now

                self.hotword_manager.reload()

                # v3.2: Preserve ASR engine type after reload (for polish layer optimization)
                self.hotword_manager.config.asr_engine_type = self._asr_engine_type

                # Update Layer 1: ASR engine hotwords
                if self.asr_engine:
                    if self._asr_engine_type == "funasr" and hasattr(
                        self.asr_engine, "set_hotwords_with_score"
                    ):
                        # FunASR: update hotwords with score mapping
                        hotwords_with_score = (
                            self.hotword_manager.get_asr_hotwords_with_score()
                        )
                        self.asr_engine.set_hotwords_with_score(hotwords_with_score)
                        print(
                            f"[HOT-RELOAD] Updated FunASR hotwords: {len(hotwords_with_score)} words"
                        )
                    elif self._asr_engine_type == "qwen3" and hasattr(
                        self.asr_engine, "set_context"
                    ):
                        # Qwen3: update context string (structured V2 format)
                        context_string = self.hotword_manager.to_qwen3_context()
                        self.asr_engine.set_context(context_string or "")
                        print(
                            f"[HOT-RELOAD] Updated Qwen3 context: {len(context_string)} chars"
                        )

                # Update Layer 2: Regex replacements
                if self.hotword_processor:
                    new_replacements = self.hotword_manager.get_replacements()
                    self.hotword_processor.replacements = new_replacements
                    self.hotword_processor._build_patterns()
                    print(
                        f"[HOT-RELOAD] Updated {len(new_replacements)} replacement rules"
                    )

                # Update Layer 2.5: Fuzzy matcher (weight >= 1.0 only)
                if self.fuzzy_matcher:
                    layer_hotwords = self.hotword_manager.get_hotwords_by_layer()
                    fuzzy_hotwords = layer_hotwords.get("layer2_5_pinyin", [])
                    self.fuzzy_matcher.update_hotwords(fuzzy_hotwords)
                    print(
                        f"[HOT-RELOAD] Updated fuzzy matcher: {len(fuzzy_hotwords)} hotwords (weight>=1.0)"
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
                    new_energy = max(
                        0.0005,
                        min(0.02, vad_cfg.get("energy_threshold", 0.003)),
                    )

                    # Update VAD config in place
                    if self.audio_capture and self.audio_capture._vad:
                        self.audio_capture._vad.config.threshold = new_threshold
                        self.audio_capture._vad.config.min_silence_ms = new_min_silence

                    # Update energy gate (used by ASR worker)
                    self._energy_threshold = new_energy

                    # Update noise filter and screen OCR
                    self._noise_filter_enabled = vad_cfg.get("noise_filter", True)
                    self._screen_ocr_enabled = vad_cfg.get("screen_ocr", True)
                    self._screen_ocr_polish_enabled = vad_cfg.get(
                        "screen_ocr_polish", False
                    )

                    print(
                        f"[HOT-RELOAD] Updated VAD: threshold={new_threshold}, "
                        f"min_silence={new_min_silence}ms, energy_gate={new_energy}, "
                        f"noise_filter={self._noise_filter_enabled}, "
                        f"screen_ocr={self._screen_ocr_enabled}"
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
        except Exception as e:
            # Catch all exceptions to prevent config watcher from crashing the app
            logger.error(f"Config reload failed: {e}", exc_info=True)
            print(f"[HOT-RELOAD] Error: {e}")

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
            self.hotkey_manager.register(
                self.hotkey, self._on_hotkey, "Toggle voice recording"
            )

            logger.info(f"Hotkey changed: {old_hotkey} -> {hotkey}")
            print(f"[Aria] Hotkey changed to: {hotkey}")
            return True

        except Exception as e:
            logger.error(f"Failed to change hotkey: {e}")
            print(f"[Aria] Failed to change hotkey: {e}")
            # Try to restore old hotkey
            try:
                self.hotkey_manager.register(
                    self.hotkey, self._on_hotkey, "Toggle voice recording"
                )
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
                self.hotkey_manager.register(
                    self.hotkey, self._on_hotkey, "Toggle voice recording"
                )
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
