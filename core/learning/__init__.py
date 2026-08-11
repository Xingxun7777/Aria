"""Local correction rules and passive correction-learning primitives.

``explicit_corrections`` is the product-wired, user-initiated rule path.
``correction_store`` / ``correction_policy`` remain the separate passive
candidate-learning skeleton and are not silently enabled by the explicit path.
"""

from .explicit_corrections import (
    CorrectionMutationResult,
    ExplicitCorrectionProcessor,
    ExplicitCorrectionRule,
    ExplicitCorrectionStore,
    VoiceCorrectionCommand,
    VoiceCorrectionParseResult,
    parse_voice_correction,
    validate_operands,
)

__all__ = [
    "CorrectionMutationResult",
    "ExplicitCorrectionProcessor",
    "ExplicitCorrectionRule",
    "ExplicitCorrectionStore",
    "VoiceCorrectionCommand",
    "VoiceCorrectionParseResult",
    "parse_voice_correction",
    "validate_operands",
]
