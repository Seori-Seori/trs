from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from core.config import ValidatorsConfig
from core.parser import ParsedResponse
from core.segment import Segment, ValidationResult, ValidationSeverity
from validators.policy import policy_severity


_ROW_ID_RE = re.compile(r"(?<![A-Za-z0-9_])(?:SEG|ADULT)_[A-Za-z0-9_-]+")
_PROMPT_LEAK_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"translate the following",
        r"translation rules?",
        r"output only",
        r"target rows?",
        r"context only",
        r"번역\s*규칙",
        r"다음(?:의)?\s*텍스트를\s*번역",
        r"출력\s*형식",
        r"<<<(?:item|targets|context|failures|end)",
    )
]


@dataclass
class BatchValidation:
    by_segment_id: dict[str, ValidationResult]
    global_result: ValidationResult


def validate_structure(
    parsed: ParsedResponse,
    expected: list[Segment],
    config: ValidatorsConfig,
) -> BatchValidation:
    expected_ids = [segment.id for segment in expected]
    expected_set = set(expected_ids)
    results = {segment_id: ValidationResult() for segment_id in expected_ids}
    global_result = ValidationResult()

    for segment_id in expected_ids:
        if segment_id not in parsed.rows:
            results[segment_id].add(
                "MISSING_ID",
                ValidationSeverity.ERROR,
                f"Expected row {segment_id} is missing from the model response",
                "structure",
                segment_id=segment_id,
            )

    identical_duplicates = set(parsed.identical_duplicates)
    for duplicate in sorted(identical_duplicates):
        target = results.get(duplicate, global_result)
        target.add(
            "IDENTICAL_DUPLICATE_ID",
            ValidationSeverity.WARNING,
            f"Row ID {duplicate} was repeated with an exactly identical translation",
            "structure",
            segment_id=duplicate,
            occurrence_count=len(parsed.occurrences.get(duplicate, [])),
        )

    conflicting_duplicates = set(parsed.conflicting_duplicates)
    # Preserve the legacy ParsedResponse contract for callers that only populate
    # duplicates: an unclassified duplicate must remain a conflict, never salvage.
    conflicting_duplicates.update(set(parsed.duplicates) - identical_duplicates)
    for duplicate in sorted(conflicting_duplicates):
        target = results.get(duplicate, global_result)
        target.add(
            "DUPLICATE_ID",
            ValidationSeverity.ERROR,
            f"Row ID {duplicate} appears with conflicting translations",
            "structure",
            segment_id=duplicate,
            occurrence_count=len(parsed.occurrences.get(duplicate, [])),
        )

    unexpected = [row_id for row_id in parsed.rows if row_id not in expected_set]
    if unexpected:
        global_result.add(
            "UNEXPECTED_ID",
            ValidationSeverity.ERROR,
            "The response contains IDs that were not requested",
            "structure",
            ids=unexpected,
        )

    if parsed.malformed_lines:
        global_result.add(
            "MALFORMED_LINE",
            ValidationSeverity.ERROR,
            "The response contains lines that do not use ID<TAB>translation format",
            "structure",
            lines=[
                {"line_number": item.line_number, "text": item.text}
                for item in parsed.malformed_lines
            ],
        )

    if parsed.code_fence_removed:
        global_result.add(
            "RESPONSE_CODE_FENCE",
            ValidationSeverity.WARNING,
            "A single enclosing Markdown code fence was removed before parsing",
            "structure",
        )

    actual_expected_order: list[str] = []
    seen: set[str] = set()
    for row_id in parsed.row_order:
        if row_id in expected_set and row_id not in seen:
            actual_expected_order.append(row_id)
            seen.add(row_id)
    present_expected_order = [row_id for row_id in expected_ids if row_id in parsed.rows]
    if config.strict_id_order and actual_expected_order != present_expected_order:
        actual_positions = {row_id: index for index, row_id in enumerate(actual_expected_order)}
        expected_positions = {row_id: index for index, row_id in enumerate(present_expected_order)}
        for row_id in present_expected_order:
            if actual_positions.get(row_id) != expected_positions.get(row_id):
                results[row_id].add(
                    "ORDER_MISMATCH",
                    ValidationSeverity.ERROR,
                    f"Row {row_id} is not in the requested output order",
                    "structure",
                    expected_order=present_expected_order,
                    actual_order=actual_expected_order,
                )

    for segment in expected:
        if segment.id not in parsed.rows:
            continue
        translation = parsed.rows[segment.id]
        result = results[segment.id]
        if not translation.strip():
            result.add(
                "EMPTY_TRANSLATION",
                ValidationSeverity.ERROR,
                "Translation is empty",
                "structure",
            )
        if config.detect_row_id_leak:
            leaked = set(_ROW_ID_RE.findall(translation))
            leaked.update(
                expected_id for expected_id in expected_ids if expected_id in translation
            )
            if leaked:
                result.add(
                    "ROW_ID_LEAK",
                    ValidationSeverity.ERROR,
                    "A row ID leaked into the translation text",
                    "structure",
                    ids=sorted(leaked),
                )
        if config.detect_prompt_leak:
            leaks = [pattern.pattern for pattern in _PROMPT_LEAK_PATTERNS if pattern.search(translation)]
            if leaks or "```" in translation:
                result.add(
                    "PROMPT_LEAK",
                    ValidationSeverity.ERROR,
                    "Prompt instructions or formatting leaked into the translation",
                    "structure",
                    patterns=leaks,
                )
        if re.search(r"\\n\s*(?:SEG|ADULT)_[A-Za-z0-9_-]+", translation):
            result.add(
                "MULTIPLE_ROWS_MERGED",
                ValidationSeverity.ERROR,
                "The translation appears to contain another output row",
                "structure",
            )

    return BatchValidation(results, global_result)


