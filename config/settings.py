"""
VoiceType Configuration Management
==================================
Handles loading, saving, and accessing configuration settings.
"""

import json
import os
import tempfile
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

# Default paths
APP_NAME = "VoiceType"
CONFIG_DIR = Path(os.environ.get('APPDATA', '~')) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class HotkeyConfig:
    """Hotkey configuration."""
    voice_trigger: str = "ctrl+shift+space"
    voice_stop: str = "ctrl+shift+space"  # Toggle mode
    autocomplete_trigger: str = "ctrl+shift+tab"
    cancel: str = "escape"


@dataclass
class AudioConfig:
    """Audio capture configuration."""
    sample_rate: int = 16000
    channels: int = 1
    device_id: Optional[int] = None  # None = default device
    vad_threshold: float = 0.01


@dataclass
class ASRConfig:
    """ASR engine configuration."""
    engine: str = "whisper"  # whisper, moonshine, faster-whisper
    model: str = "base"  # tiny, base, small, medium, large
    language: str = "auto"  # auto, en, zh, etc.
    device: str = "cpu"  # cpu, cuda


@dataclass
class LLMConfig:
    """LLM configuration for text cleanup."""
    provider: str = "ollama"  # ollama, api
    model: str = "qwen2.5:1.5b"
    base_url: str = "http://localhost:11434"
    timeout: int = 30


@dataclass
class OutputConfig:
    """Output injection configuration."""
    method: str = "clipboard"  # clipboard, sendinput, hybrid
    clipboard_restore: bool = True
    paste_delay_ms: int = 100


@dataclass
class UIConfig:
    """UI configuration."""
    overlay_position: str = "bottom-right"  # top-left, top-right, bottom-left, bottom-right
    overlay_opacity: float = 0.95
    show_tray_icon: bool = True
    minimize_to_tray: bool = True


@dataclass
class Settings:
    """Main settings container."""
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    ui: UIConfig = field(default_factory=UIConfig)

    # Metadata
    version: str = "1.0"
    first_run: bool = True

    def save(self, path: Optional[Path] = None) -> None:
        """Save settings to JSON file using atomic write."""
        path = path or CONFIG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            'version': self.version,
            'first_run': self.first_run,
            'hotkeys': asdict(self.hotkeys),
            'audio': asdict(self.audio),
            'asr': asdict(self.asr),
            'llm': asdict(self.llm),
            'output': asdict(self.output),
            'ui': asdict(self.ui),
        }

        # Atomic write: write to temp file, then rename
        fd, tmp_path = tempfile.mkstemp(
            suffix='.tmp',
            prefix='config_',
            dir=path.parent
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            # Atomic replace (works on Windows and Unix)
            os.replace(tmp_path, path)
        except Exception:
            # Clean up temp file on failure
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    @classmethod
    def load(cls, path: Optional[Path] = None) -> 'Settings':
        """Load settings from JSON file."""
        path = path or CONFIG_FILE

        if not path.exists():
            return cls()

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            return cls(
                version=data.get('version', '1.0'),
                first_run=data.get('first_run', True),
                hotkeys=HotkeyConfig(**data.get('hotkeys', {})),
                audio=AudioConfig(**data.get('audio', {})),
                asr=ASRConfig(**data.get('asr', {})),
                llm=LLMConfig(**data.get('llm', {})),
                output=OutputConfig(**data.get('output', {})),
                ui=UIConfig(**data.get('ui', {})),
            )
        except Exception as e:
            print(f"[WARN] Failed to load config: {e}, using defaults")
            return cls()


# Singleton instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get the global settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


def reset_settings() -> Settings:
    """Reset settings to defaults."""
    global _settings
    _settings = Settings()
    _settings.save()
    return _settings
