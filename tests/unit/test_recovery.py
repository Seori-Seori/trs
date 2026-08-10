from __future__ import annotations

import unittest
from pathlib import Path

from core.config import load_config, load_profile
from core.parser import SingleTranslationParser
from core.placeholders import PlaceholderEngine
from core.recovery import RecoveryEngine
from core.segment import Segment
from core.validation import ValidationCoordinator
from tests.helpers import ScriptedTranslator, native_prompt_source


ROOT = Path(__file__).resolve().parents[2]


class RecoverySplitTests(unittest.TestCase):
    def test_native_recovery_never_retranslates_completed_segments(self) -> None:
        config = load_config(ROOT / "config.json")
        profile = load_profile("novel")
        engine = PlaceholderEngine()
        segments = [Segment(f"SEG_{i:08d}", f"原文甲{i}") for i in range(1, 5)]
        for segment in segments:
            segment.source_language = "ja"
            prepared = engine.protect(segment.source)
            segment.prepare(prepared.text, prepared.tokens)

        outputs = [
            ["정상 번역"],
            ["오류的", "정상 번역"],
            ["오류的", "오류的", "정상 번역"],
            ["오류的", "오류的", "정상 번역"],
        ]
        responses = []
        for segment, segment_outputs in zip(segments, outputs):
            for output in segment_outputs:
                def response(
                    prompt: str,
                    *,
                    expected=segment,
                    value=output,
                ) -> str:
                    self.assertEqual(
                        native_prompt_source(prompt), expected.prepared_source
                    )
                    self.assertNotIn(expected.id, prompt)
                    return value

                responses.append(response)

        translator = ScriptedTranslator(responses)
        recovery = RecoveryEngine(
            translator,
            SingleTranslationParser(),
            ValidationCoordinator(config.validators, engine, profile),
            engine,
            config.recovery,
            config.translation,
            profile,
        )
        result = recovery.translate_batch(segments)

        self.assertFalse(result.failed)
        self.assertEqual([segment.attempt_count for segment in segments], [1, 2, 3, 3])
        self.assertEqual(len(translator.calls), 9)


if __name__ == "__main__":
    unittest.main()
