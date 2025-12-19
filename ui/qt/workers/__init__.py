"""
Workers Module
==============
QThreadPool-based workers for non-blocking network operations.
"""

from .translation_worker import TranslationWorker
from .llm_worker import LLMWorker

__all__ = ["TranslationWorker", "LLMWorker"]
