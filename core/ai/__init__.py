"""Unified AI / LLM gateway package."""

from .gateway import (
    AIErrorCategory,
    AIRequestSpec,
    AIResult,
    chat,
    stream,
    iter_stream,
    targets_deepseek,
    build_chat_completions_url,
)

__all__ = [
    "AIErrorCategory",
    "AIRequestSpec",
    "AIResult",
    "chat",
    "stream",
    "iter_stream",
    "targets_deepseek",
    "build_chat_completions_url",
]
