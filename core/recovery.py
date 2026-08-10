from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.batching import safe_split_with_separators
from core.config import RecoveryConfig, TranslationConfig
from core.novel_register import NovelJobNameMap
from core.parser import SingleTranslationParser
from core.placeholders import PlaceholderEngine
from core.prompting import PromptBuildError, build_single_translation_prompt
from core.segment import (
    Segment,
    SegmentStatus,
    ValidationResult,
    ValidationSeverity,
)
from core.validation import ValidationCoordinator
from translators.base import Translator


@dataclass
class RecoveryResult:
    valid: list[Segment]
    failed: list[Segment]
    global_issues: list[Any] = field(default_factory=list)


class RecoveryEngine:
    def __init__(
        self,
        translator: Translator,
        parser: SingleTranslationParser,
        validator: ValidationCoordinator,
        placeholder_engine: PlaceholderEngine,
        recovery_config: RecoveryConfig,
        translation_config: TranslationConfig,
        profile: dict[str, Any],
        *,
        on_valid: Callable[[Segment], None] | None = None,
        on_valid_batch: Callable[[list[Segment]], None] | None = None,
        on_failed_attempt: Callable[[Segment], None] | None = None,
        on_validated: Callable[[Segment], None] | None = None,
        name_map: NovelJobNameMap | None = None,
    ) -> None:
        self.translator = translator
        self.parser = parser
        self.validator = validator
        self.placeholder_engine = placeholder_engine
        self.recovery_config = recovery_config
        self.translation_config = translation_config
        self.profile = profile
        self.on_valid = on_valid or (lambda _segment: None)
        self.on_valid_batch = on_valid_batch
        self.on_failed_attempt = on_failed_attempt or (lambda _segment: None)
        self.on_validated = on_validated or (lambda _segment: None)
        self.name_map = name_map or NovelJobNameMap.from_profile(profile)
        self.global_issues: list[Any] = []

    def translate_segment(self, segment: Segment) -> RecoveryResult:
        if segment.status == SegmentStatus.VALID:
            raise RuntimeError(
                f"Invariant violation: VALID segment {segment.id} entered recovery"
            )
        issue_start = len(self.global_issues)
        self._translate_one(segment)
        return RecoveryResult(
            valid=[segment] if segment.status == SegmentStatus.VALID else [],
            failed=[segment] if segment.status == SegmentStatus.FAILED else [],
            global_issues=list(self.global_issues[issue_start:]),
        )

    def translate_batch(self, batch: list[Segment]) -> RecoveryResult:
        """Compatibility entrypoint that still sends one native request per Segment."""
        if any(segment.status == SegmentStatus.VALID for segment in batch):
            ids = [segment.id for segment in batch if segment.status == SegmentStatus.VALID]
            raise RuntimeError(
                f"Invariant violation: VALID segments entered recovery: {ids}"
            )
        issue_start = len(self.global_issues)
        for segment in batch:
            self._translate_one(segment)
        return RecoveryResult(
            valid=[segment for segment in batch if segment.status == SegmentStatus.VALID],
            failed=[segment for segment in batch if segment.status == SegmentStatus.FAILED],
            global_issues=list(self.global_issues[issue_start:]),
        )

    def _translate_one(self, segment: Segment) -> None:
        if len(segment.source) > self.translation_config.max_segment_chars:
            self._translate_long_segment(segment)
        else:
            self._recover_single(segment, initial_mode="single")

    def _recover_single(self, segment: Segment, *, initial_mode: str) -> None:
        first_request = True
        while (
            segment.status != SegmentStatus.VALID
            and segment.attempt_count < self.recovery_config.max_attempts
        ):
            mode = initial_mode if first_request else "repair"
            self._request_single(segment, mode=mode)
            first_request = False

        if segment.status != SegmentStatus.VALID:
            if segment.status != SegmentStatus.FAILED:
                segment.mark_failed(
                    error=segment.last_error or "Validation failed after recovery"
                )
            self.on_failed_attempt(segment)

    def _request_single(self, segment: Segment, *, mode: str) -> None:
        if segment.status == SegmentStatus.VALID:
            raise RuntimeError(
                f"Invariant violation: VALID segment {segment.id} entered request"
            )
        prompt = build_single_translation_prompt(
            segment, self.profile, mode=mode, name_map=self.name_map
        )
        limit = self.translation_config.max_prompt_chars
        if len(prompt) > limit:
            raise PromptBuildError(
                f"{segment.id}의 단일 프롬프트가 translation.max_prompt_chars "
                f"제한({limit}자)을 초과합니다"
            )

        # Transport failures remain job-level: no state transition or semantic
        # attempt is consumed until a model response actually exists.
        raw_response = self.translator.translate(prompt)
        segment.begin_translation()
        segment.record_prompt(prompt, mode=mode)
        segment.record_raw_response(raw_response)

        parsed = self.parser.parse(raw_response)
        segment.mark_parsed(parsed.translation)
        evaluation = self.validator.evaluate_single(parsed, segment)
        self.global_issues.extend(evaluation.global_result.issues)
        segment_evaluation = evaluation.by_segment_id[segment.id]

        if (
            segment_evaluation.translation is not None
            and not segment_evaluation.result.has_errors
        ):
            segment.mark_valid(
                segment_evaluation.translation,
                segment_evaluation.result,
                repaired=segment.attempt_count > 1,
            )
            self._notify_valid([segment])
            return

        segment.mark_for_repair(segment_evaluation.result)
        self.on_failed_attempt(segment)

    def _notify_valid(self, segments: list[Segment]) -> None:
        if not segments:
            return
        for segment in segments:
            self.on_validated(segment)
        if self.on_valid_batch is not None:
            self.on_valid_batch(segments)
            return
        for segment in segments:
            self.on_valid(segment)

    def _translate_long_segment(self, parent: Segment) -> None:
        pieces = safe_split_with_separators(
            parent.source,
            self.translation_config.max_segment_chars,
            self.placeholder_engine,
        )
        if len(pieces) == 1:
            self._recover_single(parent, initial_mode="single")
            return

        children: list[Segment] = []
        for index, piece in enumerate(pieces, start=1):
            if not piece.text:
                continue
            child = Segment(
                id=f"{parent.id}__PART_{index:04d}",
                source=piece.text,
                source_language=parent.source_language,
                target_language="ko",
                file_path=parent.file_path,
                location=parent.location,
                metadata={"parent_id": parent.id, "piece_index": index},
            )
            prepared = self.placeholder_engine.protect(child.source)
            child.prepare(prepared.text, prepared.tokens)
            children.append(child)

        for index, child in enumerate(children):
            child.context_before = parent.context_before + [
                item.source for item in children[max(0, index - 2):index]
            ]
            child.context_after = [
                item.source for item in children[index + 1:index + 3]
            ] + parent.context_after

        original_callback = self.on_valid
        original_batch_callback = self.on_valid_batch
        self.on_valid = lambda _segment: None
        self.on_valid_batch = lambda _segments: None
        try:
            for child in children:
                self._recover_single(child, initial_mode="split")
        finally:
            self.on_valid = original_callback
            self.on_valid_batch = original_batch_callback

        parent.attempt_count = max(
            (child.attempt_count for child in children), default=0
        )
        failed_children = [
            child for child in children if child.status != SegmentStatus.VALID
        ]
        if failed_children:
            diagnostic_child = failed_children[-1]
            self._copy_diagnostics(diagnostic_child, parent)
            result = ValidationResult()
            result.add(
                "LONG_SEGMENT_PART_FAILED",
                ValidationSeverity.ERROR,
                "One or more safe-split parts failed translation",
                "recovery",
                child_ids=[child.id for child in failed_children],
                child_failure_codes={
                    child.id: sorted(
                        {
                            issue.code
                            for issue in child.validation_issues
                            if issue.severity == ValidationSeverity.ERROR
                        }
                    )
                    for child in failed_children
                },
            )
            parent.mark_failed(
                result, "긴 문단의 분할 조각 번역이 끝까지 실패했습니다"
            )
            self.on_failed_attempt(parent)
            return

        translated_parts: list[str] = []
        child_index = 0
        for piece in pieces:
            if piece.text:
                translated_parts.append(children[child_index].translation or "")
                child_index += 1
            translated_parts.append(piece.separator_after)
        candidate = "".join(translated_parts)
        final_result = self.validator.validate_final(parent, candidate)
        if final_result.has_errors:
            if children:
                self._copy_diagnostics(children[-1], parent)
            parent.mark_failed(
                final_result, "Joined long Segment failed final validation"
            )
            self.on_failed_attempt(parent)
            return
        parent.mark_valid(
            candidate,
            final_result,
            repaired=any(child.was_repaired for child in children),
        )
        self._notify_valid([parent])

    @staticmethod
    def _copy_diagnostics(source: Segment, target: Segment) -> None:
        target.last_raw_response = source.last_raw_response
        target.last_raw_response_truncated = source.last_raw_response_truncated
        target.last_prompt = source.last_prompt
        target.last_prompt_truncated = source.last_prompt_truncated
        target.last_request_mode = source.last_request_mode
