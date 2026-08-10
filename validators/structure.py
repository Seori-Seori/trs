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
        r"<<<(?:item|targets|context|failures|reference_context|source|end)",
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
_QUOTE_CHARACTERS = {
    character for pair in _QUOTE_PAIRS for character in pair
}
_PARENTHETICAL_RE = re.compile(r"\(([^()]*)\)|（([^（）]*)）")
_TERMINAL_PUNCTUATION = frozenset(".!?。！？…")
_TRAILING_WRAPPERS = frozenset("」』”’\"')]}）］｝")
_INCOMPLETE_KOREAN_END_RE = re.compile(
    r"(?:^|[\s,，])(?:울|하|되|있|없|않|였|했|었|겠|시키|말하|느끼|"
    r"생각하|이어지|계속되|퍼지|흐르|떨리|울리|들리)$"
)


def _balanced_pair_count(text: str, opening: str, closing: str) -> int:
    if opening == closing:
        return text.count(opening) // 2 if text.count(opening) % 2 == 0 else -1
    if text.count(opening) != text.count(closing):
        return -1
    return text.count(opening)


def restore_full_span_outer_quote(source: str, candidate: str) -> tuple[str, bool]:
    """Restore only one unambiguous source-owned quote wrapper."""
    source_text = source.strip()
    candidate_text = candidate.strip()
    if not candidate_text or any(
        character in candidate_text for character in _QUOTE_CHARACTERS
    ):
        return candidate, False

    for opening, closing in _QUOTE_PAIRS:
        if not source_text.startswith(opening) or not source_text.endswith(closing):
            continue
        if opening == closing:
            if source_text.count(opening) != 2:
                continue
        elif source_text.count(opening) != 1 or source_text.count(closing) != 1:
            continue
        interior = source_text[len(opening):len(source_text) - len(closing)]
        if not interior or any(
            character in interior for character in _QUOTE_CHARACTERS
        ):
            continue
        return f"{opening}{candidate_text}{closing}", True
    return candidate, False


def _nonempty_parenthetical_count(text: str) -> int:
    return sum(
        bool((match.group(1) or match.group(2) or "").strip())
        for match in _PARENTHETICAL_RE.finditer(text)
    )


def _without_trailing_wrappers(text: str) -> str:
    value = text.rstrip()
    while value and value[-1] in _TRAILING_WRAPPERS:
        value = value[:-1].rstrip()
    return value


def _is_obviously_truncated(source: str, translation: str) -> bool:
    source_core = _without_trailing_wrappers(source)
    translation_core = _without_trailing_wrappers(translation)
    if not source_core or not translation_core:
        return False
    if source_core[-1] not in _TERMINAL_PUNCTUATION:
        return False
    if translation_core[-1] in _TERMINAL_PUNCTUATION:
        return False

    source_length = len(re.sub(r"\s", "", source_core))
    translation_length = len(re.sub(r"\s", "", translation_core))
    materially_short = (
        source_length >= 14
        and translation_length <= max(8, int(source_length * 1.1))
    )
    return materially_short and bool(
        _INCOMPLETE_KOREAN_END_RE.search(translation_core)
    )


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

    if policy and "parenthetical_content_loss_severity" in policy:
        source_parenthetical_count = _nonempty_parenthetical_count(source)
        translated_parenthetical_count = _nonempty_parenthetical_count(translation)
        translated_parenthesis_pairs = sum(
            max(_balanced_pair_count(translation, opening, closing), 0)
            for opening, closing in _PAIR_GROUPS[0][1]
        )
        if (
            source_parenthetical_count > translated_parenthetical_count
            and translated_parenthesis_pairs > 0
        ):
            result.add(
                "PARENTHETICAL_CONTENT_LOSS",
                policy_severity(
                    policy,
                    "parenthetical_content_loss_severity",
                    ValidationSeverity.ERROR,
                ),
                "One or more source parenthetical asides lost their contents",
                "structure",
                source_asides=source_parenthetical_count,
                translated_asides=translated_parenthetical_count,
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
    if (
        policy
        and "truncated_output_severity" in policy
        and _is_obviously_truncated(source, translation)
    ):
        result.add(
            "TRUNCATED_OUTPUT",
            policy_severity(
                policy,
                "truncated_output_severity",
                ValidationSeverity.ERROR,
            ),
            "Korean output appears to stop at an incomplete clause",
            "structure",
            source_ending=source[-24:],
            translation_ending=translation[-24:],
        )
    return result