_PAIR_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("parenthesis", [("(", ")"), ("（", "）")]),
    ("square_bracket", [("[", "]"), ("［", "］")]),
    ("curly_bracket", [("{", "}"), ("｛", "｝")]),
]
_QUOTE_PAIRS = [("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"), ('"', '"')]


def _balanced_pair_count(text: str, opening: str, closing: str) -> int:
    if opening == closing:
        return text.count(opening) // 2 if text.count(opening) % 2 == 0 else -1
    if text.count(opening) != text.count(closing):
        return -1
    return text.count(opening)


def validate_text_structure(
    source: str,
    translation: str,
    policy: Mapping[str, object] | None = None,
) -> ValidationResult:
    result = ValidationResult()
    partial_loss_severity = policy_severity(
        policy, "partial_delimiter_loss_severity", ValidationSeverity.RISK
    )

    for group_name, pairs in _PAIR_GROUPS:
        source_count = sum(max(_balanced_pair_count(source, op, cl), 0) for op, cl in pairs)
        translated_counts = [_balanced_pair_count(translation, op, cl) for op, cl in pairs]
        if any(count < 0 for count in translated_counts):
            result.add(
                "UNBALANCED_DELIMITERS",
                ValidationSeverity.ERROR,
                f"Translation has unbalanced {group_name} delimiters",
                "structure",
                group=group_name,
            )
        else:
            translated_count = sum(translated_counts)
            if source_count > 0 and translated_count == 0:
                result.add(
                    "BRACKET_STRUCTURE_LOSS",
                    ValidationSeverity.ERROR,
                    f"A {group_name} pair from the source disappeared",
                    "structure",
                    group=group_name,
                )
            elif source_count > translated_count:
                result.add(
                    "BRACKET_STRUCTURE_PARTIAL_LOSS",
                    partial_loss_severity,
                    f"Some {group_name} pairs from the source disappeared",
                    "structure",
                    group=group_name,
                    source_pairs=source_count,
                    translation_pairs=translated_count,
                )

    source_quote_count = sum(max(_balanced_pair_count(source, op, cl), 0) for op, cl in _QUOTE_PAIRS)
    translated_quote_counts = [_balanced_pair_count(translation, op, cl) for op, cl in _QUOTE_PAIRS]
    if any(count < 0 for count in translated_quote_counts):
        result.add(
            "UNBALANCED_QUOTES",
            ValidationSeverity.ERROR,
            "Translation has unbalanced quotation marks",
            "structure",
        )
    else:
        translated_quote_count = sum(translated_quote_counts)
        if source_quote_count > 0 and translated_quote_count == 0:
            result.add(
                "QUOTE_STRUCTURE_LOSS",
                ValidationSeverity.ERROR,
                "Quotation marks from the source disappeared",
                "structure",
            )
        elif source_quote_count > translated_quote_count:
            result.add(
                "QUOTE_STRUCTURE_PARTIAL_LOSS",
                partial_loss_severity,
                "Some structural quote pairs from the source disappeared",
                "structure",
                source_pairs=source_quote_count,
                translation_pairs=translated_quote_count,
            )

    source_len = len(source.strip())
    translated_len = len(translation.strip())
    if source_len >= 4 and translated_len > max(120, source_len * 4):
        result.add(
            "MERGED_OUTPUT_LENGTH_RISK",
            ValidationSeverity.RISK,
            "Translation is unusually long and may contain generated or merged content",
            "structure",
            source_length=source_len,
            translation_length=translated_len,
        )
    elif source_len >= 30 and translated_len < max(2, int(source_len * 0.12)):
        result.add(
            "TRUNCATION_LENGTH_RISK",
            ValidationSeverity.RISK,
            "Translation is unusually short and may be truncated",
            "structure",
            source_length=source_len,
            translation_length=translated_len,
        )
    return result
