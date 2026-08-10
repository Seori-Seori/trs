from __future__ import annotations

from typing import Any

from core.segment import ValidationResult, ValidationSeverity
from core.terminology import matching_novel_term_hints


def validate_novel_terminology(
    source: str,
    translation: str,
    source_language: str,
    profile: dict[str, Any] | None,
) -> ValidationResult:
    """Reject only explicit, source-anchored catastrophic term mismatches."""
    result = ValidationResult()
    for hint in matching_novel_term_hints(source, source_language, profile):
        observed = [
            term for term in hint.disallowed_korean if term in translation
        ]
        if not observed:
            continue
        exception_present = any(
            term in source for term in hint.exception_source_terms
        )
        guarded_meaning_present = any(
            term in translation for term in hint.preferred_korean
        )
        if exception_present and guarded_meaning_present:
            continue
        result.add(
            "NOVEL_TERM_MISTRANSLATION",
            ValidationSeverity.ERROR,
            f"Source term {hint.source!r} was mapped to a disallowed meaning class",
            "terminology",
            source_term=hint.source,
            meaning=hint.meaning,
            disallowed_matches=observed,
            preferred_korean=list(hint.preferred_korean),
        )
    return result
