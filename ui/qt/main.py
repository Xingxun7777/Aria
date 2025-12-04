# main.py
# Qt frontend entry point for VoiceType
# Floating ball UI with mouse interactions

import sys
import signal
import atexit
import argparse
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QAction
from PySide6.QtCore import Qt, QTimer

from .bridge import QtBridge
from .floating_ball import FloatingBall
from .settings import SettingsWindow
from .sound import play_sound
from .history import HistoryWindow


def main():
    """Main entry point for Qt frontend with floating ball UI."""
    # Parse arguments
    parser = argparse.ArgumentParser(description="VoiceType Qt Frontend")
    parser.add_argument("--hotkey", default="grave", help="Hotkey for recording (default: grave/`)")
    parser.add_argument("--demo", action="store_true", help="Use mock backend for demo")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # Keep running with floating ball
    app.setApplicationName("VoiceType")

    # Create UI components
    bridge = QtBridge()
    ball = FloatingBall(size=48)
    settings = SettingsWindow()
    history = HistoryWindow()

    # Create minimal system tray for unlock and quit
    tray = QSystemTrayIcon()
    # Use fallback icon on Windows (fromTheme doesn't work reliably)
    icon = QIcon.fromTheme("audio-input-microphone")
    if icon.isNull():
        # Fallback: create a simple colored icon
        from PySide6.QtGui import QPixmap, QPainter, QBrush, QColor
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QBrush(QColor(100, 100, 255)))  # Blue circle
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(2, 2, 28, 28)
        painter.end()
        icon = QIcon(pixmap)
    tray.setIcon(icon)
    tray_menu = QMenu()

    action_unlock = QAction("Unlock Ball", None)
    action_unlock.triggered.connect(ball.unlock)
    tray_menu.addAction(action_unlock)

    action_mute = QAction("Mute Sound", None)
    action_mute.setCheckable(True)
    action_mute.setChecked(False)
    tray_menu.addAction(action_mute)

    tray_menu.addSeparator()

    action_quit = QAction("Quit", None)
    tray_menu.addAction(action_quit)

    tray.setContextMenu(tray_menu)
    tray.setToolTip("VoiceType - 单击显示历史，双击打开热词设置")
    tray.show()

    # Tray icon click handlers
    def on_tray_activated(reason):
        if reason == QSystemTrayIcon.Trigger:  # Single click
            # Show history popup near tray icon
            geo = tray.geometry()
            if geo.isValid():
                history.showAt(geo.center())
            else:
                # Fallback: show near cursor
                from PySide6.QtGui import QCursor
                history.showAt(QCursor.pos())
        elif reason == QSystemTrayIcon.DoubleClick:  # Double click
            # Open settings and navigate to hotwords tab (index 1)
            settings.show()
            settings.raise_()
            settings.activateWindow()
            settings.sidebar.setCurrentRow(1)  # Hotwords tab

    tray.activated.connect(on_tray_activated)

    # Connect signals: Bridge -> Ball
    bridge.stateChanged.connect(ball.on_state_changed)
    bridge.textUpdated.connect(ball.on_text_updated)
    bridge.insertComplete.connect(ball.on_insert_complete)
    bridge.voiceActivity.connect(ball.on_voice_activity)
    bridge.levelChanged.connect(ball.on_level_changed)  # Audio level for waveform
    bridge.error.connect(lambda msg: QMessageBox.warning(None, "VoiceType Error", msg))

    # Sound effects disabled - only hotkey press sounds in app.py
    # (start_recording beep and stop_recording beep)

    # Initialize backend
    backend = None

    if args.demo:
        # Demo mode with mock backend
        from .mock_backend import MockBackend
        backend = MockBackend(bridge)
        print("VoiceType Qt Frontend Started (Demo Mode - Floating Ball)")
    else:
        # Real backend
        try:
            from voicetype.app import VoiceTypeApp

            backend = VoiceTypeApp(hotkey=args.hotkey)
            backend.set_bridge(bridge)
            backend.start()
            print(f"VoiceType Qt Frontend Started (Hotkey: {args.hotkey})")
        except Exception as e:
            # Clean up any partially started resources
            if backend is not None and hasattr(backend, 'stop'):
                try:
                    backend.stop()
                except Exception:
                    pass  # Ignore cleanup errors

            QMessageBox.critical(
                None, "Startup Error",
                f"Failed to start VoiceType backend:\n{e}\n\nFalling back to demo mode."
            )
            from .mock_backend import MockBackend
            backend = MockBackend(bridge)

    # Connect ball actions
    ball.toggleRequested.connect(backend.toggle_recording)

    # Connect mute action to backend
    def on_mute_toggled():
        muted = action_mute.isChecked()
        if hasattr(backend, 'set_sound_enabled'):
            backend.set_sound_enabled(not muted)
        # Also mute UI sounds
        from .sound import get_sound_manager
        get_sound_manager().enabled = not muted
    action_mute.triggered.connect(on_mute_toggled)

    # Settings window: show and bring to front
    def show_settings():
        settings.show()
        settings.raise_()
        settings.activateWindow()

    ball.detailsRequested.connect(show_settings)

    # Handle enable toggle from popup menu
    def on_enable_toggled(enabled):
        print(f"[VoiceType] Enable toggled: {enabled}")
        if hasattr(backend, 'set_enabled'):
            backend.set_enabled(enabled)

    ball.enableToggled.connect(on_enable_toggled)

    # Handle mode change from popup menu
    def on_mode_changed(mode):
        print(f"[VoiceType] Polish mode changed: {mode}")
        if hasattr(backend, 'set_polish_mode'):
            backend.set_polish_mode(mode)
        # Sync settings window
        settings.set_polish_mode(mode)

    ball.modeChanged.connect(on_mode_changed)

    # Sync initial mode from backend to popup menu
    if hasattr(backend, 'get_polish_mode'):
        initial_mode = backend.get_polish_mode()
        ball.set_polish_mode(initial_mode)
        print(f"[VoiceType] Initial polish mode: {initial_mode}")

    def cleanup_and_quit():
        """Cleanup backend before quitting."""
        print("[VoiceType] Cleaning up and quitting...")
        # Hide tray icon first to prevent ghost icons on Windows
        tray.hide()
        if hasattr(backend, 'stop'):
            backend.stop()
        app.quit()

    action_quit.triggered.connect(cleanup_and_quit)

    # Register cleanup for signal handling and atexit
    def signal_handler(signum, frame):
        print(f"[VoiceType] Received signal {signum}, cleaning up...")
        cleanup_and_quit()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    atexit.register(lambda: backend.stop() if hasattr(backend, 'stop') else None)

    # Settings saved -> reload backend config and sync popup menu
    def on_settings_saved(config):
        if hasattr(backend, 'reload_config'):
            backend.reload_config()

        # Sync hotkey if changed
        general = config.get('general', {})
        saved_hotkey = general.get('hotkey', '')
        if saved_hotkey and hasattr(backend, 'set_hotkey'):
            # Convert Qt key sequence format to hotkey format if needed
            hotkey_lower = saved_hotkey.lower().replace(' ', '')
            backend.set_hotkey(hotkey_lower)

        # Sync popup menu with saved mode
        saved_mode = config.get('polish_mode', 'fast')
        ball.set_polish_mode(saved_mode)
        print(f"[VoiceType] Settings saved, polish mode synced: {saved_mode}")

    settings.settingsSaved.connect(on_settings_saved)

    # Show floating ball
    ball.show()

    print("VoiceType Floating Ball is now visible.")
    print("  - Left-click: Show popup menu")
    print("  - Double-click: Open settings")
    print("  - Middle-click: Toggle recording")
    print("  - Right-click: Lock position")
    print("  - Drag: Move ball (when unlocked)")
    print("  - System tray single-click: Show history (Ctrl+1-9 to copy)")
    print("  - System tray double-click: Open hotwords settings")

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
