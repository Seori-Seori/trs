from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

from core.checkpoint import CheckpointStore
from core.config import load_config, load_profile
from core.diagnostics import FailureDebugStore
from core.parser import ResponseParser, SingleTranslationParser
from core.pipeline import PipelineResult, TranslationPipeline
from core.placeholders import PlaceholderEngine
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
        self.assertIn("원문의 의미를 임의로 순화하거나 강화하지 않는다.", prompt)
        self.assertIn("성인 표현도 원문의 강도를 유지한다.", prompt)

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
        segment = self._prepared_zh(
            "持续不断的震动声音清晰地在她的耳边回响。"
        )
        parsed = SingleTranslationParser().parse(
            "계속되는 진동 소리가 그녀의 귀에 울"
        )
        item = ValidationCoordinator(
            self.config.validators, self.engine, self.profile
        ).evaluate_single(parsed, segment).by_segment_id[segment.id]

        self.assertIsNone(item.translation)
        self.assertIn(
            "TRUNCATED_OUTPUT", [issue.code for issue in item.result.issues]
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

    def test_r50_terminology_hints_are_source_triggered(self) -> None:
        segment = self._prepared_zh("她的阴蒂贴着内裤。")
        prompt = build_single_translation_prompt(segment, self.profile)

        self.assertIn("- 阴蒂:", prompt)
        self.assertIn("- 内裤:", prompt)
        self.assertNotIn("- 爱液:", prompt)
        self.assertNotIn("- 主卧:", prompt)

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


if __name__ == "__main__":
    unittest.main()
