"""
Wakeword Detector
=================
Detects wakeword and parses following commands.
Uses pinyin-based matching for ASR variant tolerance.
"""

import json
import re
import unicodedata
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, Iterable

from ..utils import get_config_path
from ..utils.phonetic import get_matcher, PinyinMatcher


class WakewordDetector:
    """
    Detects wakeword from transcribed text and parses commands.

    Design principles:
    - Pinyin-based wakeword matching (homophone tolerance)
    - Multi-trigger command matching (开启/打开/etc)
    - Returns structured result for executor
    """

    # Keep the original product wakeword usable after the user chooses a
    # personalized primary wakeword.  `available_wakewords` is only the UI
    # choice list and must not silently turn every option into a live alias.
    _COMPATIBILITY_WAKEWORD_ALIASES = ("小助手",)

    # These legacy preset IDs have been superseded by the generic wakeword AI
    # rewrite path or by an explicit popup tool (summary uses summary_popup).
    # Keep the denylist in code as a fail-closed fallback for upgraded personal
    # configs; the tracked template carries the same list so package migration
    # can physically remove the retired entries as well.
    _RETIRED_STOCK_COMMAND_IDS = frozenset(
        {
            "selection_polish",
            "selection_expand",
            "selection_summarize",
            "selection_rewrite",
        }
    )

    # ASR can preserve a short hesitation immediately before a direct address,
    # for example ``呃，阿蓝把上一句话……``.  That is still a prefix-scoped
    # invocation, not an in-sentence wakeword mention.  Keep this list narrow:
    # arbitrary words such as ``我想让`` must never be stripped.
    _INVOCATION_LEAD_IN_FILLER_RE = re.compile(
        r"^(?:嗯+|呃+|额+|啊+|唔+|哦+|诶+|欸+|哎+)"
    )
    _INVOCATION_LEAD_IN_SEPARATORS = " \t\r\n，,、。.!！？?：:；;"

    # Fallback whitelist for "打开XX" natural phrases that aren't in the
    # explicit trigger list and aren't custom instructions either.
    # Only generic SELECTED-FILE-TYPE words are accepted here — app names
    # like "我的电脑" or "叉叉" stay unmatched so we don't silently fire
    # open_path with no selection and confuse the user.
    _OPEN_PATH_FALLBACK_PREFIXES = ("帮我打开", "打开")
    _OPEN_PATH_FALLBACK_MAX_SUFFIX_LEN = 12
    # Compound-word tails. "jpg图片" / "Excel表格" / "mp4视频" need the type
    # suffix stripped so the extension can be matched against TARGETS.
    _OPEN_PATH_FALLBACK_TAILS = (
        "文件",
        "文档",
        "表格",
        "图片",
        "图像",
        "照片",
        "视频",
        "音频",
    )
    _OPEN_PATH_FALLBACK_TARGETS = frozenset(
        {
            "文件",
            "文档",
            "资料",
            "图片",
            "图像",
            "照片",
            "相片",
            "截图",
            "视频",
            "影片",
            "录像",
            "音频",
            "音乐",
            "录音",
            "pdf",
            "word",
            "doc",
            "docx",
            "excel",
            "xls",
            "xlsx",
            "表格",
            "电子表格",
            "ppt",
            "pptx",
            "powerpoint",
            "幻灯片",
            "演示文稿",
            "markdown",
            "md",
            "txt",
            "文本",
            "csv",
            "html",
            "xml",
            "yaml",
            "yml",
            "toml",
            "ini",
            "json",
            "代码",
            "脚本",
            "日志",
            "log",
            "压缩包",
            "压缩文件",
            "zip",
            "rar",
            "7z",
            "exe",
            "安装包",
            "jpg",
            "jpeg",
            "png",
            "gif",
            "webp",
            "bmp",
            "svg",
            "mp4",
            "mov",
            "mkv",
            "avi",
            "webm",
            "mp3",
            "wav",
            "flac",
            "m4a",
            "电子书",
            "epub",
            "网页",
            "网站",
            "网址",
            "链接",
        }
    )

    def __init__(self, config_path: Optional[str] = None):
        self.enabled = False
        self.wakeword = "小助手"
        self.wakeword_aliases = list(self._COMPATIBILITY_WAKEWORD_ALIASES)
        self.available_wakewords = ["小助手", "小朋友", "小溪", "助手"]  # UI options
        self.commands: Dict[str, Any] = {}
        self.keyboard_commands: Dict[str, Any] = {}
        self.custom_instructions: list[Dict[str, Any]] = []
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

            self.wakeword = config.get("wakeword", "") or "小助手"
            configured_aliases = config.get(
                "wakeword_aliases", list(self._COMPATIBILITY_WAKEWORD_ALIASES)
            )
            if not isinstance(configured_aliases, list):
                configured_aliases = []
            self.wakeword_aliases = []
            for alias in [
                *configured_aliases,
                *self._COMPATIBILITY_WAKEWORD_ALIASES,
            ]:
                alias = str(alias).strip()
                if (
                    alias
                    and alias != self.wakeword
                    and alias not in self.wakeword_aliases
                ):
                    self.wakeword_aliases.append(alias)
            self.enabled = (
                True  # Always enabled — wakeword system is core functionality
            )
            self.available_wakewords = config.get(
                "available_wakewords", ["小助手", "小朋友", "小溪", "助手"]
            )
            configured_commands = config.get("commands", {})
            if not isinstance(configured_commands, dict):
                configured_commands = {}
            self.commands = self._merge_missing_stock_commands(
                config_path, configured_commands
            )
            self.keyboard_commands = self._load_keyboard_commands(config_path)
            custom_entries = config.get("custom_instructions", [])
            self.custom_instructions = self._normalize_custom_instructions(
                custom_entries
            )
            self.cooldown_ms = config.get("cooldown_ms", 500)

            print(
                f"[WAKEWORD] Loaded: '{self.wakeword}' "
                f"(pinyin matching, {len(self.commands)} commands, "
                f"{len(self.keyboard_commands)} keyboard commands, "
                f"{len(self.custom_instructions)} custom instructions, "
                f"{len(self.wakeword_aliases)} aliases)"
            )
        except Exception as e:
            print(f"[WAKEWORD] Failed to load config: {e}")

    @classmethod
    def _merge_missing_stock_commands(
        cls, wakeword_config_path: Path, configured_commands: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Merge current built-ins while removing retired legacy presets.

        Portable upgrades intentionally preserve the user's ``wakeword.json``.
        Without this in-memory merge, a newly added stock action would remain
        invisible forever on upgraded installs. Existing user/configured command
        definitions always win unless their stock command ID has been retired;
        only missing current IDs come from the sibling tracked template.
        """
        retired_ids = set(cls._RETIRED_STOCK_COMMAND_IDS)
        merged = {
            command_id: command_config
            for command_id, command_config in configured_commands.items()
            if command_id not in retired_ids
        }
        template_path = wakeword_config_path.with_name("wakeword.template.json")
        if not template_path.exists() or template_path == wakeword_config_path:
            return merged
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                template = json.load(f)
            configured_retired = template.get("retired_command_ids", ())
            if isinstance(configured_retired, list):
                retired_ids.update(
                    str(command_id).strip()
                    for command_id in configured_retired
                    if str(command_id).strip()
                )
                merged = {
                    command_id: command_config
                    for command_id, command_config in merged.items()
                    if command_id not in retired_ids
                }
            stock_commands = template.get("commands", {})
            if not isinstance(stock_commands, dict):
                return merged
            for command_id, command_config in stock_commands.items():
                if (
                    command_id not in retired_ids
                    and command_id not in merged
                    and isinstance(command_config, dict)
                ):
                    merged[command_id] = command_config
        except (OSError, ValueError, TypeError):
            pass
        return merged

    def _load_keyboard_commands(self, wakeword_config_path: Path) -> Dict[str, Any]:
        """Load legacy keyboard shortcut voice commands for unified detection.

        `commands.json` remains the storage/source of truth for keyboard
        shortcuts, but routing them through WakewordDetector gives them the same
        pinyin wakeword tolerance as the newer app/custom instruction commands.
        """
        try:
            commands_path = wakeword_config_path.parent / "commands.json"
            if not commands_path.exists():
                commands_path = get_config_path("commands.json")
            if not commands_path.exists():
                # Untracked runtime file — fresh checkouts ship the template
                # only. Same fallback as core/command/detector.py.
                commands_path = get_config_path("commands.template.json")
            if not commands_path.exists():
                return {}
            with open(commands_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            commands = config.get("commands", {})
            return commands if isinstance(commands, dict) else {}
        except Exception as e:
            print(f"[WAKEWORD] Failed to load keyboard commands: {e}")
            return {}

    def _normalize_custom_instructions(self, entries: Any) -> list[Dict[str, Any]]:
        """Normalize user-configured custom instructions.

        Custom instructions are intentionally separate from normal dictation
        hotwords: they only run after a wakeword match, and their fuzzy matching
        is limited to the configured command phrase itself.
        """
        if not isinstance(entries, list):
            return []

        normalized: list[Dict[str, Any]] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            phrase = str(
                entry.get("phrase") or entry.get("trigger") or entry.get("name") or ""
            ).strip()
            command = str(entry.get("command") or entry.get("target") or "").strip()
            if not phrase or len(self._normalize_for_match(phrase)) < 3 or not command:
                continue

            aliases_raw = entry.get("aliases", [])
            if isinstance(aliases_raw, str):
                aliases = [
                    alias.strip()
                    for alias in re.split(r"[,，、;\n；]", aliases_raw)
                    if alias.strip()
                ]
            elif isinstance(aliases_raw, list):
                aliases = [
                    str(alias).strip() for alias in aliases_raw if str(alias).strip()
                ]
            else:
                aliases = []

            triggers = []
            seen = set()
            for trigger in [phrase, *aliases]:
                key = self._normalize_for_match(trigger)
                if len(key) >= 3 and key not in seen:
                    seen.add(key)
                    triggers.append(trigger)

            if not triggers:
                continue

            mode = str(entry.get("mode") or "open").strip().lower()
            if mode not in ("open", "command", "shell"):
                mode = "open"
            if mode == "shell":
                mode = "command"

            normalized.append(
                {
                    "id": str(
                        entry.get("id") or f"custom_instruction_{index + 1}"
                    ).strip(),
                    "enabled": bool(entry.get("enabled", True)),
                    "phrase": phrase,
                    "aliases": aliases,
                    "triggers": triggers,
                    "command": command,
                    "mode": mode,
                    "working_dir": str(entry.get("working_dir") or "").strip(),
                    "elevate": bool(entry.get("elevate", False)),
                    "trust_writable_target": bool(
                        entry.get("trust_writable_target", False)
                    ),
                    "response": str(
                        entry.get("response") or f"正在执行：{phrase}"
                    ).strip(),
                    "phonetic": bool(entry.get("phonetic", True)),
                }
            )
        return normalized

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        """Normalize command text for trigger matching."""
        return re.sub(r"[\s，,。.、：:；;！!？?\"'“”‘’（）()【】\[\]]", "", text or "")

    def _iter_command_configs(self) -> Iterable[Tuple[str, Dict[str, Any]]]:
        """Yield built-in commands and synthetic custom instructions."""
        for cmd_id, cmd_config in self.commands.items():
            if isinstance(cmd_config, dict):
                yield cmd_id, cmd_config

        for cmd_id, cmd_config in self.keyboard_commands.items():
            if not isinstance(cmd_config, dict):
                continue
            key = cmd_config.get("key")
            if not key:
                continue
            trigger = str(cmd_id).strip()
            if not trigger:
                continue
            modifiers = cmd_config.get("modifiers", [])
            if not isinstance(modifiers, list):
                modifiers = []
            yield (
                f"keyboard:{trigger}",
                {
                    "action": "keyboard_shortcut",
                    "value": {
                        "command_id": trigger,
                        "key": key,
                        "modifiers": modifiers,
                    },
                    "triggers": [trigger],
                    "response": f"已执行{trigger}",
                    "phonetic": False,
                },
            )

        for index, entry in enumerate(self.custom_instructions):
            if not entry.get("enabled", True):
                continue
            phrase = entry.get("phrase", "").strip()
            if not phrase:
                continue
            cmd_id = entry.get("id") or f"custom_instruction_{index + 1}"
            yield (
                f"custom_instruction:{cmd_id}",
                {
                    "action": "custom_instruction_launch",
                    "value": {
                        "id": cmd_id,
                        "phrase": phrase,
                        "command": entry.get("command", ""),
                        "mode": entry.get("mode", "open"),
                        "working_dir": entry.get("working_dir", ""),
                        "elevate": entry.get("elevate", False),
                        "trust_writable_target": entry.get(
                            "trust_writable_target", False
                        ),
                    },
                    "triggers": entry.get("triggers", [phrase]),
                    "response": entry.get("response", f"正在执行：{phrase}"),
                    "custom_instruction": True,
                    "phonetic": entry.get("phonetic", True),
                },
            )

    def _find_phonetic_trigger(
        self, command_text_normalized: str, trigger_normalized: str
    ) -> bool:
        """Return True when command text contains a pinyin match of trigger.

        This is deliberately conservative:
        - only phrases of 3+ Chinese characters use fuzzy phonetic matching;
        - the detector looks for a window around the trigger length, so
          "星河启动一下" can match "星核启动" without allowing one-character
          fragments like "启动" to fire a launcher.
        """
        if len(trigger_normalized) < 3 or not command_text_normalized:
            return False

        target_len = len(trigger_normalized)
        text_len = len(command_text_normalized)
        target_pinyin = self._matcher.to_pinyin(trigger_normalized)
        if not target_pinyin:
            return False

        if target_len > text_len:
            return False

        for start in range(0, text_len - target_len + 1):
            candidate = command_text_normalized[start : start + target_len]
            if self._matcher.to_pinyin(candidate) == target_pinyin:
                return True
        return False

    def _find_open_path_fallback(
        self, command_text_normalized: str
    ) -> Optional[Tuple[str, Dict[str, Any], str]]:
        """Fallback for generic "open selected file" natural phrases.

        Runs only after all normal built-in, keyboard, and custom instruction
        matching has failed. Accepts only generic selected-target words
        (e.g. 图片/视频/PDF/markdown), not arbitrary app/folder names — so
        a missed custom like "打开叉叉" → ASR "打开叉子" returns unknown
        instead of silently firing open_path with no selection.
        """
        prefix = next(
            (
                item
                for item in self._OPEN_PATH_FALLBACK_PREFIXES
                if command_text_normalized.startswith(item)
            ),
            None,
        )
        if not prefix:
            return None

        suffix = command_text_normalized[len(prefix) :]
        if not suffix or len(suffix) > self._OPEN_PATH_FALLBACK_MAX_SUFFIX_LEN:
            return None

        # NFKC folds full-width ASCII (ＰＤＦ → PDF, ＷＯＲＤ → WORD) so ASR
        # outputs that produce full-width letters still match the whitelist.
        suffix_key = unicodedata.normalize("NFKC", suffix).lower()
        candidate_keys = {suffix_key}
        for tail in self._OPEN_PATH_FALLBACK_TAILS:
            if suffix_key.endswith(tail) and len(suffix_key) > len(tail):
                candidate_keys.add(suffix_key[: -len(tail)])

        if not any(key in self._OPEN_PATH_FALLBACK_TARGETS for key in candidate_keys):
            return None

        cmd_config = self.commands.get("open_path")
        if not isinstance(cmd_config, dict) or cmd_config.get("action") != "open_path":
            return None

        return ("open_path", cmd_config, f"{prefix}{suffix}")

    def extract_invocation(self, text: str) -> Optional[Tuple[str, str, str]]:
        """Extract a configured wakeword prefix with the normal pinyin tolerance.

        This is intentionally command-agnostic.  Callers can give deterministic
        command detectors first refusal, then route the remaining spoken request
        to an AI fallback without reverting to fragile literal-prefix checks.
        """

        if not self.enabled or not text:
            return None
        value = str(text).strip()
        candidates = [self.wakeword, *self.wakeword_aliases]

        # Preserve the strict, direct-prefix path first.  This also protects a
        # personalized wakeword that itself begins with a filler-like syllable.
        for candidate in candidates:
            result = self._matcher.extract_wakeword(value, candidate)
            if result:
                return result

        # One or more leading punctuation/filler tokens may be ASR residue
        # before a direct address.  Strip only the narrow allowlist above and
        # then retry the same prefix matcher.  We deliberately do not search
        # later in the sentence, so ``我想让阿蓝……`` remains ordinary dictation.
        retry_value = value.lstrip(self._INVOCATION_LEAD_IN_SEPARATORS)
        stripped_filler = False
        for _ in range(3):
            match = self._INVOCATION_LEAD_IN_FILLER_RE.match(retry_value)
            if match is None:
                break
            stripped_filler = True
            retry_value = retry_value[match.end() :].lstrip(
                self._INVOCATION_LEAD_IN_SEPARATORS
            )
        if stripped_filler and retry_value:
            for candidate in candidates:
                result = self._matcher.extract_wakeword(retry_value, candidate)
                if result:
                    return result
        return None

    def detect(
        self, text: str
    ) -> Optional[Tuple[str, str, Any, str, Optional[str], str]]:
        """
        Detect wakeword and parse command from text using pinyin matching.

        Args:
            text: Transcribed text to check

        Returns:
            Tuple of (command_id, action, value, response, following_text, command_text) if detected, None otherwise
            command_text is the full text after wakeword (needed by reminder parser for pre-trigger time).
            Example: ("auto_send_on", "set_auto_send", True, "已开启自动发送", None, "开启自动发送")
            For capture_following commands: ("save_highlight_idea", "save_highlight", {...}, "已记录想法", "要记录的内容", "记一下要记录的内容")
        """
        if not self.enabled or not text:
            return None

        text = text.strip()

        # Match the configured primary wakeword first, then explicit/compatibility
        # aliases.  All candidates still use prefix-only pinyin matching, so a
        # mention such as "我想让小助手……" remains ordinary dictation.
        result = self.extract_invocation(text)
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
        command_text_normalized = self._normalize_for_match(command_text)

        # Collect matches with priority:
        # 1. Trigger is IN command text (user said the trigger or more)
        # 2. Command text is IN trigger (user said part of a trigger)
        # Among same priority, prefer longest trigger
        best_match = None
        best_trigger_length = 0
        best_priority = -1

        for cmd_id, cmd_config in self._iter_command_configs():
            triggers = cmd_config.get("triggers", [])
            for trigger in triggers:
                trigger_normalized = self._normalize_for_match(str(trigger))
                # Require minimum trigger length to avoid false matches
                if len(trigger_normalized) < 2:
                    continue

                # Check match type
                trigger_in_command = trigger_normalized in command_text_normalized
                command_in_trigger = command_text_normalized in trigger_normalized
                phonetic_match = False

                # "打开" is intentionally kept as a quick built-in command for
                # the selected path/URL, but it must not swallow natural launch
                # phrases such as "打开我的电脑" before the user imports or configures
                # a personal voice command.  More specific built-in triggers like
                # "打开文件" / "打开目录" / "打开看看" remain valid.
                if (
                    trigger_in_command
                    and cmd_config.get("action") in ("open_path", "open_directory")
                    and trigger_normalized in ("打开", "帮我打开")
                    and command_text_normalized != trigger_normalized
                ):
                    continue

                # "截图" / "截屏" are short triggers for the take_screenshot
                # command, but they must not swallow phrases like "打开截图"
                # (open the selected image file) or "截图发送" (a user-
                # configured custom). They only fire when the command text
                # is the short trigger by itself.
                if (
                    trigger_in_command
                    and cmd_config.get("action") == "take_screenshot"
                    and trigger_normalized in ("截图", "截屏")
                    and command_text_normalized != trigger_normalized
                ):
                    continue

                # Keyboard-shortcut triggers (e.g. "发送" → Enter) must
                # likewise stay exact-only. Without this, "截图发送" would
                # trip the keyboard "发送" trigger (it's a substring) and
                # press Enter — even though the user clearly meant a
                # different intent.
                if (
                    trigger_in_command
                    and cmd_config.get("action") == "keyboard_shortcut"
                    and command_text_normalized != trigger_normalized
                ):
                    continue

                # Reject "command IN trigger" when command is too short relative to trigger
                # e.g., "发送"(2chars) should NOT match "开启自动发送"(6chars)
                # This prevents wakeword system from stealing keyboard commands
                if command_in_trigger and not trigger_in_command:
                    if len(command_text_normalized) < len(trigger_normalized) * 0.6:
                        continue

                if not trigger_in_command and not command_in_trigger:
                    if cmd_config.get("custom_instruction") and cmd_config.get(
                        "phonetic", True
                    ):
                        phonetic_match = self._find_phonetic_trigger(
                            command_text_normalized, trigger_normalized
                        )

                if (
                    not trigger_in_command
                    and not command_in_trigger
                    and not phonetic_match
                ):
                    continue

                # Prioritize "trigger IN command" over "command IN trigger"
                # This ensures "翻译" matches translate_popup, not "翻译成英文"
                if trigger_in_command:
                    priority = 3
                elif command_in_trigger:
                    priority = 2
                else:
                    priority = 1

                # Choose this match if:
                # 1. It has higher priority (trigger_in_command beats command_in_trigger)
                # 2. Same priority but longer trigger
                if priority > best_priority:
                    best_match = (cmd_id, cmd_config, trigger)
                    best_trigger_length = len(trigger_normalized)
                    best_priority = priority
                elif priority == best_priority:
                    # Same priority, prefer longer trigger
                    if len(trigger_normalized) > best_trigger_length:
                        best_match = (cmd_id, cmd_config, trigger)
                        best_trigger_length = len(trigger_normalized)
                        best_priority = priority

        if not best_match:
            best_match = self._find_open_path_fallback(command_text_normalized)

        if best_match:
            cmd_id, cmd_config, matched_trigger = best_match
            action = cmd_config.get("action")
            value = cmd_config.get("value")
            response = cmd_config.get("response", "")
            capture_following = cmd_config.get("capture_following", False)

            # Extract following text if capture_following is enabled
            following_text = None
            if capture_following:
                following_text = self._extract_following_text(
                    command_text, matched_trigger
                )

            log_msg = (
                f"[WAKEWORD] Detected: '{wakeword_found}' + '{command_text}' "
                f"-> {cmd_id} ({action}={value}) [trigger: '{matched_trigger}']"
            )
            if following_text:
                preview = (
                    following_text[:30] + "..."
                    if len(following_text) > 30
                    else following_text
                )
                log_msg += f", following='{preview}'"
            print(log_msg)

            return (cmd_id, action, value, response, following_text, command_text)

        print(f"[WAKEWORD] Unknown command: '{command_text}'")
        return None

    def _extract_following_text(self, command_text: str, trigger: str) -> Optional[str]:
        """Extract text that follows the trigger phrase.

        For capture_following commands like "记录想法 明天开会",
        this extracts "明天开会" as the content to save.
        """
        trigger_normalized = self._normalize_for_match(trigger)
        command_normalized = self._normalize_for_match(command_text)

        idx = command_normalized.find(trigger_normalized)
        if idx == -1:
            return None

        trigger_end = idx + len(trigger_normalized)

        # Map back to original text position
        char_count = 0
        original_pos = len(command_text)
        for i, c in enumerate(command_text):
            if not re.match(r"[\s，,。.、：:；;！!？?]", c):
                if char_count == trigger_end:
                    original_pos = i
                    break
                char_count += 1

        following = command_text[original_pos:].strip()
        # Remove leading punctuation
        following = re.sub(r"^[\s，,。.、：:；;！!？?]+", "", following)
        return following if following else None

    def get_command_info(self, cmd_id: str) -> Optional[Dict[str, Any]]:
        """Get command configuration by ID."""
        return self.commands.get(cmd_id)

    def get_available_wakewords(self) -> list[str]:
        """Get list of available wakeword options for UI."""
        return self.available_wakewords.copy()

    def set_wakeword(self, wakeword: str) -> None:
        """Change the active wakeword."""
        self.wakeword = wakeword
        self.wakeword_aliases = [
            alias for alias in self.wakeword_aliases if alias != self.wakeword
        ]
        for alias in self._COMPATIBILITY_WAKEWORD_ALIASES:
            if alias != self.wakeword and alias not in self.wakeword_aliases:
                self.wakeword_aliases.append(alias)
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
