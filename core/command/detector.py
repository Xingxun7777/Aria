"""
Command Detector
================
Detects voice commands from transcribed text using exact matching with prefix.

Security:
- Uses exact matching only (no fuzzy matching)
- Requires prefix to prevent accidental triggers
- Whitelist-based command recognition
"""

import json
from pathlib import Path
from typing import Optional, Dict, Any

from ..logging import get_system_logger
from ..utils import get_config_path

logger = get_system_logger()


class CommandDetector:
    """
    Detects voice commands from transcribed text.

    Commands must follow the format: "{prefix}，{command}"
    Example: "小助手，发送" -> "发送"
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize command detector.

        Args:
            config_path: Path to commands.json. If None, uses default location.
        """
        self.enabled = False
        self.prefix = "小助手"
        self.commands: Dict[str, Any] = {}
        self.cooldown_ms = 500  # Cooldown between command executions

        self._load_config(config_path)

    def _load_config(self, config_path: Optional[str] = None) -> None:
        """Load command configuration from JSON file."""
        if config_path is None:
            config_path = get_config_path("commands.json")
        else:
            config_path = Path(config_path)

        if not config_path.exists():
            logger.warning(f"Command config not found: {config_path}")
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            self.prefix = config.get("prefix", "") or "小助手"
            self.enabled = True  # Always enabled — uses same wakeword as prefix
            self.commands = config.get("commands", {})

            logger.info(
                f"Command detector loaded: {len(self.commands)} commands, prefix='{self.prefix}'"
            )

        except Exception as e:
            logger.error(f"Failed to load command config: {e}")

    def detect(self, text: str) -> Optional[str]:
        """
        Detect if text is a voice command.

        Args:
            text: Transcribed text to check

        Returns:
            Command ID (e.g., "发送") if detected, None otherwise
        """
        if not self.enabled:
            return None

        if not text:
            return None

        text = text.strip()

        # Check prefix
        if not text.startswith(self.prefix):
            return None

        # Extract command part (after prefix)
        cmd_text = text[len(self.prefix) :].strip()

        # Strip ALL punctuation (ASR adds commas, periods, etc.)
        import re

        cmd_text = re.sub(r"[\s，,。.、：:；;！!？?\-—]", "", cmd_text)

        # Exact match only (security: no fuzzy matching)
        if cmd_text in self.commands:
            logger.info(f"Command detected: '{cmd_text}' from '{text}'")
            return cmd_text

        return None

    def get_command_info(self, command_id: str) -> Optional[Dict[str, Any]]:
        """Get command configuration by ID."""
        return self.commands.get(command_id)

    def reload(self, config_path: Optional[str] = None) -> None:
        """Reload configuration from file."""
        self._load_config(config_path)
