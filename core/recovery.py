from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.batching import safe_split_with_separators
from core.config import RecoveryConfig, TranslationConfig
from core.parser import ResponseParser
from core.placeholders import PlaceholderEngine
from core.prompting import PromptBuildError, build_prompt
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
        parser: ResponseParser,
        validator: ValidationCoordinator,
        placeholder_engine: PlaceholderEngine,
        recovery_config: RecoveryConfig,
        translation_config: TranslationConfig,
        profile: dict[str, Any],
        *,
        on_valid: Callable[[Segment], None] | None = None,
        on_valid_batch: Callable[[list[Segment]], None] | None = None,
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
        self.global_issues: list[Any] = []

    def translate_batch(self, batch: list[Segment]) -> RecoveryResult:
        if any(segment.status == SegmentStatus.VALID for segment in batch):
            ids = [segment.id for segment in batch if segment.status == SegmentStatus.VALID]
            raise RuntimeError(f"Invariant violation: VALID segments entered recovery: {ids}")

        if len(batch) == 1 and len(batch[0].source) > self.translation_config.max_segment_chars:
            self._translate_long_segment(batch[0])
        else:
            self._recover_group(batch, initial_mode="batch")

        failed = [segment for segment in batch if segment.status == SegmentStatus.FAILED]
        valid = [segment for segment in batch if segment.status == SegmentStatus.VALID]
        return RecoveryResult(valid=valid, failed=failed, global_issues=list(self.global_issues))

    def _request_group(self, segments: list[Segment], *, mode: str, repaired: bool) -> list[Segment]:
        targets = [
            segment
            for segment in segments
            if segment.status != SegmentStatus.VALID
            and segment.attempt_count < self.recovery_config.max_attempts
        ]
        if not targets:
            return [segment for segment in segments if segment.status != SegmentStatus.VALID]

        failed: list[Segment] = []
        for group in self._partition_by_prompt_budget(targets, mode=mode):
            failed.extend(self._request_exact_group(group, mode=mode, repaired=repaired))
        return failed

    def _partition_by_prompt_budget(
        self, targets: list[Segment], *, mode: str
    ) -> list[list[Segment]]:
        limit = self.translation_config.max_prompt_chars
        groups: list[list[Segment]] = []
        current: list[Segment] = []

        for segment in targets:
            candidate = current + [segment]
            candidate_prompt = build_prompt(candidate, self.profile, mode=mode)
            if len(candidate_prompt) <= limit:
                current = candidate
                continue
            if current:
                groups.append(current)
                current = [segment]
                single_prompt = build_prompt(current, self.profile, mode=mode)
                if len(single_prompt) <= limit:
                    continue
            raise PromptBuildError(
                f"{segment.id}의 단일 프롬프트가 translation.max_prompt_chars "
                f"제한({limit}자)을 초과합니다"
            )

        if current:
            groups.append(current)
        return groups

    def _request_exact_group(
        self, targets: list[Segment], *, mode: str, repaired: bool
    ) -> list[Segment]:
        prompt = build_prompt(targets, self.profile, mode=mode)
        # Transport/server failures are job-level failures. Do not change Segment
        # state or consume its semantic retry budget until a model response exists.
        raw_response = self.translator.translate(prompt)

        for segment in targets:
            segment.begin_translation()
            segment.record_raw_response(raw_response)

        parsed = self.parser.parse(raw_response)
        evaluation = self.validator.evaluate_response(parsed, targets)
        self.global_issues.extend(evaluation.global_result.issues)

        failed: list[Segment] = []
        valid: list[Segment] = []
        for segment in targets:
            raw_translation = parsed.rows.get(segment.id, "")
            segment.mark_parsed(raw_translation)
            segment_evaluation = evaluation.by_segment_id[segment.id]
            if (
                segment_evaluation.translation is not None
                and not segment_evaluation.result.has_errors
            ):
                segment.mark_valid(
                    segment_evaluation.translation,
                    segment_evaluation.result,
                    repaired=repaired or segment.attempt_count > 1,
                )
                valid.append(segment)
            else:
                segment.mark_for_repair(segment_evaluation.result)
                failed.append(segment)
        self._notify_valid(valid)
        return failed

    def _notify_valid(self, segments: list[Segment]) -> None:
        if not segments:
            return
        if self.on_valid_batch is not None:
            self.on_valid_batch(segments)
            return
        for segment in segments:
            self.on_valid(segment)

    def _recover_group(self, segments: list[Segment], *, initial_mode: str) -> None:
        initial_failed = self._request_group(
            segments, mode=initial_mode, repaired=initial_mode != "batch"
        )
        remaining = [segment for segment in initial_failed if segment.status != SegmentStatus.VALID]
        if not remaining:
            return

        failed_ratio = len(remaining) / max(1, len(segments))
        # The first recovery request is always a partial repair containing only
        # failed rows. Splitting is allowed only after this request also fails.
        remaining = self._request_group(remaining, mode="repair", repaired=True)
        prefer_single_fallback = (
            failed_ratio <= self.recovery_config.partial_repair_max_ratio
        )

        while remaining:
            eligible = [
                segment
                for segment in remaining
                if segment.status != SegmentStatus.VALID
                and segment.attempt_count < self.recovery_config.max_attempts
            ]
            if not eligible:
                break

            if not self.recovery_config.split_on_failure:
                groups = [eligible]
            elif len(eligible) == 1:
                groups = [[eligible[0]]]
            else:
                remaining_attempts = min(
                    self.recovery_config.max_attempts - segment.attempt_count
                    for segment in eligible
                )
                if prefer_single_fallback or remaining_attempts <= 1:
                    groups = [[segment] for segment in eligible]
                else:
                    midpoint = max(1, len(eligible) // 2)
                    groups = [eligible[:midpoint], eligible[midpoint:]]
                    groups = [group for group in groups if group]

            next_remaining: list[Segment] = []
            for group in groups:
                mode = "single" if len(group) == 1 else "repair"
                next_remaining.extend(self._request_group(group, mode=mode, repaired=True))
            remaining = [
                segment for segment in next_remaining if segment.status != SegmentStatus.VALID
            ]

        for segment in remaining:
            if segment.status != SegmentStatus.FAILED:
                segment.mark_failed(error=segment.last_error or "Validation failed after recovery")

    def _translate_long_segment(self, parent: Segment) -> None:
        pieces = safe_split_with_separators(
            parent.source,
            self.translation_config.max_segment_chars,
            self.placeholder_engine,
        )
        if len(pieces) == 1:
            self._recover_group([parent], initial_mode="batch")
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
            child.context_before = parent.context_before + [item.source for item in children[max(0, index - 2):index]]
            child.context_after = [item.source for item in children[index + 1:index + 3]] + parent.context_after

        original_callback = self.on_valid
        original_batch_callback = self.on_valid_batch
        self.on_valid = lambda _segment: None
        self.on_valid_batch = lambda _segments: None
        try:
            # A long parent is split specifically to reduce each model request,
            # so its children must not be recombined into one oversized prompt.
            for child in children:
                self._recover_group([child], initial_mode="split")
        finally:
            self.on_valid = original_callback
            self.on_valid_batch = original_batch_callback

        parent.attempt_count = max((child.attempt_count for child in children), default=0)
        failed_children = [child for child in children if child.status != SegmentStatus.VALID]
        if failed_children:
            diagnostic_child = failed_children[-1]
            parent.last_raw_response = diagnostic_child.last_raw_response
            parent.last_raw_response_truncated = (
                diagnostic_child.last_raw_response_truncated
            )
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
            parent.mark_failed(result, "긴 문단의 분할 조각 번역이 끝까지 실패했습니다")
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
            parent.mark_failed(final_result, "Joined long Segment failed final validation")
            return
        parent.mark_valid(
            candidate,
            final_result,
            repaired=any(child.was_repaired for child in children),
        )
        self._notify_valid([parent])
