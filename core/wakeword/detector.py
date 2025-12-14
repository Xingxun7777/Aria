"""
Wakeword Detector
=================
Detects wakeword and parses following commands.
Supports fuzzy matching for ASR variants.
"""

import json
import re
from pathlib import Path
from typing import Optional, Dict, Any, Tuple


class WakewordDetector:
    """
    Detects wakeword from transcribed text and parses commands.

    Design principles:
    - Multi-variant wakeword matching (ASR may produce 摇摇/妖妖/etc)
    - Multi-trigger command matching (开启/打开/etc)
    - Returns structured result for executor
    """

    def __init__(self, config_path: Optional[str] = None):
        self.enabled = False
        self.wakeword = "瑶瑶"
        self.variants: list[str] = []
        self.commands: Dict[str, Any] = {}
        self.cooldown_ms = 500

        self._wakeword_pattern: Optional[re.Pattern] = None

        self._load_config(config_path)
        self._build_pattern()

    def _load_config(self, config_path: Optional[str] = None) -> None:
        """Load wakeword configuration from JSON."""
        if config_path is None:
            config_path = (
                Path(__file__).parent.parent.parent / "config" / "wakeword.json"
            )
        else:
            config_path = Path(config_path)

        if not config_path.exists():
            print(f"[WAKEWORD] Config not found: {config_path}")
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            self.enabled = config.get("enabled", False)
            self.wakeword = config.get("wakeword", "瑶瑶")
            self.variants = config.get("wakeword_variants", [self.wakeword])
            self.commands = config.get("commands", {})
            self.cooldown_ms = config.get("cooldown_ms", 500)

            print(
                f"[WAKEWORD] Loaded: '{self.wakeword}' "
                f"({len(self.variants)} variants, {len(self.commands)} commands)"
            )
        except Exception as e:
            print(f"[WAKEWORD] Failed to load config: {e}")

    def _build_pattern(self) -> None:
        """Build regex pattern for wakeword variants."""
        if not self.variants:
            self.variants = [self.wakeword]

        # Escape special chars and join with |
        escaped = [re.escape(v) for v in self.variants]
        self._wakeword_pattern = re.compile(
            f"^({'|'.join(escaped)})[，,。.：:\s]*(.*)$"
        )

    def detect(self, text: str) -> Optional[Tuple[str, str, Any, str]]:
        """
        Detect wakeword and parse command from text.

        Args:
            text: Transcribed text to check

        Returns:
            Tuple of (command_id, action, value, response) if detected, None otherwise
            Example: ("auto_send_on", "set_auto_send", True, "已开启自动发送")
        """
        if not self.enabled or not text or not self._wakeword_pattern:
            return None

        text = text.strip()

        # Match wakeword + command
        match = self._wakeword_pattern.match(text)
        if not match:
            return None

        wakeword_found = match.group(1)
        command_text = match.group(2).strip()

        if not command_text:
            print(f"[WAKEWORD] Detected but no command: '{text}'")
            return None

        # Find matching command
        for cmd_id, cmd_config in self.commands.items():
            triggers = cmd_config.get("triggers", [])
            for trigger in triggers:
                # Support both exact match and partial match
                if trigger in command_text or command_text in trigger:
                    action = cmd_config.get("action")
                    value = cmd_config.get("value")
                    response = cmd_config.get("response", "")

                    print(
                        f"[WAKEWORD] Detected: '{wakeword_found}' + '{command_text}' "
                        f"-> {cmd_id} ({action}={value})"
                    )
                    return (cmd_id, action, value, response)

        print(f"[WAKEWORD] Unknown command: '{command_text}'")
        return None

    def get_command_info(self, cmd_id: str) -> Optional[Dict[str, Any]]:
        """Get command configuration by ID."""
        return self.commands.get(cmd_id)

    def reload(self, config_path: Optional[str] = None) -> None:
        """Reload configuration."""
        self._load_config(config_path)
        self._build_pattern()
