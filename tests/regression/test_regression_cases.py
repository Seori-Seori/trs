from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.checkpoint import CheckpointStore
from core.config import load_config, load_profile
from core.parser import ResponseParser
from core.pipeline import TranslationPipeline
from core.placeholders import PlaceholderEngine
from core.prompting import build_prompt
from core.recovery import RecoveryEngine
from core.segment import Segment, ValidationSeverity
from core.validation import ValidationCoordinator
from tests.helpers import NoCallTranslator, ScriptedTranslator, prompt_targets
from validators.korean import validate_korean
from validators.placeholders import validate_placeholders
from validators.risk import validate_risks
from validators.structure import validate_structure, validate_text_structure


ROOT = Path(__file__).resolve().parents[2]


class CountingParser(ResponseParser):
    def __init__(self) -> None:
        self.calls = 0

    def parse(self, raw_response: str):
        self.calls += 1
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
        prompt = build_prompt([first], self.profile)
        self.assertIn("次の段落です", prompt)
        self.assertEqual([item[0] for item in prompt_targets(prompt)], [first.id])

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
            translator = ScriptedTranslator([f"{first.id}\t안녕하세요"])
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

    def test_r23_partial_repair_only_requests_two_failed_rows(self) -> None:
        segments = self._segments(10)
        for segment in segments:
            segment.source_language = "ja"
            prepared = self.engine.protect(segment.source)
            segment.prepare(prepared.text, prepared.tokens)

        bad_ids = {segments[2].id, segments[6].id}

        def initial(prompt: str) -> str:
            targets = prompt_targets(prompt)
            self.assertEqual({item[0] for item in targets}, {s.id for s in segments})
            return "\n".join(
                f"{segment_id}\t{'오류的' if segment_id in bad_ids else '정상 번역'}"
                for segment_id, _language, _source in targets
            )

        def repair(prompt: str) -> str:
            targets = prompt_targets(prompt)
            self.assertEqual({item[0] for item in targets}, bad_ids)
            return "\n".join(
                f"{segment_id}\t복구된 번역"
                for segment_id, _language, _source in targets
            )

        translator = ScriptedTranslator([initial, repair])
        parser = CountingParser()
        validator = ValidationCoordinator(self.config.validators, self.engine)
        recovery = RecoveryEngine(
            translator,
            parser,
            validator,
            self.engine,
            self.config.recovery,
            self.config.translation,
            self.profile,
        )
        result = recovery.translate_batch(segments)
        self.assertFalse(result.failed)
        self.assertEqual(len(translator.calls), 2)
        self.assertEqual(parser.calls, 2)
        self.assertTrue(all(s.attempt_count == 1 for s in segments if s.id not in bad_ids))
        self.assertTrue(all(s.attempt_count == 2 for s in segments if s.id in bad_ids))


if __name__ == "__main__":
    unittest.main()
