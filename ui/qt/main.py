# main.py
# Qt frontend entry point for Aria
# Floating ball UI with mouse interactions

import sys
import signal
import atexit
import argparse
import json
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QSystemTrayIcon,
    QMenu,
)
from PySide6.QtGui import QIcon, QAction, QClipboard
from PySide6.QtCore import Qt, QTimer, QThreadPool

from .bridge import QtBridge
from .floating_ball import FloatingBall
from .settings import SettingsWindow
from .sound import play_sound
from .history import HistoryWindow
from .history_browser import HistoryBrowserWindow
from .translation_popup import TranslationPopup
from .ai_chat_window import AIChatWindow
from .workers import TranslationWorker, SummaryWorker, LLMWorker, ReplyWorker
from .elevation_dialog import ElevationWarningDialog
from .draft_box import DraftBoxWindow
from .correction_rules import CorrectionRulesWindow
from .transcript_recovery import (
    RecoveryStatus,
    copy_recovery_text,
    copy_last_transcript,
    paste_recovery_text,
    paste_last_transcript,
    resolve_last_transcript,
)
from core.history.models import RecordType

# Debug log for main.py
_DEBUG_LOG = Path(__file__).parent.parent.parent / "DebugLog" / "wakeword_debug.log"

# Product switch: Draft Box is intentionally hidden and disconnected for now.
# Keeping the implementation in-tree makes the decision easy to reverse without
# letting automatic fallback signals or a tray action open the window.
DRAFT_BOX_ENABLED = False


def _log(msg: str):
    """Write debug message to shared log file (pythonw.exe safe)."""
    import datetime
    import sys

    from core.debug import append_log_line

    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    # Guard for pythonw.exe (sys.stdout is None)
    if sys.stdout is not None:
        print(line)
    append_log_line(_DEBUG_LOG, line)


def _sync_settings_output_mode(settings, mode: str) -> None:
    """Mirror the popup's output-mode choice into the settings window.

    SettingsWindow.save_config snapshots chk_typewriter_mode back into
    output.typewriter_mode, so a stale checkbox would silently roll the
    popup's choice back on the next unrelated settings save. Same defensive
    sync the polish/OCR handlers do via settings.set_polish_mode /
    set_ocr_mode; module-level so tests can lock the behavior without
    booting the GUI. The checkbox has no connected slots, so no
    blockSignals dance is needed.
    """
    checkbox = getattr(settings, "chk_typewriter_mode", None)
    if checkbox is None:
        return
    checkbox.setChecked(mode == "typewriter")


