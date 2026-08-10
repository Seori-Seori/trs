from __future__ import annotations

from collections import Counter

from core.placeholders import PlaceholderEngine, PlaceholderError
from core.segment import ProtectedToken, ValidationResult, ValidationSeverity


def validate_placeholders(text: str, tokens: list[ProtectedToken]) -> ValidationResult:
    result = ValidationResult()
    ordered = sorted(tokens, key=lambda token: token.order)
    expected = [token.placeholder for token in ordered]
    observed = PlaceholderEngine.observed_placeholders(text)
    expected_counts = Counter(expected)
    observed_counts = Counter(observed)

    for placeholder in expected:
        count = observed_counts[placeholder]
        if count == 0:
            result.add(
                "MISSING_PLACEHOLDER",
                ValidationSeverity.ERROR,
                f"Required placeholder {placeholder} is missing",
                "placeholders",
                placeholder=placeholder,
            )
        elif count > expected_counts[placeholder]:
            result.add(
                "DUPLICATE_PLACEHOLDER",
                ValidationSeverity.ERROR,
                f"Placeholder {placeholder} appears too many times",
                "placeholders",
                placeholder=placeholder,
                expected=expected_counts[placeholder],
                actual=count,
            )

    unexpected = [placeholder for placeholder in observed if placeholder not in expected_counts]
    if unexpected:
        result.add(
            "UNEXPECTED_PLACEHOLDER",
            ValidationSeverity.ERROR,
            "Translation contains placeholder-like tokens not registered for this Segment",
            "placeholders",
            placeholders=unexpected,
        )

    if not result.has_errors and observed != expected:
        result.add(
            "PLACEHOLDER_ORDER_MISMATCH",
            ValidationSeverity.ERROR,
            "Placeholder order changed",
            "placeholders",
            expected=expected,
            actual=observed,
        )

    if not result.has_errors:
        try:
            PlaceholderEngine.restore(text, ordered)
        except PlaceholderError as exc:
            result.add(
                "PLACEHOLDER_RESTORATION_MISMATCH",
                ValidationSeverity.ERROR,
                str(exc),
                "placeholders",
            )
    return result


def validate_restored_tokens(text: str, tokens: list[ProtectedToken]) -> ValidationResult:
    """Conservatively revalidate protected values loaded from a final checkpoint."""
    result = ValidationResult()
    ordered = sorted(tokens, key=lambda token: token.order)
    expected_counts = Counter(token.restored_value for token in ordered)

    for restored_value, expected_count in expected_counts.items():
        actual_count = text.count(restored_value)
        if actual_count != expected_count:
            result.add(
                "RESTORED_TOKEN_COUNT_MISMATCH",
                ValidationSeverity.ERROR,
                "A protected or mapped value is missing or duplicated in checkpoint text",
                "placeholders",
                value=restored_value,
                expected=expected_count,
                actual=actual_count,
            )

    cursor = 0
    for token in ordered:
        restored_value = token.restored_value
        position = text.find(restored_value, cursor)
        if position < 0:
            result.add(
                "RESTORED_TOKEN_ORDER_MISMATCH",
                ValidationSeverity.ERROR,
                "Protected or mapped values are not in their source order",
                "placeholders",
                value=restored_value,
            )
            break
        cursor = position + len(restored_value)
    return result
