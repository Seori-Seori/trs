from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

from adapters.text import TextAdapter
from core.checkpoint import CheckpointStore
from core.config import ConfigError, load_config, load_profile
from core.diagnostics import FailureDebugStore
from core.mappings import (
    JobMappingError,
    JobMappings,
    resolve_job_mappings,
)
from core.parser import ResponseParser, SingleTranslationParser
from core.pipeline import PipelineResult, TranslationPipeline
from core.placeholders import PlaceholderEngine, PlaceholderError
from core.prompting import build_single_translation_prompt
from core.recovery import RecoveryEngine
from core.reporting import build_qa_report
from core.segment import (
    Segment,
    SegmentStatus,
    ValidationResult,
    ValidationSeverity,
)
from core.validation import ValidationCoordinator
from tests.helpers import NoCallTranslator, ScriptedTranslator, native_prompt_source
from translators.base import TranslationTransportError
from validators.korean import validate_korean
from validators.placeholders import validate_placeholders
from validators.risk import validate_risks
from validators.structure import validate_structure, validate_text_structure


ROOT = Path(__file__).resolve().parents[2]


class CountingSingleParser(SingleTranslationParser):
    def __init__(self) -> None:
        self.calls = 0
        self.raw_responses: list[str] = []

    def parse(self, raw_response: str):
        self.calls += 1
        self.raw_responses.append(raw_response)
        return super().parse(raw_response)


class MandatoryRegressionCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(ROOT / "config.json")
        cls.profile = load_profile("novel")
        cls.engine = PlaceholderEngine()
        cls.parser = ResponseParser()

    def _segments(self, count: int) -> list[Segment]:
        return [Segment(f"SEG_{index:08d}", f"原文{index}") for index in range(1, count + 1)]

    def _prepared_segments(self, count: int) -> list[Segment]:
        segments = self._segments(count)
        for segment in segments:
            segment.source_language = "ja"
            prepared = self.engine.protect(segment.source)
            segment.prepare(prepared.text, prepared.tokens)
        return segments

    def _prepared_zh(self, source: str, segment_id: str = "SEG_00000001") -> Segment:
        segment = Segment(segment_id, source, source_language="zh")
        prepared = self.engine.protect(source)
        segment.prepare(prepared.text, prepared.tokens)
        return segment

    def test_r01_cjk_de_residue_is_error(self) -> None:
        result = validate_korean("彼は話した", "그는 말했的", "ja", self.config.validators)
        self.assertIn("KNOWN_BAD_CJK_RESIDUE", [issue.code for issue in result.issues])
        self.assertTrue(result.has_errors)

    def test_r02_deleted_newline_placeholder_is_error(self) -> None:
        prepared = self.engine.protect("첫 줄\n둘째 줄")
        result = validate_placeholders("첫 줄 둘째 줄", prepared.tokens)
        self.assertIn("MISSING_PLACEHOLDER", [issue.code for issue in result.issues])

    def test_r03_prompt_leak_is_error(self) -> None:
        segment = Segment("SEG_00000001", "hello")
        parsed = self.parser.parse("SEG_00000001\tTranslate the following text")
        result = validate_structure(parsed, [segment], self.config.validators)
        self.assertIn("PROMPT_LEAK", [i.code for i in result.by_segment_id[segment.id].issues])

    def test_r04_placeholder_example_leak_is_error(self) -> None:
        result = validate_placeholders("번역 [[CTRL_n]]", [])
        self.assertIn("UNEXPECTED_PLACEHOLDER", [issue.code for issue in result.issues])

    def test_r05_next_row_id_leak_only_invalidates_current_row(self) -> None:
        segments = self._segments(2)
        parsed = self.parser.parse(
            f"{segments[0].id}\t첫 번역 {segments[1].id} 다음 번역\n"
            f"{segments[1].id}\t정상 번역"
        )
        result = validate_structure(parsed, segments, self.config.validators)
        self.assertTrue(result.by_segment_id[segments[0].id].has_errors)
        self.assertFalse(result.by_segment_id[segments[1].id].has_errors)

    def test_r06_missing_id_salvages_present_rows(self) -> None:
        segments = self._segments(3)
        parsed = self.parser.parse(
            f"{segments[0].id}\t첫 번역\n{segments[2].id}\t셋째 번역"
        )
        result = validate_structure(parsed, segments, self.config.validators)
        self.assertFalse(result.by_segment_id[segments[0].id].has_errors)
        self.assertTrue(result.by_segment_id[segments[1].id].has_errors)
        self.assertFalse(result.by_segment_id[segments[2].id].has_errors)

    def test_r07_unexpected_id_is_global_error_but_expected_rows_survive(self) -> None:
        segments = self._segments(2)
        parsed = self.parser.parse(
            f"{segments[0].id}\t첫 번역\n{segments[1].id}\t둘째 번역\nSEG_99999999\t가짜"
        )
        result = validate_structure(parsed, segments, self.config.validators)
        self.assertIn("UNEXPECTED_ID", [issue.code for issue in result.global_result.issues])
        self.assertTrue(all(not item.has_errors for item in result.by_segment_id.values()))

    def test_r08_unexpected_cyrillic_is_error(self) -> None:
        result = validate_korean("Hello", "안녕하세요 Привет", "en", self.config.validators)
        self.assertIn("UNEXPECTED_SCRIPT", [issue.code for issue in result.issues])
        self.assertTrue(result.has_errors)

    def test_r09_skip_negation_flip_is_risk_not_error(self) -> None:
        result = validate_risks("Skip", "건너뛰지 마세요", self.config.validators)
        issue = next(issue for issue in result.issues if issue.code == "NEGATION_FLIP_RISK")
        self.assertEqual(issue.severity, ValidationSeverity.RISK)
        self.assertFalse(result.has_errors)

    def test_r10_following_context_is_in_prompt_but_not_a_target(self) -> None:
        first = Segment("SEG_00000001", "彼女は言った")
        first.source_language = "ja"
        first.context_after = ["次の段落です"]
        first.prepare(first.source, [])
        prompt = build_single_translation_prompt(first, self.profile)
        self.assertIn("次の段落です", prompt)
        self.assertEqual(native_prompt_source(prompt), first.source)
        self.assertNotIn(first.id, prompt)

    def test_r11_unrelated_generated_sentence_gets_length_risk(self) -> None:
        result = validate_text_structure("短い原文です", "전혀 다른 생성 문장 " * 30)
        self.assertIn("MERGED_OUTPUT_LENGTH_RISK", [issue.code for issue in result.issues])

    def test_r12_quote_loss_is_error(self) -> None:
        result = validate_text_structure("「Alice」", "앨리스")
        self.assertIn("QUOTE_STRUCTURE_LOSS", [issue.code for issue in result.issues])
        self.assertTrue(result.has_errors)

    def test_r13_rpg_control_code_round_trip(self) -> None:
        source = "\\N[1]에게 \\F[10]을 지급"
        prepared = self.engine.protect(source)
        self.assertEqual(self.engine.restore(prepared.text, prepared.tokens), source)

    def test_r14_excessive_random_latin_mix_is_risk_not_blanket_ban(self) -> None:
        result = validate_korean(
            "ああ", "아아 oh my god yes please now", "ja", self.config.validators
        )
        self.assertIn("EXCESSIVE_LATIN_MIX_RISK", [issue.code for issue in result.issues])
        self.assertFalse(result.has_errors)

    def test_r15_multiple_row_merge_heuristic(self) -> None:
        result = validate_text_structure("원문 한 문장", "다른 문장 " * 40)
        self.assertIn("MERGED_OUTPUT_LENGTH_RISK", [issue.code for issue in result.issues])

    def test_r16_duplicate_id_is_error(self) -> None:
        segment = self._segments(1)[0]
        parsed = self.parser.parse(f"{segment.id}\t하나\n{segment.id}\t둘")
        result = validate_structure(parsed, [segment], self.config.validators)
        self.assertIn("DUPLICATE_ID", [i.code for i in result.by_segment_id[segment.id].issues])

    def test_r17_strict_output_order_mismatch_is_error(self) -> None:
        segments = self._segments(2)
        parsed = self.parser.parse(
            f"{segments[1].id}\t둘째\n{segments[0].id}\t첫째"
        )
        result = validate_structure(parsed, segments, self.config.validators)
        self.assertTrue(all(item.has_errors for item in result.by_segment_id.values()))

    def test_r18_empty_translation_is_error(self) -> None:
        segment = self._segments(1)[0]
        parsed = self.parser.parse(f"{segment.id}\t   ")
        result = validate_structure(parsed, [segment], self.config.validators)
        self.assertIn("EMPTY_TRANSLATION", [i.code for i in result.by_segment_id[segment.id].issues])

    def test_r19_duplicate_placeholder_is_error(self) -> None:
        prepared = self.engine.protect("A \\N[1]")
        marker = prepared.tokens[0].placeholder
        result = validate_placeholders(f"번역 {marker} {marker}", prepared.tokens)
        self.assertIn("DUPLICATE_PLACEHOLDER", [issue.code for issue in result.issues])

    def test_r20_changed_placeholder_order_is_error(self) -> None:
        prepared = self.engine.protect("\\N[1] then %PLAYER%")
        reversed_markers = " ".join(token.placeholder for token in reversed(prepared.tokens))
        result = validate_placeholders(reversed_markers, prepared.tokens)
        self.assertIn("PLACEHOLDER_ORDER_MISMATCH", [issue.code for issue in result.issues])

    def test_r21_enclosing_code_fence_is_normalized_once_and_reported(self) -> None:
        segment = self._segments(1)[0]
        parsed = self.parser.parse(f"```text\n{segment.id}\t정상 번역\n```")
        result = validate_structure(parsed, [segment], self.config.validators)
        self.assertEqual(parsed.rows[segment.id], "정상 번역")
        self.assertIn("RESPONSE_CODE_FENCE", [i.code for i in result.global_result.issues])
        self.assertFalse(result.by_segment_id[segment.id].has_errors)

    def test_r22_resume_never_retranslates_valid_same_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint_path = Path(directory) / "resume.sqlite"
            first = Segment("SEG_00000001", "こんにちは")
            translator = ScriptedTranslator(["안녕하세요"])
            with CheckpointStore(checkpoint_path) as checkpoint:
                result = TranslationPipeline(
                    self.config, self.profile, translator, checkpoint
                ).process([first])
            self.assertFalse(result.failed)
            self.assertEqual(len(translator.calls), 1)

            same = Segment("SEG_00000001", "こんにちは")
            no_call = NoCallTranslator()
            with CheckpointStore(checkpoint_path) as checkpoint:
                resumed = TranslationPipeline(
                    self.config, self.profile, no_call, checkpoint
                ).process([same])
            self.assertEqual(resumed.resumed, 1)
            self.assertEqual(no_call.calls, 0)

    def test_r23_partial_repair_only_retries_failed_native_segments(self) -> None:
        segments = self._prepared_segments(10)
        bad_ids = {segments[2].id, segments[6].id}
        responses = []

        for segment in segments:
            def initial(prompt: str, *, expected=segment) -> str:
                self.assertEqual(
                    native_prompt_source(prompt), expected.prepared_source
                )
                self.assertNotIn(expected.id, prompt)
                return "오류的" if expected.id in bad_ids else "정상 번역"

            responses.append(initial)
            if segment.id in bad_ids:
                def repair(prompt: str, *, expected=segment) -> str:
                    self.assertEqual(
                        native_prompt_source(prompt), expected.prepared_source
                    )
                    self.assertIn("KNOWN_BAD_CJK_RESIDUE", prompt)
                    self.assertNotIn(expected.id, prompt)
                    return "복구된 번역"

                responses.append(repair)

        translator = ScriptedTranslator(responses)
        parser = CountingSingleParser()
        recovery = RecoveryEngine(
            translator,
            parser,
            ValidationCoordinator(self.config.validators, self.engine, self.profile),
            self.engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
        )
        result = recovery.translate_batch(segments)

        self.assertFalse(result.failed)
        self.assertEqual(len(translator.calls), 12)
        self.assertEqual(parser.calls, 12)
        self.assertTrue(
            all(segment.attempt_count == 1 for segment in segments if segment.id not in bad_ids)
        )
        self.assertTrue(
            all(segment.attempt_count == 2 for segment in segments if segment.id in bad_ids)
        )

    def test_r24_default_prompt_has_no_row_protocol(self) -> None:
        for segment in self._prepared_segments(2):
            segment.context_before = ["앞 문맥"]
            segment.context_after = ["뒤 문맥"]
            prompt = build_single_translation_prompt(segment, self.profile)
            self.assertNotIn(segment.id, prompt)
            self.assertNotIn("<<<ITEM", prompt)
            self.assertNotIn("<<<TARGETS", prompt)
            self.assertNotIn("ID<TAB>", prompt)
            self.assertEqual(native_prompt_source(prompt), segment.prepared_source)

    def test_r25_native_repair_prompt_retains_reason_without_id(self) -> None:
        segment = self._prepared_segments(1)[0]
        failure = ValidationResult()
        failure.add(
            "KNOWN_BAD_CJK_RESIDUE",
            ValidationSeverity.ERROR,
            "residue",
            "korean",
        )
        segment.validation_issues = failure.issues

        prompt = build_single_translation_prompt(segment, self.profile, mode="repair")

        self.assertIn("KNOWN_BAD_CJK_RESIDUE", prompt)
        self.assertIn("한국어만 출력", prompt)
        self.assertNotIn(segment.id, prompt)

    def test_r26_identical_duplicate_salvage_legacy_parser(self) -> None:
        segment = self._prepared_segments(1)[0]
        raw = f"{segment.id}\t정상 번역\n{segment.id}\t정상 번역"
        parsed = self.parser.parse(raw)
        evaluation = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_response(parsed, [segment])

        item = evaluation.by_segment_id[segment.id]
        self.assertEqual(item.translation, "정상 번역")
        self.assertFalse(item.result.has_errors)
        self.assertIn(
            "IDENTICAL_DUPLICATE_ID",
            [issue.code for issue in item.result.issues],
        )

    def test_r27_conflicting_duplicate_remains_error_in_legacy_parser(self) -> None:
        segment = self._prepared_segments(1)[0]
        raw = f"{segment.id}\t번역 A\n{segment.id}\t번역 B"
        parsed = self.parser.parse(raw)
        evaluation = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_response(parsed, [segment])

        self.assertEqual(parsed.conflicting_duplicates, [segment.id])
        self.assertIsNone(evaluation.by_segment_id[segment.id].translation)
        self.assertIn(
            "DUPLICATE_ID",
            [issue.code for issue in evaluation.by_segment_id[segment.id].result.issues],
        )

    def test_r28_triple_identical_duplicate_salvage(self) -> None:
        segment = self._prepared_segments(1)[0]
        row = f"{segment.id}\t정상 번역"
        parsed = self.parser.parse("\n".join([row, row, row]))
        evaluation = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_response(parsed, [segment])

        self.assertEqual(parsed.occurrences[segment.id], ["정상 번역"] * 3)
        self.assertEqual(parsed.identical_duplicates, [segment.id])
        self.assertFalse(parsed.conflicting_duplicates)
        self.assertEqual(evaluation.by_segment_id[segment.id].translation, "정상 번역")

    def test_r29_one_conflicting_occurrence_poisons_duplicate_set(self) -> None:
        segment = self._prepared_segments(1)[0]
        raw = "\n".join(
            [
                f"{segment.id}\t같은 번역",
                f"{segment.id}\t같은 번역",
                f"{segment.id}\t다른 번역",
            ]
        )
        parsed = self.parser.parse(raw)
        evaluation = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_response(parsed, [segment])

        self.assertFalse(parsed.identical_duplicates)
        self.assertEqual(parsed.conflicting_duplicates, [segment.id])
        self.assertIsNone(evaluation.by_segment_id[segment.id].translation)

    def test_r30_valid_native_sibling_survives_failed_sibling(self) -> None:
        first, second = self._prepared_segments(2)
        checkpointed: list[str] = []

        def first_success(prompt: str) -> str:
            self.assertEqual(native_prompt_source(prompt), first.prepared_source)
            return "정상 형제 번역"

        def second_failure(prompt: str) -> str:
            self.assertEqual(native_prompt_source(prompt), second.prepared_source)
            self.assertEqual(first.status, SegmentStatus.VALID)
            return "오류的"

        def second_repair(prompt: str) -> str:
            self.assertEqual(native_prompt_source(prompt), second.prepared_source)
            self.assertEqual(first.status, SegmentStatus.VALID)
            self.assertIn(first.id, checkpointed)
            self.assertNotIn(first.id, prompt)
            return "복구된 번역"

        translator = ScriptedTranslator(
            [first_success, second_failure, second_repair]
        )
        recovery = RecoveryEngine(
            translator,
            SingleTranslationParser(),
            ValidationCoordinator(self.config.validators, self.engine, self.profile),
            self.engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
            on_valid=lambda segment: checkpointed.append(segment.id),
        )
        result = recovery.translate_batch([first, second])

        self.assertFalse(result.failed)
        self.assertEqual(checkpointed, [first.id, second.id])
        self.assertEqual(first.attempt_count, 1)
        self.assertEqual(second.attempt_count, 2)

    def test_r31_repair_hint_without_broken_translation(self) -> None:
        segment = self._prepared_segments(1)[0]
        segment.context_before = ["앞 문맥"]
        segment.context_after = ["뒤 문맥"]
        broken = "깨진 이전 번역的"

        def repair(prompt: str) -> str:
            self.assertIn(segment.source, prompt)
            self.assertIn("앞 문맥", prompt)
            self.assertIn("뒤 문맥", prompt)
            self.assertIn("KNOWN_BAD_CJK_RESIDUE", prompt)
            self.assertNotIn(broken, prompt)
            self.assertNotIn(segment.id, prompt)
            return "복구 번역"

        translator = ScriptedTranslator([broken, repair])
        recovery = RecoveryEngine(
            translator,
            SingleTranslationParser(),
            ValidationCoordinator(self.config.validators, self.engine, self.profile),
            self.engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
        )
        result = recovery.translate_segment(segment)

        self.assertFalse(result.failed)
        self.assertEqual(segment.translation, "복구 번역")

    def test_r32_native_parser_remains_parse_once(self) -> None:
        segment = self._prepared_segments(1)[0]
        initial = "오류的"
        repaired = "복구 번역"
        translator = ScriptedTranslator([initial, repaired])
        parser = CountingSingleParser()
        recovery = RecoveryEngine(
            translator,
            parser,
            ValidationCoordinator(self.config.validators, self.engine, self.profile),
            self.engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
        )
        result = recovery.translate_segment(segment)

        self.assertFalse(result.failed)
        self.assertEqual(parser.calls, len(translator.calls))
        self.assertEqual(Counter(parser.raw_responses), Counter([initial, repaired]))
        self.assertTrue(segment.was_repaired)

    def test_r33_native_single_prompt_contains_no_segment_id(self) -> None:
        segment = self._prepared_segments(1)[0]
        prompt = build_single_translation_prompt(segment, self.profile)
        self.assertNotIn(segment.id, prompt)
        self.assertNotIn("SEG_", prompt)
        self.assertNotIn("ADULT_", prompt)

    def test_r34_single_raw_translation_maps_to_known_segment(self) -> None:
        segment = self._prepared_segments(1)[0]
        parsed = SingleTranslationParser().parse("정상 번역")
        evaluation = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(parsed, segment)
        self.assertEqual(
            evaluation.by_segment_id[segment.id].translation, "정상 번역"
        )

    def test_r35_native_single_success_becomes_valid_in_one_request(self) -> None:
        segment = self._prepared_segments(1)[0]
        checkpointed: list[str] = []
        translator = ScriptedTranslator(["정상 번역"])
        recovery = RecoveryEngine(
            translator,
            SingleTranslationParser(),
            ValidationCoordinator(self.config.validators, self.engine, self.profile),
            self.engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
            on_valid=lambda item: checkpointed.append(item.id),
        )
        result = recovery.translate_segment(segment)
        self.assertFalse(result.failed)
        self.assertEqual(segment.status, SegmentStatus.VALID)
        self.assertEqual(segment.attempt_count, 1)
        self.assertEqual(checkpointed, [segment.id])
        self.assertEqual(len(translator.calls), 1)

    def test_r36_failed_segment_repair_never_retranslates_valid_sibling(self) -> None:
        first, second = self._prepared_segments(2)
        translator = ScriptedTranslator(["첫 정상 번역", "오류的", "둘째 복구 번역"])
        recovery = RecoveryEngine(
            translator,
            SingleTranslationParser(),
            ValidationCoordinator(self.config.validators, self.engine, self.profile),
            self.engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
        )
        result = recovery.translate_batch([first, second])
        self.assertFalse(result.failed)
        self.assertEqual(first.attempt_count, 1)
        self.assertEqual(second.attempt_count, 2)
        self.assertEqual(
            [native_prompt_source(prompt) for prompt in translator.calls],
            [first.prepared_source, second.prepared_source, second.prepared_source],
        )

    def test_r37_repair_prompt_has_no_id_or_broken_candidate(self) -> None:
        segment = self._prepared_segments(1)[0]
        segment.context_before = ["참고 문맥"]
        broken = "망가진 후보的"

        def inspect_repair(prompt: str) -> str:
            self.assertNotIn(segment.id, prompt)
            self.assertNotIn(broken, prompt)
            self.assertIn(segment.source, prompt)
            self.assertIn("참고 문맥", prompt)
            self.assertIn("KNOWN_BAD_CJK_RESIDUE", prompt)
            return "정상 복구 번역"

        recovery = RecoveryEngine(
            ScriptedTranslator([broken, inspect_repair]),
            SingleTranslationParser(),
            ValidationCoordinator(self.config.validators, self.engine, self.profile),
            self.engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
        )
        self.assertFalse(recovery.translate_segment(segment).failed)

    def test_r38_native_transport_failure_remains_job_level(self) -> None:
        segment = self._prepared_segments(1)[0]

        def fail_transport(_prompt: str) -> str:
            raise TranslationTransportError("offline")

        recovery = RecoveryEngine(
            ScriptedTranslator([fail_transport]),
            SingleTranslationParser(),
            ValidationCoordinator(self.config.validators, self.engine, self.profile),
            self.engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
        )
        with self.assertRaises(TranslationTransportError):
            recovery.translate_segment(segment)
        self.assertEqual(segment.status, SegmentStatus.PREPARED)
        self.assertEqual(segment.attempt_count, 0)

    def test_r39_placeholders_survive_native_single_protocol(self) -> None:
        engine = PlaceholderEngine(
            [{"pattern": r"@@KEEP@@", "kind": "synthetic"}]
        )
        source = "Hello\n\\N[1] @@KEEP@@"
        segment = Segment("SEG_00000001", source, source_language="en")
        prepared = engine.protect(source)
        segment.prepare(prepared.text, prepared.tokens)
        candidate = "안녕 " + " ".join(
            token.placeholder for token in prepared.tokens
        )
        recovery = RecoveryEngine(
            ScriptedTranslator([candidate]),
            SingleTranslationParser(),
            ValidationCoordinator(self.config.validators, engine, self.profile),
            engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
        )
        result = recovery.translate_segment(segment)
        self.assertFalse(result.failed)
        self.assertIn("\n", segment.translation or "")
        self.assertIn("\\N[1]", segment.translation or "")
        self.assertIn("@@KEEP@@", segment.translation or "")

    def test_r40_novel_adult_intensity_instruction_survives(self) -> None:
        segment = self._prepared_segments(1)[0]
        prompt = build_single_translation_prompt(segment, load_profile("novel"))
        self.assertIn("임의로 순화하거나 강화하지 않으며", prompt)
        self.assertIn("비속함·완곡함·장난스러움·노골성", prompt)

    def test_r41_terminal_failure_retains_bounded_native_exchange(self) -> None:
        segment = self._prepared_segments(1)[0]
        config = replace(
            self.config,
            recovery=replace(self.config.recovery, max_attempts=2),
        )
        raw = "오류的" + ("x" * 6000)
        with tempfile.TemporaryDirectory() as directory:
            debug_path = Path(directory) / "failed.seori-debug.json"
            debug = FailureDebugStore(debug_path, mode="novel")
            debug.reset()
            recovery = RecoveryEngine(
                ScriptedTranslator([raw, raw]),
                SingleTranslationParser(),
                ValidationCoordinator(config.validators, self.engine, self.profile),
                self.engine,
                config.recovery,
                config.translation,
                self.profile,
                on_failed_attempt=debug.record,
                on_validated=debug.clear,
            )
            result = recovery.translate_segment(segment)

            self.assertEqual(result.failed, [segment])
            self.assertTrue(segment.last_raw_response_truncated)
            self.assertLessEqual(len(segment.last_raw_response or ""), 4000)
            self.assertNotIn(segment.id, segment.last_prompt or "")
            artifact = json.loads(debug_path.read_text(encoding="utf-8"))
            exchange = artifact["failed_exchanges"][0]
            self.assertEqual(exchange["segment_id"], segment.id)
            self.assertEqual(exchange["status"], "FAILED")
            self.assertIn("KNOWN_BAD_CJK_RESIDUE", exchange["error_codes"])

        report = build_qa_report(
            PipelineResult([segment], resumed=0, already_korean=0),
            source_path="input.txt",
            source_sha256="sha",
            output_path="output.txt",
            model="test",
            backup_path="backup.txt",
            mode="novel",
            profile="novel",
        )
        failure = report["terminal_failures"][0]
        self.assertIn("<<<SOURCE>>>", failure["last_prompt"])
        self.assertIn("[중간 생략]", failure["last_raw_response"])

    def test_r42_resume_never_calls_native_translator_for_valid_segment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint_path = Path(directory) / "resume.sqlite"
            initial = Segment("SEG_00000001", "こんにちは")
            with CheckpointStore(checkpoint_path) as checkpoint:
                TranslationPipeline(
                    self.config,
                    self.profile,
                    ScriptedTranslator(["안녕하세요"]),
                    checkpoint,
                ).process([initial])

            resumed_segment = Segment("SEG_00000001", "こんにちは")
            no_call = NoCallTranslator()
            with CheckpointStore(checkpoint_path) as checkpoint:
                result = TranslationPipeline(
                    self.config, self.profile, no_call, checkpoint
                ).process([resumed_segment])
            self.assertEqual(result.resumed, 1)
            self.assertEqual(no_call.calls, 0)

    def test_r43_full_span_outer_quote_is_restored_without_retry(self) -> None:
        segment = self._prepared_zh("「她轻声回答了。」")
        translator = ScriptedTranslator(["그녀는 조용히 대답했다."])
        recovery = RecoveryEngine(
            translator,
            SingleTranslationParser(),
            ValidationCoordinator(self.config.validators, self.engine, self.profile),
            self.engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
        )

        result = recovery.translate_segment(segment)

        self.assertFalse(result.failed)
        self.assertEqual(segment.translation, "「그녀는 조용히 대답했다.」")
        self.assertEqual(segment.attempt_count, 1)
        self.assertEqual(len(translator.calls), 1)
        self.assertIn(
            "OUTER_QUOTE_RESTORED",
            [issue.code for issue in segment.validation_issues],
        )

    def test_r44_inner_quote_loss_is_not_auto_restored(self) -> None:
        segment = self._prepared_zh("她说「你好」，然后离开了。")
        parsed = SingleTranslationParser().parse("그녀는 인사한 뒤 떠났다.")
        evaluation = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(parsed, segment)
        item = evaluation.by_segment_id[segment.id]

        self.assertIsNone(item.translation)
        self.assertIn(
            "QUOTE_STRUCTURE_LOSS", [issue.code for issue in item.result.issues]
        )
        self.assertNotIn(
            "OUTER_QUOTE_RESTORED", [issue.code for issue in item.result.issues]
        )

    def test_r45_parenthetical_content_loss_repairs_with_explicit_hint(self) -> None:
        segment = self._prepared_zh("她说（必须保留这句话），然后离开了。")

        def repair(prompt: str) -> str:
            self.assertIn("BRACKET_STRUCTURE_LOSS", prompt)
            self.assertIn("괄호 안 내용", prompt)
            return "그녀는 (이 문장을 반드시 남겨야 한다고) 말한 뒤 떠났다."

        translator = ScriptedTranslator(["그녀는 말한 뒤 떠났다.", repair])
        recovery = RecoveryEngine(
            translator,
            SingleTranslationParser(),
            ValidationCoordinator(self.config.validators, self.engine, self.profile),
            self.engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
        )

        result = recovery.translate_segment(segment)

        self.assertFalse(result.failed)
        self.assertEqual(segment.attempt_count, 2)
        self.assertTrue(segment.was_repaired)

        empty_aside_segment = self._prepared_zh(
            "她说（必须保留这句话），然后离开了。", "SEG_00000045"
        )
        empty_aside = SingleTranslationParser().parse(
            "그녀는 () 말한 뒤 떠났다."
        )
        empty_item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(empty_aside, empty_aside_segment).by_segment_id[
            empty_aside_segment.id
        ]
        self.assertIn(
            "PARENTHETICAL_CONTENT_LOSS",
            [issue.code for issue in empty_item.result.issues],
        )

    def test_r46_novel_residual_cjk_mixed_string_is_error(self) -> None:
        segment = self._prepared_zh("她走进主卧。")
        parsed = SingleTranslationParser().parse("그녀는 주卧에 들어갔다.")
        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(parsed, segment).by_segment_id[segment.id]

        self.assertIsNone(item.translation)
        self.assertIn(
            "NOVEL_CJK_RESIDUE", [issue.code for issue in item.result.issues]
        )

    def test_r47_source_backed_cjk_whitelist_is_configurable(self) -> None:
        profile = json.loads(json.dumps(self.profile))
        profile["cjk_residue_whitelist"] = ["龍門"]
        segment = self._prepared_zh("她抵达了龍門。")
        parsed = SingleTranslationParser().parse("그녀는 龍門에 도착했다.")
        item = ValidationCoordinator(
            self.config.validators, self.engine, profile
        ).evaluate_single(parsed, segment).by_segment_id[segment.id]

        self.assertEqual(item.translation, "그녀는 龍門에 도착했다.")
        self.assertFalse(item.result.has_errors)

        unrelated_source = self._prepared_zh(
            "她抵达了城门。", "SEG_00000047"
        )
        unrelated_item = ValidationCoordinator(
            self.config.validators, self.engine, profile
        ).evaluate_single(
            SingleTranslationParser().parse("그녀는 龍門에 도착했다."),
            unrelated_source,
        ).by_segment_id[unrelated_source.id]
        self.assertIn(
            "NOVEL_CJK_RESIDUE",
            [issue.code for issue in unrelated_item.result.issues],
        )

        protected_engine = PlaceholderEngine(
            [{"pattern": "龍門", "kind": "proper_name"}]
        )
        protected_segment = Segment(
            "SEG_00000048", "她抵达了龍門。", source_language="zh"
        )
        protected = protected_engine.protect(protected_segment.source)
        protected_segment.prepare(protected.text, protected.tokens)
        protected_item = ValidationCoordinator(
            self.config.validators, protected_engine, self.profile
        ).evaluate_single(
            SingleTranslationParser().parse(
                f"그녀는 {protected.tokens[0].placeholder}에 도착했다."
            ),
            protected_segment,
        ).by_segment_id[protected_segment.id]
        self.assertEqual(
            protected_item.translation, "그녀는 龍門에 도착했다."
        )
        self.assertFalse(protected_item.result.has_errors)

    def test_r48_obvious_korean_mid_clause_truncation_is_error(self) -> None:
        cases = [
            (
                "持续不断的震动声音清晰地在她的耳边回响。",
                "계속되는 진동 소리가 그녀의 귀에 울",
            ),
            (
                "持续不断的震动声音清晰地在她的耳边回响。💕",
                "계속되는 진동 소리가 그녀의 귀에 울💕",
            ),
            (
                "持续不断的震动声音清晰地在她的耳边回响。❤️",
                "계속되는 진동 소리가 그녀의 귀에 울❤️",
            ),
            (
                "「持续不断的震动声音清晰地在她的耳边回响。💪🏻」",
                "「계속되는 진동 소리가 그녀의 귀에 울💪🏻」",
            ),
        ]
        for index, (source, translation) in enumerate(cases, start=1):
            with self.subTest(translation=translation):
                segment = self._prepared_zh(
                    source, f"SEG_00000048_{index}"
                )
                parsed = SingleTranslationParser().parse(translation)
                item = ValidationCoordinator(
                    self.config.validators, self.engine, self.profile
                ).evaluate_single(parsed, segment).by_segment_id[segment.id]

                self.assertIsNone(item.translation)
                self.assertIn(
                    "TRUNCATED_OUTPUT",
                    [issue.code for issue in item.result.issues],
                )

        complete_segment = self._prepared_zh(
            "持续不断的震动声音清晰地在她的耳边回响。💕",
            "SEG_00000048_COMPLETE",
        )
        complete_translation = "계속되는 진동 소리가 그녀의 귀에 울렸다💕"
        complete_item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(
            SingleTranslationParser().parse(complete_translation),
            complete_segment,
        ).by_segment_id[complete_segment.id]
        self.assertEqual(complete_item.translation, complete_translation)
        self.assertNotIn(
            "TRUNCATED_OUTPUT",
            [issue.code for issue in complete_item.result.issues],
        )

    def test_r49_legitimate_short_fragment_is_not_truncated(self) -> None:
        segment = self._prepared_zh("短句。")
        parsed = SingleTranslationParser().parse("짧은 문장")
        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(parsed, segment).by_segment_id[segment.id]

        self.assertEqual(item.translation, "짧은 문장")
        self.assertNotIn(
            "TRUNCATED_OUTPUT", [issue.code for issue in item.result.issues]
        )

    def test_r50_terminology_guards_do_not_expand_normal_prompt(self) -> None:
        segment = self._prepared_zh("她的阴蒂贴着内裤。")
        prompt = build_single_translation_prompt(segment, self.profile)

        self.assertNotIn("용어 의미 참고", prompt)
        self.assertNotIn("클리토리스", prompt)
        self.assertNotIn("속옷", prompt)
        self.assertNotIn("금지 의미", prompt)

    def test_r51_term_hints_have_no_ids_or_unrelated_dictionary(self) -> None:
        segment = self._prepared_zh("她走进房间。", "ADULT_00000051")
        prompt = build_single_translation_prompt(segment, self.profile)

        self.assertNotIn(segment.id, prompt)
        self.assertNotIn("SEG_", prompt)
        self.assertNotIn("ADULT_", prompt)
        self.assertNotIn("阴蒂", prompt)
        self.assertNotIn("爱液", prompt)

    def test_r52_clitoris_cannot_validate_as_male_organ(self) -> None:
        segment = self._prepared_zh("阴蒂")
        parsed = SingleTranslationParser().parse("음경")
        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(parsed, segment).by_segment_id[segment.id]

        self.assertIsNone(item.translation)
        self.assertIn(
            "NOVEL_TERM_MISTRANSLATION",
            [issue.code for issue in item.result.issues],
        )

    def test_r53_arousal_fluid_cannot_validate_as_semen_without_source_basis(self) -> None:
        segment = self._prepared_zh("爱液")
        parsed = SingleTranslationParser().parse("정액")
        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(parsed, segment).by_segment_id[segment.id]
        self.assertIsNone(item.translation)
        self.assertIn(
            "NOVEL_TERM_MISTRANSLATION",
            [issue.code for issue in item.result.issues],
        )

        justified = self._prepared_zh("爱液和精液混在一起。", "SEG_00000002")
        justified_parsed = SingleTranslationParser().parse(
            "애액과 정액이 한데 섞였다."
        )
        justified_item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(justified_parsed, justified).by_segment_id[justified.id]
        self.assertEqual(justified_item.translation, "애액과 정액이 한데 섞였다.")

        omitted = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(
            SingleTranslationParser().parse("정액이 한데 섞였다."), justified
        ).by_segment_id[justified.id]
        self.assertIn(
            "NOVEL_TERM_MISTRANSLATION",
            [issue.code for issue in omitted.result.issues],
        )

    def test_r54_underwear_cannot_validate_as_long_underwear(self) -> None:
        segment = self._prepared_zh("她脱下内裤。")
        parsed = SingleTranslationParser().parse("그녀는 내복을 벗었다.")
        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(parsed, segment).by_segment_id[segment.id]

        self.assertIsNone(item.translation)
        self.assertIn(
            "NOVEL_TERM_MISTRANSLATION",
            [issue.code for issue in item.result.issues],
        )

    def test_r55_risk_only_segment_is_valid_and_never_retried(self) -> None:
        segment = self._prepared_zh("她买了 1 个玩具。")
        translator = ScriptedTranslator(["그녀는 장난감 1 개를 샀다."])
        checkpointed: list[str] = []
        recovery = RecoveryEngine(
            translator,
            SingleTranslationParser(),
            ValidationCoordinator(self.config.validators, self.engine, self.profile),
            self.engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
            on_valid=lambda item: checkpointed.append(item.id),
        )

        result = recovery.translate_segment(segment)

        self.assertFalse(result.failed)
        self.assertEqual(segment.status, SegmentStatus.VALID)
        self.assertEqual(segment.attempt_count, 1)
        self.assertEqual(len(translator.calls), 1)
        self.assertEqual(checkpointed, [segment.id])
        self.assertIn(
            "NUMBER_PRESENT_RISK",
            [issue.code for issue in segment.validation_issues],
        )

        precise_negation = validate_risks(
            "她不想离开。",
            "그녀는 안 떠나고 싶었다.",
            self.config.validators,
            self.profile["validation"],
        )
        self.assertNotIn(
            "NEGATION_FLIP_RISK",
            [issue.code for issue in precise_negation.issues],
        )

    def test_r56_colloquial_source_gets_only_global_register_policy(self) -> None:
        segment = self._prepared_zh("她挺起丰满的奶子，故意贴近他。")
        prompt = build_single_translation_prompt(segment, self.profile)

        self.assertIn("자연스러운 한국 장르소설 문체", prompt)
        self.assertNotIn("장르 문체 참고", prompt)
        self.assertNotIn("가슴", prompt)
        self.assertNotIn("natural_korean_genre_fiction", prompt)

    def test_r57_explicit_medical_context_allows_anatomical_korean(self) -> None:
        segment = self._prepared_zh("医生向患者解释阴户的解剖结构。")
        translation = "의사는 환자에게 여성 외부 생식기의 해부학적 구조를 설명했다."
        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(
            SingleTranslationParser().parse(translation), segment
        ).by_segment_id[segment.id]

        self.assertEqual(item.translation, translation)
        self.assertNotIn(
            "NOVEL_REGISTER_MISMATCH",
            [issue.code for issue in item.result.issues],
        )

    def test_r58_register_and_name_prompt_hints_are_retired(self) -> None:
        thigh = self._prepared_zh("她揉着自己的大腿。")
        thigh_prompt = build_single_translation_prompt(thigh, self.profile)
        self.assertIn("大腿", thigh_prompt)
        self.assertNotIn("허벅지", thigh_prompt)
        self.assertNotIn("장르 문체 참고", thigh_prompt)

        named = self._prepared_zh("鱼鱼笑了。", "SEG_00000058")
        named_prompt = build_single_translation_prompt(named, self.profile)
        self.assertNotIn("이름 참고", named_prompt)
        self.assertNotIn("위위", named_prompt)

    def test_r59_unrelated_segment_gets_no_global_register_glossary(self) -> None:
        segment = self._prepared_zh("她走进房间。")
        prompt = build_single_translation_prompt(segment, self.profile)

        self.assertNotIn("장르 문체 참고", prompt)
        self.assertNotIn("용어 의미 참고", prompt)
        self.assertNotIn("奶子", prompt)
        self.assertNotIn("花壶", prompt)
        self.assertNotIn("本小姐", prompt)

    def test_r60_style_diagnostic_never_rewrites_output(self) -> None:
        segment = self._prepared_zh("她揉着丰满的奶子。")
        translation = "그녀는 풍만한 유방을 어루만졌다."
        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(
            SingleTranslationParser().parse(translation), segment
        ).by_segment_id[segment.id]

        self.assertEqual(item.translation, translation)
        issue = next(
            issue
            for issue in item.result.issues
            if issue.code == "NOVEL_REGISTER_MISMATCH"
        )
        self.assertEqual(issue.severity, ValidationSeverity.RISK)

    def test_r61_main_bedroom_cannot_become_kitchen(self) -> None:
        segment = self._prepared_zh("她回到主卧休息。")
        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(
            SingleTranslationParser().parse("그녀는 주방으로 돌아가 쉬었다."),
            segment,
        ).by_segment_id[segment.id]

        self.assertIsNone(item.translation)
        self.assertIn(
            "NOVEL_TERM_MISTRANSLATION",
            [issue.code for issue in item.result.issues],
        )

        both = self._prepared_zh(
            "她从主卧走进厨房。", "SEG_00000061_BOTH"
        )
        both_translation = "그녀는 안방에서 주방으로 들어갔다."
        both_item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(
            SingleTranslationParser().parse(both_translation), both
        ).by_segment_id[both.id]
        self.assertEqual(both_item.translation, both_translation)

    def test_r62_clitoris_still_cannot_map_to_male_anatomy(self) -> None:
        segment = self._prepared_zh("她的阴蒂微微颤抖。")
        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(
            SingleTranslationParser().parse("그녀의 음경이 미세하게 떨렸다."),
            segment,
        ).by_segment_id[segment.id]

        self.assertIsNone(item.translation)
        self.assertIn(
            "NOVEL_TERM_MISTRANSLATION",
            [issue.code for issue in item.result.issues],
        )

    def test_r63_arousal_fluid_still_cannot_become_semen(self) -> None:
        segment = self._prepared_zh("爱液顺着大腿流下。")
        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(
            SingleTranslationParser().parse("정액이 허벅지를 따라 흘러내렸다."),
            segment,
        ).by_segment_id[segment.id]

        self.assertIsNone(item.translation)
        self.assertIn(
            "NOVEL_TERM_MISTRANSLATION",
            [issue.code for issue in item.result.issues],
        )

    def test_r64_context_dependent_euphemism_is_not_hard_replaced(self) -> None:
        segment = self._prepared_zh("她轻轻抚摸自己的花壶。")
        translation = "그녀는 자신의 화분을 가볍게 쓰다듬었다."
        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(
            SingleTranslationParser().parse(translation),
            segment,
        ).by_segment_id[segment.id]

        self.assertEqual(item.translation, translation)
        self.assertNotIn(
            "NOVEL_TERM_MISTRANSLATION",
            [issue.code for issue in item.result.issues],
        )

    def test_r65_style_only_risk_is_valid_without_retry(self) -> None:
        segment = self._prepared_zh("她轻轻抚摸自己的大腿。")
        translator = ScriptedTranslator(["그녀는 자신의 대퇴부를 가볍게 쓰다듬었다."])
        checkpointed: list[str] = []
        recovery = RecoveryEngine(
            translator,
            SingleTranslationParser(),
            ValidationCoordinator(self.config.validators, self.engine, self.profile),
            self.engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
            on_valid=lambda item: checkpointed.append(item.id),
        )

        result = recovery.translate_segment(segment)

        self.assertFalse(result.failed)
        self.assertEqual(segment.status, SegmentStatus.VALID)
        self.assertEqual(segment.attempt_count, 1)
        self.assertEqual(len(translator.calls), 1)
        self.assertEqual(checkpointed, [segment.id])
        self.assertIn(
            "NOVEL_REGISTER_MISMATCH",
            [issue.code for issue in segment.validation_issues],
        )

    def test_r66_semantic_error_repairs_only_current_segment(self) -> None:
        first = self._prepared_zh("她推开房门。", "SEG_00000066_GOOD")
        second = self._prepared_zh("她回到主卧。", "SEG_00000066_BAD")

        def repair(prompt: str) -> str:
            self.assertIn("NOVEL_TERM_MISTRANSLATION", prompt)
            self.assertIn("보존할 의미 범주: main_bedroom", prompt)
            self.assertNotIn("금지 의미/표현", prompt)
            self.assertNotIn("그녀는 주방으로 돌아갔다.", prompt)
            self.assertNotIn(first.id, prompt)
            self.assertNotIn(second.id, prompt)
            return "그녀는 안방으로 돌아갔다."

        translator = ScriptedTranslator(
            [
                "그녀는 방문을 열었다.",
                "그녀는 주방으로 돌아갔다.",
                repair,
            ]
        )
        recovery = RecoveryEngine(
            translator,
            SingleTranslationParser(),
            ValidationCoordinator(self.config.validators, self.engine, self.profile),
            self.engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
        )

        result = recovery.translate_batch([first, second])

        self.assertFalse(result.failed)
        self.assertEqual(first.translation, "그녀는 방문을 열었다.")
        self.assertEqual(first.attempt_count, 1)
        self.assertEqual(second.translation, "그녀는 안방으로 돌아갔다.")
        self.assertEqual(second.attempt_count, 2)
        self.assertTrue(second.was_repaired)
        self.assertEqual(len(translator.calls), 3)

    def test_r67_default_single_prompt_is_minimal(self) -> None:
        segment = self._prepared_zh("鱼鱼揉着奶子回到主卧。")
        prompt = build_single_translation_prompt(segment, self.profile)

        self.assertNotIn("용어 의미 참고", prompt)
        self.assertNotIn("장르 문체 참고", prompt)
        self.assertNotIn("이름 참고", prompt)
        self.assertNotIn("클리토리스", prompt)
        self.assertNotIn("허벅지", prompt)
        self.assertNotIn("안방", prompt)
        self.assertLess(len(prompt.splitlines()), 25)

    def test_r68_no_work_specific_default_names(self) -> None:
        profile_path = ROOT / "profiles" / "novel.json"
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
        self.assertFalse(raw.get("name_map", {}))
        serialized = json.dumps(raw, ensure_ascii=False)
        self.assertNotIn("鱼鱼", serialized)
        self.assertNotIn("魚魚", serialized)

        with tempfile.TemporaryDirectory() as directory:
            custom_path = Path(directory) / "custom.json"
            raw["name_map"] = {"鱼鱼": "위위"}
            custom_path.write_text(
                json.dumps(raw, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaises(ConfigError):
                load_profile(custom_path)

    def test_r69_no_unrelated_dictionary_dump(self) -> None:
        segment = self._prepared_zh("她走进陌生的房间。")
        prompt = build_single_translation_prompt(segment, self.profile)

        for unrelated in ("阴蒂", "爱液", "内裤", "主卧", "花壶"):
            self.assertNotIn(unrelated, prompt)
        self.assertNotIn("자연스러운 후보", prompt)
        self.assertNotIn("금지 의미", prompt)

    def test_r70_job_local_mapped_name_round_trip(self) -> None:
        mappings = JobMappings.from_data(
            {"version": 1, "names": {"鱼鱼": "위위"}, "mappings": {}}
        )
        engine = PlaceholderEngine(job_mappings=mappings)
        segment = Segment("SEG_00000070", "鱼鱼笑了。", source_language="zh")
        prepared = engine.protect(segment.source)
        segment.prepare(prepared.text, prepared.tokens)

        self.assertEqual(prepared.text, "[[NAME_0001]]笑了。")
        self.assertEqual(prepared.tokens[0].mapped_target, "위위")

        def translated(prompt: str) -> str:
            self.assertEqual(native_prompt_source(prompt), prepared.text)
            self.assertNotIn("鱼鱼", prompt)
            self.assertNotIn("위위", prompt)
            return "[[NAME_0001]]가 웃었다."

        result = RecoveryEngine(
            ScriptedTranslator([translated]),
            SingleTranslationParser(),
            ValidationCoordinator(self.config.validators, engine, self.profile),
            engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
        ).translate_segment(segment)

        self.assertFalse(result.failed)
        self.assertEqual(segment.translation, "위위가 웃었다.")

    def test_r71_mapped_placeholder_count_and_order_safety(self) -> None:
        mappings = JobMappings.from_data(
            {
                "names": {"鱼鱼": "위위"},
                "mappings": {"圣剑": "성검"},
            }
        )
        engine = PlaceholderEngine(job_mappings=mappings)
        prepared = engine.protect("鱼鱼拿着圣剑。")
        name, item = [token.placeholder for token in prepared.tokens]

        missing = validate_placeholders(name, prepared.tokens)
        self.assertIn("MISSING_PLACEHOLDER", [issue.code for issue in missing.issues])

        duplicate = validate_placeholders(
            f"{name}{name}{item}", prepared.tokens
        )
        self.assertIn(
            "DUPLICATE_PLACEHOLDER",
            [issue.code for issue in duplicate.issues],
        )

        unexpected = validate_placeholders(
            f"{name}{item}[[MAP_9999]]", prepared.tokens
        )
        self.assertIn(
            "UNEXPECTED_PLACEHOLDER",
            [issue.code for issue in unexpected.issues],
        )

        reversed_result = validate_placeholders(
            f"{item}{name}", prepared.tokens
        )
        self.assertIn(
            "PLACEHOLDER_ORDER_MISMATCH",
            [issue.code for issue in reversed_result.issues],
        )
        with self.assertRaises(PlaceholderError):
            engine.restore(f"{item}{name}", prepared.tokens)

    def test_r72_same_source_name_may_differ_between_jobs(self) -> None:
        first_engine = PlaceholderEngine(
            job_mappings=JobMappings.from_data(
                {"names": {"鱼鱼": "위위"}, "mappings": {}}
            )
        )
        second_engine = PlaceholderEngine(
            job_mappings=JobMappings.from_data(
                {"names": {"鱼鱼": "유유"}, "mappings": {}}
            )
        )
        first = first_engine.protect("鱼鱼笑了。")
        second = second_engine.protect("鱼鱼笑了。")

        self.assertEqual(first.text, second.text)
        candidate = "[[NAME_0001]]가 웃었다."
        self.assertEqual(
            first_engine.restore(candidate, first.tokens), "위위가 웃었다."
        )
        self.assertEqual(
            second_engine.restore(candidate, second.tokens), "유유가 웃었다."
        )

    def test_r73_absent_job_map_means_no_forced_canonicalization(self) -> None:
        engine = PlaceholderEngine()
        prepared = engine.protect("鱼鱼笑了。")
        self.assertEqual(prepared.text, "鱼鱼笑了。")
        self.assertEqual(prepared.tokens, [])

        segment = self._prepared_zh("鱼鱼笑了。", "SEG_00000073")
        prompt = build_single_translation_prompt(segment, self.profile)
        self.assertIn("鱼鱼笑了。", prompt)
        self.assertNotIn("위위", prompt)

    def test_r74_class_b_rule_is_validator_side_not_prompt_glossary(self) -> None:
        segment = self._prepared_zh("她回到主卧休息。", "SEG_00000074")
        prompt = build_single_translation_prompt(segment, self.profile)
        self.assertNotIn("안방", prompt)
        self.assertNotIn("주방", prompt)
        self.assertNotIn("용어 의미 참고", prompt)

        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(
            SingleTranslationParser().parse("그녀는 주방으로 돌아가 쉬었다."),
            segment,
        ).by_segment_id[segment.id]
        self.assertIsNone(item.translation)
        self.assertIn(
            "NOVEL_TERM_MISTRANSLATION",
            [issue.code for issue in item.result.issues],
        )

    def test_r75_class_c_slang_is_not_deterministic_replacement(self) -> None:
        source = "她轻轻抚摸自己的花壶。"
        engine = PlaceholderEngine()
        prepared = engine.protect(source)
        self.assertEqual(prepared.text, source)
        self.assertFalse(
            any(token.kind.startswith("mapped_") for token in prepared.tokens)
        )

        segment = self._prepared_zh(source, "SEG_00000075")
        translation = "그녀는 자신의 화분을 가볍게 쓰다듬었다."
        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(
            SingleTranslationParser().parse(translation), segment
        ).by_segment_id[segment.id]
        self.assertEqual(item.translation, translation)

    def test_r76_adult_novel_register_is_one_short_policy(self) -> None:
        segment = self._prepared_zh("她靠近了他。", "SEG_00000076")
        prompt = build_single_translation_prompt(segment, self.profile)
        policy_lines = [
            line
            for line in prompt.splitlines()
            if "비속함·완곡함·장난스러움·노골성" in line
        ]
        self.assertEqual(len(policy_lines), 1)
        self.assertIn("임상·해부학", policy_lines[0])
        self.assertNotIn("장르 문체 참고", prompt)
        self.assertNotIn("자연스러운 후보", prompt)

    def test_r77_medical_context_may_remain_clinical(self) -> None:
        segment = self._prepared_zh(
            "医生向患者解释奶子的解剖结构。", "SEG_00000077"
        )
        translation = "의사는 환자에게 유방의 해부학적 구조를 설명했다."
        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(
            SingleTranslationParser().parse(translation), segment
        ).by_segment_id[segment.id]
        self.assertEqual(item.translation, translation)
        self.assertNotIn(
            "NOVEL_REGISTER_MISMATCH",
            [issue.code for issue in item.result.issues],
        )

    def test_r78_repair_prompt_remains_bounded(self) -> None:
        segment = self._prepared_zh("她回到主卧。", "SEG_00000078")
        bad_candidate = "그녀는 주방으로 돌아갔다."
        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(
            SingleTranslationParser().parse(bad_candidate), segment
        ).by_segment_id[segment.id]
        segment.validation_issues = list(item.result.issues)

        prompt = build_single_translation_prompt(
            segment, self.profile, mode="repair"
        )
        self.assertIn("NOVEL_TERM_MISTRANSLATION", prompt)
        self.assertIn("보존할 의미 범주: main_bedroom", prompt)
        self.assertNotIn(bad_candidate, prompt)
        self.assertNotIn("용어 의미 참고", prompt)
        self.assertNotIn("장르 문체 참고", prompt)
        self.assertNotIn("자연스러운 후보", prompt)
        self.assertNotIn("금지 의미", prompt)
        self.assertNotIn(segment.id, prompt)
        self.assertLess(len(prompt), 2500)

    def test_r79_terminal_failure_still_falls_back_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            input_path = temp / "input.txt"
            output_path = temp / "input.ko.txt"
            input_path.write_text("她回到主卧。", encoding="utf-8")
            adapter = TextAdapter()
            document, segments = adapter.load(input_path)
            translator = ScriptedTranslator(
                ["그녀는 주방으로 돌아갔다."] * self.config.recovery.max_attempts
            )
            result = TranslationPipeline(
                self.config, self.profile, translator
            ).process(segments, resume=False)

            self.assertEqual(segments[0].status, SegmentStatus.FAILED)
            adapter.save(document, result.segments, output_path)
            self.assertEqual(
                output_path.read_text(encoding="utf-8"), segments[0].source
            )

    def test_r80_valid_resume_with_mapping_causes_zero_model_calls(self) -> None:
        mappings = JobMappings.from_data(
            {"names": {"鱼鱼": "위위"}, "mappings": {}}
        )
        with tempfile.TemporaryDirectory() as directory:
            checkpoint_path = Path(directory) / "resume.sqlite"
            first = Segment("SEG_00000080", "鱼鱼笑了。")
            translator = ScriptedTranslator(["[[NAME_0001]]가 웃었다."])
            with CheckpointStore(checkpoint_path) as checkpoint:
                resolved = resolve_job_mappings(
                    checkpoint, mappings, resume=False
                )
                result = TranslationPipeline(
                    self.config,
                    self.profile,
                    translator,
                    checkpoint,
                    job_mappings=resolved,
                ).process([first], resume=False)
            self.assertFalse(result.failed)
            self.assertEqual(first.translation, "위위가 웃었다.")

            different = JobMappings.from_data(
                {"names": {"鱼鱼": "유유"}, "mappings": {}}
            )
            with CheckpointStore(checkpoint_path) as checkpoint:
                with self.assertRaises(JobMappingError):
                    resolve_job_mappings(checkpoint, different, resume=True)
                resumed_mapping = resolve_job_mappings(
                    checkpoint, None, resume=True
                )
                same = Segment("SEG_00000080", "鱼鱼笑了。")
                no_call = NoCallTranslator()
                resumed = TranslationPipeline(
                    self.config,
                    self.profile,
                    no_call,
                    checkpoint,
                    job_mappings=resumed_mapping,
                ).process([same], resume=True)
            self.assertEqual(resumed.resumed, 1)
            self.assertEqual(no_call.calls, 0)
            self.assertEqual(same.translation, "위위가 웃었다.")

    def test_r81_game_mode_is_unaffected_by_novel_policy(self) -> None:
        mappings = JobMappings.from_data(
            {"names": {"勇者": "용사"}, "mappings": {}}
        )
        engine = PlaceholderEngine(job_mappings=mappings)
        segment = Segment("SEG_00000081", "勇者 attacks!", source_language="zh")
        prepared = engine.protect(segment.source)
        segment.prepare(prepared.text, prepared.tokens)
        prompt = build_single_translation_prompt(segment, load_profile("game"))

        self.assertIn("[[NAME_0001]]", prompt)
        self.assertNotIn("장르소설", prompt)
        self.assertNotIn("비속함·완곡함", prompt)
        self.assertNotIn("장르 문체 참고", prompt)
        self.assertEqual(
            engine.restore("[[NAME_0001]]가 공격한다!", prepared.tokens),
            "용사가 공격한다!",
        )

    def test_r82_document_mode_is_unaffected_by_novel_policy(self) -> None:
        profile = load_profile("document")
        segment = Segment(
            "SEG_00000082", "The report is complete.", source_language="en"
        )
        segment.prepare(segment.source, [])
        prompt = build_single_translation_prompt(segment, profile)

        self.assertIn("정보를 보존", prompt)
        self.assertNotIn("장르소설", prompt)
        self.assertNotIn("비속함·완곡함", prompt)
        self.assertNotIn("임상·해부학", prompt)
        self.assertNotIn("용어 의미 참고", prompt)


if __name__ == "__main__":
    unittest.main()
