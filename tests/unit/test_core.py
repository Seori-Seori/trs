from __future__ import annotations

import codecs
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from adapters.text import TextAdapter
from core.batching import safe_split_with_separators
from core.checkpoint import CheckpointStore
from core.config import ConfigError, load_config, load_profile
from core.parser import ResponseParser
from core.pipeline import TranslationPipeline
from core.placeholders import PlaceholderEngine
from core.segment import Segment, SegmentStatus, ValidationResult
from tests.helpers import ScriptedTranslator, prompt_targets


ROOT = Path(__file__).resolve().parents[2]


class SegmentTests(unittest.TestCase):
    def test_source_is_immutable_and_valid_cannot_reenter_translation(self) -> None:
        segment = Segment("SEG_00000001", "source")
        with self.assertRaises(AttributeError):
            segment.source = "changed"
        segment.prepare("source", [])
        segment.mark_valid("번역", ValidationResult())
        with self.assertRaises(RuntimeError):
            segment.begin_translation()

    def test_invalid_status_transition_is_rejected(self) -> None:
        segment = Segment("SEG_00000001", "source")
        with self.assertRaises(ValueError):
            segment.transition(SegmentStatus.PARSED)


class ConfigTests(unittest.TestCase):
    def test_default_runtime_config_loads(self) -> None:
        config = load_config(ROOT / "config.json")
        self.assertEqual(config.translation.target_language, "ko")
        self.assertTrue(config.output.create_backup)

    def test_target_language_and_backup_invariants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(
                '{"translation":{"target_language":"en"}}', encoding="utf-8"
            )
            with self.assertRaises(ConfigError):
                load_config(path)
            path.write_text(
                '{"output":{"create_backup":false}}', encoding="utf-8"
            )
            with self.assertRaises(ConfigError):
                load_config(path)


class PlaceholderTests(unittest.TestCase):
    def test_builtin_placeholder_round_trip(self) -> None:
        source = (
            "\\N[1] %PLAYER% {name} ${gold} <b>text</b> "
            "https://example.com/a C:\\game\\file.txt\r\n\tend"
        )
        engine = PlaceholderEngine()
        prepared = engine.protect(source)
        self.assertNotEqual(prepared.text, source)
        self.assertEqual(engine.restore(prepared.text, prepared.tokens), source)

    def test_literal_placeholder_like_source_round_trip(self) -> None:
        engine = PlaceholderEngine()
        source = "literal [[CTRL_n]] value"
        prepared = engine.protect(source)
        self.assertEqual(engine.restore(prepared.text, prepared.tokens), source)


class TextAdapterTests(unittest.TestCase):
    def test_bom_crlf_paragraphs_and_internal_newlines_are_preserved(self) -> None:
        source = "  「待って！」\r\n彼女は叫んだ。  \r\n\r\n\r\nNext\r\n"
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "novel.txt"
            input_path.write_bytes(codecs.BOM_UTF8 + source.encode("utf-8"))
            adapter = TextAdapter()
            document, segments = adapter.load(input_path)
            self.assertTrue(document.has_bom)
            self.assertEqual(document.newline_style, "\r\n")
            self.assertEqual(len(segments), 2)
            self.assertIn("\r\n", segments[0].source)
            segments[0].prepare(segments[0].source, [])
            segments[0].mark_valid("「기다려!」\r\n그녀가 외쳤다.")
            segments[1].prepare(segments[1].source, [])
            segments[1].mark_valid("다음\r\n")
            output_path = Path(directory) / "novel.ko.txt"
            adapter.save(document, segments, output_path)
            output = output_path.read_bytes()
            self.assertTrue(output.startswith(codecs.BOM_UTF8))
            decoded = output[len(codecs.BOM_UTF8):].decode("utf-8")
            self.assertIn("\r\n\r\n\r\n", decoded)
            self.assertTrue(decoded.startswith("  "))

    def test_source_hash_guard_rejects_mid_run_modification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "novel.txt"
            input_path.write_text("original", encoding="utf-8")
            adapter = TextAdapter()
            document, segments = adapter.load(input_path)
            segments[0].prepare("original", [])
            segments[0].mark_valid("번역")
            input_path.write_text("modified", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                adapter.save(document, segments, Path(directory) / "out.txt")


class BatchingTests(unittest.TestCase):
    def test_safe_split_reconstructs_exact_text_without_cutting_tokens(self) -> None:
        engine = PlaceholderEngine()
        source = "첫 문장입니다. https://example.com/very/long/path 다음 문장입니다! 마지막입니다."
        pieces = safe_split_with_separators(source, 22, engine)
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(p.text + p.separator_after for p in pieces), source)
        self.assertTrue(any("https://example.com/very/long/path" in p.text for p in pieces))


