from __future__ import annotations

from collections.abc import Callable

from translators.base import Translator


def prompt_targets(prompt: str) -> list[tuple[str, str, str]]:
    block = prompt.split("<<<TARGETS>>>\n", 1)[1].split("\n<<<END_TARGETS>>>", 1)[0]
    return [tuple(line.split("\t", 2)) for line in block.splitlines() if line]


class ScriptedTranslator(Translator):
    def __init__(self, responses: list[str | Callable[[str], str]]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def health_check(self) -> bool:
        return True

    def translate(self, prompt: str) -> str:
        self.calls.append(prompt)
        if not self.responses:
            raise AssertionError("Unexpected translator call")
        response = self.responses.pop(0)
        return response(prompt) if callable(response) else response


class NoCallTranslator(Translator):
    def __init__(self) -> None:
        self.calls = 0

    def health_check(self) -> bool:
        return True

    def translate(self, prompt: str) -> str:
        self.calls += 1
        raise AssertionError(f"Translator must not be called:\n{prompt}")
