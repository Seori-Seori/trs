from __future__ import annotations

from typing import Any

from core.novel_register import matching_novel_register_hints
from core.segment import ValidationResult, ValidationSeverity


def validate_novel_register(
    source: str,
    translation: str,
    source_language: str,
    profile: dict[str, Any] | None,
    *,
    context: list[str] | None = None,
) -> ValidationResult:
    """Report source-anchored style drift without consuming repair attempts."""
    result = ValidationResult()
    for hint in matching_novel_register_hints(
        source, source_language, profile, context=context
    ):
        observed = [term for term in hint.avoid_korean if term in translation]
        if not observed:
            continue
        result.add(
            "NOVEL_REGISTER_MISMATCH",
            ValidationSeverity.RISK,
            "Korean wording is semantically readable but mismatches the source register",
            "register",
            source_terms=list(hint.matched_source_terms(source)),
            meaning_class=hint.meaning_class,
            source_register=hint.source_register,
            preferred_register=hint.preferred_register,
            avoided_matches=observed,
            preferred_korean=list(hint.preferred_korean),
        )
    return result
