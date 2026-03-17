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
_DEBUG_LOG_INITIALIZED = False


def _debug(msg: str):
    """Write debug message to file (pythonw.exe safe)."""
    global _DEBUG_LOG_INITIALIZED
    import datetime
    import sys

    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}\n"
    # Guard for pythonw.exe (sys.stdout is None)
    if sys.stdout is not None:
        print(line.strip())

    # Lazy initialization of debug log directory (with error handling)
    if not _DEBUG_LOG_INITIALIZED:
        try:
            _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
            _DEBUG_LOG_INITIALIZED = True
        except (OSError, PermissionError):
            # Can't create log directory, just skip file logging
            return

    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except (OSError, PermissionError):
        # Can't write to log file, skip silently
        pass


if TYPE_CHECKING:
    from ui.qt.bridge import QtBridge


# Selection command type mapping
SELECTION_COMMAND_MAP = {
    "polish": "POLISH",
    "translate_en": "TRANSLATE_EN",
    "translate_zh": "TRANSLATE_ZH",
    "translate_ja": "TRANSLATE_JA",
    "expand": "EXPAND",
    "summarize": "SUMMARIZE",
    "rewrite": "REWRITE",
}


class WakewordExecutor:
    """
    Executes wakeword commands by calling app methods.

    Unlike CommandExecutor (sends keystrokes), this executor:
    - Calls application methods directly (set_auto_send, etc)
    - Handles selection processing (润色, 翻译, etc.)
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
            app_instance: AriaApp instance with setter methods
            bridge: QtBridge for UI notification (optional)
            cooldown_ms: Minimum time between commands
        """
        self.app = app_instance
        self.bridge = bridge
        self.cooldown_ms = cooldown_ms
        self._last_exec_time = 0.0
        self._exec_count = 0
        self._pending_following_text: Optional[str] = None

        # Action -> Method mapping
        self._action_map: Dict[str, Callable[[Any], bool]] = {
            "set_auto_send": self._set_auto_send,
            "set_sleeping": self._set_sleeping,
            "selection_process": self._selection_process,
            "translate_popup": self._translate_popup,
            "summarize_popup": self._summarize_popup,
            "ask_ai": self._ask_ai,
            "save_highlight": self._save_highlight,
            "reply_popup": self._reply_popup,
        }

    def execute(
        self,
        cmd_id: str,
        action: str,
        value: Any,
        response: str = "",
        following_text: Optional[str] = None,
    ) -> bool:
        """
        Execute a wakeword command.

        Args:
            cmd_id: Command identifier (e.g., "auto_send_on")
            action: Method name to call (e.g., "set_auto_send")
            value: Value to pass to method
            response: Optional response message
            following_text: Text following the trigger (for capture_following commands)

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

        # Store following_text for handlers that need it
        self._pending_following_text = following_text

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
            import traceback

            error_msg = f"[WAKEWORD] Error executing {cmd_id}: {e}"
            print(error_msg)
            _debug(error_msg)
            _debug(traceback.format_exc())
            # Emit failure signal
            if self.bridge and hasattr(self.bridge, "emit_command"):
                self.bridge.emit_command(cmd_id, False)
            return False
        finally:
            self._pending_following_text = None

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

    def _save_highlight(self, config: Dict[str, Any]) -> bool:
        """Save highlight with following text to InsightStore."""
        from datetime import datetime

        following_text = self._pending_following_text
        if not following_text or not following_text.strip():
            _debug("[HIGHLIGHT] No content to save")
            if self.bridge and hasattr(self.bridge, "emit_error"):
                self.bridge.emit_error("未检测到要记录的内容")
            return False

        if not hasattr(self.app, "insight_store") or not self.app.insight_store:
            _debug("[HIGHLIGHT] InsightStore not available")
            if self.bridge and hasattr(self.bridge, "emit_error"):
                self.bridge.emit_error("存储服务不可用")
            return False

        attributes = {
            "importance": config.get("importance", "high"),
            "tags": config.get("tags", []),
        }

        timestamp = datetime.now().isoformat()
        success = self.app.insight_store.add(
            text=following_text.strip(),
            timestamp=timestamp,
            entry_type="highlight",
            attributes=attributes,
        )

        if success:
            _debug(f"[HIGHLIGHT] Saved: {following_text[:50]}...")
            if self.bridge and hasattr(self.bridge, "emit_highlight_saved"):
                self.bridge.emit_highlight_saved(
                    following_text[:50], attributes["tags"]
                )
            return True
        return False

    def _selection_process(self, command_type: str) -> bool:
        """
        Process selected text with the specified command.

        This is the ONLY way to trigger selection processing - via wakeword.
        Normal dictation will NEVER trigger selection detection.

        Args:
            command_type: One of "polish", "translate_en", "translate_zh",
                         "expand", "summarize", "rewrite"

        Returns:
            True if processing succeeded, False otherwise
        """
        _debug(f"_selection_process({command_type}) called")

        # Import here to avoid circular imports
        from ..selection import SelectionCommand, CommandType

        # Validate command type
        cmd_type_str = SELECTION_COMMAND_MAP.get(command_type)
        if not cmd_type_str:
            _debug(f"Unknown selection command type: {command_type}")
            return False

        # Get CommandType enum
        try:
            cmd_type = CommandType[cmd_type_str]
        except KeyError:
            _debug(f"Invalid CommandType: {cmd_type_str}")
            return False

        # Check if app has required components
        if (
            not hasattr(self.app, "selection_detector")
            or not self.app.selection_detector
        ):
            _debug("No selection_detector available")
            return False
        if (
            not hasattr(self.app, "selection_processor")
            or not self.app.selection_processor
        ):
            _debug("No selection_processor available")
            return False

        # Step 1: Detect selected text (sends Ctrl+C)
        _debug("Detecting selection...")
        detection = self.app.selection_detector.detect()

        if not detection.has_selection or not detection.selected_text:
            _debug("No text selected, cannot process")
            print("[SELECTION] 未检测到选中文本")
            # Notify UI
            if self.bridge and hasattr(self.bridge, "emit_error"):
                self.bridge.emit_error("未检测到选中文本，请先选中要处理的文字")
            return False

        _debug(f"Found selected text: {len(detection.selected_text)} chars")
        print(f"[SELECTION] 检测到选中文本: {len(detection.selected_text)} 字符")

        # === Translation commands: check output mode setting ===
        if command_type in ("translate_en", "translate_zh", "translate_ja"):
            translation_config = self._get_translation_config()
            output_mode = translation_config.get("output_mode", "popup")
            target_lang_map = {
                "translate_en": "en",
                "translate_zh": "zh",
                "translate_ja": "ja",
            }
            target_lang = target_lang_map.get(command_type, "en")

            _debug(f"Translation output mode: {output_mode}, target: {target_lang}")

            # Restore clipboard before UI action
            if detection.original_clipboard is not None:
                self.app.selection_detector.restore_clipboard(
                    detection.original_clipboard
                )

            if output_mode == "clipboard":
                # Clipboard mode: translate and copy to clipboard
                return self._translate_to_clipboard(
                    detection.selected_text.strip(), target_lang
                )
            else:
                # Popup mode: show translation in popup
                return self._translate_popup_with_target(
                    detection.selected_text.strip(), target_lang
                )

        # Step 2: Create SelectionCommand
        selection_cmd = SelectionCommand(
            command_type=cmd_type,
            raw_text=command_type,
        )

        # Step 3: Process with LLM
        _debug(f"Processing with command type: {cmd_type.name}")
        result = self.app.selection_processor.process(
            detection.selected_text, selection_cmd
        )

        if result.success and result.output_text:
            # Step 4: Replace selected text with result
            self.app.output_injector.insert_text(result.output_text)
            _debug(
                f"Selection processed OK: {len(result.output_text)} chars, {result.processing_time_ms:.0f}ms"
            )
            print(f"[SELECTION] 处理完成 ({result.processing_time_ms:.0f}ms)")

            # Restore original clipboard
            if detection.original_clipboard is not None:
                self.app.selection_detector.restore_clipboard(
                    detection.original_clipboard
                )

            return True
        else:
            _debug(f"Selection processing failed: {result.error}")
            print(f"[SELECTION] 处理失败: {result.error}")
            if self.bridge and hasattr(self.bridge, "emit_error"):
                self.bridge.emit_error(f"处理失败: {result.error}")
            return False

    def _translate_popup(self, _value) -> bool:
        """
        Show translation popup for selected text (v1.1 feature).

        Unlike selection_process which replaces text, this:
        1. Detects selected text
        2. Immediately restores clipboard
        3. Emits TranslationAction to UI (non-blocking)
        4. UI worker handles actual translation

        Returns:
            True if action emitted successfully
        """
        _debug("_translate_popup() called")

        # Check if app has selection detector
        if (
            not hasattr(self.app, "selection_detector")
            or not self.app.selection_detector
        ):
            _debug("No selection_detector available")
            return False

        # Detect selected text
        _debug("Detecting selection for translation popup...")
        detection = self.app.selection_detector.detect()

        # Immediately restore clipboard (before any processing)
        try:
            if not detection.has_selection or not detection.selected_text:
                _debug("No text selected for translation popup")
                print("[TRANSLATE_POPUP] 未检测到选中文本")
                if self.bridge and hasattr(self.bridge, "emit_error"):
                    self.bridge.emit_error("未检测到选中文本，请先选中要翻译的文字")
                return False

            selected_text = detection.selected_text.strip()
            text_len = len(selected_text)
            _debug(f"Found text for translation: {text_len} chars")

            # Text length validation
            MAX_TRANSLATE_LEN = 500
            MIN_TRANSLATE_LEN = 2

            if text_len < MIN_TRANSLATE_LEN:
                _debug(f"Text too short: {text_len} chars")
                print(f"[TRANSLATE_POPUP] 选中文本过短 ({text_len} 字符)")
                if self.bridge and hasattr(self.bridge, "emit_error"):
                    self.bridge.emit_error("选中文本过短，请选择更多内容")
                return False

            if text_len > MAX_TRANSLATE_LEN:
                _debug(f"Text too long: {text_len} > {MAX_TRANSLATE_LEN}, truncating")
                print(f"[TRANSLATE_POPUP] 文本过长，已截断至 {MAX_TRANSLATE_LEN} 字符")
                selected_text = selected_text[:MAX_TRANSLATE_LEN]

            # Log preview of source text for debugging
            preview = selected_text[:50].replace(chr(10), " ").replace(chr(13), "")
            print(f"[TRANSLATE_POPUP] 源文本: {preview}...")

        finally:
            # Always restore clipboard
            if detection.original_clipboard is not None:
                self.app.selection_detector.restore_clipboard(
                    detection.original_clipboard
                )
                _debug("Clipboard restored")
            _debug("Finally block completed")

        _debug("=== AFTER FINALLY BLOCK ===")
        _debug(f"selected_text defined: {'selected_text' in dir()}")

        # Emit TranslationAction to UI (non-blocking)
        _debug("About to import TranslationAction...")
        try:
            from ..action import TranslationAction

            _debug(f"Creating TranslationAction with {len(selected_text)} chars...")
            action = TranslationAction(source_text=selected_text)
            _debug(f"TranslationAction created: {action.request_id}")

            if self.bridge and hasattr(self.bridge, "emit_action"):
                _debug("Calling bridge.emit_action...")
                self.bridge.emit_action(action)
                _debug(f"TranslationAction emitted: {action.request_id}")
                print(f"[TRANSLATE_POPUP] 已发送翻译请求 ({len(selected_text)} 字符)")
                return True
            else:
                _debug("No bridge.emit_action available")
                return False
        except Exception as e:
            _debug(f"ERROR in translate_popup emit: {e}")
            import traceback

            _debug(traceback.format_exc())
            return False

    def _summarize_popup(self, _value) -> bool:
        """
        Show summary popup for selected text (v1.1 feature).

        Flow mirrors translate_popup:
        1. Detect selected text
        2. Restore clipboard immediately
        3. Emit SummaryAction to UI (non-blocking)
        4. UI worker handles summarization

        Returns:
            True if action emitted successfully
        """
        _debug("_summarize_popup() called")

        if (
            not hasattr(self.app, "selection_detector")
            or not self.app.selection_detector
        ):
            _debug("No selection_detector available")
            return False

        _debug("Detecting selection for summary popup...")
        detection = self.app.selection_detector.detect()

        try:
            if not detection.has_selection or not detection.selected_text:
                _debug("No text selected for summary popup")
                print("[SUMMARY_POPUP] 未检测到选中文本")
                if self.bridge and hasattr(self.bridge, "emit_error"):
                    self.bridge.emit_error("未检测到选中文本，请先选中要总结的内容")
                return False

            selected_text = detection.selected_text.strip()
            text_len = len(selected_text)
            _debug(f"Found text for summary: {text_len} chars")

            MIN_SUMMARY_LEN = 20
            MAX_SUMMARY_LEN = 20000

            if text_len < MIN_SUMMARY_LEN:
                _debug(f"Text too short for summary: {text_len} chars")
                print(f"[SUMMARY_POPUP] 选中文本过短 ({text_len} 字符)")
                if self.bridge and hasattr(self.bridge, "emit_error"):
                    self.bridge.emit_error("选中文本过短，请选择更多内容")
                return False

            if text_len > MAX_SUMMARY_LEN:
                _debug(f"Text too long for summary: {text_len} > {MAX_SUMMARY_LEN}")
                print(f"[SUMMARY_POPUP] 选中文本过长 ({text_len} 字符)")
                if self.bridge and hasattr(self.bridge, "emit_error"):
                    self.bridge.emit_error(
                        f"选中文本过长，请控制在 {MAX_SUMMARY_LEN} 字符以内"
                    )
                return False

        finally:
            if detection.original_clipboard is not None:
                self.app.selection_detector.restore_clipboard(
                    detection.original_clipboard
                )
                _debug("Clipboard restored")

        try:
            from ..action import SummaryAction

            action = SummaryAction(source_text=selected_text)
            if self.bridge and hasattr(self.bridge, "emit_action"):
                self.bridge.emit_action(action)
                _debug(f"SummaryAction emitted: {action.request_id}")
                print(f"[SUMMARY] 已发送总结请求 ({len(selected_text)} 字符)")
                return True
            else:
                _debug("No bridge.emit_action available")
                return False
        except Exception as e:
            _debug(f"ERROR in _summarize_popup: {e}")
            return False

    def _ask_ai(self, _value) -> bool:
        """
        Open AI chat dialog with selected text as context (v1.1 feature).

        Unlike selection_process which replaces text, this:
        1. Detects selected text
        2. Immediately restores clipboard
        3. Emits ChatAction to UI (non-blocking)
        4. UI opens chat window with context

        Returns:
            True if action emitted successfully
        """
        _debug("_ask_ai() called")

        # Check if app has selection detector
        if (
            not hasattr(self.app, "selection_detector")
            or not self.app.selection_detector
        ):
            _debug("No selection_detector available")
            return False

        # Detect selected text
        _debug("Detecting selection for AI chat...")
        detection = self.app.selection_detector.detect()

        # Immediately restore clipboard (before any processing)
        try:
            if not detection.has_selection or not detection.selected_text:
                _debug("No text selected for AI chat")
                print("[ASK_AI] 未检测到选中文本")
                if self.bridge and hasattr(self.bridge, "emit_error"):
                    self.bridge.emit_error("未检测到选中文本，请先选中要询问的内容")
                return False

            selected_text = detection.selected_text
            _debug(f"Found text for AI chat: {len(selected_text)} chars")

        finally:
            # Always restore clipboard
            if detection.original_clipboard is not None:
                self.app.selection_detector.restore_clipboard(
                    detection.original_clipboard
                )
                _debug("Clipboard restored")

        # Emit ChatAction to UI (non-blocking)
        from ..action import ChatAction

        action = ChatAction(context_text=selected_text)
        if self.bridge and hasattr(self.bridge, "emit_action"):
            self.bridge.emit_action(action)
            _debug(f"ChatAction emitted: {action.request_id}")
            print(f"[ASK_AI] 已发送AI对话请求 ({len(selected_text)} 字符)")
            return True
        else:
            _debug("No bridge.emit_action available")
            return False

    def _reply_popup(self, _value) -> bool:
        """
        Show reply popup for selected text (v1.2 feature).

        Flow mirrors translate_popup:
        1. Detect selected text (the message to reply to)
        2. Restore clipboard immediately
        3. Emit ReplyAction to UI (non-blocking)
        4. UI worker generates reply via LLM
        5. Popup shows suggested reply

        capture_following text (e.g., "语气强硬一点") is passed as style_hint.

        Returns:
            True if action emitted successfully
        """
        _debug("_reply_popup() called")

        # Check if app has selection detector
        if (
            not hasattr(self.app, "selection_detector")
            or not self.app.selection_detector
        ):
            _debug("No selection_detector available")
            return False

        # Detect selected text
        _debug("Detecting selection for reply popup...")
        detection = self.app.selection_detector.detect()

        try:
            if not detection.has_selection or not detection.selected_text:
                _debug("No text selected for reply popup")
                print("[REPLY_POPUP] 未检测到选中文本")
                if self.bridge and hasattr(self.bridge, "emit_error"):
                    self.bridge.emit_error("未检测到选中文本，请先选中要回复的消息")
                return False

            selected_text = detection.selected_text.strip()
            text_len = len(selected_text)
            _debug(f"Found text for reply: {text_len} chars")

            # Text length validation
            MAX_REPLY_LEN = 2000
            MIN_REPLY_LEN = 2

            if text_len < MIN_REPLY_LEN:
                _debug(f"Text too short: {text_len} chars")
                print(f"[REPLY_POPUP] 选中文本过短 ({text_len} 字符)")
                if self.bridge and hasattr(self.bridge, "emit_error"):
                    self.bridge.emit_error("选中文本过短，请选择更多内容")
                return False

            if text_len > MAX_REPLY_LEN:
                _debug(f"Text too long: {text_len} > {MAX_REPLY_LEN}, truncating")
                print(f"[REPLY_POPUP] 文本过长，已截断至 {MAX_REPLY_LEN} 字符")
                selected_text = selected_text[:MAX_REPLY_LEN]

            # Log preview
            preview = selected_text[:50].replace(chr(10), " ").replace(chr(13), "")
            print(f"[REPLY_POPUP] 源消息: {preview}...")

        finally:
            # Always restore clipboard
            if detection.original_clipboard is not None:
                self.app.selection_detector.restore_clipboard(
                    detection.original_clipboard
                )
                _debug("Clipboard restored")

        # Get style hint from capture_following (e.g., "语气强硬一点")
        style_hint = self._pending_following_text
        if style_hint:
            _debug(f"Style hint from following text: '{style_hint}'")

        # Emit ReplyAction to UI (non-blocking)
        try:
            from ..action import ReplyAction

            action = ReplyAction(
                source_text=selected_text,
                style_hint=style_hint,
            )

            if self.bridge and hasattr(self.bridge, "emit_action"):
                self.bridge.emit_action(action)
                _debug(f"ReplyAction emitted: {action.request_id}")
                print(f"[REPLY] 已发送回复请求 ({len(selected_text)} 字符)")
                return True
            else:
                _debug("No bridge.emit_action available")
                return False
        except Exception as e:
            _debug(f"ERROR in _reply_popup: {e}")
            import traceback

            _debug(traceback.format_exc())
            return False

    def _get_translation_config(self) -> Dict[str, Any]:
        """Get translation configuration from hotwords.json."""
        try:
            import json

            config_path = (
                Path(__file__).parent.parent.parent / "config" / "hotwords.json"
            )
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            return config.get("translation", {})
        except Exception as e:
            _debug(f"Failed to load translation config: {e}")
            return {}

    def _translate_popup_with_target(self, source_text: str, target_lang: str) -> bool:
        """
        Show translation popup with specified target language.

        Args:
            source_text: Text to translate
            target_lang: Target language ("en" or "zh")

        Returns:
            True if action emitted successfully
        """
        _debug(f"_translate_popup_with_target({len(source_text)} chars, {target_lang})")

        try:
            from ..action import TranslationAction

            action = TranslationAction(source_text=source_text, target_lang=target_lang)

            if self.bridge and hasattr(self.bridge, "emit_action"):
                self.bridge.emit_action(action)
                _debug(f"TranslationAction emitted: {action.request_id}")
                print(
                    f"[TRANSLATE] 已发送翻译请求 ({len(source_text)} 字符) -> {target_lang}"
                )
                return True
            else:
                _debug("No bridge.emit_action available")
                return False
        except Exception as e:
            _debug(f"ERROR in _translate_popup_with_target: {e}")
            return False

    def _translate_to_clipboard(self, source_text: str, target_lang: str) -> bool:
        """
        Translate and copy result to clipboard.

        Args:
            source_text: Text to translate
            target_lang: Target language ("en" or "zh")

        Returns:
            True if action emitted successfully
        """
        _debug(f"_translate_to_clipboard({len(source_text)} chars, {target_lang})")

        try:
            from ..action import ClipboardTranslationAction

            action = ClipboardTranslationAction(
                source_text=source_text, target_lang=target_lang
            )

            if self.bridge and hasattr(self.bridge, "emit_action"):
                self.bridge.emit_action(action)
                _debug(f"ClipboardTranslationAction emitted: {action.request_id}")
                print(
                    f"[TRANSLATE] 已发送剪贴板翻译请求 ({len(source_text)} 字符) -> {target_lang}"
                )
                return True
            else:
                _debug("No bridge.emit_action available")
                return False
        except Exception as e:
            _debug(f"ERROR in _translate_to_clipboard: {e}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Get executor statistics."""
        return {
            "total_executions": self._exec_count,
            "cooldown_ms": self.cooldown_ms,
            "available_actions": list(self._action_map.keys()),
        }
