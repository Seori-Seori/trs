from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core.checkpoint import CheckpointStore
from core.config import load_config, load_profile
from core.parser import ResponseParser, SingleTranslationParser
from core.pipeline import PipelineResult
from core.placeholders import PlaceholderEngine
from core.prompting import build_single_translation_prompt
from core.recovery import RecoveryEngine
from core.reporting import build_qa_report
from core.segment import Segment, SegmentStatus, ValidationResult, ValidationSeverity
from core.validation import ValidationCoordinator
from main import run
from tests.helpers import ScriptedTranslator, native_prompt_source
from translators.base import Translator, TranslationTransportError
from translators.ollama import OllamaTranslator
from validators.risk import validate_risks
from validators.structure import validate_structure, validate_text_structure


ROOT = Path(__file__).resolve().parents[2]


class _UnavailableOllama:
    health_calls = 0
    translate_calls = 0

    def __init__(self, config: object) -> None:
        self.base_url = getattr(config, "base_url")
        self.model = getattr(config, "model")

    def health_check(self) -> bool:
        type(self).health_calls += 1
        raise TranslationTransportError("connection refused")

    def translate(self, _prompt: str) -> str:
        type(self).translate_calls += 1
        raise AssertionError("translation loop must not start")


class _MissingModelOllama(_UnavailableOllama):
    def health_check(self) -> bool:
        type(self).health_calls += 1
        return False


class _HealthyNoCallOllama(_UnavailableOllama):
    def health_check(self) -> bool:
        type(self).health_calls += 1
        return True


class _TransportFailTranslator(Translator):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def health_check(self) -> bool:
        return True

    def translate(self, prompt: str) -> str:
        self.calls.append(prompt)
        raise TranslationTransportError("offline")


class _EchoTranslator(Translator):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def health_check(self) -> bool:
        return True

    def translate(self, prompt: str) -> str:
        self.calls.append(prompt)
        if not native_prompt_source(prompt):
            raise AssertionError("Native prompt source is empty")
        return "정상 번역"


