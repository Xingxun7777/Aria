"""
Wakeword Detector
=================
Detects wakeword and parses following commands.
Uses pinyin-based matching for ASR variant tolerance.
"""

import json
import re
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

        # Find matching command using LONGEST MATCH strategy
        # This prevents "翻译" from matching before "翻译成英文"
        # Normalize: remove spaces AND punctuation for matching
        # ASR may add spaces/commas between words (e.g., "开启，自动发送")
        command_text_normalized = re.sub(r"[\s，,。.、：:；;！!？?]", "", command_text)

        # Collect matches with priority:
        # 1. Trigger is IN command text (user said the trigger or more)
        # 2. Command text is IN trigger (user said part of a trigger)
        # Among same priority, prefer longest trigger
        best_match = None
        best_trigger_length = 0
        best_is_exact_or_contains = (
            False  # Priority: trigger IN command > command IN trigger
        )

        for cmd_id, cmd_config in self.commands.items():
            triggers = cmd_config.get("triggers", [])
            for trigger in triggers:
                trigger_normalized = trigger.replace(" ", "")
                # Require minimum trigger length to avoid false matches
                if len(trigger_normalized) < 2:
                    continue

                # Check match type
                trigger_in_command = trigger_normalized in command_text_normalized
                command_in_trigger = command_text_normalized in trigger_normalized

                if not trigger_in_command and not command_in_trigger:
                    continue

                # Prioritize "trigger IN command" over "command IN trigger"
                # This ensures "翻译" matches translate_popup, not "翻译成英文"
                is_exact_or_contains = trigger_in_command

                # Choose this match if:
                # 1. It has higher priority (trigger_in_command beats command_in_trigger)
                # 2. Same priority but longer trigger
                if is_exact_or_contains and not best_is_exact_or_contains:
                    # This is a better priority match
                    best_match = (cmd_id, cmd_config, trigger)
                    best_trigger_length = len(trigger_normalized)
                    best_is_exact_or_contains = True
                elif is_exact_or_contains == best_is_exact_or_contains:
                    # Same priority, prefer longer trigger
                    if len(trigger_normalized) > best_trigger_length:
                        best_match = (cmd_id, cmd_config, trigger)
                        best_trigger_length = len(trigger_normalized)
                        best_is_exact_or_contains = is_exact_or_contains

        if best_match:
            cmd_id, cmd_config, matched_trigger = best_match
            action = cmd_config.get("action")
            value = cmd_config.get("value")
            response = cmd_config.get("response", "")

            print(
                f"[WAKEWORD] Detected: '{wakeword_found}' + '{command_text}' "
                f"-> {cmd_id} ({action}={value}) [trigger: '{matched_trigger}']"
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
