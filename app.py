"""
Aria Application
=====================
Main application that orchestrates all components.

Usage:
    python -m aria.app

Press CapsLock to toggle recording.
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
if sys.platform == "win32" and sys.stdout is not None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.platform == "win32" and sys.stderr is not None:
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# === Safe print for pythonw.exe (sys.stdout/stderr can be None) ===
import builtins

_original_print = builtins.print


def _safe_print(*args, **kwargs):
    """Safe print that handles pythonw.exe environment where stdout is None."""
    if sys.stdout is None:
        return  # Silent fail when no console
    try:
        _original_print(*args, **kwargs)
    except OSError:
        pass  # Ignore [Errno 22] Invalid argument


builtins.print = _safe_print

# === Centralized Debug Logging (works with pythonw.exe) ===
import datetime

_DEBUG_LOG_PATH = Path(__file__).parent / "DebugLog" / "pipeline_debug.log"


def _pipeline_log(stage: str, msg: str):
    """Log to pipeline debug file - works even without console."""
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    try:
        _DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{stage}] {msg}\n")
    except Exception:
        pass  # Silent fail


from .core.audio.capture import AudioCapture, AudioConfig
from .core.audio.vad import VADConfig
from .core.asr.whisper_engine import WhisperEngine, WhisperConfig
from .core.asr.funasr_engine import FunASREngine, FunASRConfig
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
from .core.selection import (
    SelectionDetector,
    SelectionProcessor,
    SelectionCommand,
    CommandType,
)
from .core.action import TranslationAction, ChatAction
from .system.hotkey import HotkeyManager
from .system.output import OutputInjector
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


@dataclass
class StreamingConfig:
    """流式识别配置"""

    enabled: bool = True  # 是否启用流式显示
    chunk_interval_ms: int = 1500  # 每1.5秒触发中间识别（降低延迟）
    min_chunk_samples: int = 16000  # 最少1秒音频才处理 (16000 samples = 1s @ 16kHz)
    min_speech_ms: int = 1000  # 最少说话1秒才开始流式识别（更快响应）


class AriaApp:
    """
    Main Aria application.

    Orchestrates:
    - Hotkey listening (CapsLock toggle)
    - Audio capture with VAD
    - ASR transcription
    - HotWord correction:
      - Layer 1: ASR initial_prompt (zero latency)
      - Layer 2: Regex replacement (zero latency)
      - Layer 3: AI polish via LLM (optional, ~100ms)
    - Text insertion

    Usage (Qt mode):
        app = AriaApp(hotkey="capslock")
        app.set_bridge(bridge)  # QtBridge for UI updates
        app.start()  # Non-blocking
        ...
        app.stop()  # Cleanup

    Usage (CLI mode):
        app = AriaApp(hotkey="capslock")
        app.run()  # Blocking
    """

    def __init__(self, hotkey: str = "capslock"):
        self.hotkey = hotkey
        self.state = AppState.IDLE
        self._lock = threading.Lock()
        self._running = False

        # UI Bridge (optional, for Qt frontend)
        self._bridge = None

        # Components
        self.hotkey_manager = HotkeyManager()
        self.audio_capture: AudioCapture = None
        self.asr_engine: WhisperEngine = None
        self.output_injector = OutputInjector()
        self._clipboard_lock = threading.Lock()  # Thread-safe clipboard access
        self.output_injector.set_clipboard_lock(self._clipboard_lock)
        self._asr_engine_type: str = "whisper"  # "whisper" or "funasr"
        self.display = DisplayBuffer()

        # HotWord system (Layer 1: ASR prompt + Layer 2: Regex + Layer 2.5: Fuzzy + Layer 3: AI Polish)
        self.hotword_manager: HotWordManager = None
        self.hotword_processor: HotWordProcessor = None
        self.fuzzy_matcher: PinyinFuzzyMatcher = None
        self.polisher: AIPolisher = None

        # Voice command system (Layer 0: Command detection before text insertion)
        self.command_detector: CommandDetector = None
        self.command_executor: CommandExecutor = None

        # Wakeword system (Layer -1: App-level commands via "瑶瑶")
        self.wakeword_detector: WakewordDetector = None
        self.wakeword_executor: WakewordExecutor = None

        # ASR worker thread (non-blocking transcription)
        self._asr_queue: queue.Queue = queue.Queue()
        self._asr_thread: threading.Thread = None
        self._stop_event = threading.Event()

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

        # Sleeping mode: ignore all input except wakeword commands
        self._is_sleeping = False

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

    def _beep(self, frequency: int, duration: int) -> None:
        """Play beep if sound is enabled."""
        if self._sound_enabled:
            winsound.Beep(frequency, duration)

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
        Set sleeping mode.

        When sleeping:
        - VAD and ASR continue running (wakeword must still work)
        - All non-wakeword input is ignored
        - UI shows sleeping indicator

        Args:
            sleeping: True to enter sleeping mode, False to wake up
            force_emit: If True, emit UI signals even if state didn't change
                       (useful for wakeword to re-sync UI if it got out of sync)
        """
        with self._lock:
            changed = self._is_sleeping != sleeping
            self._is_sleeping = sleeping
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
        Detect Whisper hallucinations (random outputs when no real speech).

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

        # Pattern 4: Repeated patterns (same char 4+ times)
        if re.search(r"(.)\1{3,}", text):
            return True

        # Pattern 5: Repeated sentences (same phrase 3+ times = hallucination)
        # Note: 2x repetition is handled by _deduplicate_sentences (Whisper bug, not hallucination)
        sentences = re.split(r"[。！？，,\.!?]", text)
        sentences = [s.strip() for s in sentences if len(s.strip()) > 3]
        if len(sentences) >= 3:
            from collections import Counter

            counts = Counter(sentences)
            for phrase, count in counts.items():
                if count >= 3 and len(phrase) > 5:
                    return True

        # Pattern 6: Common hallucination phrases
        hallucination_phrases = [
            "请不吝点赞",
            "订阅",
            "谢谢观看",
            "感谢收看",
            "字幕",
            "subtitle",
            "www.",
            "http",
            "再次回断",
        ]
        text_lower = text.lower()
        for phrase in hallucination_phrases:
            if phrase in text_lower:
                return True

        return False

    def _deduplicate_sentences(self, text: str) -> str:
        """
        Fix Whisper's sentence repetition bug.

        Whisper sometimes outputs the same sentence twice:
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

    def _load_asr_config(self) -> dict:
        """Load ASR configuration from hotwords.json."""
        import json

        config_path = get_config_path("hotwords.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            general = data.get("general", {})
            return {
                "engine": data.get("asr_engine", "funasr"),
                "whisper": data.get("whisper", {}),
                "funasr": data.get("funasr", {}),
                "vad": data.get("vad", {}),
                "audio_device": general.get("audio_device"),  # Device name string
            }
        except Exception as e:
            logger.warning(f"Failed to load ASR config: {e}, using defaults")
            return {
                "engine": "funasr",
                "whisper": {},
                "funasr": {},
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
        vad_threshold = max(0.1, min(0.9, vad_cfg.get("threshold", 0.35)))
        vad_min_speech = max(50, min(1000, vad_cfg.get("min_speech_ms", 150)))
        vad_min_silence = max(100, min(5000, vad_cfg.get("min_silence_ms", 500)))
        vad_max_speech = max(3000, min(30000, vad_cfg.get("max_speech_ms", 8000)))

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

        if engine_type == "funasr":
            # FunASR (Paraformer/SenseVoice)
            self._asr_engine_type = "funasr"
            # Check for pre-loaded engine (loaded before Qt to avoid conflict)
            import aria

            preloaded = getattr(aria, "_preloaded_asr_engine", None)
            if preloaded is not None:
                print("Using pre-loaded FunASR engine")
                self.asr_engine = preloaded
            else:
                print("Loading FunASR model (this may take a few seconds)...")
                funasr_cfg = asr_cfg["funasr"]
                asr_config = FunASRConfig(
                    model_name=funasr_cfg.get("model_name", "paraformer-zh"),
                    device=funasr_cfg.get("device", "cuda"),
                    enable_vad=funasr_cfg.get("enable_vad", True),
                    enable_punc=funasr_cfg.get("enable_punc", True),
                )
                self.asr_engine = FunASREngine(asr_config)
                self.asr_engine.load()
            print(f"FunASR ready!")
        elif engine_type == "whisper":
            # Whisper (faster-whisper, supports English hotwords via initial_prompt)
            self._asr_engine_type = "whisper"
            # Check for pre-loaded engine (loaded before Qt to avoid conflict)
            import aria

            preloaded = getattr(aria, "_preloaded_asr_engine", None)
            if preloaded is not None and isinstance(preloaded, WhisperEngine):
                print("Using pre-loaded Whisper engine")
                self.asr_engine = preloaded
            else:
                whisper_cfg = asr_cfg.get("whisper", {})
                print("Loading Whisper model (this may take a few seconds)...")
                asr_config = WhisperConfig(
                    model_name=whisper_cfg.get("model_name", "large-v3-turbo"),
                    device=whisper_cfg.get("device", "cuda"),
                    language=whisper_cfg.get("language", "zh"),
                    compute_type=whisper_cfg.get("compute_type", "float16"),
                )
                self.asr_engine = WhisperEngine(asr_config)
                self.asr_engine.load()
            print(f"Whisper ready!")
        elif engine_type == "fireredasr":
            # FireRedASR (SOTA Chinese/English)
            self._asr_engine_type = "fireredasr"
            firered_cfg = asr_cfg.get("fireredasr", {})
            model_type = firered_cfg.get("model_type", "aed")
            # Check for pre-loaded engine
            import aria

            preloaded = getattr(aria, "_preloaded_asr_engine", None)
            if preloaded is not None and preloaded.name.startswith("FireRedASR"):
                print("Using pre-loaded FireRedASR engine")
                self.asr_engine = preloaded
            else:
                print("Loading FireRedASR model (this may take a few seconds)...")
                from .core.asr.fireredasr_engine import (
                    FireRedASREngine,
                    FireRedASRConfig,
                )

                # Default model path: models/FireRedASR-AED-L relative to app
                default_model_path = str(get_models_path("FireRedASR-AED-L"))
                asr_config = FireRedASRConfig(
                    model_type=model_type,
                    model_path=firered_cfg.get("model_path", default_model_path),
                    use_gpu=firered_cfg.get("use_gpu", True),
                    beam_size=firered_cfg.get("beam_size", 2),
                )
                self.asr_engine = FireRedASREngine(asr_config)
                self.asr_engine.load()
            print(f"FireRedASR ready! ({model_type.upper()})")
        else:
            # Unknown engine type - fall back to FunASR
            logger.warning(
                f"Unknown ASR engine '{engine_type}', falling back to FunASR"
            )
            self._asr_engine_type = "funasr"
            funasr_cfg = asr_cfg.get("funasr", {})
            asr_config = FunASRConfig(
                model_name=funasr_cfg.get("model_name", "paraformer-zh"),
                device=funasr_cfg.get("device", "cuda"),
            )
            self.asr_engine = FunASREngine(asr_config)
            self.asr_engine.load()
            print(f"FunASR model loaded (fallback): {asr_config.model_name}")

        # HotWord system initialization (warmup moved to after initial_prompt is set)
        print("Loading hotword configuration...")
        self.hotword_manager = HotWordManager.from_default()

        # Set hotwords based on engine type
        if engine_type == "funasr" and hasattr(self.asr_engine, "set_hotwords"):
            # FunASR: use weighted hotwords (high weight = repeated for emphasis)
            weighted_hotwords = self.hotword_manager.get_weighted_hotwords()
            self.asr_engine.set_hotwords(weighted_hotwords)
            print(
                f"[HOTWORD] FunASR hotwords: {len(weighted_hotwords)} words (with weight repetition)"
            )
        elif engine_type == "fireredasr":
            # FireRedASR: NO native hotword support, rely on Layer 2/3 post-processing
            print(
                f"[HOTWORD] FireRedASR: Using post-processing only (no native hotword support)"
            )
        else:
            # Whisper: use initial_prompt
            initial_prompt = self.hotword_manager.build_initial_prompt()
            if initial_prompt:
                self.asr_engine.set_initial_prompt(initial_prompt)
                print(f"[HOTWORD] Initial prompt: {initial_prompt[:60]}...")

        # GPU Warmup - MUST run AFTER initial_prompt is set (Gemini "Prompt Shock" fix)
        # Previous warmup ran before initial_prompt, causing first sentence to fail
        if self._asr_engine_type == "whisper":
            try:
                import numpy as np

                # Use 1s of silence (zeros), not random noise
                # - Silence matches what Whisper was trained on
                # - Random noise can skew LayerNorm statistics
                # - 16000 samples = 1 second @ 16kHz
                warmup_audio = np.zeros(16000, dtype=np.float32)
                print("[WARMUP] Pre-heating GPU with initial_prompt...")
                _ = self.asr_engine.transcribe(warmup_audio)
                print("[WARMUP] GPU ready (prompt-aware)!")
            except Exception as e:
                print(
                    f"[WARMUP] Warning: warmup failed ({e}), first transcription may be slow"
                )

        self.hotword_processor = HotWordProcessor(
            self.hotword_manager.get_replacements()
        )
        print(
            f"[HOTWORD] {len(self.hotword_manager.config.prompt_words)} words, {len(self.hotword_manager.config.replacements)} replacements"
        )

        # Layer 2.5: Pinyin fuzzy matching
        fuzzy_hotwords = self.hotword_manager.config.prompt_words + list(
            self.hotword_manager.config.replacements.values()
        )
        fuzzy_hotwords = list(set(fuzzy_hotwords))  # Deduplicate
        self.fuzzy_matcher = PinyinFuzzyMatcher(
            fuzzy_hotwords,
            FuzzyMatchConfig(enabled=True, threshold=0.7, min_word_length=2),
        )
        print(f"[FUZZY] Pinyin matcher enabled with {len(fuzzy_hotwords)} hotwords")

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

        # Layer -1: Wakeword system (app-level commands via "瑶瑶")
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

        # Insight store for voice memo recording
        self.insight_store = InsightStore(
            data_dir=Path(__file__).parent / "data" / "insights"
        )
        print("[INSIGHT] Voice insight store initialized")

        # Selection mode components
        self.selection_detector = SelectionDetector(self.output_injector)
        self.selection_processor = SelectionProcessor(self.polisher)
        print("[SELECTION] Selection mode initialized")

    def _on_speech_start(self) -> None:
        """Called when speech is detected."""
        logger.debug("Speech detected")
        print("\n[MIC] Speaking...")
        self._emit_voice_activity(True)

        # Start streaming ASR (interim results while speaking)
        self._last_interim_text = ""
        self._start_interim_timer()

    def _on_speech_end(self, audio) -> None:
        """Called when speech ends - queue for transcription (non-blocking)."""
        self._stop_interim_timer()  # Stop streaming ASR
        self._emit_voice_activity(False)

        if audio is None or len(audio) < 1600:  # < 0.1s
            return

        logger.debug(f"Speech ended, {len(audio)} samples, queuing for ASR")
        print(f"[QUEUE] Audio segment queued ({len(audio)/16000:.1f}s)")

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

                # Quick transcription (no hotword processing for interim)
                result = self.asr_engine.transcribe(audio)
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
                # Wait for audio with timeout to allow checking stop event
                session_id, audio = self._asr_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            _pipeline_log(
                "ASR",
                f">>> Got audio from queue: session={session_id}, samples={len(audio)}",
            )
            print("[...] Transcribing...")

            # Create debug session
            debug = DebugSession(session_id=session_id, enabled=DebugConfig.enabled)

            # Debug: save audio for inspection
            debug_dir = os.path.join(os.path.dirname(__file__), "DebugLog")
            os.makedirs(debug_dir, exist_ok=True)
            debug_path = os.path.join(debug_dir, f"audio_{session_id}.wav")

            try:
                audio_int16 = (audio * 32767).astype("int16")
                with wave.open(debug_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(16000)
                    wf.writeframes(audio_int16.tobytes())

                # Log audio debug info
                audio_level_avg = float(np.abs(audio).mean())
                audio_level_max = float(np.abs(audio).max())
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
                    f"[DEBUG] Audio: {len(audio)/16000:.1f}s, level_avg={audio_level_avg:.4f}, level_max={audio_level_max:.4f}"
                )

            except Exception as e:
                logger.warning(f"Failed to save debug audio: {e}")
                debug.log_error(f"Audio save failed: {e}")

            inserted = False
            final_text = ""

            try:
                # Transcribe (Layer 1: initial_prompt already set)
                import time as time_module

                _pipeline_log("ASR", "Starting transcription...")
                asr_start = time_module.time()
                # Use lock to prevent concurrent ASR (interim vs final)
                with self._asr_lock:
                    result = self.asr_engine.transcribe(audio)
                asr_time = (time_module.time() - asr_start) * 1000
                text = result.text.strip()
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

                # Fix Whisper sentence repetition bug (before hallucination check)
                if self._asr_engine_type == "whisper" and text:
                    deduped = self._deduplicate_sentences(text)
                    if deduped != text:
                        print(f"[ASR] Deduplicated: '{text}' -> '{deduped}'")
                        text = deduped

                # Filter Whisper hallucinations (skip for FunASR - it doesn't have this problem)
                # Enhanced: retry once on hallucination, then fallback to interim result
                if (
                    self._asr_engine_type == "whisper"
                    and text
                    and self._is_hallucination(text)
                ):
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

                # Emit interim text to UI (before polish)
                if text:
                    self._emit_text(text, is_final=False)

                # === Selection Command Detection ===
                # REMOVED: Automatic selection detection based on ASR keywords
                # Selection processing is now ONLY triggered via wakeword (瑶瑶润色, etc.)
                # This prevents accidental Ctrl+C during normal dictation
                # See: wakeword/executor.py -> _selection_process()

                # === Layer -1: Wakeword Detection (app-level commands via "瑶瑶") ===
                # Check for wakeword to control app settings (auto-send, etc.)
                if text and self.wakeword_detector and self.wakeword_executor:
                    wakeword_result = self.wakeword_detector.detect(text)
                    if wakeword_result:
                        cmd_id, action, value, response, following_text = (
                            wakeword_result
                        )
                        success = self.wakeword_executor.execute(
                            cmd_id, action, value, response, following_text
                        )
                        status = "OK" if success else "FAIL"
                        print(f"[WAKEWORD] {status}: {cmd_id} (raw ASR: '{text}')")
                        # Notify UI about wakeword command
                        if self._bridge and hasattr(self._bridge, "emit_command"):
                            self._bridge.emit_command(f"瑶瑶:{cmd_id}", success)
                        inserted = success
                        final_text = (
                            f"[唤醒词] {response}" if response else f"[唤醒词] {cmd_id}"
                        )
                        continue  # Skip all processing layers

                # === Sleeping Mode Check ===
                # If sleeping, ignore all input (wakeword already handled above)
                with self._lock:
                    is_sleeping = self._is_sleeping
                if is_sleeping:
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
                    # Layer 2: Apply regex corrections
                    original_text = text
                    layer2_replacements = []
                    import time as time_module

                    layer2_start = time_module.time()

                    if self.hotword_processor:
                        text, changes = self.hotword_processor.process_with_info(
                            original_text
                        )
                        layer2_replacements = [{"change": c} for c in changes]
                        if text != original_text:
                            print(f"[HOTWORD] '{original_text}' -> '{text}'")

                    layer2_time = (time_module.time() - layer2_start) * 1000

                    # Log HotWord debug info
                    debug.log_hotword(
                        layer1_enabled=initial_prompt_enabled,
                        layer1_prompt_words=(
                            self.hotword_manager.config.prompt_words
                            if self.hotword_manager
                            else []
                        ),
                        layer1_domain_context=(
                            self.hotword_manager.config.domain_context
                            if self.hotword_manager
                            else ""
                        ),
                        layer2_input=original_text,
                        layer2_output=text,
                        layer2_replacements_applied=layer2_replacements,
                        layer2_rules_count=(
                            len(self.hotword_processor.replacements)
                            if self.hotword_processor
                            else 0
                        ),
                        layer2_time_ms=layer2_time,
                    )

                    # Layer 2.5: Pinyin fuzzy matching
                    if self.fuzzy_matcher:
                        before_fuzzy = text
                        text, fuzzy_corrections = self.fuzzy_matcher.process_with_info(
                            text
                        )
                        if fuzzy_corrections:
                            for corr in fuzzy_corrections:
                                print(
                                    f"[FUZZY] '{corr['original']}' -> '{corr['corrected']}' (score: {corr['score']})"
                                )

                    # Layer 3: AI Polish (optional)
                    if self.polisher:
                        before_polish = text
                        polish_debug = self.polisher.polish_with_debug(text)
                        text = polish_debug["output_text"]

                        # Log Polish debug info
                        debug.log_polish(
                            enabled=polish_debug["enabled"],
                            api_url=polish_debug["api_url"],
                            model=polish_debug["model"],
                            timeout=polish_debug["timeout"],
                            input_text=polish_debug["input_text"],
                            prompt_template=polish_debug["prompt_template"],
                            full_prompt=polish_debug["full_prompt"],
                            output_text=polish_debug["output_text"],
                            changed=polish_debug["changed"],
                            api_time_ms=polish_debug["api_time_ms"],
                            error=polish_debug["error"],
                            http_status=polish_debug["http_status"],
                        )

                        if polish_debug["changed"]:
                            print(
                                f"[POLISH] '{before_polish}' -> '{text}' ({polish_debug['api_time_ms']:.0f}ms)"
                            )
                        elif polish_debug["error"]:
                            print(f"[POLISH] ERROR: {polish_debug['error']}")
                    else:
                        # Log that polish is disabled
                        debug.log_polish(enabled=False)

                    final_text = text
                    print(f"[TEXT] {text}")
                    _pipeline_log("OUTPUT", f"Final text: '{text}'")

                    # Emit final text to UI
                    self._emit_text(text, is_final=True)

                    # Insert into active application
                    _pipeline_log("OUTPUT", "Calling output_injector.insert_text()...")
                    self.output_injector.insert_text(text)
                    inserted = True
                    _pipeline_log("OUTPUT", ">>> Text inserted successfully!")
                    print("[OK] Inserted!")

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
                logger.error(f"Transcription error: {e}")
                _pipeline_log("ERROR", f"Transcription exception: {e}")
                print(f"[ERR] Error: {e}")
                debug.log_error(f"Transcription error: {e}")

            finally:
                # Always notify UI that processing is complete (success or failure)
                self._emit_insert_complete()

                # Finalize and save debug session
                debug.finalize(final_text=final_text, inserted=inserted)

                # Save to insight store for AI retrieval
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
        self._emit_level(level)

    def _on_hotkey(self) -> None:
        """Called when hotkey is pressed - toggle recording."""
        _pipeline_log("HOTKEY", ">>> Hotkey callback triggered!")

        # Allow hotkey in sleeping mode - wakeword detection happens BEFORE
        # sleeping check in _asr_worker(), so "瑶瑶醒来" will still work
        with self._lock:
            is_sleeping = self._is_sleeping
        if is_sleeping:
            print("[HOTKEY] Pressed in sleeping mode - wakeword detection enabled")
            _pipeline_log("HOTKEY", "In sleeping mode, wakeword detection enabled")

        print(f"[HOTKEY] Pressed! Current state: {self.state.name}")
        _pipeline_log("HOTKEY", f"Current state: {self.state.name}")

        with self._lock:
            if self.state == AppState.IDLE:
                # Normal recording start
                _pipeline_log("HOTKEY", "Starting recording...")
                self._start_recording()
            elif self.state == AppState.RECORDING:
                # Stop recording
                _pipeline_log("HOTKEY", "Stopping recording...")
                self._stop_recording()
            else:
                _pipeline_log("HOTKEY", f"Ignored (state={self.state.name})")
            # TRANSCRIBING: ignore hotkey

    def _start_recording(self) -> None:
        """Start recording."""
        _pipeline_log("RECORD", ">>> _start_recording called")

        # Beep: high pitch = start recording (short, subtle)
        self._beep(800, 50)

        self._session_count += 1
        print(f"\n{'='*50}")
        print(f"Recording Session #{self._session_count}")
        print(f"{'='*50}")
        print("[REC] Recording started")

        self.state = AppState.RECORDING
        self._emit_state("RECORDING")
        _pipeline_log(
            "RECORD", f"Session #{self._session_count}, starting audio capture..."
        )
        self.audio_capture.start()
        _pipeline_log("RECORD", "Audio capture started")

    def _stop_recording(self) -> None:
        """Stop recording."""
        _pipeline_log("RECORD", ">>> _stop_recording called")

        # Stop streaming ASR timer first
        self._stop_interim_timer()

        # Beep: low pitch = stop recording (short, subtle)
        self._beep(400, 50)
        print("[STOP] Recording stopped")

        # Emit TRANSCRIBING state while processing
        self._emit_state("TRANSCRIBING")

        # Stop capture and get any remaining audio
        _pipeline_log("RECORD", "Stopping audio capture...")
        final_audio = self.audio_capture.stop()
        _pipeline_log(
            "RECORD",
            f"Audio captured: {len(final_audio) if final_audio is not None else 0} samples",
        )

        # Minimum duration check: 0.5 seconds = 8000 samples at 16kHz
        # (Just to filter accidental clicks, short phrases are OK)
        MIN_SAMPLES = 8000  # 0.5 seconds
        if final_audio is not None:
            duration_s = len(final_audio) / 16000
            if len(final_audio) < MIN_SAMPLES:
                print(
                    f"[WARN] Recording too short ({duration_s:.2f}s < 0.5s) - accidental click?"
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
                self.audio_capture.start()

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

            # Start config file watcher for hot-reload
            self._start_config_watcher()

            # Register hotkey
            print(f"\nRegistering hotkey: {self.hotkey}")
            try:
                self.hotkey_manager.register(
                    self.hotkey, self._on_hotkey, "Toggle voice recording"
                )
            except RuntimeError as e:
                error_msg = f"Failed to register hotkey: {e}"
                print(f"[ERR] {error_msg}")
                self._emit_error(error_msg)
                raise

            # Start hotkey listener
            self.hotkey_manager.start()
            self._running = True

            print()
            print("=" * 60)
            print(f"  Press [{self.hotkey.upper()}] to start/stop recording")
            print("=" * 60)
            print()
            print("Ready! Waiting for hotkey...")

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

        # Stop recording if active
        if self.audio_capture and self.audio_capture.is_recording:
            self.audio_capture.stop()

        # Stop ASR worker
        self._stop_asr_worker()

        # Stop hotkey listener
        self.hotkey_manager.stop()

        self._running = False
        self._emit_state("IDLE")
        print("AriaApp stopped.")

    def toggle_recording(self) -> None:
        """
        Programmatically toggle recording (for UI buttons).
        Same as pressing the hotkey.
        """
        self._on_hotkey()

    def is_running(self) -> bool:
        """Check if the application is running."""
        return self._running

    def reload_config(self) -> None:
        """Reload configuration from hotwords.json (hot-reload support).

        Thread-safe: Uses self._lock to prevent race conditions with ASR worker.
        """
        with self._lock:
            if not self.hotword_manager:
                return

            self.hotword_manager.reload()

            # Update Layer 1: ASR engine hotwords
            if self.asr_engine:
                if self._asr_engine_type == "funasr" and hasattr(
                    self.asr_engine, "set_hotwords"
                ):
                    # FunASR: update weighted hotwords
                    weighted_hotwords = self.hotword_manager.get_weighted_hotwords()
                    self.asr_engine.set_hotwords(weighted_hotwords)
                    print(
                        f"[HOT-RELOAD] Updated FunASR hotwords: {len(weighted_hotwords)} words"
                    )
                else:
                    # Whisper: update initial_prompt
                    initial_prompt = self.hotword_manager.build_initial_prompt()
                    if initial_prompt:
                        self.asr_engine.set_initial_prompt(initial_prompt)
                        print(
                            f"[HOT-RELOAD] Updated initial_prompt: {initial_prompt[:50]}..."
                        )

            # Update Layer 2: Regex replacements
            if self.hotword_processor:
                new_replacements = self.hotword_manager.get_replacements()
                self.hotword_processor.replacements = new_replacements
                self.hotword_processor._build_patterns()
                print(f"[HOT-RELOAD] Updated {len(new_replacements)} replacement rules")

            # Update Layer 2.5: Fuzzy matcher
            if self.fuzzy_matcher:
                fuzzy_hotwords = self.hotword_manager.config.prompt_words + list(
                    self.hotword_manager.config.replacements.values()
                )
                fuzzy_hotwords = list(set(fuzzy_hotwords))
                self.fuzzy_matcher.update_hotwords(fuzzy_hotwords)
                print(
                    f"[HOT-RELOAD] Updated fuzzy matcher with {len(fuzzy_hotwords)} hotwords"
                )

            # Update Layer 3: Polisher
            self.polisher = self.hotword_manager.get_active_polisher()

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

            # Update VAD settings
            if self.audio_capture and self.audio_capture._vad:
                try:
                    import json

                    with open(self._config_path, "r", encoding="utf-8") as f:
                        config = json.load(f)
                    vad_cfg = config.get("vad", {})
                    new_threshold = max(0.1, min(0.9, vad_cfg.get("threshold", 0.2)))
                    new_min_silence = max(
                        100, min(5000, vad_cfg.get("min_silence_ms", 1200))
                    )

                    # Update VAD config in place
                    self.audio_capture._vad.config.threshold = new_threshold
                    self.audio_capture._vad.config.min_silence_ms = new_min_silence
                    print(
                        f"[HOT-RELOAD] Updated VAD: threshold={new_threshold}, min_silence={new_min_silence}ms"
                    )
                except Exception as e:
                    print(f"[HOT-RELOAD] VAD update failed: {e}")

            logger.info("Configuration hot-reloaded (all 4 layers + VAD)")
            print("[HOT-RELOAD] Config reloaded successfully!")

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

        When disabled, hotkey listening is paused but all components
        (ASR, audio capture) remain initialized for quick re-enable.

        Args:
            enabled: True to enable, False to disable
        """
        if enabled:
            if self._running:
                # Already running, just restart hotkey listener
                self.hotkey_manager.start()
                print("[Aria] Hotkey listening resumed")
            else:
                # Not running at all, full start
                self.start()
            logger.info("Aria enabled")
        else:
            if self._running:
                # Stop hotkey listening but keep app alive
                self.hotkey_manager.stop()
                print("[Aria] Hotkey listening paused")
                logger.info("Aria disabled (hotkey listening paused)")

    def set_polish_mode(self, mode: str) -> None:
        """
        Set polish mode from UI.

        Args:
            mode: "fast" (local Qwen) or "quality" (Gemini API)
        """
        if self.hotword_manager:
            try:
                self.hotword_manager.set_polish_mode(mode)
                # Update active polisher
                self.polisher = self.hotword_manager.get_active_polisher()
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
            "fast" or "quality"
        """
        if self.hotword_manager:
            return self.hotword_manager.polish_mode
        return "fast"  # Default

    def set_wakeword(self, wakeword: str) -> None:
        """
        Set wakeword from UI.

        Args:
            wakeword: New wakeword (e.g., "瑶瑶", "小朋友", "小溪")
        """
        if self.wakeword_detector:
            self.wakeword_detector.set_wakeword(wakeword)
            logger.info(f"Wakeword set to: {wakeword}")

    def get_wakeword(self) -> str:
        """Get current wakeword."""
        if self.wakeword_detector:
            return self.wakeword_detector.wakeword
        return "瑶瑶"

    def get_available_wakewords(self) -> list:
        """Get list of available wakeword options."""
        if self.wakeword_detector:
            return self.wakeword_detector.get_available_wakewords()
        return ["瑶瑶", "小朋友", "小溪", "助手"]

    def get_command_hints(self) -> list:
        """Get list of command hints for UI display."""
        if self.wakeword_detector:
            return self.wakeword_detector.get_command_hints()
        return []

    def set_hotkey(self, hotkey: str) -> bool:
        """
        Change the recording hotkey dynamically.

        Args:
            hotkey: New hotkey string (e.g., "capslock", "ctrl+shift+space")

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

            # Start config file watcher for hot-reload
            self._start_config_watcher()

            # Register hotkey
            print(f"\nRegistering hotkey: {self.hotkey}")
            try:
                self.hotkey_manager.register(
                    self.hotkey, self._on_hotkey, "Toggle voice recording"
                )
            except RuntimeError as e:
                print(f"[ERR] Failed to register hotkey: {e}")
                print("   Try using a different hotkey (e.g., 'ctrl+shift+space')")
                return

            # Start hotkey listener
            self.hotkey_manager.start()

            print()
            print("=" * 60)
            print(f"  Press [{self.hotkey.upper()}] to start/stop recording")
            print("  Press [Ctrl+C] to exit")
            print("=" * 60)
            print()
            print("Ready! Waiting for hotkey...")

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
    parser.add_argument(
        "--hotkey",
        "-k",
        default="capslock",
        help="Hotkey to toggle recording (default: CapsLock)",
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