class EndToEndMockTests(unittest.TestCase):
    def test_txt_pipeline_and_resume(self) -> None:
        config = load_config(ROOT / "config.json")
        profile = load_profile("novel")

        def translate(prompt: str) -> str:
            rows = []
            for segment_id, _language, source in prompt_targets(prompt):
                mapping = {
                    "こんにちは": "안녕하세요",
                    "Next [[PH_0001]]": "다음 [[PH_0001]]",
                }
                rows.append(f"{segment_id}\t{mapping[source]}")
            return "\n".join(rows)

        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "novel.txt"
            input_path.write_text("こんにちは\n\nNext %PLAYER%", encoding="utf-8")
            adapter = TextAdapter()
            document, segments = adapter.load(input_path)
            checkpoint_path = Path(directory) / "novel.seori.sqlite"
            translator = ScriptedTranslator([translate])
            with CheckpointStore(checkpoint_path) as checkpoint:
                result = TranslationPipeline(
                    config, profile, translator, checkpoint
                ).process(segments)
            self.assertFalse(result.failed)
            self.assertEqual(len(translator.calls), 1)
            output_path = Path(directory) / "novel.ko.txt"
            adapter.save(document, segments, output_path)
            self.assertEqual(output_path.read_text(encoding="utf-8"), "안녕하세요\n\n다음 %PLAYER%")

            _document2, segments2 = adapter.load(input_path)
            no_call = ScriptedTranslator([])
            with CheckpointStore(checkpoint_path) as checkpoint:
                resumed = TranslationPipeline(
                    config, profile, no_call, checkpoint
                ).process(segments2)
            self.assertEqual(resumed.resumed, 2)
            self.assertEqual(len(no_call.calls), 0)

    def test_oversized_segment_is_split_and_rejoined(self) -> None:
        base = load_config(ROOT / "config.json")
        config = replace(
            base,
            translation=replace(
                base.translation, max_segment_chars=16, max_batch_chars=100
            ),
        )
        profile = load_profile("novel")

        def translate(prompt: str) -> str:
            return "\n".join(
                f"{segment_id}\t번역"
                for segment_id, _language, _source in prompt_targets(prompt)
            )

        source = "これは最初の長い文です。これは二番目の長い文です。これは最後です。"
        segment = Segment("SEG_00000001", source)
        translator = ScriptedTranslator([translate, translate, translate, translate])
        result = TranslationPipeline(config, profile, translator).process([segment])
        self.assertFalse(result.failed)
        self.assertIn("번역", segment.translation or "")
        self.assertGreater(len(translator.calls), 1)

    def test_invalid_checkpoint_translation_is_revalidated_and_retranslated(self) -> None:
        config = load_config(ROOT / "config.json")
        profile = load_profile("novel")

        def good_translation(prompt: str) -> str:
            segment_id, _language, _source = prompt_targets(prompt)[0]
            return f"{segment_id}\t안녕 [[PH_0001]]"

        with tempfile.TemporaryDirectory() as directory:
            checkpoint_path = Path(directory) / "resume.sqlite"
            first = Segment("SEG_00000001", "Hello %PLAYER%")
            with CheckpointStore(checkpoint_path) as checkpoint:
                TranslationPipeline(
                    config,
                    profile,
                    ScriptedTranslator([good_translation]),
                    checkpoint,
                ).process([first])

            with sqlite3.connect(checkpoint_path) as connection:
                connection.execute(
                    "UPDATE segments SET translation = '안녕', status = 'VALID' "
                    "WHERE segment_id = 'SEG_00000001'"
                )
                connection.commit()

            second = Segment("SEG_00000001", "Hello %PLAYER%")
            translator = ScriptedTranslator([good_translation])
            with CheckpointStore(checkpoint_path) as checkpoint:
                result = TranslationPipeline(
                    config, profile, translator, checkpoint
                ).process([second])
            self.assertEqual(result.resumed, 0)
            self.assertEqual(len(translator.calls), 1)
            self.assertEqual(second.translation, "안녕 %PLAYER%")


if __name__ == "__main__":
    unittest.main()
