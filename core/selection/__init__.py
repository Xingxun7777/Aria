"""
VoiceType Selection Processing Module
=====================================
Process selected text with voice commands (polish, translate, rewrite, etc.)

Usage:
    from voicetype.core.selection import SelectionDetector, SelectionProcessor, SelectionCommand

    # Detect selected text
    detector = SelectionDetector(output_injector)
    selected_text = detector.detect()

    # Parse voice command
    command = SelectionCommand.parse(asr_text)

    # Process with LLM
    processor = SelectionProcessor(polisher)
    result = processor.process(selected_text, command)
"""

from .detector import SelectionDetector
from .processor import SelectionProcessor
from .commands import SelectionCommand, CommandType

__all__ = [
    "SelectionDetector",
    "SelectionProcessor",
    "SelectionCommand",
    "CommandType",
]
