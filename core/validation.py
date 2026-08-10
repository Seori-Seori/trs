from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.config import ValidatorsConfig
from core.parser import ParsedResponse, SingleTranslationResponse
from core.placeholders import PlaceholderEngine, PlaceholderError
from core.segment import Segment, ValidationResult, ValidationSeverity
from core.terminology import is_novel_profile
from validators.korean import validate_korean
from validators.placeholders import validate_placeholders, validate_restored_tokens
from validators.risk import validate_risks
from validators.structure import (
    restore_full_span_outer_quote,
    validate_structure,
    validate_text_structure,
)
from validators.terminology import validate_novel_terminology


@dataclass
class SegmentEvaluation:
    result: ValidationResult
    translation: str | None


@dataclass
class ResponseEvaluation:
    by_segment_id: dict[str, SegmentEvaluation]
    global_result: ValidationResult


class ValidationCoordinator:
    def __init__(
        self,
        config: ValidatorsConfig,
        placeholder_engine: PlaceholderEngine,
        profile: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.placeholder_engine = placeholder_engine
        self.profile = dict(profile or {})
        self.policy = dict(self.profile.get("validation", {}))
        self.novel_mode = is_novel_profile(self.profile)
        self.cjk_whitelist = [
            str(item) for item in self.profile.get("cjk_residue_whitelist", [])
        ]

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
                candidate, quote_restored = restore_full_span_outer_quote(
                    segment.source, candidate
                )
                if quote_restored:
                    result.add(
                        "OUTER_QUOTE_RESTORED",
                        ValidationSeverity.WARNING,
                        "A single source-owned full-span outer quote pair was restored",
                        "structure",
                    )
                result.extend(
                    validate_text_structure(segment.source, candidate, self.policy)
                )
                result.extend(
                    validate_korean(
                        segment.source,
                        candidate,
                        segment.source_language,
                        self.config,
                        novel_mode=self.novel_mode,
                        cjk_whitelist=self.cjk_whitelist,
                        protected_values=[
                            token.original for token in segment.protected_tokens
                        ],
                    )
                )
                result.extend(
                    validate_novel_terminology(
                        segment.source,
                        candidate,
                        segment.source_language,
                        self.profile,
                    )
                )
                result.extend(
                    validate_risks(
                        segment.source, candidate, self.config, self.policy
                    )
                )
            evaluations[segment.id] = SegmentEvaluation(
                result=result,
                translation=candidate if not result.has_errors else None,
            )
        return ResponseEvaluation(evaluations, structure.global_result)

    def evaluate_single(
        self,
        response: SingleTranslationResponse,
        segment: Segment,
    ) -> ResponseEvaluation:
        """Map one native response to its already-known Segment and reuse validators."""
        synthetic = ParsedResponse(
            rows={segment.id: response.translation},
            row_order=[segment.id],
            duplicates=[],
            malformed_lines=[],
            raw_response=response.raw_response,
            code_fence_removed=response.code_fence_removed,
        )
        return self.evaluate_response(synthetic, [segment])

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
        result.extend(validate_text_structure(segment.source, translation, self.policy))
        result.extend(
            validate_korean(
                segment.source,
                translation,
                segment.source_language,
                self.config,
                novel_mode=self.novel_mode,
                cjk_whitelist=self.cjk_whitelist,
                protected_values=[
                    token.original for token in segment.protected_tokens
                ],
            )
        )
        result.extend(
            validate_novel_terminology(
                segment.source,
                translation,
                segment.source_language,
                self.profile,
            )
        )
        result.extend(
            validate_risks(segment.source, translation, self.config, self.policy)
        )
        return result
