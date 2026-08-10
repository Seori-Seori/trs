from __future__ import annotations

from core.segment import ValidationResult, ValidationSeverity
from core.terminology import matching_semantic_term_rules


def validate_semantic_terminology(
    source: str,
    translation: str,
    source_language: str,
) -> ValidationResult:
    """Reject only reusable, source-anchored catastrophic meaning swaps."""
    result = ValidationResult()
    for rule in matching_semantic_term_rules(source, source_language):
        observed = [
            term for term in rule.conflicting_korean if term in translation
        ]
        if not observed:
            continue
        exception_present = any(
            term in source for term in rule.exception_source_terms
        )
        guarded_meaning_present = any(
            term in translation for term in rule.guarded_korean
        )
        if exception_present and guarded_meaning_present:
            continue
        result.add(
            "NOVEL_TERM_MISTRANSLATION",
            ValidationSeverity.ERROR,
            "A source concept was mapped to a conflicting semantic class",
            "terminology",
            source_terms=list(rule.matched_source_terms(source)),
            meaning_class=rule.meaning_class,
            meaning=rule.meaning,
            disallowed_matches=observed,
        )
    return result
