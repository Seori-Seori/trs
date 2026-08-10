from __future__ import annotations

from typing import Protocol

from core.segment import Segment, ValidationResult


class SegmentValidator(Protocol):
    def validate(self, segment: Segment, translation: str) -> ValidationResult:
        ...
