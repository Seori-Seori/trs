from __future__ import annotations

from collections.abc import Mapping

from core.segment import ValidationSeverity


def policy_severity(
    policy: Mapping[str, object] | None,
    key: str,
    default: ValidationSeverity,
) -> ValidationSeverity:
    if not policy or key not in policy:
        return default
    value = str(policy[key]).upper()
    try:
        return ValidationSeverity(value)
    except ValueError as exc:
        raise ValueError(f"Unsupported validation severity for {key}: {value}") from exc
