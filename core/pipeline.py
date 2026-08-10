from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any

from core.batching import build_batches
from core.checkpoint import CheckpointStore
from core.config import AppConfig
from core.language import detect_language
from core.parser import ResponseParser
from core.placeholders import PlaceholderEngine
from core.recovery import RecoveryEngine
from core.segment import Segment, SegmentStatus, ValidationIssue
from core.validation import ValidationCoordinator
from translators.base import Translator


@dataclass
class PipelineResult:
    segments: list[Segment]
    resumed: int
    already_korean: int
    global_issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def failed(self) -> list[Segment]:
        return [segment for segment in self.segments if segment.status == SegmentStatus.FAILED]

    @property
    def valid(self) -> list[Segment]:
        return [segment for segment in self.segments if segment.status == SegmentStatus.VALID]


class TranslationPipeline:
    def __init__(
        self,
        config: AppConfig,
        profile: dict[str, Any],
        translator: Translator,
        checkpoint: CheckpointStore | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.profile = profile
        self.translator = translator
        self.checkpoint = checkpoint
        self.progress = progress or (lambda _message: None)
        self.placeholder_engine = PlaceholderEngine(
            config.placeholders.custom_patterns,
            protect_internal_newlines=config.placeholders.protect_internal_newlines,
        )
        self.parser = ResponseParser()
        self.validator = ValidationCoordinator(config.validators, self.placeholder_engine)

    def process(self, segments: list[Segment], *, resume: bool = True) -> PipelineResult:
        self._build_context(segments)
        resumed = 0
        already_korean = 0

        for segment in segments:
            segment.source_language = detect_language(
                segment.source, self.config.translation.source_language
            )
            prepared = self.placeholder_engine.protect(segment.source)
            segment.prepare(prepared.text, prepared.tokens)

            record = self.checkpoint.load_valid(segment) if resume and self.checkpoint else None
            if record is not None and record.translation is not None:
                validation = self.validator.validate_final(segment, record.translation)
                if not validation.has_errors:
                    segment.attempt_count = record.attempt_count
                    segment.was_repaired = record.repaired
                    segment.mark_valid(record.translation, validation, repaired=record.repaired)
                    resumed += 1
                    continue

            if segment.source_language == "ko":
                validation = self.validator.validate_final(segment, segment.source)
                if not validation.has_errors:
                    segment.mark_valid(segment.source, validation)
                    already_korean += 1
                    if self.checkpoint:
                        self.checkpoint.save_segment(segment)

        pending = [segment for segment in segments if segment.status != SegmentStatus.VALID]
        batches = build_batches(
            pending,
            batch_size=self.config.translation.batch_size,
            max_batch_chars=self.config.translation.max_batch_chars,
        )
        global_issues: list[ValidationIssue] = []

        def save_valid(segment: Segment) -> None:
            if self.checkpoint:
                self.checkpoint.save_segment(segment)

        recovery = RecoveryEngine(
            translator=self.translator,
            parser=self.parser,
            validator=self.validator,
            placeholder_engine=self.placeholder_engine,
            recovery_config=self.config.recovery,
            translation_config=self.config.translation,
            profile=self.profile,
            on_valid=save_valid,
        )
        self.progress(
            f"총 {len(segments)}개 문단: 재개 {resumed}, 한국어 유지 {already_korean}, 번역 대상 {len(pending)}"
        )
        for batch_index, batch in enumerate(batches, start=1):
            self.progress(
                f"배치 {batch_index}/{len(batches)} 번역 중 ({len(batch)}개 문단)"
            )
            result = recovery.translate_batch(batch)
            global_issues.extend(result.global_issues)
            recovery.global_issues.clear()
            for failed in result.failed:
                if self.checkpoint:
                    self.checkpoint.save_segment(failed)
            self.progress(
                f"배치 {batch_index}/{len(batches)} 완료: VALID {len(result.valid)}, FAILED {len(result.failed)}"
            )

        nonterminal = [
            segment
            for segment in segments
            if segment.status not in {SegmentStatus.VALID, SegmentStatus.FAILED}
        ]
        if nonterminal:
            raise RuntimeError(
                f"Pipeline ended with non-terminal Segment states: {[s.id for s in nonterminal]}"
            )
        return PipelineResult(
            segments=segments,
            resumed=resumed,
            already_korean=already_korean,
            global_issues=global_issues,
        )

    def _build_context(self, segments: list[Segment]) -> None:
        before_count = int(
            self.profile.get("context_before", self.config.translation.context_before)
        )
        after_count = int(
            self.profile.get("context_after", self.config.translation.context_after)
        )
        for index, segment in enumerate(segments):
            segment.context_before = [
                item.source for item in segments[max(0, index - before_count):index]
            ]
            segment.context_after = [
                item.source for item in segments[index + 1:index + 1 + after_count]
            ]
