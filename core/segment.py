from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SegmentStatus(str, Enum):
    PENDING = "PENDING"
    PREPARED = "PREPARED"
    TRANSLATING = "TRANSLATING"
    PARSED = "PARSED"
    VALID = "VALID"
    REPAIR_PENDING = "REPAIR_PENDING"
    FAILED = "FAILED"


class ValidationSeverity(str, Enum):
    PASS = "PASS"
    WARNING = "WARNING"
    RISK = "RISK"
    ERROR = "ERROR"


@dataclass(frozen=True)
class ProtectedToken:
    placeholder: str
    original: str
    kind: str
    order: int


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    severity: ValidationSeverity
    message: str
    validator: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)

    def add(
        self,
        code: str,
        severity: ValidationSeverity,
        message: str,
        validator: str,
        **details: Any,
    ) -> None:
        self.issues.append(
            ValidationIssue(
                code=code,
                severity=severity,
                message=message,
                validator=validator,
                details=details,
            )
        )

    def extend(self, other: "ValidationResult") -> None:
        self.issues.extend(other.issues)

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == ValidationSeverity.ERROR for issue in self.issues)

    @property
    def risks(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == ValidationSeverity.RISK]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == ValidationSeverity.WARNING]


_ALLOWED_TRANSITIONS: dict[SegmentStatus, set[SegmentStatus]] = {
    SegmentStatus.PENDING: {
        SegmentStatus.PREPARED,
        SegmentStatus.VALID,
        SegmentStatus.FAILED,
    },
    SegmentStatus.PREPARED: {
        SegmentStatus.TRANSLATING,
        SegmentStatus.VALID,
        SegmentStatus.FAILED,
    },
    SegmentStatus.TRANSLATING: {
        SegmentStatus.PARSED,
        SegmentStatus.REPAIR_PENDING,
        SegmentStatus.FAILED,
    },
    SegmentStatus.PARSED: {
        SegmentStatus.VALID,
        SegmentStatus.REPAIR_PENDING,
        SegmentStatus.FAILED,
    },
    SegmentStatus.REPAIR_PENDING: {
        SegmentStatus.TRANSLATING,
        SegmentStatus.FAILED,
    },
    SegmentStatus.VALID: set(),
    SegmentStatus.FAILED: set(),
}


@dataclass
class Segment:
    id: str
    source: str
    source_language: str = "auto"
    target_language: str = "ko"
    speaker: str | None = None
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)
    protected_tokens: list[ProtectedToken] = field(default_factory=list)
    file_path: str | None = None
    location: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    prepared_source: str | None = None
    raw_translation: str | None = None
    last_raw_response: str | None = None
    last_raw_response_truncated: bool = False
    translation: str | None = None
    status: SegmentStatus = SegmentStatus.PENDING
    attempt_count: int = 0
    last_error: str | None = None
    validation_issues: list[ValidationIssue] = field(default_factory=list)
    was_repaired: bool = False
    _source_locked: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.id or any(char.isspace() for char in self.id):
            raise ValueError("Segment ID must be non-empty and contain no whitespace")
        if self.target_language != "ko":
            raise ValueError("Seori Translator v7.0 target language is fixed to 'ko'")
        object.__setattr__(self, "_source_locked", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "source" and getattr(self, "_source_locked", False):
            current = getattr(self, "source", None)
            if value != current:
                raise AttributeError("Segment.source is immutable after creation")
        object.__setattr__(self, name, value)

    def transition(self, new_status: SegmentStatus) -> None:
        if new_status == self.status:
            return
        if new_status not in _ALLOWED_TRANSITIONS[self.status]:
            raise ValueError(f"Invalid Segment status transition: {self.status} -> {new_status}")
        self.status = new_status

    def prepare(self, prepared_source: str, tokens: list[ProtectedToken]) -> None:
        if self.status != SegmentStatus.PENDING:
            raise ValueError(f"Only PENDING segments can be prepared, got {self.status}")
        self.prepared_source = prepared_source
        self.protected_tokens = list(tokens)
        self.transition(SegmentStatus.PREPARED)

    def begin_translation(self) -> None:
        if self.status == SegmentStatus.VALID:
            raise RuntimeError(f"Invariant violation: VALID segment {self.id} cannot be translated")
        self.transition(SegmentStatus.TRANSLATING)
        self.attempt_count += 1

    def mark_parsed(self, raw_translation: str) -> None:
        self.raw_translation = raw_translation
        self.transition(SegmentStatus.PARSED)

    def record_raw_response(self, raw_response: str, *, limit: int = 4000) -> None:
        if limit <= 0:
            raise ValueError("Raw response limit must be positive")
        if len(raw_response) <= limit:
            self.last_raw_response = raw_response
            self.last_raw_response_truncated = False
            return
        marker = "\n... [중간 생략] ...\n"
        remaining = max(0, limit - len(marker))
        head = (remaining * 3) // 4
        tail = remaining - head
        self.last_raw_response = (
            raw_response[:head] + marker + (raw_response[-tail:] if tail else "")
        )
        self.last_raw_response_truncated = True

    def mark_for_repair(self, result: ValidationResult, error: str | None = None) -> None:
        self.validation_issues = list(result.issues)
        self.last_error = error or "; ".join(
            issue.code for issue in result.issues if issue.severity == ValidationSeverity.ERROR
        )
        if self.status == SegmentStatus.TRANSLATING:
            self.transition(SegmentStatus.REPAIR_PENDING)
        elif self.status == SegmentStatus.PARSED:
            self.transition(SegmentStatus.REPAIR_PENDING)
        elif self.status != SegmentStatus.REPAIR_PENDING:
            raise ValueError(f"Cannot mark {self.status} for repair")

    def mark_valid(
        self,
        translation: str,
        result: ValidationResult | None = None,
        *,
        repaired: bool = False,
    ) -> None:
        self.translation = translation
        self.validation_issues = list(result.issues) if result else []
        self.last_error = None
        self.was_repaired = self.was_repaired or repaired
        self.transition(SegmentStatus.VALID)

    def mark_failed(self, result: ValidationResult | None = None, error: str | None = None) -> None:
        if result is not None:
            self.validation_issues = list(result.issues)
        self.last_error = error or self.last_error or "terminal failure"
        self.transition(SegmentStatus.FAILED)