class Round2HardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(ROOT / "config.json")
        cls.novel = load_profile("novel")

    def _prepared_segments(
        self, count: int, engine: PlaceholderEngine | None = None
    ) -> tuple[PlaceholderEngine, list[Segment]]:
        placeholder_engine = engine or PlaceholderEngine()
        segments = [
            Segment(f"SEG_{index:08d}", f"原文{index}") for index in range(1, count + 1)
        ]
        for segment in segments:
            segment.source_language = "ja"
            prepared = placeholder_engine.protect(segment.source)
            segment.prepare(prepared.text, prepared.tokens)
        return placeholder_engine, segments

    def _recovery(
        self,
        translator: Translator,
        engine: PlaceholderEngine,
        *,
        config=None,
        profile=None,
    ) -> RecoveryEngine:
        selected_config = config or self.config
        selected_profile = profile or self.novel
        return RecoveryEngine(
            translator,
            SingleTranslationParser(),
            ValidationCoordinator(
                selected_config.validators, engine, selected_profile
            ),
            engine,
            selected_config.recovery,
            selected_config.translation,
            selected_profile,
        )

    def test_ollama_unavailable_fails_before_translation_loop(self) -> None:
        _UnavailableOllama.health_calls = 0
        _UnavailableOllama.translate_calls = 0
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.txt"
            input_path.write_text("こんにちは", encoding="utf-8")
            stderr = io.StringIO()
            with patch("main.OllamaTranslator", _UnavailableOllama):
                with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    exit_code = run([str(input_path), "--mode", "novel"])
            self.assertEqual(exit_code, 2)
            self.assertIn("Ollama에 연결할 수 없습니다", stderr.getvalue())
            self.assertEqual(_UnavailableOllama.health_calls, 1)
            self.assertEqual(_UnavailableOllama.translate_calls, 0)
            self.assertFalse((input_path.parent / "backup").exists())
            self.assertFalse((input_path.parent / "input.ko.txt").exists())

    def test_missing_ollama_model_fails_before_translation_loop(self) -> None:
        _MissingModelOllama.health_calls = 0
        _MissingModelOllama.translate_calls = 0
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.txt"
            input_path.write_text("你好", encoding="utf-8")
            stderr = io.StringIO()
            with patch("main.OllamaTranslator", _MissingModelOllama):
                with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    exit_code = run([str(input_path), "--mode", "document"])
            self.assertEqual(exit_code, 2)
            self.assertIn("Ollama에 모델이 없습니다", stderr.getvalue())
            self.assertEqual(_MissingModelOllama.translate_calls, 0)

    def test_invalid_ollama_health_envelope_is_transport_failure(self) -> None:
        translator = OllamaTranslator(self.config.ollama)
        with patch.object(translator, "_request", return_value={}):
            with self.assertRaises(TranslationTransportError):
                translator.health_check()

    def test_transport_failure_does_not_trigger_repair_or_split_storm(self) -> None:
        engine, segments = self._prepared_segments(4)
        translator = _TransportFailTranslator()
        with self.assertRaises(TranslationTransportError):
            self._recovery(translator, engine).translate_batch(segments)
        self.assertEqual(len(translator.calls), 1)
        self.assertTrue(
            all(segment.status == SegmentStatus.PREPARED for segment in segments)
        )

    def test_transport_failure_does_not_consume_semantic_retry_budget(self) -> None:
        engine, segments = self._prepared_segments(2)
        translator = _TransportFailTranslator()
        with self.assertRaises(TranslationTransportError):
            self._recovery(translator, engine).translate_batch(segments)
        self.assertEqual([segment.attempt_count for segment in segments], [0, 0])
        self.assertTrue(all(not segment.validation_issues for segment in segments))

    def test_actual_native_prompt_budget_is_checked_before_send(self) -> None:
        engine, segments = self._prepared_segments(3)
        for segment in segments:
            segment.context_before = ["前の文脈" * 40]
            segment.context_after = ["後の文脈" * 40]
        single_sizes = [
            len(build_single_translation_prompt(segment, self.novel))
            for segment in segments
        ]
        limit = max(single_sizes) + 5
        config = replace(
            self.config,
            translation=replace(
                self.config.translation,
                max_batch_chars=10000,
                max_prompt_chars=limit,
            ),
        )
        translator = _EchoTranslator()
        result = self._recovery(
            translator, engine, config=config
        ).translate_batch(segments)
        self.assertFalse(result.failed)
        self.assertEqual(len(translator.calls), 3)
        self.assertTrue(all(len(prompt) <= limit for prompt in translator.calls))
        self.assertEqual(
            [native_prompt_source(prompt) for prompt in translator.calls],
            [segment.prepared_source for segment in segments],
        )

    def test_unquoted_windows_path_stops_at_whitespace(self) -> None:
        engine = PlaceholderEngine()
        source = r"파일은 C:\game\data.txt 에 저장되어 있습니다."
        prepared = engine.protect(source)
        paths = [
            token.original
            for token in prepared.tokens
            if token.kind == "windows_path"
        ]
        self.assertEqual(paths, [r"C:\game\data.txt"])
        self.assertIn(" 에 저장되어 있습니다.", prepared.text)
        self.assertEqual(engine.restore(prepared.text, prepared.tokens), source)

    def test_quoted_windows_path_with_spaces_round_trips(self) -> None:
        engine = PlaceholderEngine()
        source = r'파일은 "C:\Program Files\Game\data.txt" 에 있습니다.'
        prepared = engine.protect(source)
        paths = [
            token.original
            for token in prepared.tokens
            if token.kind == "quoted_windows_path"
        ]
        self.assertEqual(paths, [r'"C:\Program Files\Game\data.txt"'])
        self.assertEqual(engine.restore(prepared.text, prepared.tokens), source)

    def test_unc_paths_remain_supported_without_sentence_greediness(self) -> None:
        engine = PlaceholderEngine()
        source = r'\\server\share\data.txt 다음과 "\\server\shared folder\data.txt" 끝'
        prepared = engine.protect(source)
        protected = [
            (token.kind, token.original) for token in prepared.tokens
        ]
        self.assertEqual(
            protected,
            [
                ("windows_path", r"\\server\share\data.txt"),
                ("quoted_windows_path", r'"\\server\shared folder\data.txt"'),
            ],
        )
        self.assertEqual(engine.restore(prepared.text, prepared.tokens), source)

    def test_repair_prompt_gets_missing_placeholder_reason_not_bad_translation(self) -> None:
        engine = PlaceholderEngine()
        segment = Segment("SEG_00000001", "Hello %PLAYER%")
        segment.source_language = "en"
        prepared = engine.protect(segment.source)
        segment.prepare(prepared.text, prepared.tokens)

        def repair(prompt: str) -> str:
            self.assertIn("MISSING_PLACEHOLDER", prompt)
            self.assertIn("모든 보호 토큰", prompt)
            self.assertNotIn("안녕하세요", prompt)
            self.assertNotIn(segment.id, prompt)
            marker = segment.protected_tokens[0].placeholder
            return f"안녕 {marker}"

        translator = ScriptedTranslator(["안녕하세요", repair])
        result = self._recovery(translator, engine).translate_batch([segment])
        self.assertFalse(result.failed)
        self.assertEqual(segment.translation, "안녕 %PLAYER%")
        self.assertEqual(len(translator.calls), 2)

    def test_repair_prompt_gets_cjk_residue_reason_not_bad_translation(self) -> None:
        engine, segments = self._prepared_segments(1)
        segment = segments[0]

        def repair(prompt: str) -> str:
            self.assertIn("KNOWN_BAD_CJK_RESIDUE", prompt)
            self.assertIn("한국어만 출력", prompt)
            self.assertNotIn("오류的", prompt)
            self.assertNotIn(segment.id, prompt)
            return "정상 번역"

        translator = ScriptedTranslator(["오류的", repair])
        result = self._recovery(translator, engine).translate_batch([segment])
        self.assertFalse(result.failed)
        self.assertEqual(len(translator.calls), 2)

    def test_resume_refuses_checkpoint_from_different_source_sha(self) -> None:
        _HealthyNoCallOllama.health_calls = 0
        _HealthyNoCallOllama.translate_calls = 0
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            input_path = temp / "input.txt"
            input_path.write_text("새 원문", encoding="utf-8")
            checkpoint_path = temp / "input.seori.sqlite"
            with CheckpointStore(checkpoint_path) as checkpoint:
                checkpoint.set_metadata("source_sha256", "old-source-sha")

            stderr = io.StringIO()
            with patch("main.OllamaTranslator", _HealthyNoCallOllama):
                with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    exit_code = run([str(input_path), "--mode", "novel"])
            self.assertEqual(exit_code, 2)
            self.assertIn("안전한 재개를 거부합니다", stderr.getvalue())
            self.assertIn("--no-resume", stderr.getvalue())
            self.assertEqual(_HealthyNoCallOllama.health_calls, 0)
            with CheckpointStore(checkpoint_path) as checkpoint:
                self.assertEqual(
                    checkpoint.get_metadata("source_sha256"), "old-source-sha"
                )

    def test_mode_novel_loads_novel_policy(self) -> None:
        profile = load_profile("novel")
        self.assertEqual(profile["name"], "novel")
        self.assertEqual(
            profile["validation"]["number_mismatch_severity"], "RISK"
        )

    def test_mode_game_loads_game_policy(self) -> None:
        profile = load_profile("game")
        self.assertEqual(profile["name"], "game")
        self.assertEqual(
            profile["validation"]["number_mismatch_severity"], "ERROR"
        )

    def test_mode_document_loads_document_policy(self) -> None:
        profile = load_profile("document")
        self.assertEqual(profile["name"], "document")
        self.assertIn("정보", profile["instructions"][0])

    def test_actual_expected_id_leak_is_detected_for_nonlegacy_prefix(self) -> None:
        segments = [
            Segment("TXT:alpha", "first"),
            Segment("TXT:beta", "second"),
        ]
        parsed = ResponseParser().parse(
            "TXT:alpha\t첫 번역 TXT:beta 누출\nTXT:beta\t둘째 번역"
        )
        result = validate_structure(parsed, segments, self.config.validators)
        first_codes = {
            issue.code for issue in result.by_segment_id["TXT:alpha"].issues
        }
        self.assertIn("ROW_ID_LEAK", first_codes)
        self.assertFalse(result.by_segment_id["TXT:beta"].has_errors)

    def test_game_number_and_state_reversal_are_stricter_than_novel(self) -> None:
        game = load_profile("game")
        novel_number = validate_risks(
            "HP +30", "HP +20", self.config.validators, self.novel["validation"]
        )
        game_number = validate_risks(
            "HP +30", "HP +20", self.config.validators, game["validation"]
        )
        self.assertFalse(novel_number.has_errors)
        self.assertTrue(game_number.has_errors)

        novel_state = validate_risks(
            "Enable", "비활성화", self.config.validators, self.novel["validation"]
        )
        game_state = validate_risks(
            "Enable", "비활성화", self.config.validators, game["validation"]
        )
        self.assertFalse(novel_state.has_errors)
        self.assertTrue(game_state.has_errors)
        self.assertIn(
            "ENABLE_DISABLE_FLIP_RISK",
            {issue.code for issue in game_state.issues},
        )

    def test_partial_quote_loss_is_mode_aware_without_exact_style_requirement(self) -> None:
        game = load_profile("game")
        source = "「A」와 「B」"
        translation = "“에이”와 비"
        novel_result = validate_text_structure(
            source, translation, self.novel["validation"]
        )
        game_result = validate_text_structure(
            source, translation, game["validation"]
        )
        novel_issue = next(
            issue
            for issue in novel_result.issues
            if issue.code == "QUOTE_STRUCTURE_PARTIAL_LOSS"
        )
        game_issue = next(
            issue
            for issue in game_result.issues
            if issue.code == "QUOTE_STRUCTURE_PARTIAL_LOSS"
        )
        self.assertEqual(novel_issue.severity, ValidationSeverity.RISK)
        self.assertEqual(game_issue.severity, ValidationSeverity.ERROR)

    def test_checkpoint_batch_save_uses_one_transaction(self) -> None:
        segments = [Segment("ONE", "첫째"), Segment("TWO", "둘째")]
        for segment in segments:
            segment.prepare(segment.source, [])
            segment.mark_valid(segment.source, ValidationResult())
        with tempfile.TemporaryDirectory() as directory:
            with CheckpointStore(Path(directory) / "batch.sqlite") as checkpoint:
                trace: list[str] = []
                assert checkpoint._connection is not None
                checkpoint._connection.set_trace_callback(trace.append)
                checkpoint.save_segments(segments)
                statements = [item.strip().upper() for item in trace]
                self.assertEqual(statements.count("BEGIN"), 1)
                self.assertEqual(statements.count("COMMIT"), 1)
                self.assertEqual(checkpoint.valid_count(), 2)

    def test_terminal_failure_qa_contains_bounded_last_raw_response(self) -> None:
        engine, segments = self._prepared_segments(1)
        segment = segments[0]
        config = replace(
            self.config,
            recovery=replace(self.config.recovery, max_attempts=2),
        )
        raw = "오류的" + ("x" * 6000)
        translator = ScriptedTranslator([raw, raw])
        recovery = self._recovery(translator, engine, config=config)
        result = recovery.translate_batch([segment])
        self.assertEqual(result.failed, [segment])
        self.assertTrue(segment.last_raw_response_truncated)
        self.assertLessEqual(len(segment.last_raw_response or ""), 4000)
        self.assertIsNotNone(segment.last_prompt)
        self.assertNotIn(segment.id, segment.last_prompt or "")

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
        self.assertIn("KNOWN_BAD_CJK_RESIDUE", failure["failure_codes"])
        self.assertTrue(failure["last_raw_response_truncated"])
        self.assertIn("[중간 생략]", failure["last_raw_response"])
        self.assertIn("<<<SOURCE>>>", failure["last_prompt"])


if __name__ == "__main__":
    unittest.main()
