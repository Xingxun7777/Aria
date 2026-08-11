"""
History Data Models
===================
Record types and data structures for unified history storage.
"""

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Dict, Any, Optional


class RecordType(Enum):
    """Types of history records."""

    ASR = auto()  # 语音转文字
    SELECTION_POLISH = auto()  # 选中润色
    SELECTION_TRANSLATE = auto()  # 选中翻译
    SELECTION_REPLY = auto()  # 选中回复
    SELECTION_OTHER = auto()  # 其他选中操作
    AI_CHAT = auto()  # AI 对话
    SUMMARY = auto()  # 总结
    HIGHLIGHT = auto()  # 高亮/想法记录


# Display labels for each record type (Chinese)
RECORD_TYPE_LABELS: Dict[RecordType, str] = {
    RecordType.ASR: "语音",
    RecordType.SELECTION_POLISH: "润色",
    RecordType.SELECTION_TRANSLATE: "翻译",
    RecordType.SELECTION_REPLY: "回复",
    RecordType.SELECTION_OTHER: "处理",
    RecordType.AI_CHAT: "对话",
    RecordType.SUMMARY: "总结",
    RecordType.HIGHLIGHT: "记录",
}

# Color tags for each record type (used by UI)
RECORD_TYPE_COLORS: Dict[RecordType, str] = {
    RecordType.ASR: "#F59E0B",  # amber/accent
    RecordType.SELECTION_POLISH: "#10B981",  # green
    RecordType.SELECTION_TRANSLATE: "#3B82F6",  # blue
    RecordType.SELECTION_REPLY: "#F97316",  # orange
    RecordType.SELECTION_OTHER: "#6B7280",  # gray
    RecordType.AI_CHAT: "#8B5CF6",  # purple
    RecordType.SUMMARY: "#06B6D4",  # cyan
    RecordType.HIGHLIGHT: "#EC4899",  # pink
}


@dataclass
class HistoryRecord:
    """
    A single history record.

    Stored as one JSON line in daily .jsonl files.
    """

    id: str  # UUID (short 8-char)
    record_type: RecordType
    timestamp: str  # ISO format
    input_text: str  # 原始输入
    output_text: str = ""  # 处理结果
    metadata: Dict[str, Any] = field(default_factory=dict)
    # metadata examples:
    #   app_context: str       — 当前窗口上下文
    #   command_type: str      — polish/translate_en/etc.
    #   model_used: str        — LLM model name
    #   processing_time_ms: float
    #   session_id: int
    #   duration_s: float      — audio duration
    #   style_hint: str        — reply style hint
    #   target_lang: str       — translation target

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON storage."""
        return {
            "id": self.id,
            "type": self.record_type.name,
            "timestamp": self.timestamp,
            "input_text": self.input_text,
            "output_text": self.output_text,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["HistoryRecord"]:
        """Deserialize from dict. Returns None if data is invalid."""
        try:
            record_type = RecordType[data["type"]]
            return cls(
                id=data["id"],
                record_type=record_type,
                timestamp=data["timestamp"],
                input_text=data.get("input_text", ""),
                output_text=data.get("output_text", ""),
                metadata=data.get("metadata", {}),
            )
        except (KeyError, ValueError):
            return None
