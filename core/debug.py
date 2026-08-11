"""
Aria Debug System
=================
Comprehensive debug logging for all processing layers.

Usage:
    from aria.core.debug import DebugSession

    debug = DebugSession()
    debug.log_audio(...)
    debug.log_asr(...)
    debug.log_hotword(...)
    debug.log_polish(...)
    debug.save()
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
import threading

# Debug log directory
DEBUG_DIR = Path(__file__).parent.parent / "DebugLog"

# === Size-rotated append logging (shared by pipeline_debug.log / qwen3_debug.log) ===

DEFAULT_LOG_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 1

_log_rotate_lock = threading.Lock()


def _rotate_log_locked(path: Path, backup_count: int) -> None:
    """Rename path -> path.1 (-> path.2 ...), mirroring the crash.log scheme.

    Any rename/unlink failure (e.g. file held open on Windows) is swallowed;
    the caller keeps appending to the current file and rotation is retried on
    a later write.
    """
    for index in range(backup_count, 0, -1):
        source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
        target = path.with_name(f"{path.name}.{index}")
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass
        try:
            if source.exists():
                os.replace(str(source), str(target))
        except OSError:
            pass


def append_log_line(
    path,
    text: str,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Append a line to a debug log file, rotating it once it exceeds max_bytes.

    Never raises: debug logging must not take down the caller.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        if max_bytes > 0 and backup_count > 0:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if size >= max_bytes:
                with _log_rotate_lock:
                    # Re-check under the lock: another thread may have rotated.
                    try:
                        size = path.stat().st_size
                    except OSError:
                        size = 0
                    if size >= max_bytes:
                        _rotate_log_locked(path, backup_count)

        with open(path, "a", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except Exception:
        pass  # Silent fail (mirrors previous inline logging behavior)


@dataclass
class AudioDebugInfo:
    """Audio capture debug information."""

    timestamp: str = ""
    session_id: int = 0

    # Recording info
    duration_seconds: float = 0.0
    sample_count: int = 0
    sample_rate: int = 16000
    channels: int = 1

    # VAD info
    vad_enabled: bool = True
    vad_threshold: float = 0.5
    speech_segments: int = 0

    # Audio stats
    audio_level_avg: float = 0.0
    audio_level_max: float = 0.0
    # 95th-percentile absolute amplitude (pre-DSP). Distinguishes real speech
    # diluted by leading/trailing silence (loud p95) from sustained noise
    # (flat p95 ≈ avg); feeds the ASR context-starve peak-escape calibration.
    audio_level_p95: float = 0.0
    # Per-segment Silero VAD probability telemetry (observation only).
    vad_prob_avg: float = -1.0
    vad_prob_max: float = -1.0
    vad_voiced_ratio: float = -1.0
    # VAD endpoint telemetry (observation only, mining/report.md section C).
    # All optional: old sessions and non-VAD paths keep the defaults below.
    # endpoint_reason: tail_silence | max_speech | soft_split | manual_stop
    # ("" = unknown / stats-less path).
    endpoint_reason: str = ""
    # Endpoint-accounting silence accumulated at commit (ms); -1 = unknown.
    tail_silence_ms: float = -1.0
    # Wall-clock ms from last voiced chunk to segment commit; -1 = unknown.
    last_voice_to_commit_ms: float = -1.0
    # Intra-segment pauses that ended with resumed speech, 100ms buckets
    # ([count@0-100, count@100-200, ...], trailing zeros trimmed).
    pause_histogram: List[int] = field(default_factory=list)
    audio_file_path: str = ""


@dataclass
class ASRDebugInfo:
    """ASR transcription debug information."""

    timestamp: str = ""
    session_id: int = 0

    # Model info
    model_name: str = ""
    device: str = ""
    language: str = ""

    # Input
    audio_duration: float = 0.0
    initial_prompt: str = ""
    initial_prompt_enabled: bool = False

    # Output
    raw_text: str = ""
    transcribe_time_ms: float = 0.0

    # ASR internals (if available)
    segments: List[Dict] = field(default_factory=list)
    language_detected: str = ""
    language_probability: float = 0.0


@dataclass
class HotWordDebugInfo:
    """HotWord correction debug information."""

    timestamp: str = ""
    session_id: int = 0

    # Layer 1: Initial prompt
    layer1_enabled: bool = False
    layer1_prompt_words: List[str] = field(default_factory=list)
    layer1_domain_context: str = ""

    # Layer 2: Regex replacement
    layer2_input: str = ""
    layer2_output: str = ""
    layer2_replacements_applied: List[Dict[str, str]] = field(default_factory=list)
    layer2_rules_count: int = 0

    # Processing time
    layer2_time_ms: float = 0.0


@dataclass
class PolishDebugInfo:
    """AI Polish debug information."""

    timestamp: str = ""
    session_id: int = 0

    # Config
    enabled: bool = False
    api_url: str = ""
    model: str = ""
    timeout: float = 0.0

    # Request
    input_text: str = ""
    prompt_template: str = ""
    full_prompt: str = ""

    # Response
    output_text: str = ""
    changed: bool = False

    # Timing
    api_time_ms: float = 0.0

    # Error info
    error: str = ""
    http_status: int = 0


@dataclass
class DiagnosticsSummary:
    """Auto-computed diagnostics for LLM analysis."""

    likely_issue: str = "N/A"  # Main issue category

    # Audio health
    audio_duration_ok: bool = True
    audio_level_ok: bool = True
    vad_activity_ok: bool = True

    # Performance
    total_latency_ms: float = 0.0
    asr_latency_ratio: float = 0.0
    polish_latency_ratio: float = 0.0
    is_performant: bool = True

    # Layer 2 - HotWord
    hotword_corrections_applied: int = 0

    # Layer 3 - Polish
    polish_was_used: bool = False
    polish_changed_text: bool = False
    polish_failed: bool = False

    # Final output
    final_text_is_empty: bool = False


@dataclass
class SessionDebugInfo:
    """Complete session debug information."""

    session_id: int = 0
    start_time: str = ""
    end_time: str = ""

    # Final output
    final_text: str = ""
    inserted: bool = False

    # Layer details
    audio: Optional[AudioDebugInfo] = None
    asr: Optional[ASRDebugInfo] = None
    hotword: Optional[HotWordDebugInfo] = None
    polish: Optional[PolishDebugInfo] = None

    # Total timing
    total_time_ms: float = 0.0

    # Auto-computed diagnostics for LLM analysis
    diagnostics: Optional[DiagnosticsSummary] = None

    # Errors
    errors: List[str] = field(default_factory=list)


class DebugSession:
    """
    Debug session manager for a single transcription.

    Collects debug info from all layers and saves to JSON file.
    """

    _lock = threading.Lock()

    def __init__(self, session_id: int = 0, enabled: bool = True):
        self.enabled = enabled
        self.session_id = session_id
        self._start_time = time.time()

        self.info = SessionDebugInfo(
            session_id=session_id, start_time=datetime.now().isoformat()
        )

        # Ensure debug directory exists
        if self.enabled:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    def _timestamp(self) -> str:
        return datetime.now().isoformat()

    def log_audio(
        self,
        duration_seconds: float,
        sample_count: int,
        sample_rate: int = 16000,
        channels: int = 1,
        vad_enabled: bool = True,
        vad_threshold: float = 0.5,
        speech_segments: int = 0,
        audio_level_avg: float = 0.0,
        audio_level_max: float = 0.0,
        audio_level_p95: float = 0.0,
        vad_prob_avg: float = -1.0,
        vad_prob_max: float = -1.0,
        vad_voiced_ratio: float = -1.0,
        endpoint_reason: str = "",
        tail_silence_ms: float = -1.0,
        last_voice_to_commit_ms: float = -1.0,
        pause_histogram: Optional[List[int]] = None,
        audio_file_path: str = "",
    ) -> None:
        """Log audio capture information."""
        if not self.enabled:
            return

        self.info.audio = AudioDebugInfo(
            timestamp=self._timestamp(),
            session_id=self.session_id,
            duration_seconds=duration_seconds,
            sample_count=sample_count,
            sample_rate=sample_rate,
            channels=channels,
            vad_enabled=vad_enabled,
            vad_threshold=vad_threshold,
            speech_segments=speech_segments,
            audio_level_avg=audio_level_avg,
            audio_level_max=audio_level_max,
            audio_level_p95=audio_level_p95,
            vad_prob_avg=vad_prob_avg,
            vad_prob_max=vad_prob_max,
            vad_voiced_ratio=vad_voiced_ratio,
            endpoint_reason=endpoint_reason,
            tail_silence_ms=tail_silence_ms,
            last_voice_to_commit_ms=last_voice_to_commit_ms,
            pause_histogram=list(pause_histogram) if pause_histogram else [],
            audio_file_path=audio_file_path,
        )

    def log_asr(
        self,
        model_name: str,
        device: str,
        language: str,
        audio_duration: float,
        initial_prompt: str,
        initial_prompt_enabled: bool,
        raw_text: str,
        transcribe_time_ms: float,
        segments: Optional[List[Dict]] = None,
        language_detected: str = "",
        language_probability: float = 0.0,
    ) -> None:
        """Log ASR transcription information."""
        if not self.enabled:
            return

        self.info.asr = ASRDebugInfo(
            timestamp=self._timestamp(),
            session_id=self.session_id,
            model_name=model_name,
            device=device,
            language=language,
            audio_duration=audio_duration,
            initial_prompt=initial_prompt,
            initial_prompt_enabled=initial_prompt_enabled,
            raw_text=raw_text,
            transcribe_time_ms=transcribe_time_ms,
            segments=segments or [],
            language_detected=language_detected,
            language_probability=language_probability,
        )

    def log_hotword(
        self,
        layer1_enabled: bool,
        layer1_prompt_words: List[str],
        layer1_domain_context: str,
        layer2_input: str,
        layer2_output: str,
        layer2_replacements_applied: List[Dict[str, str]],
        layer2_rules_count: int,
        layer2_time_ms: float,
    ) -> None:
        """Log HotWord correction information."""
        if not self.enabled:
            return

        self.info.hotword = HotWordDebugInfo(
            timestamp=self._timestamp(),
            session_id=self.session_id,
            layer1_enabled=layer1_enabled,
            layer1_prompt_words=layer1_prompt_words,
            layer1_domain_context=layer1_domain_context,
            layer2_input=layer2_input,
            layer2_output=layer2_output,
            layer2_replacements_applied=layer2_replacements_applied,
            layer2_rules_count=layer2_rules_count,
            layer2_time_ms=layer2_time_ms,
        )

    def log_polish(
        self,
        enabled: bool,
        api_url: str = "",
        model: str = "",
        timeout: float = 0.0,
        input_text: str = "",
        prompt_template: str = "",
        full_prompt: str = "",
        output_text: str = "",
        changed: bool = False,
        api_time_ms: float = 0.0,
        error: str = "",
        http_status: int = 0,
    ) -> None:
        """Log AI Polish information."""
        if not self.enabled:
            return

        self.info.polish = PolishDebugInfo(
            timestamp=self._timestamp(),
            session_id=self.session_id,
            enabled=enabled,
            api_url=api_url,
            model=model,
            timeout=timeout,
            input_text=input_text,
            prompt_template=prompt_template,
            full_prompt=full_prompt,
            output_text=output_text,
            changed=changed,
            api_time_ms=api_time_ms,
            error=error,
            http_status=http_status,
        )

    def log_error(self, error: str) -> None:
        """Log an error."""
        if not self.enabled:
            return
        self.info.errors.append(f"{self._timestamp()}: {error}")

    def finalize(self, final_text: str, inserted: bool) -> None:
        """Finalize the session and compute diagnostic summary."""
        if not self.enabled:
            return

        self.info.end_time = self._timestamp()
        self.info.final_text = final_text
        self.info.inserted = inserted
        self.info.total_time_ms = (time.time() - self._start_time) * 1000

        # Auto-compute diagnostics for LLM analysis
        summary = DiagnosticsSummary()
        summary.total_latency_ms = self.info.total_time_ms
        summary.final_text_is_empty = not final_text.strip()

        # Audio diagnostics
        if self.info.audio:
            summary.audio_duration_ok = self.info.audio.duration_seconds > 0.2
            summary.audio_level_ok = self.info.audio.audio_level_avg > 0.001
            summary.vad_activity_ok = (
                self.info.audio.speech_segments > 0
                if self.info.audio.vad_enabled
                else True
            )

        # Performance diagnostics
        if self.info.total_time_ms > 0:
            if self.info.asr:
                summary.asr_latency_ratio = (
                    self.info.asr.transcribe_time_ms / self.info.total_time_ms
                )
            if self.info.polish:
                summary.polish_latency_ratio = (
                    self.info.polish.api_time_ms / self.info.total_time_ms
                )
        summary.is_performant = self.info.total_time_ms < 3000

        # HotWord diagnostics
        if self.info.hotword:
            summary.hotword_corrections_applied = len(
                self.info.hotword.layer2_replacements_applied
            )

        # Polish diagnostics
        if self.info.polish:
            summary.polish_was_used = self.info.polish.enabled
            summary.polish_changed_text = self.info.polish.changed
            summary.polish_failed = bool(self.info.polish.error)

        # Determine likely issue (priority order)
        if not summary.audio_level_ok:
            summary.likely_issue = "Audio Quality (Low Volume)"
        elif summary.polish_failed:
            summary.likely_issue = "Polish API Failure"
        elif self.info.asr and not self.info.asr.raw_text.strip():
            summary.likely_issue = "ASR Error (No text produced)"
        elif summary.final_text_is_empty:
            summary.likely_issue = "Processing Error (Empty final text)"
        elif summary.polish_changed_text:
            summary.likely_issue = "ASR Accuracy (Corrected by Polish)"
        else:
            summary.likely_issue = "OK"

        self.info.diagnostics = summary

    def save(self) -> Optional[str]:
        """Save debug info to JSON file. Returns file path."""
        if not self.enabled:
            return None

        with self._lock:
            try:
                # Generate filename. Millisecond suffix — two worker iterations
                # (e.g. a buffered-final commit and a trailing-silence ghost
                # final) can finish within the same second, and a plain seconds
                # timestamp makes the later save silently overwrite the earlier
                # session, which corrupts quality telemetry.
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                filename = f"session_{self.session_id}_{timestamp}.json"
                filepath = DEBUG_DIR / filename

                # Convert dataclasses to dict
                def convert(obj):
                    if hasattr(obj, "__dataclass_fields__"):
                        return asdict(obj)
                    return obj

                data = asdict(self.info)

                # Write JSON
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

                return str(filepath)

            except Exception as e:
                print(f"[DEBUG] Failed to save debug info: {e}")
                return None

    def print_summary(self) -> None:
        """Print a summary to console."""
        if not self.enabled:
            return

        print(f"\n{'='*60}")
        print(f"DEBUG SESSION #{self.session_id} SUMMARY")
        print(f"{'='*60}")

        if self.info.audio:
            a = self.info.audio
            print(
                f"[AUDIO] duration={a.duration_seconds:.2f}s, samples={a.sample_count}, "
                f"level_avg={a.audio_level_avg:.4f}, level_max={a.audio_level_max:.4f}"
            )

        if self.info.asr:
            r = self.info.asr
            print(
                f"[ASR] model={r.model_name}, device={r.device}, time={r.transcribe_time_ms:.0f}ms"
            )
            print(f"[ASR] initial_prompt_enabled={r.initial_prompt_enabled}")
            print(f"[ASR] raw_text: {r.raw_text}")

        if self.info.hotword:
            h = self.info.hotword
            print(
                f"[HOTWORD] L1_enabled={h.layer1_enabled}, L2_rules={h.layer2_rules_count}"
            )
            if h.layer2_replacements_applied:
                print(f"[HOTWORD] replacements: {h.layer2_replacements_applied}")
            print(f"[HOTWORD] input: {h.layer2_input}")
            print(f"[HOTWORD] output: {h.layer2_output}")

        if self.info.polish:
            p = self.info.polish
            print(
                f"[POLISH] enabled={p.enabled}, model={p.model}, time={p.api_time_ms:.0f}ms"
            )
            if p.enabled:
                print(f"[POLISH] input: {p.input_text}")
                print(f"[POLISH] output: {p.output_text}")
                print(f"[POLISH] changed={p.changed}")
                if p.error:
                    print(f"[POLISH] ERROR: {p.error}")

        print(f"[FINAL] text: {self.info.final_text}")
        print(
            f"[FINAL] inserted={self.info.inserted}, total_time={self.info.total_time_ms:.0f}ms"
        )

        if self.info.errors:
            print(f"[ERRORS] {self.info.errors}")

        print(f"{'='*60}\n")


# Global debug configuration
class DebugConfig:
    """Global debug configuration."""

    enabled: bool = True
    print_summary: bool = True
    save_to_file: bool = True
    # Full-utterance WAV dumps are a privacy + disk liability (every dictation
    # recorded to DebugLog/), so unlike the session JSON (which the tray
    # history popup depends on) audio saving is OPT-IN.
    save_audio: bool = False
    # Full OCR/page text can contain names, messages, documents, and other
    # on-screen data. Keep diagnostic content dumps opt-in as well; normal OCR
    # telemetry only needs timing, lengths, hashes, and backend status.
    save_screen_text: bool = False

    @classmethod
    def from_env(cls):
        """Load config from environment variables."""
        cls.enabled = os.environ.get("ARIA_DEBUG", "1") == "1"
        cls.print_summary = os.environ.get("ARIA_DEBUG_PRINT", "1") == "1"
        cls.save_to_file = os.environ.get("ARIA_DEBUG_SAVE", "1") == "1"
        cls.save_audio = os.environ.get("ARIA_DEBUG_SAVE_AUDIO", "0") == "1"
        cls.save_screen_text = (
            os.environ.get("ARIA_DEBUG_SAVE_SCREEN_TEXT", "0") == "1"
        )


# Initialize from environment
DebugConfig.from_env()