def main(*, on_ui_ready=None):
    """Main entry point for Qt frontend with floating ball UI.

    ``on_ui_ready`` is called only after Qt has processed the floating ball's
    first show event.  The launcher uses this as the authoritative 100% splash
    handoff instead of guessing readiness before UI construction begins.
    """
    # Parse arguments
    parser = argparse.ArgumentParser(description="Aria Qt Frontend")
    parser.add_argument(
        "--hotkey", default="grave", help="Hotkey for recording (default: grave/`)"
    )
    args = parser.parse_args()

    # Windows taskbar identity — must be set BEFORE QApplication creates any
    # window. launcher.py already does this for the normal boot path; repeating
    # it here (idempotent) covers direct runs like `python -m aria.ui.qt.main`
    # that bypass launcher.py, so taskbar grouping/icon still identify as Aria.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Xingxun.Aria.VoiceInput"
            )
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # Keep running with floating ball
    app.setApplicationName("Aria")

    # Unify the application-wide UI font. Qt's default GUI font follows the
    # Windows display language ("Segoe UI" on non-Chinese systems), which has
    # no CJK glyphs: Chinese then renders through per-glyph DirectWrite
    # fallback while the settings QSS pins QLabel to another family — several
    # typefaces end up mixed in one window. Pin the whole app to the system
    # CJK UI font when available (all Win10/11 ship it); font size is left
    # untouched to respect system scaling. Single-family setFamily on purpose:
    # multi-family QFont + QFontInfo hard-crashes PySide6 6.8.0 (access
    # violation), so the comma-fallback variant is reserved for QSS only.
    try:
        from PySide6.QtGui import QFontDatabase

        _available_families = set(QFontDatabase.families())
        for _fam in ("Microsoft YaHei UI", "Microsoft YaHei"):
            if _fam in _available_families:
                _app_font = app.font()
                _app_font.setFamily(_fam)
                app.setFont(_app_font)
                break
    except Exception:
        pass

    # Application-wide window icon. Without this, top-level windows
    # (Settings/HistoryBrowser/dialogs) fall back to the Windows default
    # icon in the taskbar / Alt+Tab.
    _aria_icon = None
    try:
        from aria.core.utils.paths import get_base_path

        _icon_path = get_base_path() / "assets" / "aria.ico"
        if _icon_path.exists():
            _candidate_icon = QIcon(str(_icon_path))
            # A corrupt/unreadable .ico yields a null QIcon; keep _aria_icon
            # None so the tray falls back to the programmatic drawing below.
            if not _candidate_icon.isNull():
                _aria_icon = _candidate_icon
                app.setWindowIcon(_aria_icon)
    except Exception:
        pass

    # Create UI components
    bridge = QtBridge()
    ball = FloatingBall(size=48)
    settings = SettingsWindow()
    history = HistoryWindow()
    history_browser = HistoryBrowserWindow()
    draft_box = DraftBoxWindow() if DRAFT_BOX_ENABLED else None
    correction_rules = CorrectionRulesWindow()
    translation_popup = TranslationPopup()
    summary_popup = TranslationPopup()
    reply_popup = TranslationPopup()
    ai_chat_window = AIChatWindow()
    elevation_dialog = ElevationWarningDialog()

    from .reminder_dialog import ReminderDialog

    reminder_dialog = ReminderDialog()

    # Stamp the brand icon on every taskbar / Alt-Tab-visible top-level window.
    # app.setWindowIcon alone can be overridden by Windows when no explicit
    # AppUserModelID is in effect; setting each window guarantees Aria's own
    # logo on the taskbar button (e.g. the Settings window).
    if _aria_icon is not None:
        for _win in (
            settings,
            history,
            history_browser,
            correction_rules,
            ai_chat_window,
            elevation_dialog,
            reminder_dialog,
        ):
            try:
                _win.setWindowIcon(_aria_icon)
            except Exception:
                pass

    # Connect undo signal — cancel the reminder in store
    def on_reminder_undo(reminder_id: str):
        _log(f"[MAIN] Reminder undo: {reminder_id}")
        if hasattr(backend, "reminder_store") and backend.reminder_store:
            backend.reminder_store.cancel(reminder_id)

    reminder_dialog.undoClicked.connect(on_reminder_undo)

    def on_reminder_confirmed(reminder_id: str):
        """User confirmed the reminder — show a quiet local notice."""
        _log(f"[MAIN] Reminder confirmed: {reminder_id}")
        count = 0
        if hasattr(backend, "reminder_store") and backend.reminder_store:
            count = len(backend.reminder_store.get_pending())
        tray.setToolTip(f"Aria — {count} 个提醒待触发" if count else "Aria")
        if hasattr(ball, "show_notice"):
            ball.show_notice("提醒已设置", level="success", duration_ms=1800)

    reminder_dialog.dismissClicked.connect(on_reminder_confirmed)

    # Thread pool for background workers
    thread_pool = QThreadPool.globalInstance()

    # Container to keep signal objects alive until delivery
    # (QRunnable with autoDelete=True can delete signals before delivery)
    _active_signals = []
    _active_dialogs = []
    _quit_in_progress = False

    # Track pending action source texts for history recording
    # {request_id: {"source_text": str, "type": RecordType}}
    _pending_action_sources = {}

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

    # Prefer the official icon asset (assets/aria.ico, exported from this very
    # drawing code via build_portable/generate_brand_icon.py) so tray, taskbar
    # and EXE all share one brand mark; fall back to programmatic drawing.
    tray.setIcon(_aria_icon if _aria_icon is not None else create_tray_icon())
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

    action_deep_sleep = QAction("深度休眠", None)
    action_deep_sleep.setCheckable(True)
    action_deep_sleep.setChecked(False)
    tray_menu.addAction(action_deep_sleep)

    tray_menu.addSeparator()

    action_settings = QAction("高级设置", None)
    tray_menu.addAction(action_settings)

    action_draft_box = None
    if DRAFT_BOX_ENABLED:
        action_draft_box = QAction("打开草稿箱", None)
        tray_menu.addAction(action_draft_box)

    action_correction_rules = QAction("纠正规则", None)
    tray_menu.addAction(action_correction_rules)

    tray_menu.addSeparator()

    action_check_update = QAction("检查更新", None)
    tray_menu.addAction(action_check_update)

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
    bridge.slowStage.connect(ball.on_slow_stage)  # Slow pipeline glow indicator
    bridge.apiStatusChanged.connect(settings.update_api_status)
    bridge.apiStatusChanged.connect(ball.set_api_status)
    bridge.asrStatusChanged.connect(ball.set_asr_status)
    bridge.asrFailure.connect(ball.on_asr_failure)  # Red flash on lost segment

    def show_quiet_notice(
        msg: str, level: str = "warning", duration_ms: int = 1800
    ) -> None:
        """Show a local no-sound notice near the floating ball."""
        try:
            if hasattr(ball, "show_notice"):
                ball.show_notice(msg, level=level, duration_ms=duration_ms)
            else:
                ball.setToolTip(str(msg))
        except Exception as e:
            _log(f"[UI] Failed to show quiet notice: {e}")

    def _friendly_runtime_error(msg: str) -> str:
        """Short, beginner-friendly text for recoverable runtime failures."""
        text = str(msg or "").strip()
        if "未识别到有效路径" in text:
            return "未识别到有效路径"
        if "未检测到选中文本" in text:
            return "未检测到选中文本"
        if text.startswith("打开失败"):
            return "打开失败，请检查路径"
        if "API 未配置" in text:
            return "API 未配置"
        if "麦克风启动失败" in text:
            return "麦克风启动失败，请检查设备"
        if not text:
            return "操作未完成"
        return text

    def show_error_dialog(msg: str) -> None:
        """Show a real dialog only for blocking/severe errors."""
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
            ball.setToolTip("快捷键被占用\n点击悬浮窗启用语音输入")
            show_quiet_notice("快捷键被占用，可点击悬浮窗使用", "warning", 2400)
        else:
            show_quiet_notice(_friendly_runtime_error(msg), "warning", 2200)

    bridge.error.connect(on_error)

    # Leveled quiet notices from the backend (failure/rescue captions etc.):
    # same local no-focus notice as errors, but with the sender's level so
    # info/success states don't render as warnings.
    def on_notice(msg: str, level: str, duration_ms: int) -> None:
        show_quiet_notice(str(msg), str(level or "info"), int(duration_ms))

    bridge.notice.connect(on_notice)

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
                show_quiet_notice("管理员重启已取消或失败", "warning", 2200)
        except Exception as e:
            _log(f"[UI] Exception during admin restart: {e}")
            show_quiet_notice(f"重启失败: {e}", "warning", 2400)

    # Connect elevation dialog signals (connected later after cleanup_and_quit is defined)

    # Handle setting changes from backend (e.g., via wakeword commands)
    def on_setting_changed(setting: str, value: bool):
        _log(f"[MAIN] on_setting_changed received: {setting} = {value}")
        _log(f"[UI] Setting changed via wakeword: {setting} = {value}")
        if setting == "auto_send":
            action_auto_send.setChecked(value)
            ball.set_auto_send(value)  # Update floating ball color indicator
        elif setting == "mute" or setting == "sound_enabled":
            # sound_enabled=False means mute=True
            action_mute.setChecked(not value if setting == "sound_enabled" else value)
        elif setting == "sleeping":
            # Light sleep state change (from voice command)
            ball.on_state_changed("SLEEPING" if value else "IDLE")
        elif setting == "deep_sleeping":
            # Deep sleep state change (sync popup menu button + tray menu)
            ball.set_deep_sleeping_state(value)
            action_deep_sleep.setChecked(value)
        elif setting == "enabled":
            # Update popup menu toggle when hotkey re-enables from disabled state
            ball.set_enabled_state(value)

    bridge.settingChanged.connect(on_setting_changed)

    # === Auto-update UI (v1.0.5) ===
    _update_info = {"local": "", "remote": "", "notes": "", "armed": False}

    def _load_manifest_notes() -> str:
        """Pull notes_summary from stage's manifest snapshot, best-effort."""
        try:
            from aria.update_tool import get_update_state

            st = get_update_state()
            snap = st.get("manifest_snapshot") or {}
            return snap.get("notes_summary", "") or ""
        except Exception:
            return ""

    def _open_update_dialog():
        if not _update_info["armed"]:
            return
        from aria.ui.qt.update_dialog import show_update_dialog
        from aria.core.utils import get_config_path
        from aria.core.update_gates import load_update_prefs, save_update_prefs

        def _apply_now():
            if backend and hasattr(backend, "apply_staged_update"):
                ok = backend.apply_staged_update()
                if ok:
                    # Use the standard shutdown path (stops backend threads,
                    # closes dialogs, hides tray) then Qt quits. updater_runner.bat
                    # is already detached and will pick up the PID to wait on.
                    try:
                        cleanup_and_quit()
                    except Exception as e:
                        _log(f"[UPDATE] cleanup_and_quit fallback: {e}")
                        try:
                            app.quit()
                        except Exception:
                            pass

        def _skip(version: str):
            try:
                cfg_path = Path(get_config_path("hotwords.json"))
                prefs = load_update_prefs(cfg_path)
                sv = list(prefs.get("skipped_versions", []))
                if version not in sv:
                    sv.append(version)
                prefs["skipped_versions"] = sv
                save_update_prefs(cfg_path, prefs)
                _update_info["armed"] = False
                ball.set_update_badge(False)
            except Exception as e:
                _log(f"[UPDATE] skip save failed: {e}")

        show_update_dialog(
            _update_info["local"],
            _update_info["remote"],
            _update_info["notes"],
            _apply_now,
            _skip,
        )

    import time as _time_mod

    _boot_time = _time_mod.time()
    _current_app_state = {"s": "IDLE"}

    def _track_app_state(state: str):
        _current_app_state["s"] = state

    bridge.stateChanged.connect(_track_app_state)

    def _fire_update_bubble(local_ver: str, remote_ver: str):
        show_quiet_notice(f"新版本已就绪：v{remote_ver}", "info", 3600)

    def on_update_available(local_ver: str, remote_ver: str):
        _update_info["local"] = local_ver
        _update_info["remote"] = remote_ver
        _update_info["notes"] = _load_manifest_notes()
        _update_info["armed"] = True

        # Badge: passive. Always on.
        ball.set_update_badge(True, f"新版本 v{remote_ver} 已就绪，点击查看")

        # Gate decides whether to actively interrupt with a tray bubble
        try:
            from aria.core.update_gates import (
                load_update_prefs,
                save_update_prefs,
                should_show_update_prompt,
                now_utc_iso,
            )
            from aria.core.utils import get_config_path
            from aria.update_tool import get_update_state

            cfg_path = Path(get_config_path("hotwords.json"))
            prefs = load_update_prefs(cfg_path)
            st = get_update_state()
            manifest = st.get("manifest_snapshot") or {}
            is_critical = bool(manifest.get("critical", False))
            stage_ready = st.get("status") == "ready"

            show, reason = should_show_update_prompt(
                remote_ver,
                is_critical,
                prefs,
                _current_app_state["s"],
                _boot_time,
                stage_is_ready=stage_ready,
            )
            if show:
                _fire_update_bubble(local_ver, remote_ver)
                prefs.setdefault("last_prompt_per_version", {})[remote_ver] = {
                    "first_shown_at": now_utc_iso(),
                    "user_dismissed_count": 0,
                    "was_critical": is_critical,
                }
                save_update_prefs(cfg_path, prefs)
            else:
                _log(f"[UPDATE] bubble suppressed: {reason}")
        except Exception as e:
            _log(f"[UPDATE] gate check failed: {e}; fallback to plain bubble")
            _fire_update_bubble(local_ver, remote_ver)

    bridge.updateAvailable.connect(on_update_available)
    tray.messageClicked.connect(_open_update_dialog)

    def _on_check_update_clicked():
        """Menu: 检查更新.

        If stage already ready → open dialog directly.
        Else → trigger background re-check; surface the toast regardless of
        anti-nuisance gate (user explicitly asked).
        """
        if _update_info["armed"]:
            _open_update_dialog()
            return
        try:
            from aria.update_tool import get_update_state

            st = get_update_state()
            if st.get("status") == "ready":
                to_ver = st.get("to_version", "")
                if to_ver:
                    on_update_available(_update_info.get("local") or "", to_ver)
                    return
        except Exception as e:
            _log(f"[UPDATE] menu state check failed: {e}")

        show_quiet_notice("正在后台检查更新…", "info", 2200)
        if backend and hasattr(backend, "_check_update_background"):
            import threading as _th

            _th.Thread(
                target=backend._check_update_background,
                kwargs={"force_stage": True},
                daemon=True,
            ).start()

    action_check_update.triggered.connect(_on_check_update_clicked)

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
        config = {}  # Default empty config (prevents NameError if JSON load fails)
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
        if hasattr(backend, "get_api_status"):
            bridge.emit_api_status(
                json.dumps(backend.get_api_status(), ensure_ascii=False)
            )
        if hasattr(backend, "get_asr_runtime_status"):
            bridge.emit_asr_status(
                json.dumps(backend.get_asr_runtime_status(), ensure_ascii=False)
            )
        _log(f"Aria Qt Frontend Started (Hotkey: {actual_hotkey})")

        # Pass history store to both history UIs (single data source:
        # data/history/*.jsonl — the tray popup no longer reads DebugLog)
        if hasattr(backend, "history_store"):
            history_browser.set_history_store(backend.history_store)
            history.set_history_store(backend.history_store)
            _log("[Aria] HistoryStore connected to history windows")

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
                    ball.set_deep_sleeping_state(False)
                    _log("[STARTUP] UI state synced to IDLE")
                    # 4. Sync UI toggle state (don't trigger signal, just update visual)
                    #    NOTE: Do NOT toggle False→True as it calls stop() which
                    #    unregisters hotkeys that start() won't re-register!
                    ball.set_enabled_state(True)
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
            f"Aria 启动失败:\n{e}\n\n请重新启动；如果仍然失败，请在 GitHub 反馈。",
        )
        sys.exit(1)

    # Connect ball actions
    ball.toggleRequested.connect(backend.toggle_recording)

    # =========================================================================
    # v1.1: Action-driven UI handling (Translation Popup, AI Chat)
    # =========================================================================

    # Singleton holder for the active region selector overlay.
    # Region screenshots can take seconds (drag + decide); the wakeword
    # cooldown is only 500ms, so a second "阿蓝区域截图" can fire while
    # the first overlay is still up. We tear down the old one first to
    # avoid two overlays competing for the mouse.
    _active_region_overlay = {"value": None}

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
            ReplyAction,
            ReminderConfirmAction,
            ReminderNotifyAction,
            ReminderCancelAction,
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
                from core.debug import append_log_line as _raw_append

                _raw_append(
                    _DEBUG_LOG,
                    "[RAW] MAIN: About to call translation_popup.show_loading()",
                )
                translation_popup.show_loading(action.source_text, action.request_id)
                _pending_action_sources[action.request_id] = {
                    "source_text": action.source_text,
                    "type": RecordType.SELECTION_TRANSLATE,
                }
                _log("[MAIN] show_loading called OK")

                # Get API config from backend's polisher (reuse existing config)
                api_url = ""
                api_key = ""
                model = "google/gemini-2.5-flash-lite-preview-09-2025"

                if (
                    hasattr(backend, "polisher")
                    and backend.polisher
                    and hasattr(backend.polisher.config, "api_url")
                ):
                    api_url = backend.polisher.config.api_url
                    api_key = backend.polisher.config.api_key
                    model = backend.polisher.config.model
                    _log(f"[MAIN] Got API config: url={api_url[:30]}..., model={model}")

                if not api_url or not api_key:
                    _log("[MAIN] ERROR: API not configured")
                    _pending_action_sources.pop(action.request_id, None)
                    translation_popup.show_error(
                        "API 未配置（本地模式不支持翻译）", action.request_id
                    )
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
                _pending_action_sources[action.request_id] = {
                    "source_text": action.source_text,
                    "type": RecordType.SUMMARY,
                }

                api_url = ""
                api_key = ""
                model = "google/gemini-2.5-flash-lite-preview-09-2025"

                if (
                    hasattr(backend, "polisher")
                    and backend.polisher
                    and hasattr(backend.polisher.config, "api_url")
                ):
                    api_url = backend.polisher.config.api_url
                    api_key = backend.polisher.config.api_key
                    model = backend.polisher.config.model
                    _log(f"[MAIN] Got API config: url={api_url[:30]}..., model={model}")

                if not api_url or not api_key:
                    _log("[MAIN] ERROR: API not configured for summary")
                    _pending_action_sources.pop(action.request_id, None)
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

                if (
                    hasattr(backend, "polisher")
                    and backend.polisher
                    and hasattr(backend.polisher.config, "api_url")
                ):
                    api_url = backend.polisher.config.api_url
                    api_key = backend.polisher.config.api_key
                    model = backend.polisher.config.model

                if not api_url or not api_key:
                    _log("[MAIN] ERROR: API not configured for clipboard translation")
                    show_quiet_notice("API 未配置", "warning", 2200)
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
                _pending_action_sources[action.request_id] = {
                    "source_text": action.source_text,
                    "type": RecordType.SELECTION_TRANSLATE,
                }
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

        elif action.type == ActionType.SHOW_REPLY:
            # Reply popup: generate reply to selected message
            try:
                _log(f"[MAIN] Reply popup: {len(action.source_text)} chars")
                reply_popup.show_loading(
                    action.source_text,
                    action.request_id,
                    title_prefix="回复",
                    title_done="回复建议",
                    loading_text="正在生成回复...",
                    error_prefix="回复失败",
                )
                _pending_action_sources[action.request_id] = {
                    "source_text": action.source_text,
                    "type": RecordType.SELECTION_REPLY,
                    "style_hint": getattr(action, "style_hint", ""),
                }

                api_url = ""
                api_key = ""
                model = "google/gemini-2.5-flash-lite-preview-09-2025"

                if (
                    hasattr(backend, "polisher")
                    and backend.polisher
                    and hasattr(backend.polisher.config, "api_url")
                ):
                    api_url = backend.polisher.config.api_url
                    api_key = backend.polisher.config.api_key
                    model = backend.polisher.config.model
                    _log(
                        f"[MAIN] Got API config for reply: url={api_url[:30]}..., model={model}"
                    )

                if not api_url or not api_key:
                    _log("[MAIN] ERROR: API not configured for reply")
                    _pending_action_sources.pop(action.request_id, None)
                    reply_popup.show_error("API 未配置", action.request_id)
                    return

                # Combine config reply_style with action's style_hint
                reply_style = ""
                try:
                    import json as _json
                    from pathlib import Path as _Path

                    _cfg_path = (
                        _Path(__file__).parent.parent.parent
                        / "config"
                        / "hotwords.json"
                    )
                    with open(_cfg_path, "r", encoding="utf-8") as _f:
                        _cfg = _json.load(_f)
                    reply_style = _cfg.get("reply_style", "")
                except Exception:
                    pass
                combined_style = reply_style
                if action.style_hint:
                    combined_style = (
                        f"{reply_style}\n{action.style_hint}"
                        if reply_style
                        else action.style_hint
                    )

                worker = ReplyWorker(
                    request_id=action.request_id,
                    source_text=action.source_text,
                    api_url=api_url,
                    api_key=api_key,
                    model=model,
                    style_hint=combined_style if combined_style else None,
                )
                signals_ref = worker.signals
                _active_signals.append(signals_ref)

                def cleanup_reply_signals(sig_ref):
                    if sig_ref in _active_signals:
                        _active_signals.remove(sig_ref)

                signals_ref.finished.connect(on_reply_finished)
                signals_ref.finished.connect(
                    lambda *_: cleanup_reply_signals(signals_ref)
                )
                signals_ref.error.connect(on_reply_error)
                signals_ref.error.connect(lambda *_: cleanup_reply_signals(signals_ref))
                thread_pool.start(worker)
                _log("[MAIN] ReplyWorker started")
            except Exception as e:
                _log(f"[MAIN] ERROR in SHOW_REPLY: {e}")
                import traceback

                _log(traceback.format_exc())

        elif action.type == ActionType.SHOW_REMINDER_CONFIRM:
            _log(
                f"[MAIN] Reminder confirm: {action.content} @ {action.trigger_display}"
            )
            # Position above the floating ball
            ball_pos = ball.mapToGlobal(ball.rect().center())
            reminder_dialog.show_confirm(
                reminder_id=action.reminder_id,
                content=action.content,
                trigger_display=action.trigger_display,
                anchor_pos=ball_pos,
            )

        elif action.type == ActionType.SHOW_REMINDER_NOTIFY:
            _log(
                f"[MAIN] Reminder notify: {action.content} (batch={action.batch_count})"
            )
            ball_pos = ball.mapToGlobal(ball.rect().center())
            reminder_dialog.show_notify(
                reminder_id=action.reminder_id,
                content=action.content,
                batch_count=action.batch_count,
                anchor_pos=ball_pos,
                repeat_interval_seconds=action.repeat_interval_seconds,
            )
            # Sound
            try:
                from .sound import play_sound

                play_sound("reminder")
            except Exception:
                pass
            # Ball flash
            if hasattr(ball, "on_reminder_fired"):
                ball.on_reminder_fired()

        elif action.type == ActionType.REMINDER_CANCELLED:
            _log(
                f"[MAIN] Reminder cancelled: ids={action.reminder_ids}, "
                f"dismiss_active={action.dismiss_active}"
            )
            reminder_dialog.dismiss_cancelled(
                action.reminder_ids, dismiss_active=action.dismiss_active
            )
            count = 0
            if hasattr(backend, "reminder_store") and backend.reminder_store:
                count = len(backend.reminder_store.get_pending())
            tray.setToolTip(f"Aria — {count} 个提醒待触发" if count else "Aria")
            if hasattr(ball, "show_notice"):
                ball.show_notice(action.message, level="success", duration_ms=2400)

        elif action.type == ActionType.SCREENSHOT_FULL:
            _log(f"[MAIN] Screenshot full requested, id={action.request_id}")
            try:
                from aria.core.screenshot import CaptureEngine, ScreenshotSaver

                # Hide our own visible chrome before capturing so the
                # ball / popups don't end up in the screenshot. WDA is
                # used on the ball itself via _exclude_from_capture
                # already; this is a defense-in-depth fallback.
                was_ball_visible = ball.isVisible()
                if was_ball_visible:
                    ball.hide()
                    QApplication.processEvents()
                try:
                    image = CaptureEngine.grab_primary_screen()
                finally:
                    if was_ball_visible:
                        ball.show()
                saver = ScreenshotSaver()
                result = saver.save(image)
                if result.success:
                    msg = "已截图并复制到剪贴板"
                    if result.path is not None:
                        msg += f"\n{result.path}"
                    show_quiet_notice(msg, "success", 2600)
                else:
                    bridge.emit_error(f"截图失败: {result.error or '未知原因'}")
            except Exception as e:
                _log(f"[MAIN] Screenshot full failed: {e}")
                import traceback

                _log(traceback.format_exc())
                bridge.emit_error(f"截图失败: {e}")

        elif action.type == ActionType.SCREENSHOT_REGION:
            _log(f"[MAIN] Screenshot region requested, id={action.request_id}")
            try:
                from aria.ui.qt.screenshot_overlay import RegionSelectorOverlay
                from aria.core.screenshot import ScreenshotSaver
                from aria.core.action import PinScreenshotAction

                # Replace any previous still-open overlay (user fires a
                # second 阿蓝区域截图 while the first overlay is up — the
                # 500ms wakeword cooldown is much shorter than the
                # typical drag-decide time).
                prev = _active_region_overlay.get("value")
                if prev is not None:
                    try:
                        prev._tear_down()
                    except Exception as e:
                        _log(f"[MAIN] prev overlay teardown failed: {e}")
                    _active_region_overlay["value"] = None

                def _release_overlay():
                    _active_region_overlay["value"] = None

                def _on_region_confirm(image, dpr, sel):
                    """Overlay cropped from its frozen background already.
                    Do NOT re-capture — the live desktop may have changed."""
                    try:
                        saver = ScreenshotSaver()
                        result = saver.save(image)
                        if result.success:
                            msg = "已截图并复制到剪贴板"
                            if result.path is not None:
                                msg += f"\n{result.path}"
                            show_quiet_notice(msg, "success", 2600)
                        else:
                            bridge.emit_error(f"截图失败: {result.error or '未知原因'}")
                    except Exception as e:
                        _log(f"[MAIN] Region confirm failed: {e}")
                        bridge.emit_error(f"截图失败: {e}")
                    finally:
                        _release_overlay()

                def _on_region_pin(image, dpr, sel):
                    """Pin = save + clipboard + floating pin window.

                    Unifies the three actions (full / ✓ / 📌) so the
                    user can always find a captured image back later
                    via the file or the clipboard, regardless of which
                    button they clicked.
                    """
                    try:
                        saver = ScreenshotSaver()
                        # 1) Save to disk + clipboard (same as ✓)
                        result = saver.save(image)
                        if result.success:
                            msg = "已贴图并复制到剪贴板"
                            if result.path is not None:
                                msg += f"\n{result.path}"
                            show_quiet_notice(msg, "success", 2600)
                        else:
                            bridge.emit_error(
                                f"贴图保存失败: {result.error or '未知原因'}"
                            )
                        # 2) Emit pin window (always, even if disk save failed —
                        # the pin is the user's primary intent here)
                        png_bytes = saver.encode_png_bytes(image)
                        bridge.emit_action(
                            PinScreenshotAction(
                                image_bytes=png_bytes,
                                width_px=image.width(),
                                height_px=image.height(),
                                global_x=sel.x,
                                global_y=sel.y,
                                device_pixel_ratio=dpr,
                            )
                        )
                    except Exception as e:
                        _log(f"[MAIN] Region pin failed: {e}")
                        bridge.emit_error(f"图钉创建失败: {e}")
                    finally:
                        _release_overlay()

                def _on_region_cancel():
                    _log("[MAIN] Region capture cancelled")
                    _release_overlay()

                overlay = RegionSelectorOverlay(
                    on_confirm=_on_region_confirm,
                    on_pin=_on_region_pin,
                    on_cancel=_on_region_cancel,
                )
                _active_region_overlay["value"] = overlay
                overlay.start()
            except Exception as e:
                _log(f"[MAIN] Screenshot region failed: {e}")
                import traceback

                _log(traceback.format_exc())
                bridge.emit_error(f"区域截图失败: {e}")

        elif action.type == ActionType.SHOW_PIN_WINDOW:
            _log(f"[MAIN] Pin window requested, id={action.request_id}")
            try:
                from aria.ui.qt.screenshot_pin import show_pin
                from PySide6.QtGui import QImage

                img = QImage()
                if action.image_bytes:
                    img.loadFromData(action.image_bytes, "PNG")
                    # Honor the DPR that the overlay actually captured at,
                    # not whatever the primary screen happens to be — they
                    # can differ when the user crops on a secondary
                    # monitor with a different DPR.
                    dpr = action.device_pixel_ratio or 1.0
                    img.setDevicePixelRatio(dpr)
                if img.isNull():
                    bridge.emit_error("图钉图像无效")
                else:
                    show_pin(img, action.global_x, action.global_y)
            except Exception as e:
                _log(f"[MAIN] Pin window failed: {e}")
                import traceback

                _log(traceback.format_exc())
                bridge.emit_error(f"图钉创建失败: {e}")

    def on_clipboard_translation_finished(request_id: str, translated_text: str):
        """Handle clipboard translation completion."""
        try:
            # Use Qt's clipboard (always available) instead of pyperclip
            clipboard = QApplication.clipboard()
            clipboard.setText(translated_text)
            _record_to_history(request_id, translated_text)
            _log(
                f"[UI] Clipboard translation finished: {len(translated_text)} chars copied"
            )
            show_quiet_notice("已复制到剪贴板", "success", 1800)
        except Exception as e:
            _log(f"[UI] Failed to copy to clipboard: {e}")
            show_quiet_notice(f"复制失败: {e}", "warning", 2200)

    def on_clipboard_translation_error(request_id: str, error_msg: str):
        """Handle clipboard translation error."""
        _pending_action_sources.pop(request_id, None)
        _log(f"[UI] Clipboard translation error: {error_msg}")
        show_quiet_notice(f"翻译失败: {error_msg}", "warning", 2600)

    def _record_to_history(request_id: str, output_text: str):
        """Write a completed action to history store, using tracked source info."""
        source_info = _pending_action_sources.pop(request_id, None)
        if not source_info:
            return
        if not hasattr(backend, "history_store") or not backend.history_store:
            return
        try:
            metadata = {}
            if source_info.get("style_hint"):
                metadata["style_hint"] = source_info["style_hint"]
            backend.history_store.add(
                record_type=source_info["type"],
                input_text=source_info["source_text"],
                output_text=output_text,
                metadata=metadata,
            )
        except Exception as e:
            _log(f"[UI] History record failed: {e}")

    def on_translation_finished(request_id: str, translated_text: str):
        """Handle translation completion."""
        _log(
            f"[UI] Translation finished CALLBACK: request_id={request_id}, text_len={len(translated_text)}"
        )
        try:
            translation_popup.show_result(translated_text, request_id)
            _record_to_history(request_id, translated_text)
            _log(f"[UI] Translation show_result completed OK")
        except Exception as e:
            _log(f"[UI] Translation show_result ERROR: {e}")
            import traceback

            _log(f"[UI] TRACEBACK: {traceback.format_exc()}")

    def on_translation_error(request_id: str, error_msg: str):
        """Handle translation error."""
        _pending_action_sources.pop(request_id, None)
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
            _record_to_history(request_id, summary_text)
            _log(f"[UI] Summary show_result completed OK")
        except Exception as e:
            _log(f"[UI] Summary show_result ERROR: {e}")
            import traceback

            _log(f"[UI] TRACEBACK: {traceback.format_exc()}")

    def on_summary_error(request_id: str, error_msg: str):
        """Handle summary error."""
        _pending_action_sources.pop(request_id, None)
        _log(f"[UI] Summary error CALLBACK: request_id={request_id}, error={error_msg}")
        try:
            summary_popup.show_error(error_msg, request_id)
            _log(f"[UI] Summary show_error completed OK")
        except Exception as e:
            _log(f"[UI] Summary show_error ERROR: {e}")
            import traceback

            _log(f"[UI] TRACEBACK: {traceback.format_exc()}")

    def on_reply_finished(request_id: str, reply_text: str):
        """Handle reply generation completion."""
        _log(
            f"[UI] Reply finished CALLBACK: request_id={request_id}, text_len={len(reply_text)}"
        )
        try:
            reply_popup.show_result(reply_text, request_id)
            _record_to_history(request_id, reply_text)
            _log("[UI] Reply show_result completed OK")
        except Exception as e:
            _log(f"[UI] Reply show_result ERROR: {e}")
            import traceback

            _log(f"[UI] TRACEBACK: {traceback.format_exc()}")

    def on_reply_error(request_id: str, error_msg: str):
        """Handle reply generation error."""
        _pending_action_sources.pop(request_id, None)
        _log(f"[UI] Reply error CALLBACK: request_id={request_id}, error={error_msg}")
        try:
            reply_popup.show_error(error_msg, request_id)
            _log("[UI] Reply show_error completed OK")
        except Exception as e:
            _log(f"[UI] Reply show_error ERROR: {e}")
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

        if (
            hasattr(backend, "polisher")
            and backend.polisher
            and hasattr(backend.polisher.config, "api_url")
        ):
            api_url = backend.polisher.config.api_url
            api_key = backend.polisher.config.api_key
            model = backend.polisher.config.model

        if not api_url or not api_key:
            ai_chat_window.show_error("API 未配置（本地模式不支持对话）")
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
    reply_popup.copyRequested.connect(on_copy_translation)

    # Connect mute action to backend
    def on_mute_toggled():
        muted = action_mute.isChecked()
        if hasattr(backend, "set_sound_enabled"):
            backend.set_sound_enabled(not muted)
        # Runtime mute for UI cues. Session-scoped `muted`, NOT `enabled`:
        # `enabled` mirrors the persistent config switch (sound.enabled) and
        # must survive a tray mute/unmute round-trip.
        from .sound import get_sound_manager

        get_sound_manager().muted = muted

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

    def refresh_correction_rules():
        getter = getattr(backend, "get_explicit_correction_rules", None)
        rules = getter() if callable(getter) else []
        correction_rules.set_rules(rules)
        _log(f"[UI] Refreshed explicit correction rules: {len(rules)} active")

    def show_correction_rules():
        correction_rules.show_for_management()

    def _correction_failure_message(status: str) -> str:
        return {
            "empty_operand": "请同时填写识别原词和正确写法。",
            "source_too_broad": "原词过短或过宽；请至少填写两个字符的完整词语。",
            "source_too_long": "原词过长；纠正规则只适合词语或短语。",
            "replacement_too_long": "正确写法过长；纠正规则只适合词语或短语。",
            "invalid_character": "规则含不支持的控制字符，未保存。",
            "outer_whitespace": "规则两端含不可见空白，未保存。",
            "no_change": "原词与正确写法相同，未新增规则。",
            "not_found": "这条规则已发生变化，请刷新后重试。",
            "store_full": "本地纠正规则已达到容量上限。",
            "load_failed": "本地规则文件无法读取；为避免覆盖，未写入新规则。",
            "persistence_failed": "规则未能可靠写入磁盘，本次没有生效。",
            "store_unavailable": "纠正规则组件尚未就绪。",
        }.get(status, "纠正规则未能保存，本次没有生效。")

    def on_correction_add(source: str, replacement: str):
        add_rule = getattr(backend, "add_explicit_correction", None)
        if not callable(add_rule):
            correction_rules.set_status("纠正规则组件尚未就绪。", level="error")
            return
        result = add_rule(source, replacement)
        _log(f"[UI] Add explicit correction: {result.status}")
        if result.success:
            correction_rules.source_edit.clear()
            correction_rules.replacement_edit.clear()
            message = (
                "这条规则已经生效。"
                if result.status == "unchanged"
                else "规则已保存，从下一次语音识别起生效。"
            )
            correction_rules.set_status(message, level="success")
            refresh_correction_rules()
        else:
            correction_rules.set_status(
                _correction_failure_message(result.status), level="error"
            )

    def on_correction_clear(rule_id: str):
        clear_rule = getattr(backend, "clear_explicit_correction", None)
        if not callable(clear_rule):
            correction_rules.set_status("纠正规则组件尚未就绪。", level="error")
            return
        result = clear_rule(rule_id)
        _log(f"[UI] Clear explicit correction: {result.status}")
        if result.success:
            refresh_correction_rules()
            correction_rules.set_status(
                "已停用选中来源；较早的同词规则也不会恢复。", level="success"
            )
        else:
            correction_rules.set_status(
                _correction_failure_message(result.status), level="error"
            )

    correction_rules.refreshRequested.connect(refresh_correction_rules)
    correction_rules.addRequested.connect(on_correction_add)
    correction_rules.clearRequested.connect(on_correction_clear)
    ball.correctionRulesRequested.connect(show_correction_rules)
    action_correction_rules.triggered.connect(show_correction_rules)
    bridge.correctionRulesRequested.connect(show_correction_rules)

    # History browser: show and bring to front
    def show_history_browser():
        history_browser.show()
        history_browser.raise_()
        history_browser.activateWindow()

    ball.historyRequested.connect(show_history_browser)

    # History browser: re-inject a record's text into the focused app.
    # The browser hides itself before emitting, so Windows hands focus back
    # to the previously active window; after a short settle delay, refuse to
    # paste if an Aria-owned window is still foreground.
    def on_history_reinject(text: str):
        _log(f"[UI] History reinject requested ({len(text)} chars)")

        def _do_inject():
            try:
                from aria.system.output import foreground_belongs_to_current_process

                if foreground_belongs_to_current_process():
                    show_quiet_notice("请先点击目标窗口，再重新上屏", "warning", 2600)
                    return
                if getattr(backend, "output_injector", None):
                    if backend.output_injector.insert_text(text):
                        _log("[UI] History reinject inserted")
                    else:
                        show_quiet_notice("重新上屏失败", "warning", 2600)
                else:
                    show_quiet_notice("输入通道未就绪，无法重新上屏", "warning", 2600)
            except Exception as e:
                _log(f"[UI] History reinject failed: {e}")
                show_quiet_notice("重新上屏失败", "warning", 2600)

        QTimer.singleShot(300, _do_inject)

    history_browser.reinjectRequested.connect(on_history_reinject)

    # Last-transcript recovery: the popup carries no transcript text. The
    # backend owns runtime/history state, and paste re-enters the guarded
    # OutputInjector transaction after the popup has closed and focus settled.
    def on_copy_last_transcript():
        status = copy_last_transcript(backend, QApplication.clipboard())
        _log(f"[UI] Copy last transcript: {status.value}")
        if status == RecoveryStatus.COPIED:
            show_quiet_notice("已复制上次转写", "success", 1800)
        elif status == RecoveryStatus.NO_TEXT:
            show_quiet_notice("暂无可恢复的语音转写", "info", 2200)
        else:
            show_quiet_notice("复制上次转写失败", "warning", 2600)

    def on_paste_last_transcript():
        def _do_paste():
            status = paste_last_transcript(backend)
            _log(f"[UI] Paste last transcript: {status.value}")
            if status == RecoveryStatus.INSERTED:
                show_quiet_notice("已重新上屏", "success", 1600)
            elif status == RecoveryStatus.NO_TEXT:
                show_quiet_notice("暂无可恢复的语音转写", "info", 2200)
            elif status == RecoveryStatus.TARGET_REQUIRED:
                show_quiet_notice(
                    "请先点击目标窗口，再粘贴上次转写", "warning", 2800
                )
            elif status == RecoveryStatus.TARGET_UNAVAILABLE:
                show_quiet_notice("无法确认目标窗口，未重新上屏", "warning", 2800)
            elif status == RecoveryStatus.OUTPUT_UNAVAILABLE:
                show_quiet_notice("输入通道未就绪，无法重新上屏", "warning", 2600)
            else:
                show_quiet_notice("重新上屏失败，转写仍保留", "warning", 2800)

        QTimer.singleShot(300, _do_paste)

    ball.copyLastRequested.connect(on_copy_last_transcript)
    ball.pasteLastRequested.connect(on_paste_last_transcript)

    def show_draft_box():
        """Open without overwriting an in-progress draft."""
        if draft_box is None:
            return
        if not draft_box.current_text().strip():
            last_text = resolve_last_transcript(backend)
            draft_box.set_draft(last_text, replace=True)
        draft_box.show_for_editing()

    def on_draft_requested(text: str, reason: str):
        """Compatibility handler used only when Draft Box is re-enabled."""
        if draft_box is None:
            return
        existing = draft_box.current_text()
        if existing.strip():
            draft_box.queue_draft(text, reason)
        else:
            draft_box.set_draft(text, reason, replace=True)
        _log(f"[UI] Draft fallback stored: reason={reason}")

    def on_draft_load_last():
        if draft_box is None:
            return
        last_text = resolve_last_transcript(backend)
        if not last_text:
            draft_box.set_status("暂无可载入的语音转写。", level="info")
            return
        current = draft_box.current_text()
        if current.strip() and current != last_text:
            draft_box.set_status(
                "草稿已有内容，为避免覆盖未自动载入。请先复制或清空当前内容。",
                level="warning",
            )
            return
        draft_box.set_draft(last_text, replace=True)
        draft_box.set_status("已载入上次转写。", level="success")

    def on_draft_copy(text: str):
        if draft_box is None:
            return
        status = copy_recovery_text(text, QApplication.clipboard())
        _log(f"[UI] Copy Draft Box text: {status.value}")
        if status == RecoveryStatus.COPIED:
            draft_box.set_status("草稿已复制到剪贴板。", level="success")
        elif status == RecoveryStatus.NO_TEXT:
            draft_box.set_status("草稿为空，没有可复制的内容。", level="info")
        else:
            draft_box.set_status("复制失败，草稿仍保留。", level="error")

    def on_draft_send(text: str):
        if draft_box is None:
            return
        def _do_send():
            status = paste_recovery_text(backend, text)
            _log(f"[UI] Send Draft Box text: {status.value}")
            if status == RecoveryStatus.INSERTED:
                draft_box.set_status(
                    "已投递到目标；草稿仍保留，不会自动按 Enter。",
                    level="success",
                )
                show_quiet_notice("草稿已投递", "success", 1600)
                return
            if status == RecoveryStatus.TARGET_REQUIRED:
                message = "未投递：焦点仍在 Aria。请回到目标窗口后再试。"
            elif status == RecoveryStatus.TARGET_UNAVAILABLE:
                message = "未投递：无法确认目标窗口。草稿没有丢失。"
            elif status == RecoveryStatus.OUTPUT_UNAVAILABLE:
                message = "未投递：输入通道尚未就绪。草稿没有丢失。"
            else:
                message = "投递失败，草稿没有丢失，也没有自动按 Enter。"
            draft_box.set_status(message, level="error")
            draft_box.show_for_editing()

        QTimer.singleShot(300, _do_send)

    if DRAFT_BOX_ENABLED and draft_box is not None and action_draft_box is not None:
        ball.draftBoxRequested.connect(show_draft_box)
        action_draft_box.triggered.connect(show_draft_box)
        bridge.draftRequested.connect(on_draft_requested)
        draft_box.loadLastRequested.connect(on_draft_load_last)
        draft_box.copyRequested.connect(on_draft_copy)
        draft_box.sendRequested.connect(on_draft_send)

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

    # Handle OCR-tier change from popup menu (off / auto / full).
    # set_ocr_mode persists to hotwords.json and tears down/spins up the
    # auto-hotword tracker as needed.
    def on_ocr_mode_changed(mode):
        _log(f"[Aria] OCR mode changed: {mode}")
        if hasattr(backend, "set_ocr_mode"):
            backend.set_ocr_mode(mode)
        if hasattr(settings, "set_ocr_mode"):
            settings.set_ocr_mode(mode)

    ball.ocrModeChanged.connect(on_ocr_mode_changed)

    # Handle capture-mode change from popup menu (off / standard / whisper).
    # set_capture_mode persists to hotwords.json's audio.capture_mode block
    # and the ASR worker reads self._capture_mode each utterance, so the
    # next speech segment will see the new DSP preset.
    def on_capture_mode_changed(mode):
        _log(f"[Aria] Capture mode changed: {mode}")
        try:
            if hasattr(backend, "set_capture_mode"):
                backend.set_capture_mode(mode)
            applied = (
                backend.get_capture_mode()
                if hasattr(backend, "get_capture_mode")
                else mode
            )
            ball.set_capture_mode(applied)
            if hasattr(backend, "get_mic_input_gain"):
                ball.set_mic_input_gain(backend.get_mic_input_gain())
            labels = {
                "standard": "正常模式",
                "noisy": "嘈杂模式",
                "whisper": "轻语模式",
            }
            show_quiet_notice(
                f"收音模式已切换：{labels.get(applied, applied)}", "success", 2200
            )
        except Exception as exc:
            _log(f"[Aria] Capture mode switch failed: {exc}")
            show_quiet_notice(f"收音模式切换失败：{exc}", "error", 3600)

    ball.captureModeChanged.connect(on_capture_mode_changed)

    # Handle text output-mode change from popup menu (clipboard Ctrl+V paste
    # vs typewriter per-character injection). set_output_mode flips the live
    # injector flag and persists output.typewriter_mode — the same key the
    # settings page edits — so the next transcription uses the new mode and
    # both UIs stay consistent.
    def on_output_mode_changed(mode):
        _log(f"[Aria] Output mode changed: {mode}")
        if not backend or not hasattr(backend, "set_output_mode"):
            return
        try:
            applied = backend.set_output_mode(mode)
            ball.set_output_mode(applied)
            # Keep an already-open settings window coherent: its save_config
            # snapshots the typewriter checkbox, which would otherwise roll
            # this switch back on the next unrelated save.
            _sync_settings_output_mode(settings, applied)
            label = "剪贴板粘贴" if applied == "clipboard" else "逐字输入"
            show_quiet_notice(f"输出方式：{label}", "success", 1800)
        except Exception as exc:
            _log(f"[Aria] Output mode switch failed: {exc}")
            show_quiet_notice(f"切换输出方式失败: {exc}", "warning", 2400)

    ball.outputModeChanged.connect(on_output_mode_changed)

    def on_mic_input_gain_changed(gain):
        _log(f"[Aria] Mic input gain changed: {gain:.2f}x")
        if not backend or not hasattr(backend, "set_mic_input_gain"):
            return
        try:
            applied = backend.set_mic_input_gain(gain)
            ball.set_mic_input_gain(applied)
        except Exception as exc:
            _log(f"[Aria] Mic input gain update failed: {exc}")
            show_quiet_notice(f"麦克风音量调整失败: {exc}", "warning", 2200)

    ball.micInputGainChanged.connect(on_mic_input_gain_changed)

    # Handle ASR CPU/GPU mode change from popup menu.
    def on_asr_device_mode_changed(mode):
        _log(f"[Aria] ASR device mode changed: {mode}")
        if not backend or not hasattr(backend, "set_asr_device_mode"):
            return
        try:
            status = backend.set_asr_device_mode(mode)
            status_json = json.dumps(status, ensure_ascii=False)
            ball.set_asr_status(status_json)
            if isinstance(status, dict) and not status.get("can_request_gpu", True):
                # sherpa/llamacpp runtimes refuse the torch-profile quick
                # switch (backend no-op) — never show a success toast here.
                engine = str(status.get("engine") or "")
                hint = (
                    "轻量引擎模式下暂不支持此切换"
                    if engine == "qwen3_sherpa"
                    else "GPU 加速引擎模式下暂不支持此切换"
                )
                show_quiet_notice(hint, "warning", 2400)
                return
            label = "显卡加速" if mode == "gpu" else "CPU加速"
            show_quiet_notice(f"已切换为{label}", "success", 1800)
        except Exception as exc:
            _log(f"[Aria] ASR device mode switch failed: {exc}")
            show_quiet_notice(f"切换识别方式失败: {exc}", "warning", 2400)

    ball.asrDeviceModeChanged.connect(on_asr_device_mode_changed)

    # Handle cross-engine switch (llamacpp GPU <-> sherpa CPU) from popup menu.
    # The hot reload runs on a background thread (sherpa ~8s, llamacpp ~18s);
    # completion/rollback arrives later via bridge.asrStatusChanged, and the
    # backend already toasts refusals/failures through emit_error.
    def on_asr_engine_mode_changed(engine):
        _log(f"[Aria] ASR engine mode changed: {engine}")
        if not backend or not hasattr(backend, "set_asr_engine_mode"):
            return
        try:
            runtime_status = (
                backend.get_asr_runtime_status()
                if hasattr(backend, "get_asr_runtime_status")
                else {}
            )
            target_status = next(
                (
                    item
                    for item in runtime_status.get("engine_targets", [])
                    if isinstance(item, dict) and item.get("engine") == engine
                ),
                {},
            )
            needs_install = bool(
                engine == "qwen3_llamacpp"
                and not target_status.get("available", False)
                and target_status.get("installable", False)
            )
            if needs_install:
                reply = QMessageBox.question(
                    ball,
                    "安装 GPU 加速",
                    "GPU 加速组件尚未安装，需要下载约 3 GB。\n\n"
                    "安装期间仍可继续使用 CPU 语音输入；下载、完整性校验和 "
                    "NVIDIA 显卡实机验证通过后，Aria 会自动切换到 GPU，"
                    "无需重启。\n\n现在开始安装吗？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    _log("[Aria] In-app GPU install cancelled by user")
                    return
                if not hasattr(backend, "install_gpu_engine"):
                    raise RuntimeError("当前版本缺少应用内 GPU 安装组件")
                status = backend.install_gpu_engine()
            else:
                status = backend.set_asr_engine_mode(engine)
            status_json = json.dumps(status, ensure_ascii=False)
            ball.set_asr_status(status_json)
            if isinstance(status, dict) and status.get("gpu_installing"):
                # set_asr_status owns a persistent in-place percentage notice;
                # do not overwrite it with a short-lived generic toast.
                pass
            elif isinstance(status, dict) and status.get("hot_reloading"):
                label = (
                    "GPU 加速引擎" if engine == "qwen3_llamacpp" else "轻量引擎"
                )
                show_quiet_notice(f"正在切换到{label}，需要几秒…", "info", 2600)
        except Exception as exc:
            _log(f"[Aria] ASR engine mode switch failed: {exc}")
            show_quiet_notice(f"切换识别引擎失败: {exc}", "warning", 2400)

    ball.asrEngineModeChanged.connect(on_asr_engine_mode_changed)

    def on_api_switch_back_requested():
        _log("[Aria] API switch-back requested from UI")
        if backend and hasattr(backend, "switch_api_to_primary"):
            status = backend.switch_api_to_primary()
            status_json = json.dumps(status, ensure_ascii=False)
            settings.update_api_status(status_json)
            ball.set_api_status(status_json)
            show_quiet_notice("后续润色已切回主 API", "success", 1800)

    settings.apiSwitchBackRequested.connect(on_api_switch_back_requested)
    ball.apiSwitchBackRequested.connect(on_api_switch_back_requested)

    def on_deepseek_setup_requested():
        """Collect the key securely; backend owns validation and persistence."""
        if not backend or not hasattr(backend, "configure_deepseek_api"):
            show_quiet_notice("当前版本不支持一键配置 DeepSeek", "warning", 2600)
            return

        api_key, accepted = QInputDialog.getText(
            ball,
            "配置 DeepSeek API",
            "粘贴 DeepSeek API Key：\n"
            "Aria 会自动配置 DeepSeek Flash，并在保存前验证是否可用。\n"
            "API Key 只保存在本机，并使用 Windows 加密。",
            QLineEdit.Password,
        )
        if not accepted:
            return
        api_key = str(api_key or "").strip()
        if not api_key:
            show_quiet_notice("请输入 DeepSeek API Key", "warning", 2200)
            return

        if backend.configure_deepseek_api(api_key):
            show_quiet_notice("正在验证 DeepSeek API…", "info", 3200)
        else:
            status = backend.get_api_status()
            if status.get("setup_in_progress"):
                show_quiet_notice("DeepSeek API 正在验证，请稍候", "info", 2400)
            else:
                show_quiet_notice("无法开始配置，请稍后重试", "warning", 2600)

    ball.deepseekSetupRequested.connect(on_deepseek_setup_requested)

    # Handle deep sleep toggle from popup menu
    def on_deep_sleep_toggled(deep):
        _log(f"[Aria] Deep sleep toggled via UI: {deep}")
        if hasattr(backend, "set_deep_sleep"):
            backend.set_deep_sleep(deep)

    ball.deepSleepToggled.connect(on_deep_sleep_toggled)

    # Tray menu deep sleep toggle
    def on_tray_deep_sleep_toggled(checked):
        _log(f"[Aria] Deep sleep toggled via tray: {checked}")
        if hasattr(backend, "set_deep_sleep"):
            backend.set_deep_sleep(checked)

    action_deep_sleep.triggered.connect(on_tray_deep_sleep_toggled)

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
            show_quiet_notice(
                f"翻译输出：{'弹窗显示' if mode == 'popup' else '复制到剪贴板'}",
                "success",
                1800,
            )
        except Exception as e:
            _log(f"[Aria] Failed to save translate mode: {e}")

    ball.translateModeChanged.connect(on_translate_mode_changed)

    # Load and sync initial translate mode
    try:
        import json
        from aria.core.utils import get_config_path

        config_path = get_config_path("hotwords.json")
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        translate_mode = config.get("translation", {}).get("output_mode", "popup")
        ball.set_translate_mode(translate_mode)
    except Exception:
        pass  # Default to popup mode

    # Sync initial mode from backend to popup menu
    if hasattr(backend, "get_polish_mode"):
        initial_mode = backend.get_polish_mode()
        ball.set_polish_mode(initial_mode)
        _log(f"[Aria] Initial polish mode: {initial_mode}")

    if hasattr(backend, "get_ocr_mode"):
        initial_ocr_mode = backend.get_ocr_mode()
        ball.set_ocr_mode(initial_ocr_mode)
        if hasattr(settings, "set_ocr_mode"):
            settings.set_ocr_mode(initial_ocr_mode)
        _log(f"[Aria] Initial OCR mode: {initial_ocr_mode}")

    if hasattr(backend, "get_capture_mode"):
        initial_capture_mode = backend.get_capture_mode()
        ball.set_capture_mode(initial_capture_mode)
        _log(f"[Aria] Initial capture mode: {initial_capture_mode}")

    if hasattr(backend, "get_mic_input_gain"):
        initial_mic_gain = backend.get_mic_input_gain()
        ball.set_mic_input_gain(initial_mic_gain)
        _log(f"[Aria] Initial mic input gain: {initial_mic_gain:.2f}x")

    if hasattr(backend, "get_output_mode"):
        initial_output_mode = backend.get_output_mode()
        ball.set_output_mode(initial_output_mode)
        _log(f"[Aria] Initial output mode: {initial_output_mode}")

    # Set simple popup subtitle; avoid technical model names for normal users.
    def _get_engine_display_name() -> str:
        return "语音输入"

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

        # Step 3: Stop backend (ASR, audio capture, hotkey listener).
        # Full process exit does not need explicit model unload: the OS tears
        # down CUDA/Python resources. Skipping it removes the invisible
        # 2-8s shutdown window after the tray icon disappears.
        if hasattr(backend, "stop"):
            try:
                backend.stop(unload_asr=False)
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
        ball.set_enabled_state(False)

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
                backend.stop(unload_asr=False)
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
        saved_mode = config.get("polish_mode", "quality")
        ball.set_polish_mode(saved_mode)
        _log(f"[Aria] Settings saved, polish mode synced: {saved_mode}")

        # Sync popup output-mode card with the settings page's
        # output.typewriter_mode checkbox (same config key both ways).
        output_cfg = config.get("output", {}) or {}
        ball.set_output_mode(
            "typewriter" if output_cfg.get("typewriter_mode") else "clipboard"
        )

    settings.settingsSaved.connect(on_settings_saved)

    # Show floating ball
    ball.show()
    # Flush the first native show event before telling the separate splash
    # process to fade.  This guarantees that at least one Aria surface is
    # already visible when the startup window disappears.
    app.processEvents()

    _log("Aria Floating Ball is now visible.")
    _log("  - Left-click: Toggle recording")
    _log("  - Right-click: Show popup menu")
    _log("  - Middle-click: Lock position")
    _log("  - Drag: Move ball (when unlocked)")
    _log("  - System tray single-click: Show history (Ctrl+1-9 to copy)")
    _log("  - System tray double-click: Open hotwords settings")

    if callable(on_ui_ready):
        try:
            on_ui_ready()
            _log("[STARTUP] Floating ball ready signal delivered")
        except Exception as exc:
            # Splash reporting is best-effort and must never prevent the main
            # event loop from starting.  Its own 60-second timeout is the final
            # bounded fallback if the IPC bridge has failed.
            _log(f"[STARTUP] Floating ball ready callback failed: {exc}")

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
