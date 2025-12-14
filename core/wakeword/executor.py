"""
Wakeword Executor
=================
Executes application-level commands triggered by wakeword.
"""

import time
from pathlib import Path
from typing import Callable, Dict, Any, Optional, TYPE_CHECKING

# Debug log file for wakeword executor
_DEBUG_LOG = Path(__file__).parent.parent.parent / "DebugLog" / "wakeword_debug.log"
_DEBUG_LOG.parent.mkdir(exist_ok=True)


def _debug(msg: str):
    """Write debug message to file."""
    import datetime

    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}\n"
    print(line.strip())
    with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
        f.write(line)


if TYPE_CHECKING:
    from ui.qt.bridge import QtBridge


class WakewordExecutor:
    """
    Executes wakeword commands by calling app methods.

    Unlike CommandExecutor (sends keystrokes), this executor:
    - Calls application methods directly (set_auto_send, etc)
    - Notifies UI via bridge signals
    - Supports cooldown mechanism
    """

    def __init__(
        self,
        app_instance,
        bridge: Optional["QtBridge"] = None,
        cooldown_ms: int = 500,
    ):
        """
        Initialize wakeword executor.

        Args:
            app_instance: VoiceTypeApp instance with setter methods
            bridge: QtBridge for UI notification (optional)
            cooldown_ms: Minimum time between commands
        """
        self.app = app_instance
        self.bridge = bridge
        self.cooldown_ms = cooldown_ms
        self._last_exec_time = 0.0
        self._exec_count = 0

        # Action -> Method mapping
        self._action_map: Dict[str, Callable[[Any], bool]] = {
            "set_auto_send": self._set_auto_send,
            "set_sleeping": self._set_sleeping,
        }

    def execute(self, cmd_id: str, action: str, value: Any, response: str = "") -> bool:
        """
        Execute a wakeword command.

        Args:
            cmd_id: Command identifier (e.g., "auto_send_on")
            action: Method name to call (e.g., "set_auto_send")
            value: Value to pass to method
            response: Optional response message

        Returns:
            True if executed successfully
        """
        # Debug: check bridge status
        _debug(
            f"execute() called: cmd={cmd_id}, action={action}, value={value}, bridge={self.bridge is not None}"
        )

        # Cooldown check
        now_ms = time.time() * 1000
        elapsed = now_ms - self._last_exec_time
        if elapsed < self.cooldown_ms:
            print(f"[WAKEWORD] Cooldown: {elapsed:.0f}ms < {self.cooldown_ms}ms")
            return False

        # Find and execute action
        handler = self._action_map.get(action)
        if not handler:
            print(f"[WAKEWORD] Unknown action: {action}")
            return False

        try:
            success = handler(value)

            if success:
                self._last_exec_time = now_ms
                self._exec_count += 1
                print(
                    f"[WAKEWORD] Executed: {cmd_id} -> {action}({value}) "
                    f"(#{self._exec_count})"
                )

                # Emit command executed signal for visual feedback (bounce animation)
                if self.bridge and hasattr(self.bridge, "emit_command"):
                    self.bridge.emit_command(cmd_id, True)

            return success

        except Exception as e:
            print(f"[WAKEWORD] Error executing {cmd_id}: {e}")
            # Emit failure signal
            if self.bridge and hasattr(self.bridge, "emit_command"):
                self.bridge.emit_command(cmd_id, False)
            return False

    def _set_auto_send(self, enabled: bool) -> bool:
        """Set auto-send mode and notify UI."""
        if hasattr(self.app, "set_auto_send"):
            self.app.set_auto_send(enabled)

            # Notify UI to update checkbox
            if self.bridge and hasattr(self.bridge, "emit_setting_changed"):
                self.bridge.emit_setting_changed("auto_send", enabled)

            return True
        return False

    def _set_sleeping(self, sleeping: bool) -> bool:
        """Set sleeping mode and notify UI.

        Uses force_emit=True to ensure UI updates even if backend
        state was already in sync (fixes UI desync bugs).
        """
        _debug(
            f"_set_sleeping({sleeping}) called, has set_sleeping={hasattr(self.app, 'set_sleeping')}"
        )
        if hasattr(self.app, "set_sleeping"):
            self.app.set_sleeping(sleeping, force_emit=True)
            _debug(f"_set_sleeping({sleeping}) completed")
            return True
        return False

    def get_stats(self) -> Dict[str, Any]:
        """Get executor statistics."""
        return {
            "total_executions": self._exec_count,
            "cooldown_ms": self.cooldown_ms,
            "available_actions": list(self._action_map.keys()),
        }
