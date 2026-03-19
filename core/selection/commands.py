"""
Selection Command Definitions
=============================
Voice command parsing and matching for selection processing.
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Dict


class CommandType(Enum):
    """Supported command types."""

    POLISH = auto()  # 润色
    TRANSLATE_EN = auto()  # 翻译成英文
    TRANSLATE_ZH = auto()  # 翻译成中文
    TRANSLATE_JA = auto()  # 翻译成日文
    EXPAND = auto()  # 扩写
    SUMMARIZE = auto()  # 缩写/总结
    REWRITE = auto()  # 重写/改写
    CUSTOM = auto()  # 自定义指令

    # v1.1: New action-based commands (don't replace text)
    TRANSLATE_POPUP = auto()  # 翻译弹窗 (show translation without replacing)
    ASK_AI = auto()  # AI 对话 (open chat dialog)

    # v1.2: Reply command (generate reply to message)
    REPLY = auto()  # 帮我回复 (generate reply in popup)


# Command keyword mappings (module-level constant)
# IMPORTANT: Keywords are matched by iterating this dict, so order matters!
# Longer keywords should be checked first to avoid partial matches.
# Use _get_sorted_keywords() for proper matching order.
COMMAND_KEYWORDS: Dict[str, CommandType] = {
    # Polish
    "润色": CommandType.POLISH,
    "优化": CommandType.POLISH,
    "改进": CommandType.POLISH,
    "polish": CommandType.POLISH,
    # Translate to English (replace text)
    "翻译成英文": CommandType.TRANSLATE_EN,
    "翻译成英语": CommandType.TRANSLATE_EN,
    "译成英文": CommandType.TRANSLATE_EN,
    "translate to english": CommandType.TRANSLATE_EN,
    "英文": CommandType.TRANSLATE_EN,
    # Translate to Chinese (replace text)
    "翻译成中文": CommandType.TRANSLATE_ZH,
    "翻译成汉语": CommandType.TRANSLATE_ZH,
    "译成中文": CommandType.TRANSLATE_ZH,
    "translate to chinese": CommandType.TRANSLATE_ZH,
    "中文": CommandType.TRANSLATE_ZH,
    # Translate to Japanese (replace text)
    "翻译成日文": CommandType.TRANSLATE_JA,
    "翻译成日语": CommandType.TRANSLATE_JA,
    "译成日文": CommandType.TRANSLATE_JA,
    "translate to japanese": CommandType.TRANSLATE_JA,
    "日文": CommandType.TRANSLATE_JA,
    # Expand
    "扩写": CommandType.EXPAND,
    "展开": CommandType.EXPAND,
    "expand": CommandType.EXPAND,
    # Summarize
    "缩写": CommandType.SUMMARIZE,
    "总结": CommandType.SUMMARIZE,
    "精简": CommandType.SUMMARIZE,
    "summarize": CommandType.SUMMARIZE,
    # Rewrite
    "重写": CommandType.REWRITE,
    "改写": CommandType.REWRITE,
    "rewrite": CommandType.REWRITE,
    # v1.1: Translation popup (show without replacing)
    # Use strict prefix matching - only trigger at start of utterance
    "翻译一下": CommandType.TRANSLATE_POPUP,
    "什么意思": CommandType.TRANSLATE_POPUP,
    "啥意思": CommandType.TRANSLATE_POPUP,
    "translate": CommandType.TRANSLATE_POPUP,
    "翻译": CommandType.TRANSLATE_POPUP,  # Shorter, check last
    # v1.1: AI chat dialog
    "问问ai": CommandType.ASK_AI,
    "问ai": CommandType.ASK_AI,
    "问一下ai": CommandType.ASK_AI,
    "ask ai": CommandType.ASK_AI,
    # v1.2: Reply command (generate reply to message)
    "帮我回复": CommandType.REPLY,
    "帮我回": CommandType.REPLY,
    "回复一下": CommandType.REPLY,
    "回复": CommandType.REPLY,
    "reply": CommandType.REPLY,
}


def _get_sorted_keywords() -> list[tuple[str, CommandType]]:
    """
    Get keywords sorted by length (descending) for proper matching.

    Longer keywords are checked first to avoid partial matches.
    E.g., "翻译成英文" should match before "翻译".
    """
    return sorted(COMMAND_KEYWORDS.items(), key=lambda x: len(x[0]), reverse=True)


# Prompt templates for each command type
# Note: All prompts include "直接输出结果" to prevent LLM from adding explanations
COMMAND_PROMPTS: Dict[CommandType, str] = {
    CommandType.POLISH: "润色以下文本，保持原意但提升表达质量。直接输出润色后的文本，禁止添加任何解释或说明：",
    CommandType.TRANSLATE_EN: "将以下文本翻译成英文。直接输出翻译结果，禁止添加任何解释、注释或括号说明：",
    CommandType.TRANSLATE_ZH: "将以下文本翻译成中文。直接输出翻译结果，禁止添加任何解释、注释或括号说明：",
    CommandType.TRANSLATE_JA: "将以下文本翻译成日文。直接输出翻译结果，禁止添加任何解释、注释或括号说明：",
    CommandType.EXPAND: "扩写以下文本，增加更多细节和深度。直接输出扩写后的文本，禁止添加任何解释：",
    CommandType.SUMMARIZE: "缩写以下文本，保留核心信息。直接输出缩写后的文本，禁止添加任何解释：",
    CommandType.REWRITE: "重写以下文本，使用不同的表达方式。直接输出重写后的文本，禁止添加任何解释：",
    CommandType.REPLY: "你是一个专业的回复助手。根据以下收到的消息，生成一条得体、自然的回复。直接输出回复内容：",
}


@dataclass
class SelectionCommand:
    """Parsed selection command."""

    command_type: CommandType
    raw_text: str  # Original ASR text
    custom_instruction: Optional[str] = None  # For CUSTOM type

    @classmethod
    def parse(cls, asr_text: str) -> Optional["SelectionCommand"]:
        """
        Parse ASR text to detect command type.

        Uses STRICT matching to avoid false positives during normal dictation:
        - Only matches if text IS the keyword (exact) or STARTS with keyword
        - Short text (<=6 chars) can match anywhere for brevity
        - Longer text must start with keyword to be considered a command

        Args:
            asr_text: Raw ASR transcription

        Returns:
            SelectionCommand if recognized, None otherwise
        """
        if not asr_text:
            return None

        text_lower = asr_text.lower().strip()

        # STRICT matching to avoid triggering on normal dictation
        # e.g., "这个功能很优化" should NOT trigger POLISH command
        for keyword, cmd_type in _get_sorted_keywords():
            # Exact match (most reliable)
            if text_lower == keyword:
                return cls(command_type=cmd_type, raw_text=asr_text)

            # Starts with keyword (e.g., "润色一下", "翻译成英文这段话")
            if text_lower.startswith(keyword):
                return cls(command_type=cmd_type, raw_text=asr_text)

            # For very short text (<=6 chars), allow substring match
            # This handles cases like "英文" in "说英文" (short command)
            if len(text_lower) <= 6 and keyword in text_lower:
                return cls(command_type=cmd_type, raw_text=asr_text)

        # No keyword matched - return None for normal text
        # NOTE: Returning CUSTOM here was too aggressive, causing every dictation
        # to be treated as a potential selection command
        # Selection processing is now ONLY triggered via wakeword (小助手润色)
        return None

    def get_prompt_prefix(self) -> str:
        """Get the LLM prompt prefix for this command type."""
        if self.command_type == CommandType.CUSTOM:
            return f"请按照以下要求处理文本：{self.custom_instruction}\n\n原文："

        return COMMAND_PROMPTS.get(self.command_type, "请处理以下文本：")

    def is_action_command(self) -> bool:
        """
        Check if this command triggers a UI action instead of text replacement.

        v1.1 action commands:
        - TRANSLATE_POPUP: Shows translation in popup without replacing
        - ASK_AI: Opens AI chat dialog

        Returns:
            True if command should trigger UI action, False for text replacement
        """
        return self.command_type in (
            CommandType.TRANSLATE_POPUP,
            CommandType.ASK_AI,
            CommandType.REPLY,
        )

    def is_translate_popup(self) -> bool:
        """Check if this command triggers translation popup."""
        return self.command_type == CommandType.TRANSLATE_POPUP

    def is_ask_ai(self) -> bool:
        """Check if this command triggers AI chat dialog."""
        return self.command_type == CommandType.ASK_AI
