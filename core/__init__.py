"""Core translation engine for Seori Translator."""

from .segment import (
    ProtectedToken,
    Segment,
    SegmentStatus,
    ValidationIssue,
    ValidationResult,
    ValidationSeverity,
)

__all__ = [
    "ProtectedToken",
    "Segment",
    "SegmentStatus",
    "ValidationIssue",
    "ValidationResult",
    "ValidationSeverity",
]
