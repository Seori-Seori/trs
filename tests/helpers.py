from __future__ import annotations

from collections.abc import Callable

from translators.base import Translator


def prompt_targets(prompt: str) -> list[tuple[str, str, str]]:
    lines = prompt.splitlines()
    targets: list[tuple[str, str, str]] = []
    for index, line in enumerate(lines):
        if line != "번역 대상:":
            continue
        if index + 1 >= len(lines):
            raise AssertionError("Prompt target marker has no target row")
        parts = lines[index + 1].split("\t", 2)
        if len(parts) != 3:
            raise AssertionError("Prompt target row is not ID<TAB>language<TAB>source")
        targets.append((parts[0], parts[1], parts[2]))
    return targets


def native_prompt_source(prompt: str) -> str:
    start_marker = "<<<SOURCE>>>\n"
    end_marker = "\n<<<END_SOURCE>>>"
    if prompt.count(start_marker) != 1 or prompt.count(end_marker) != 1:
        raise AssertionError("Native prompt must contain exactly one source block")
    return prompt.split(start_marker, 1)[1].split(end_marker, 1)[0]


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
