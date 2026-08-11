"""
Command Executor
================
Executes voice commands by sending keystrokes.

Security:
- Whitelist-based execution only
- Cooldown mechanism to prevent rapid-fire
- Audit logging
"""

import time
from typing import Optional, Dict, Any, List

from ..logging import get_system_logger

logger = get_system_logger()


class CommandExecutor:
    """
    Executes voice commands using OutputInjector.

    Features:
    - Key press commands with modifier support
    - Cooldown to prevent rapid execution
    - Audit logging for all executions
    """

    def __init__(self, output_injector, commands: Dict[str, Any], cooldown_ms: int = 500):
        """
        Initialize command executor.

        Args:
            output_injector: OutputInjector instance with send_key() method
            commands: Command definitions from config
            cooldown_ms: Minimum time between commands in milliseconds
        """
        self.output = output_injector
        self.commands = commands
        self.cooldown_ms = cooldown_ms
        self._last_exec_time = 0
        self._exec_count = 0

    def execute(self, command_id: str) -> bool:
        """
        Execute a command by ID.

        Args:
            command_id: Command ID (e.g., "发送", "撤销")

        Returns:
            True if executed successfully, False otherwise
        """
        # Validate command exists
        if command_id not in self.commands:
            logger.warning(f"Unknown command: {command_id}")
            return False

        # Cooldown check
        now_ms = time.time() * 1000
        elapsed = now_ms - self._last_exec_time
        if elapsed < self.cooldown_ms:
            logger.debug(f"Command cooldown: {elapsed:.0f}ms < {self.cooldown_ms}ms")
            return False

        # Get command config
        cmd = self.commands[command_id]
        key = cmd.get('key')
        modifiers = cmd.get('modifiers', [])

        if not key:
            logger.error(f"Command '{command_id}' has no key defined")
            return False

        # Execute key press
        try:
            success = self.output.send_key(key, modifiers)

            if success:
                self._last_exec_time = now_ms
                self._exec_count += 1

                # Audit log (no sensitive content)
                mod_str = '+'.join(modifiers) + '+' if modifiers else ''
                logger.info(f"[CMD] Executed: {command_id} -> {mod_str}{key} (#{self._exec_count})")
            else:
                logger.warning(f"[CMD] Failed: {command_id}")

            return success

        except Exception as e:
            logger.error(f"[CMD] Error executing {command_id}: {e}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Get executor statistics."""
        return {
            'total_executions': self._exec_count,
            'cooldown_ms': self.cooldown_ms,
            'available_commands': list(self.commands.keys()),
        }
