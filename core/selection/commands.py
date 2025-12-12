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
    EXPAND = auto()  # 扩写
    SUMMARIZE = auto()  # 缩写/总结
    REWRITE = auto()  # 重写/改写
    CUSTOM = auto()  # 自定义指令


# Command keyword mappings (module-level constant)
COMMAND_KEYWORDS: Dict[str, CommandType] = {
    # Polish
    "润色": CommandType.POLISH,
    "优化": CommandType.POLISH,
    "改进": CommandType.POLISH,
    "polish": CommandType.POLISH,
    # Translate to English
    "翻译成英文": CommandType.TRANSLATE_EN,
    "翻译成英语": CommandType.TRANSLATE_EN,
    "译成英文": CommandType.TRANSLATE_EN,
    "translate to english": CommandType.TRANSLATE_EN,
    "英文": CommandType.TRANSLATE_EN,
    # Translate to Chinese
    "翻译成中文": CommandType.TRANSLATE_ZH,
    "翻译成汉语": CommandType.TRANSLATE_ZH,
    "译成中文": CommandType.TRANSLATE_ZH,
    "translate to chinese": CommandType.TRANSLATE_ZH,
    "中文": CommandType.TRANSLATE_ZH,
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
}

# Prompt templates for each command type
# Note: All prompts include "直接输出结果" to prevent LLM from adding explanations
COMMAND_PROMPTS: Dict[CommandType, str] = {
    CommandType.POLISH: "润色以下文本，保持原意但提升表达质量。直接输出润色后的文本，禁止添加任何解释或说明：",
    CommandType.TRANSLATE_EN: "将以下文本翻译成英文。直接输出翻译结果，禁止添加任何解释、注释或括号说明：",
    CommandType.TRANSLATE_ZH: "将以下文本翻译成中文。直接输出翻译结果，禁止添加任何解释、注释或括号说明：",
    CommandType.EXPAND: "扩写以下文本，增加更多细节和深度。直接输出扩写后的文本，禁止添加任何解释：",
    CommandType.SUMMARIZE: "缩写以下文本，保留核心信息。直接输出缩写后的文本，禁止添加任何解释：",
    CommandType.REWRITE: "重写以下文本，使用不同的表达方式。直接输出重写后的文本，禁止添加任何解释：",
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

        Args:
            asr_text: Raw ASR transcription

        Returns:
            SelectionCommand if recognized, None otherwise
        """
        if not asr_text:
            return None

        text_lower = asr_text.lower().strip()

        # Try exact/partial keyword matching
        for keyword, cmd_type in COMMAND_KEYWORDS.items():
            if keyword in text_lower:
                return cls(command_type=cmd_type, raw_text=asr_text)

        # No keyword matched - treat as custom instruction
        # (User said something like "让这段话更正式一点")
        if len(asr_text.strip()) > 2:
            return cls(
                command_type=CommandType.CUSTOM,
                raw_text=asr_text,
                custom_instruction=asr_text.strip(),
            )

        return None

    def get_prompt_prefix(self) -> str:
        """Get the LLM prompt prefix for this command type."""
        if self.command_type == CommandType.CUSTOM:
            return f"请按照以下要求处理文本：{self.custom_instruction}\n\n原文："

        return COMMAND_PROMPTS.get(self.command_type, "请处理以下文本：")
