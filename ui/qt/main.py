# main.py
# Qt frontend entry point for Aria
# Floating ball UI with mouse interactions

import sys
import signal
import atexit
import argparse
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QAction, QClipboard
from PySide6.QtCore import Qt, QTimer, QThreadPool

from .bridge import QtBridge
from .floating_ball import FloatingBall
from .settings import SettingsWindow
from .sound import play_sound
from .history import HistoryWindow
from .translation_popup import TranslationPopup
from .ai_chat_window import AIChatWindow
from .workers import TranslationWorker, SummaryWorker, LLMWorker
from .elevation_dialog import ElevationWarningDialog

# Debug log for main.py
_DEBUG_LOG = Path(__file__).parent.parent.parent / "DebugLog" / "wakeword_debug.log"


def _log(msg: str):
    """Write debug message to shared log file (pythonw.exe safe)."""
    import datetime
    import sys

    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}\n"
    # Guard for pythonw.exe (sys.stdout is None)
    if sys.stdout is not None:
        print(line.strip())
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def main():
    """Main entry point for Qt frontend with floating ball UI."""
    # Parse arguments
    parser = argparse.ArgumentParser(description="Aria Qt Frontend")
    parser.add_argument(
        "--hotkey", default="grave", help="Hotkey for recording (default: grave/`)"
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # Keep running with floating ball
    app.setApplicationName("Aria")

    # Create UI components
    bridge = QtBridge()
    ball = FloatingBall(size=48)
    settings = SettingsWindow()
    history = HistoryWindow()
    translation_popup = TranslationPopup()
    summary_popup = TranslationPopup()
    ai_chat_window = AIChatWindow()
    elevation_dialog = ElevationWarningDialog()

    # Thread pool for background workers
    thread_pool = QThreadPool.globalInstance()

    # Container to keep signal objects alive until delivery
    # (QRunnable with autoDelete=True can delete signals before delivery)
    _active_signals = []
    _active_dialogs = []
    _quit_in_progress = False

    # Create minimal system tray for unlock and quit
    tray = QSystemTrayIcon()
    # Custom tray icon (don't rely on fromTheme - unreliable on Windows)
    from PySide6.QtGui import QPixmap, QPainter, QBrush, QColor, QPen, QLinearGradient

    def create_tray_icon():
        """Create a black-orange Aria tray icon."""
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        # Dark circle background
        painter.setBrush(QBrush(QColor(30, 30, 35, 240)))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(2, 2, 28, 28)

        # Orange border
        painter.setPen(QPen(QColor("#ff8c00"), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(3, 3, 26, 26)

        # Orange sound wave bars (3 bars)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#ff8c00")))
        # Left bar
        painter.drawRoundedRect(9, 12, 3, 8, 1, 1)
        # Center bar (taller)
        painter.drawRoundedRect(14, 9, 3, 14, 1, 1)
        # Right bar
        painter.drawRoundedRect(19, 12, 3, 8, 1, 1)

        painter.end()
        return QIcon(pixmap)

    tray.setIcon(create_tray_icon())
    tray_menu = QMenu()

    action_unlock = QAction("解锁悬浮球", None)
    action_unlock.triggered.connect(ball.unlock)
    tray_menu.addAction(action_unlock)

    action_mute = QAction("静音", None)
    action_mute.setCheckable(True)
    action_mute.setChecked(False)
    tray_menu.addAction(action_mute)

    action_auto_send = QAction("自动发送", None)
    action_auto_send.setCheckable(True)
    action_auto_send.setChecked(False)
    tray_menu.addAction(action_auto_send)

    tray_menu.addSeparator()

    action_settings = QAction("高级设置", None)
    tray_menu.addAction(action_settings)

    tray_menu.addSeparator()

    action_quit = QAction("退出", None)
    tray_menu.addAction(action_quit)

    tray.setContextMenu(tray_menu)
    tray.setToolTip("Aria - 单击显示历史，双击打开热词设置")
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
    bridge.commandExecuted.connect(ball.on_command_executed)  # Voice command feedback
    bridge.highlightSaved.connect(
        ball.on_highlight_saved
    )  # Gold flash for highlight save

    def show_error_dialog(msg: str) -> None:
        """Show non-blocking error dialog to avoid trapping the event loop."""
        try:
            box = QMessageBox(QMessageBox.Warning, "Aria Error", msg)
            box.setWindowModality(Qt.NonModal)
            box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            box.setAttribute(Qt.WA_DeleteOnClose, True)
            _active_dialogs.append(box)

            def _cleanup_dialog(_result):
                if box in _active_dialogs:
                    _active_dialogs.remove(box)

            box.finished.connect(_cleanup_dialog)
            box.show()
            box.raise_()
            box.activateWindow()
        except Exception as e:
            _log(f"[UI] Failed to show error dialog: {e}")

        # Also show tray notification if available
        try:
            tray.showMessage(
                "Aria 错误", msg, QSystemTrayIcon.MessageIcon.Warning, 3000
            )
        except Exception:
            pass

    def _is_elevation_error(msg: str) -> bool:
        """Check if the error message is related to elevation/permission issues."""
        elevation_keywords = ["权限", "管理员", "elevated", "elevation", "Aria 没有"]
        msg_lower = msg.lower()
        return any(kw.lower() in msg_lower for kw in elevation_keywords)

    def _is_hotkey_conflict(msg: str) -> bool:
        """Check if the error message is about hotkey conflict."""
        return "already in use" in msg.lower() or (
            "hotkey" in msg.lower() and "failed" in msg.lower()
        )

    def on_error(msg: str) -> None:
        """Handle errors - route appropriately based on error type."""
        if _is_elevation_error(msg):
            _log(f"[UI] Elevation warning detected, showing elevation dialog")
            elevation_dialog.show_warning(msg)
        elif _is_hotkey_conflict(msg):
            # Hotkey conflict: just log and set tooltip, no popup
            _log(f"[UI] Hotkey conflict (no popup): {msg}")
            print(f"[Aria] 快捷键冲突: {msg}")
            print(f"[Aria] 提示: 快捷键被占用，可点击悬浮窗手动启用语音输入")
            # Set tooltip on floating ball
            ball.setToolTip(f"⚠ 快捷键被占用\n点击悬浮窗启用语音输入")
        else:
            show_error_dialog(msg)

    bridge.error.connect(on_error)

    # Elevation dialog signal handlers
    def on_elevation_close_requested():
        """Handle user clicking 'Close Aria' in elevation dialog."""
        _log("[UI] User requested to close Aria from elevation dialog")
        cleanup_and_quit()

    def on_elevation_restart_admin():
        """Handle user clicking 'Restart as Admin' in elevation dialog."""
        _log("[UI] User requested admin restart from elevation dialog")
        try:
            from aria.system.admin import restart_as_admin

            if restart_as_admin():
                _log("[UI] Admin restart successful, exiting current instance")
                # Give the new process time to start before we exit
                QTimer.singleShot(500, cleanup_and_quit)
            else:
                _log("[UI] Admin restart failed or cancelled by user")
                # Show a brief notification - don't show another dialog
                tray.showMessage(
                    "Aria",
                    "管理员重启已取消或失败",
                    QSystemTrayIcon.MessageIcon.Information,
                    2000,
                )
        except Exception as e:
            _log(f"[UI] Exception during admin restart: {e}")
            show_error_dialog(f"重启失败: {e}")

    # Connect elevation dialog signals (connected later after cleanup_and_quit is defined)

    # Handle setting changes from backend (e.g., via wakeword commands)
    def on_setting_changed(setting: str, value: bool):
        # Write to debug log file
        from pathlib import Path
        import datetime

        log_path = (
            Path(__file__).parent.parent.parent / "DebugLog" / "wakeword_debug.log"
        )
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [MAIN] on_setting_changed received: {setting} = {value}\n")
        _log(f"[UI] Setting changed via wakeword: {setting} = {value}")
        if setting == "auto_send":
            action_auto_send.setChecked(value)
            ball.set_auto_send(value)  # Update floating ball color indicator
        elif setting == "mute" or setting == "sound_enabled":
            # sound_enabled=False means mute=True
            action_mute.setChecked(not value if setting == "sound_enabled" else value)
        elif setting == "sleeping":
            # Update popup menu's exit sleeping button visibility
            ball.set_sleeping_state(value)
            # Also update ball visual state as fallback
            # (in case stateChanged signal was missed/reordered)
            ball.on_state_changed("SLEEPING" if value else "IDLE")
        elif setting == "enabled":
            # Update popup menu toggle when hotkey re-enables from disabled state
            if ball._popup_menu:
                ball._popup_menu.setEnabled(value)

    bridge.settingChanged.connect(on_setting_changed)

    # Sound effects disabled - only hotkey press sounds in app.py
    # (start_recording beep and stop_recording beep)

    # Initialize backend
    backend = None

    try:
        from aria.app import AriaApp
        import json
        from aria.core.utils import get_config_path

        # Read hotkey from config (before creating backend)
        config_path = get_config_path("hotwords.json")
        actual_hotkey = args.hotkey  # fallback to command line arg
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            config_hotkey = config.get("general", {}).get("hotkey", "")
            if config_hotkey:
                actual_hotkey = config_hotkey.lower()
                _log(f"[Aria] Using hotkey from config: {actual_hotkey}")
        except Exception as e:
            _log(f"[Aria] Could not read hotkey from config: {e}")

        backend = AriaApp(hotkey=actual_hotkey)
        backend.set_bridge(bridge)
        backend.start()
        _log(f"Aria Qt Frontend Started (Hotkey: {actual_hotkey})")

        # Check start_active setting - if False, disable hotkey listening
        # (reuse config already loaded above)
        try:
            start_active = config.get("general", {}).get("start_active", True)
            if not start_active:
                # Enter sleeping mode (UI shows dimmed, wakeword still works)
                backend.set_sleeping(True)
                _log("[Aria] Started in sleeping mode (start_active=False)")
            else:
                # CRITICAL FIX: Explicitly ensure system is fully active
                # Issue: PopupMenu emits enableToggled(True) during __init__,
                # but main.py connects the handler AFTER ball is created.
                # This means backend.set_enabled(True) is never called!
                # Fix: Explicitly enable and sync all states after event loop starts.
                def _ensure_active_state():
                    _log("[STARTUP] _ensure_active_state() running...")
                    # 1. Ensure backend hotkey is enabled
                    if hasattr(backend, "set_enabled"):
                        backend.set_enabled(True)
                        _log("[STARTUP] backend.set_enabled(True) called")
                    # 2. Ensure not sleeping
                    if hasattr(backend, "set_sleeping"):
                        backend.set_sleeping(False)
                        _log("[STARTUP] backend.set_sleeping(False) called")
                    # 3. Sync UI state
                    bridge.emit_state("IDLE")
                    ball.set_sleeping_state(False)
                    _log("[STARTUP] UI state synced to IDLE")
                    # 4. Sync UI toggle state (don't trigger signal, just update visual)
                    #    NOTE: Do NOT toggle False→True as it calls stop() which
                    #    unregisters hotkeys that start() won't re-register!
                    if ball._popup_menu and hasattr(ball._popup_menu, "toggle"):
                        ball._popup_menu.toggle.blockSignals(True)
                        ball._popup_menu.toggle.setChecked(True)
                        ball._popup_menu.toggle.blockSignals(False)
                        _log("[STARTUP] Toggle switch synced to ON")
                    _log("[Aria] System fully activated (start_active=True)")
                    _log("[STARTUP] System fully activated!")

                def _auto_start_recording():
                    """Auto-start recording after system is ready."""
                    _log("[STARTUP] Auto-starting recording...")
                    if hasattr(backend, "toggle_recording"):
                        backend.toggle_recording()
                        _log("[STARTUP] Recording started automatically!")
                        _log("[Aria] Recording started automatically")

                # Use 500ms delay to ensure all components are ready
                # (100ms was sometimes too short on slower machines)
                QTimer.singleShot(500, _ensure_active_state)
                # Auto-start recording 200ms after activation
                QTimer.singleShot(700, _auto_start_recording)
                _log("[Aria] Started in active mode (start_active=True)")
        except Exception as e:
            _log(f"[Aria] Could not read start_active setting: {e}")
            # Default: emit IDLE state after event loop starts
            QTimer.singleShot(100, lambda: bridge.emit_state("IDLE"))
    except Exception as e:
        # Clean up any partially started resources
        if backend is not None and hasattr(backend, "stop"):
            try:
                backend.stop()
            except Exception:
                pass  # Ignore cleanup errors

        QMessageBox.critical(
            None,
            "Startup Error",
            f"Aria 启动失败:\n{e}\n\n请使用 Aria_debug.bat 查看详细错误信息。",
        )
        sys.exit(1)

    # Connect ball actions
    ball.toggleRequested.connect(backend.toggle_recording)

    # =========================================================================
    # v1.1: Action-driven UI handling (Translation Popup, AI Chat)
    # =========================================================================

    def on_action_triggered(action):
        """Handle UI actions from backend."""
        # CRITICAL: Must use aria.core.action (not core.action) to match
        # the module identity used by executor.py. Otherwise enum comparison fails.
        from aria.core.action import (
            ActionType,
            TranslationAction,
            SummaryAction,
            ChatAction,
            ClipboardTranslationAction,
        )

        _log(f"[MAIN] on_action_triggered: {action.type}, id={action.request_id}")
        _log(
            f"[MAIN] action.type value: {action.type.value if hasattr(action.type, 'value') else action.type}"
        )
        _log(f"[MAIN] ActionType.SHOW_TRANSLATION: {ActionType.SHOW_TRANSLATION}")
        _log(f"[MAIN] Type match check: {action.type == ActionType.SHOW_TRANSLATION}")
        _log(
            f"[MAIN] Type name check: {action.type.name if hasattr(action.type, 'name') else 'N/A'}"
        )
        _log(f"[UI] Action triggered: {action.type}, id={action.request_id}")

        if action.type == ActionType.SHOW_TRANSLATION:
            try:
                # Show translation popup with loading state
                _log(
                    f"[MAIN] Calling show_loading with {len(action.source_text)} chars"
                )
                # RAW DEBUG: Write directly to file before calling
                try:
                    from pathlib import Path

                    _raw_log = (
                        Path(__file__).parent.parent.parent
                        / "DebugLog"
                        / "wakeword_debug.log"
                    )
                    with open(_raw_log, "a", encoding="utf-8") as f:
                        f.write(
                            f"[RAW] MAIN: About to call translation_popup.show_loading()\n"
                        )
                except Exception:
                    pass
                translation_popup.show_loading(action.source_text, action.request_id)
                _log("[MAIN] show_loading called OK")

                # Get API config from backend's polisher (reuse existing config)
                api_url = ""
                api_key = ""
                model = "google/gemini-2.5-flash-lite-preview-09-2025"

                if hasattr(backend, "polisher") and backend.polisher:
                    api_url = backend.polisher.config.api_url
                    api_key = backend.polisher.config.api_key
                    model = backend.polisher.config.model
                    _log(f"[MAIN] Got API config: url={api_url[:30]}..., model={model}")

                if not api_url or not api_key:
                    _log("[MAIN] ERROR: API not configured")
                    translation_popup.show_error("API 未配置", action.request_id)
                    return

                # Create and start translation worker
                _log("[MAIN] Creating TranslationWorker...")
                worker = TranslationWorker(
                    request_id=action.request_id,
                    source_text=action.source_text,
                    api_url=api_url,
                    api_key=api_key,
                    model=model,
                    source_lang=action.source_lang,
                    target_lang=action.target_lang,
                )
                # CRITICAL: Keep signals reference alive until delivery
                # (QRunnable autoDelete can destroy signals before async delivery)
                signals_ref = worker.signals
                _active_signals.append(signals_ref)

                def cleanup_signals(sig_ref):
                    """Remove signals reference after delivery."""
                    if sig_ref in _active_signals:
                        _active_signals.remove(sig_ref)
                        _log(
                            f"[MAIN] Cleaned up signals ref, remaining: {len(_active_signals)}"
                        )

                signals_ref.finished.connect(on_translation_finished)
                signals_ref.finished.connect(lambda *_: cleanup_signals(signals_ref))
                signals_ref.error.connect(on_translation_error)
                signals_ref.error.connect(lambda *_: cleanup_signals(signals_ref))
                thread_pool.start(worker)
                _log(
                    f"[MAIN] TranslationWorker started, active_signals: {len(_active_signals)}"
                )
            except Exception as e:
                _log(f"[MAIN] ERROR in SHOW_TRANSLATION: {e}")
                import traceback

                _log(traceback.format_exc())

        elif action.type == ActionType.SHOW_SUMMARY:
            try:
                _log(f"[MAIN] Summary popup: {len(action.source_text)} chars")
                summary_popup.show_loading(
                    action.source_text,
                    action.request_id,
                    title_prefix="总结",
                    title_done="摘要",
                    loading_text="正在总结...",
                    error_prefix="总结失败",
                )

                api_url = ""
                api_key = ""
                model = "google/gemini-2.5-flash-lite-preview-09-2025"

                if hasattr(backend, "polisher") and backend.polisher:
                    api_url = backend.polisher.config.api_url
                    api_key = backend.polisher.config.api_key
                    model = backend.polisher.config.model
                    _log(f"[MAIN] Got API config: url={api_url[:30]}..., model={model}")

                if not api_url or not api_key:
                    _log("[MAIN] ERROR: API not configured for summary")
                    summary_popup.show_error("API 未配置", action.request_id)
                    return

                worker = SummaryWorker(
                    request_id=action.request_id,
                    source_text=action.source_text,
                    api_url=api_url,
                    api_key=api_key,
                    model=model,
                )
                signals_ref = worker.signals
                _active_signals.append(signals_ref)

                def cleanup_summary_signals(sig_ref):
                    if sig_ref in _active_signals:
                        _active_signals.remove(sig_ref)

                signals_ref.finished.connect(on_summary_finished)
                signals_ref.finished.connect(
                    lambda *_: cleanup_summary_signals(signals_ref)
                )
                signals_ref.error.connect(on_summary_error)
                signals_ref.error.connect(
                    lambda *_: cleanup_summary_signals(signals_ref)
                )
                thread_pool.start(worker)
                _log("[MAIN] SummaryWorker started")
            except Exception as e:
                _log(f"[MAIN] ERROR in SHOW_SUMMARY: {e}")
                import traceback

                _log(traceback.format_exc())

        elif action.type == ActionType.OPEN_CHAT:
            # Show AI chat window with context
            ai_chat_window.show_with_context(
                context_text=action.context_text,
                request_id=action.request_id,
                initial_question=action.initial_question,
            )
            _log(
                f"[UI] ChatAction: opened chat window with {len(action.context_text)} chars"
            )

        elif action.type == ActionType.CLIPBOARD_TRANSLATION:
            # Clipboard translation: translate and copy to clipboard, show notification
            try:
                _log(
                    f"[MAIN] ClipboardTranslation: {len(action.source_text)} chars -> {action.target_lang}"
                )

                # Get API config from backend's polisher
                api_url = ""
                api_key = ""
                model = "google/gemini-2.5-flash-lite-preview-09-2025"

                if hasattr(backend, "polisher") and backend.polisher:
                    api_url = backend.polisher.config.api_url
                    api_key = backend.polisher.config.api_key
                    model = backend.polisher.config.model

                if not api_url or not api_key:
                    _log("[MAIN] ERROR: API not configured for clipboard translation")
                    tray.showMessage(
                        "Aria", "API 未配置", QSystemTrayIcon.Warning, 2000
                    )
                    return

                # Create and start translation worker
                worker = TranslationWorker(
                    request_id=action.request_id,
                    source_text=action.source_text,
                    api_url=api_url,
                    api_key=api_key,
                    model=model,
                    source_lang="auto",
                    target_lang=action.target_lang,
                )
                # CRITICAL: Keep signals reference alive until delivery
                signals_ref = worker.signals
                _active_signals.append(signals_ref)

                def cleanup_clipboard_signals(sig_ref):
                    if sig_ref in _active_signals:
                        _active_signals.remove(sig_ref)

                signals_ref.finished.connect(on_clipboard_translation_finished)
                signals_ref.finished.connect(
                    lambda *_: cleanup_clipboard_signals(signals_ref)
                )
                signals_ref.error.connect(on_clipboard_translation_error)
                signals_ref.error.connect(
                    lambda *_: cleanup_clipboard_signals(signals_ref)
                )
                thread_pool.start(worker)
                _log("[MAIN] Clipboard TranslationWorker started")
            except Exception as e:
                _log(f"[MAIN] ERROR in CLIPBOARD_TRANSLATION: {e}")
                import traceback

                _log(traceback.format_exc())

    def on_clipboard_translation_finished(request_id: str, translated_text: str):
        """Handle clipboard translation completion."""
        try:
            # Use Qt's clipboard (always available) instead of pyperclip
            clipboard = QApplication.clipboard()
            clipboard.setText(translated_text)
            _log(
                f"[UI] Clipboard translation finished: {len(translated_text)} chars copied"
            )
            tray.showMessage(
                "Aria", "已复制到剪切板", QSystemTrayIcon.Information, 2000
            )
        except Exception as e:
            _log(f"[UI] Failed to copy to clipboard: {e}")
            tray.showMessage("Aria", f"复制失败: {e}", QSystemTrayIcon.Warning, 2000)

    def on_clipboard_translation_error(request_id: str, error_msg: str):
        """Handle clipboard translation error."""
        _log(f"[UI] Clipboard translation error: {error_msg}")
        tray.showMessage(
            "Aria", f"翻译失败: {error_msg}", QSystemTrayIcon.Warning, 3000
        )

    def on_translation_finished(request_id: str, translated_text: str):
        """Handle translation completion."""
        _log(
            f"[UI] Translation finished CALLBACK: request_id={request_id}, text_len={len(translated_text)}"
        )
        try:
            translation_popup.show_result(translated_text, request_id)
            _log(f"[UI] Translation show_result completed OK")
        except Exception as e:
            _log(f"[UI] Translation show_result ERROR: {e}")
            import traceback

            _log(f"[UI] TRACEBACK: {traceback.format_exc()}")

    def on_translation_error(request_id: str, error_msg: str):
        """Handle translation error."""
        _log(
            f"[UI] Translation error CALLBACK: request_id={request_id}, error={error_msg}"
        )
        try:
            translation_popup.show_error(error_msg, request_id)
            _log(f"[UI] Translation show_error completed OK")
        except Exception as e:
            _log(f"[UI] Translation show_error ERROR: {e}")
            import traceback

            _log(f"[UI] TRACEBACK: {traceback.format_exc()}")

    def on_summary_finished(request_id: str, summary_text: str):
        """Handle summary completion."""
        _log(
            f"[UI] Summary finished CALLBACK: request_id={request_id}, text_len={len(summary_text)}"
        )
        try:
            summary_popup.show_result(summary_text, request_id)
            _log(f"[UI] Summary show_result completed OK")
        except Exception as e:
            _log(f"[UI] Summary show_result ERROR: {e}")
            import traceback

            _log(f"[UI] TRACEBACK: {traceback.format_exc()}")

    def on_summary_error(request_id: str, error_msg: str):
        """Handle summary error."""
        _log(f"[UI] Summary error CALLBACK: request_id={request_id}, error={error_msg}")
        try:
            summary_popup.show_error(error_msg, request_id)
            _log(f"[UI] Summary show_error completed OK")
        except Exception as e:
            _log(f"[UI] Summary show_error ERROR: {e}")
            import traceback

            _log(f"[UI] TRACEBACK: {traceback.format_exc()}")

    def on_copy_translation(text: str):
        """Handle copy request from translation popup."""
        clipboard = QApplication.clipboard()
        clipboard.setText(text)
        _log(f"[UI] Translation copied to clipboard: {text[:50]}...")

    # =========================================================================
    # AI Chat LLM handling
    # =========================================================================

    def on_chat_send_message():
        """Handle send button click in chat window - start LLM worker."""
        # Get API config from backend's polisher
        api_url = ""
        api_key = ""
        model = "google/gemini-2.5-flash-lite-preview-09-2025"

        if hasattr(backend, "polisher") and backend.polisher:
            api_url = backend.polisher.config.api_url
            api_key = backend.polisher.config.api_key
            model = backend.polisher.config.model

        if not api_url or not api_key:
            ai_chat_window.show_error("API 未配置")
            return

        # Get conversation and context from chat window
        messages = ai_chat_window.get_conversation()
        context = ai_chat_window.get_context()
        request_id = ai_chat_window._request_id or "chat"

        # Create and start LLM worker
        worker = LLMWorker(
            request_id=request_id,
            messages=messages,
            context_text=context,
            api_url=api_url,
            api_key=api_key,
            model=model,
            stream=True,
        )
        worker.signals.streamUpdate.connect(on_chat_stream_update)
        worker.signals.finished.connect(on_chat_finished)
        worker.signals.error.connect(on_chat_error)
        thread_pool.start(worker)

    def on_chat_stream_update(request_id: str, partial_content: str):
        """Handle streaming update from LLM."""
        ai_chat_window.update_response(partial_content, is_final=False)

    def on_chat_finished(request_id: str, final_content: str):
        """Handle LLM completion."""
        _log(f"[UI] Chat finished: {len(final_content)} chars")
        ai_chat_window.update_response(final_content, is_final=True)

    def on_chat_error(request_id: str, error_msg: str):
        """Handle LLM error."""
        _log(f"[UI] Chat error: {error_msg}")
        ai_chat_window.show_error(error_msg)

    def on_chat_insert_requested(text: str):
        """Handle insert request from chat window."""
        if hasattr(backend, "output_injector"):
            backend.output_injector.insert_text(text)
            _log(f"[UI] Chat response inserted: {text[:50]}...")

    # Connect chat window signals
    def on_chat_send_wrapper():
        """Wrapper to handle send: add bubble then start LLM."""
        ai_chat_window._on_send_clicked()  # Original handler (adds message bubble)
        on_chat_send_message()  # Start LLM worker

    ai_chat_window._send_btn.clicked.disconnect()  # Disconnect default
    ai_chat_window._send_btn.clicked.connect(on_chat_send_wrapper)
    ai_chat_window.insertRequested.connect(on_chat_insert_requested)

    # Connect action signals
    bridge.actionTriggered.connect(on_action_triggered)
    translation_popup.copyRequested.connect(on_copy_translation)
    summary_popup.copyRequested.connect(on_copy_translation)

    # Connect mute action to backend
    def on_mute_toggled():
        muted = action_mute.isChecked()
        if hasattr(backend, "set_sound_enabled"):
            backend.set_sound_enabled(not muted)
        # Also mute UI sounds
        from .sound import get_sound_manager

        get_sound_manager().enabled = not muted

    action_mute.triggered.connect(on_mute_toggled)

    # Connect auto-send action to backend
    def on_auto_send_toggled():
        enabled = action_auto_send.isChecked()
        if hasattr(backend, "set_auto_send"):
            backend.set_auto_send(enabled)
        ball.set_auto_send(enabled)  # Update floating ball color indicator

    action_auto_send.triggered.connect(on_auto_send_toggled)

    # Settings window: show and bring to front
    def show_settings():
        settings.show()
        settings.raise_()
        settings.activateWindow()

    ball.detailsRequested.connect(show_settings)
    action_settings.triggered.connect(show_settings)  # Tray menu -> settings

    # Handle enable toggle from popup menu
    def on_enable_toggled(enabled):
        _log(f"[Aria] Enable toggled: {enabled}")
        if hasattr(backend, "set_enabled"):
            backend.set_enabled(enabled)

    ball.enableToggled.connect(on_enable_toggled)

    # Handle mode change from popup menu
    def on_mode_changed(mode):
        _log(f"[Aria] Polish mode changed: {mode}")
        if hasattr(backend, "set_polish_mode"):
            backend.set_polish_mode(mode)
        # Sync settings window
        settings.set_polish_mode(mode)

    ball.modeChanged.connect(on_mode_changed)

    # Handle sleep toggle from popup menu (fallback button)
    def on_sleep_toggled(sleeping):
        _log(f"[Aria] Sleep toggled via UI: {sleeping}")
        if hasattr(backend, "set_sleeping"):
            backend.set_sleeping(sleeping)

    if ball._popup_menu:
        ball._popup_menu.sleepToggled.connect(on_sleep_toggled)

        # Handle translate output mode change from popup menu
        def on_translate_mode_changed(mode):
            """Handle translation output mode change from popup menu."""
            _log(f"[Aria] Translate output mode changed: {mode}")
            try:
                import json
                from aria.core.utils import get_config_path

                config_path = get_config_path("hotwords.json")
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)

                # Update translation config
                if "translation" not in config:
                    config["translation"] = {}
                config["translation"]["output_mode"] = mode

                import os

                tmp_path = str(config_path) + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(config, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, config_path)

                _log(f"[Aria] Translate output mode saved: {mode}")
                tray.showMessage(
                    "Aria",
                    f"翻译输出模式: {'弹窗显示' if mode == 'popup' else '复制到剪贴板'}",
                    QSystemTrayIcon.MessageIcon.Information,
                    1500,
                )
            except Exception as e:
                _log(f"[Aria] Failed to save translate mode: {e}")

        ball._popup_menu.translateModeChanged.connect(on_translate_mode_changed)

        # Load and sync initial translate mode
        try:
            import json
            from aria.core.utils import get_config_path

            config_path = get_config_path("hotwords.json")
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            translate_mode = config.get("translation", {}).get("output_mode", "popup")
            ball._popup_menu.setTranslateMode(translate_mode)
        except Exception:
            pass  # Default to popup mode

    # Sync initial mode from backend to popup menu
    if hasattr(backend, "get_polish_mode"):
        initial_mode = backend.get_polish_mode()
        ball.set_polish_mode(initial_mode)
        _log(f"[Aria] Initial polish mode: {initial_mode}")

    # Set engine info on floating ball for popup display
    def _get_engine_display_name() -> str:
        """Get human-readable engine name from config."""
        try:
            import json
            from aria.core.utils import get_config_path

            config_path = get_config_path("hotwords.json")
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)

            engine = cfg.get("asr_engine", "funasr")

            if engine == "funasr":
                model = cfg.get("funasr", {}).get("model_name", "paraformer-zh")
                if "sensevoice" in model.lower():
                    return "FunASR (SenseVoice)"
                return "FunASR (Paraformer)"
            elif engine == "whisper":
                model = cfg.get("whisper", {}).get("model_name", "large-v3-turbo")
                return f"Whisper ({model})"
            elif engine == "qwen3":
                model = cfg.get("qwen3", {}).get("model_name", "Qwen/Qwen3-ASR-1.7B")
                short = "1.7B" if "1.7B" in model else "0.6B"
                return f"Qwen3-ASR ({short})"
            else:
                return engine.upper()
        except Exception as e:
            _log(f"[Aria] Failed to get engine display name: {e}")
            return "Unknown"

    engine_name = _get_engine_display_name()
    ball.set_engine_info(engine_name)
    _log(f"[Aria] Engine info set: {engine_name}")

    def cleanup_and_quit():
        """Cleanup backend before quitting."""
        import threading
        import os
        import time
        from PySide6.QtCore import QCoreApplication

        nonlocal _quit_in_progress
        if _quit_in_progress:
            return
        _quit_in_progress = True
        _log("[Aria] Cleaning up and quitting...")

        # Step 1: Hide tray icon first to prevent ghost icons on Windows
        try:
            tray.hide()
        except Exception as e:
            _log(f"[Aria] Tray hide error (ignored): {e}")

        # Step 2: Close any active dialogs and windows to avoid modal traps
        try:
            for dlg in list(_active_dialogs):
                try:
                    dlg.close()
                except Exception:
                    pass
            _active_dialogs.clear()
        except Exception as e:
            _log(f"[Aria] Dialog cleanup error (ignored): {e}")

        try:
            for w in app.topLevelWidgets():
                try:
                    w.close()
                except Exception:
                    pass
        except Exception as e:
            _log(f"[Aria] Window cleanup error (ignored): {e}")

        # Step 3: Stop backend (ASR, audio capture, hotkey listener)
        if hasattr(backend, "stop"):
            try:
                backend.stop()
                _log("[Aria] Backend stopped successfully")
            except Exception as e:
                _log(f"[Aria] Backend stop error: {e}")

        # Step 4: Wait briefly for threads to terminate
        time.sleep(0.3)

        # Step 5: Check for remaining non-daemon threads
        remaining = [
            t for t in threading.enumerate() if not t.daemon and t.name != "MainThread"
        ]
        if remaining:
            _log(
                f"[Aria] Warning: {len(remaining)} non-daemon threads still running: {[t.name for t in remaining]}"
            )

        # Step 6: Try to drain thread pool tasks
        try:
            if thread_pool:
                thread_pool.waitForDone(1000)
        except Exception as e:
            _log(f"[Aria] Thread pool wait error (ignored): {e}")

        # Step 7: Quit Qt application
        try:
            app.quit()
            QCoreApplication.exit(0)
        except Exception as e:
            _log(f"[Aria] App quit error (ignored): {e}")

        _log("[Aria] Cleanup complete")

        # Step 8: Force exit if still running after timeout (covers modal traps)
        def force_exit():
            time.sleep(2.0)
            _log("[Aria] Force exiting due to timeout")
            os._exit(0)

        force_thread = threading.Thread(target=force_exit, daemon=True)
        force_thread.start()

    action_quit.triggered.connect(cleanup_and_quit)

    # Handle elevation dialog disable request - just call on_enable_toggled(False)
    def on_elevation_disable_requested():
        """Handle user clicking 'Disable' in elevation dialog."""
        _log("[UI] User requested to temporarily disable from elevation dialog")
        on_enable_toggled(False)  # Reuse existing disable logic
        # Update popup menu UI state
        if ball._popup_menu:
            ball._popup_menu.setEnabled(False)

    # Connect elevation dialog signals (cleanup_and_quit is now defined)
    elevation_dialog.closeRequested.connect(on_elevation_close_requested)
    elevation_dialog.restartAsAdminRequested.connect(on_elevation_restart_admin)
    elevation_dialog.disableRequested.connect(on_elevation_disable_requested)

    # Register cleanup for signal handling and atexit
    def signal_handler(signum, frame):
        _log(f"[Aria] Received signal {signum}, cleaning up...")
        cleanup_and_quit()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    def atexit_cleanup():
        """Safe cleanup on process exit."""
        try:
            if hasattr(backend, "stop"):
                backend.stop()
        except Exception as e:
            _log(f"[Aria] atexit cleanup error (ignored): {e}")

    atexit.register(atexit_cleanup)

    # Settings saved -> reload backend config and sync popup menu
    def on_settings_saved(config):
        if hasattr(backend, "reload_config"):
            backend.reload_config()

        # Sync hotkey if changed
        general = config.get("general", {})
        saved_hotkey = general.get("hotkey", "")
        if saved_hotkey and hasattr(backend, "set_hotkey"):
            # Convert Qt key sequence format to hotkey format if needed
            hotkey_lower = saved_hotkey.lower().replace(" ", "")
            backend.set_hotkey(hotkey_lower)

        # Sync popup menu with saved mode
        saved_mode = config.get("polish_mode", "fast")
        ball.set_polish_mode(saved_mode)
        _log(f"[Aria] Settings saved, polish mode synced: {saved_mode}")

    settings.settingsSaved.connect(on_settings_saved)

    # Show floating ball
    ball.show()

    _log("Aria Floating Ball is now visible.")
    _log("  - Left-click: Toggle recording")
    _log("  - Right-click: Show popup menu")
    _log("  - Middle-click: Lock position")
    _log("  - Drag: Move ball (when unlocked)")
    _log("  - System tray single-click: Show history (Ctrl+1-9 to copy)")
    _log("  - System tray double-click: Open hotwords settings")

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
