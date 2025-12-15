"""
Wakeword Detector
=================
Detects wakeword and parses following commands.
Uses pinyin-based matching for ASR variant tolerance.
"""

import json
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

from ..utils import get_config_path
from ..utils.phonetic import get_matcher, PinyinMatcher


class WakewordDetector:
    """
    Detects wakeword from transcribed text and parses commands.

    Design principles:
    - Pinyin-based wakeword matching (瑶瑶 = 摇摇 = 妖妖 phonetically)
    - Multi-trigger command matching (开启/打开/etc)
    - Returns structured result for executor
    """

    def __init__(self, config_path: Optional[str] = None):
        self.enabled = False
        self.wakeword = "瑶瑶"
        self.available_wakewords = ["瑶瑶", "小朋友", "小溪", "助手"]  # UI options
        self.commands: Dict[str, Any] = {}
        self.cooldown_ms = 500

        self._matcher: PinyinMatcher = get_matcher()

        self._load_config(config_path)

    def _load_config(self, config_path: Optional[str] = None) -> None:
        """Load wakeword configuration from JSON."""
        if config_path is None:
            config_path = get_config_path("wakeword.json")
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
            self.available_wakewords = config.get(
                "available_wakewords", ["瑶瑶", "小朋友", "小溪", "助手"]
            )
            self.commands = config.get("commands", {})
            self.cooldown_ms = config.get("cooldown_ms", 500)

            print(
                f"[WAKEWORD] Loaded: '{self.wakeword}' "
                f"(pinyin matching, {len(self.commands)} commands)"
            )
        except Exception as e:
            print(f"[WAKEWORD] Failed to load config: {e}")

    def detect(self, text: str) -> Optional[Tuple[str, str, Any, str]]:
        """
        Detect wakeword and parse command from text using pinyin matching.

        Args:
            text: Transcribed text to check

        Returns:
            Tuple of (command_id, action, value, response) if detected, None otherwise
            Example: ("auto_send_on", "set_auto_send", True, "已开启自动发送")
        """
        if not self.enabled or not text:
            return None

        text = text.strip()

        # Use pinyin-based matching to extract wakeword
        result = self._matcher.extract_wakeword(text, self.wakeword)
        if not result:
            return None

        wakeword_found, command_text, _ = result
        command_text = command_text.strip()

        if not command_text:
            print(f"[WAKEWORD] Detected '{wakeword_found}' but no command: '{text}'")
            return None

        # Find matching command
        for cmd_id, cmd_config in self.commands.items():
            triggers = cmd_config.get("triggers", [])
            for trigger in triggers:
                # Require minimum trigger length to avoid false matches
                if len(trigger) >= 2 and (
                    trigger in command_text or command_text in trigger
                ):
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

    def get_available_wakewords(self) -> list[str]:
        """Get list of available wakeword options for UI."""
        return self.available_wakewords.copy()

    def set_wakeword(self, wakeword: str) -> None:
        """Change the active wakeword."""
        self.wakeword = wakeword
        print(f"[WAKEWORD] Changed to: '{wakeword}'")

    def get_command_hints(self) -> list[str]:
        """Get list of example commands for UI display."""
        hints = []
        for cmd_id, cmd_config in self.commands.items():
            triggers = cmd_config.get("triggers", [])
            response = cmd_config.get("response", "")
            if triggers:
                # Use first trigger as example
                hint = f"{self.wakeword}{triggers[0]}"
                if response:
                    hint += f" → {response}"
                hints.append(hint)
        return hints

    def reload(self, config_path: Optional[str] = None) -> None:
        """Reload configuration."""
        self._load_config(config_path)
