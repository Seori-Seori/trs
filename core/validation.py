from __future__ import annotations

from dataclasses import dataclass

from core.config import ValidatorsConfig
from core.parser import ParsedResponse
from core.placeholders import PlaceholderEngine, PlaceholderError
from core.segment import Segment, ValidationResult, ValidationSeverity
from validators.korean import validate_korean
from validators.placeholders import validate_placeholders, validate_restored_tokens
from validators.risk import validate_risks
from validators.structure import validate_structure, validate_text_structure


@dataclass
class SegmentEvaluation:
    result: ValidationResult
    translation: str | None


@dataclass
class ResponseEvaluation:
    by_segment_id: dict[str, SegmentEvaluation]
    global_result: ValidationResult


class ValidationCoordinator:
    def __init__(self, config: ValidatorsConfig, placeholder_engine: PlaceholderEngine) -> None:
        self.config = config
        self.placeholder_engine = placeholder_engine

    def evaluate_response(
        self, parsed: ParsedResponse, expected: list[Segment]
    ) -> ResponseEvaluation:
        structure = validate_structure(parsed, expected, self.config)
        evaluations: dict[str, SegmentEvaluation] = {}

        for segment in expected:
            result = structure.by_segment_id[segment.id]
            candidate: str | None = None
            raw_translation = parsed.rows.get(segment.id)
            if raw_translation is not None and not result.has_errors:
                result.extend(validate_placeholders(raw_translation, segment.protected_tokens))
            if raw_translation is not None and not result.has_errors:
                try:
                    candidate = self.placeholder_engine.restore(
                        raw_translation, segment.protected_tokens
                    )
                except PlaceholderError as exc:
                    placeholder_result = ValidationResult()
                    placeholder_result.add(
                        "PLACEHOLDER_RESTORATION_MISMATCH",
                        severity=ValidationSeverity.ERROR,
                        message=str(exc),
                        validator="placeholders",
                    )
                    result.extend(placeholder_result)
            if candidate is not None and not result.has_errors:
                result.extend(validate_text_structure(segment.source, candidate))
                result.extend(
                    validate_korean(
                        segment.source,
                        candidate,
                        segment.source_language,
                        self.config,
                    )
                )
                result.extend(validate_risks(segment.source, candidate, self.config))
            evaluations[segment.id] = SegmentEvaluation(
                result=result,
                translation=candidate if not result.has_errors else None,
            )
        return ResponseEvaluation(evaluations, structure.global_result)

    def validate_final(self, segment: Segment, translation: str) -> ValidationResult:
        synthetic = ParsedResponse(
            rows={segment.id: translation},
            row_order=[segment.id],
            duplicates=[],
            malformed_lines=[],
            raw_response="",
        )
        result = validate_structure(synthetic, [segment], self.config).by_segment_id[segment.id]
        result.extend(validate_restored_tokens(translation, segment.protected_tokens))
        result.extend(validate_text_structure(segment.source, translation))
        result.extend(
            validate_korean(
                segment.source,
                translation,
                segment.source_language,
                self.config,
            )
        )
        result.extend(validate_risks(segment.source, translation, self.config))
        return result
