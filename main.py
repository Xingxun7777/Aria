"""
VoiceType - Main Entry Point
============================
Local AI Voice Dictation + Smart Completion Tool
"""

import sys
import signal
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from voicetype.config import get_settings
from voicetype.core.logging import setup_logging, get_logger
from voicetype.system.hotkey import HotkeyManager
from voicetype.scheduler.task import UtteranceTask, TaskState
from voicetype.scheduler.queue import TaskQueue


def main():
    """Main entry point for VoiceType."""
    # Setup logging
    logger = setup_logging(name="voicetype")
    logger.info("VoiceType starting...")

    # Load settings
    settings = get_settings()
    logger.info(f"Config loaded (version {settings.version})")

    # Initialize components
    task_queue = TaskQueue()
    hotkey_manager = HotkeyManager()

    # State
    is_recording = False
    current_task = None

    def on_voice_trigger():
        """Handle voice trigger hotkey."""
        nonlocal is_recording, current_task

        if not is_recording:
            # Start recording
            current_task = UtteranceTask()
            task_queue.add(current_task)
            current_task.start_recording()
            is_recording = True
            logger.info(f"Recording started (task {current_task.id})")
        else:
            # Stop recording
            if current_task:
                # In real implementation, would pass actual audio data
                current_task.stop_recording(b"", 0)
                logger.info(f"Recording stopped (task {current_task.id})")
            is_recording = False
            current_task = None

    def on_cancel():
        """Handle cancel hotkey."""
        nonlocal is_recording, current_task
        if current_task:
            current_task.cancel()
            logger.info(f"Task cancelled (task {current_task.id})")
        is_recording = False
        current_task = None

    # Register hotkeys
    try:
        hotkey_manager.register(
            settings.hotkeys.voice_trigger,
            on_voice_trigger,
            "Voice trigger"
        )
        hotkey_manager.register(
            settings.hotkeys.cancel,
            on_cancel,
            "Cancel"
        )
    except RuntimeError as e:
        logger.error(f"Failed to register hotkeys: {e}")
        return 1

    # Start hotkey listener
    hotkey_manager.start()
    logger.info("Hotkey listener started")
    logger.info(f"Press {settings.hotkeys.voice_trigger} to trigger voice input")
    logger.info(f"Press {settings.hotkeys.cancel} to cancel")

    # Handle graceful shutdown
    def signal_handler(sig, frame):
        logger.info("Shutdown signal received")
        hotkey_manager.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Main loop (in real app, would have Qt event loop)
    logger.info("VoiceType running. Press Ctrl+C to exit.")
    try:
        while True:
            import time
            time.sleep(0.1)

            # Cleanup old tasks periodically
            task_queue.cleanup_completed()
    except KeyboardInterrupt:
        pass
    finally:
        hotkey_manager.stop()
        logger.info("VoiceType stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
