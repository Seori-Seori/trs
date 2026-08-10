from __future__ import annotations

import unittest
from pathlib import Path

from core.config import load_config, load_profile
from core.parser import ResponseParser
from core.placeholders import PlaceholderEngine
from core.recovery import RecoveryEngine
from core.segment import Segment
from core.validation import ValidationCoordinator
from tests.helpers import ScriptedTranslator, prompt_targets


ROOT = Path(__file__).resolve().parents[2]


class RecoverySplitTests(unittest.TestCase):
    def test_split_fallback_never_retranslates_salvaged_rows(self) -> None:
        config = load_config(ROOT / "config.json")
        profile = load_profile("novel")
        engine = PlaceholderEngine()
        segments = [Segment(f"SEG_{i:08d}", f"原文甲{i}") for i in range(1, 5)]
        for segment in segments:
            segment.source_language = "ja"
            prepared = engine.protect(segment.source)
            segment.prepare(prepared.text, prepared.tokens)

        expected_calls = [
            {segments[0].id, segments[1].id, segments[2].id, segments[3].id},
            {segments[1].id, segments[2].id, segments[3].id},
            {segments[2].id},
            {segments[3].id},
        ]

        def response(call_index: int):
            def inner(prompt: str) -> str:
                targets = prompt_targets(prompt)
                ids = {item[0] for item in targets}
                self.assertEqual(ids, expected_calls[call_index])
                lines = []
                for segment_id, _language, _source in targets:
                    good = (
                        (call_index == 0 and segment_id == segments[0].id)
                        or (call_index == 1 and segment_id == segments[1].id)
                        or call_index >= 2
                    )
                    lines.append(f"{segment_id}\t{'정상 번역' if good else '오류的'}")
                return "\n".join(lines)
            return inner

        translator = ScriptedTranslator([response(i) for i in range(4)])
        recovery = RecoveryEngine(
            translator,
            ResponseParser(),
            ValidationCoordinator(config.validators, engine),
            engine,
            config.recovery,
            config.translation,
            profile,
        )
        result = recovery.translate_batch(segments)
        self.assertFalse(result.failed)
        self.assertEqual(segments[0].attempt_count, 1)
        self.assertEqual(segments[1].attempt_count, 2)
        self.assertEqual(segments[2].attempt_count, 3)
        self.assertEqual(segments[3].attempt_count, 3)


if __name__ == "__main__":
    unittest.main()
