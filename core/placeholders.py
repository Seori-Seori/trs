from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .segment import ProtectedToken


PLACEHOLDER_LIKE_RE = re.compile(r"\[\[[A-Za-z][A-Za-z0-9_]*(?:_[A-Za-z0-9]+)*\]\]")
GENERATED_PLACEHOLDER_RE = re.compile(r"\[\[PH_\d{4,}\]\]")


class PlaceholderError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedText:
    text: str
    tokens: list[ProtectedToken]


@dataclass(frozen=True)
class _PatternSpec:
    kind: str
    regex: re.Pattern[str]
    priority: int


class PlaceholderEngine:
    def __init__(
        self,
        custom_patterns: Iterable[Any] | None = None,
        *,
        protect_internal_newlines: bool = True,
    ) -> None:
        self._patterns = self._build_patterns(
            list(custom_patterns or []), protect_internal_newlines=protect_internal_newlines
        )

    @staticmethod
    def _build_patterns(
        custom_patterns: list[Any], *, protect_internal_newlines: bool
    ) -> list[_PatternSpec]:
        specs: list[_PatternSpec] = []
        priority = 0
        for item in custom_patterns:
            if isinstance(item, str):
                pattern, kind = item, "custom"
            elif isinstance(item, dict) and isinstance(item.get("pattern"), str):
                pattern = item["pattern"]
                kind = str(item.get("kind", "custom"))
            else:
                raise PlaceholderError(
                    "Each custom placeholder pattern must be a regex string or an object with 'pattern'"
                )
            try:
                specs.append(_PatternSpec(kind, re.compile(pattern), priority))
            except re.error as exc:
                raise PlaceholderError(f"Invalid custom placeholder regex {pattern!r}: {exc}") from exc
            priority += 1

        builtins: list[tuple[str, str]] = [
            ("existing_placeholder", r"\[\[[A-Za-z][A-Za-z0-9_]*(?:_[A-Za-z0-9]+)*\]\]"),
            (
                "prompt_delimiter",
                r"<<<(?:CONTEXT|END_CONTEXT|TARGETS|END_TARGETS)>>>",
            ),
            ("url", r"(?:https?|ftp)://[^\s<>\]\[{}\"']+"),
            ("markup", r"</?[A-Za-z][^<>\r\n]*?>"),
            ("dollar_brace", r"\$\{[^{}\r\n]+\}"),
            ("rpg_control", r"\\[A-Za-z]+\[[^\]\r\n]*\]"),
            ("percent_named", r"%[A-Za-z_][A-Za-z0-9_]*%"),
            (
                "printf",
                r"%(?:\d+\$)?[-+#0 ']*\d*(?:\.\d+)?[diuoxXfFeEgGaAcspn%]",
            ),
            ("brace", r"\{[^{}\r\n]+\}"),
            (
                "windows_path",
                r"(?:[A-Za-z]:\\|\\\\)[^\r\n<>|\"?*]+(?:\\[^\r\n<>|\"?*]+)*",
            ),
            ("posix_path", r"(?<![\w:])/(?:[^/\s\r\n]+/)+[^/\s\r\n]*"),
            ("tab", r"\t"),
        ]
        if protect_internal_newlines:
            builtins.append(("newline", r"\r\n[ \t]*|\n[ \t]*|\r[ \t]*"))

        for kind, pattern in builtins:
            specs.append(_PatternSpec(kind, re.compile(pattern), priority))
            priority += 1
        return specs

    def protected_spans(self, source: str) -> list[tuple[int, int, str]]:
        candidates: list[tuple[int, int, int, str]] = []
        for spec in self._patterns:
            for match in spec.regex.finditer(source):
                if match.start() == match.end():
                    continue
                candidates.append((match.start(), match.end(), spec.priority, spec.kind))
        candidates.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))

        selected: list[tuple[int, int, str]] = []
        occupied_until = -1
        for start, end, _priority, kind in candidates:
            if start < occupied_until:
                continue
            selected.append((start, end, kind))
            occupied_until = end
        return selected

    def protect(self, source: str) -> PreparedText:
        spans = self.protected_spans(source)
        if not spans:
            return PreparedText(source, [])

        existing_markers = set(PLACEHOLDER_LIKE_RE.findall(source))
        used = set(existing_markers)
        tokens: list[ProtectedToken] = []
        pieces: list[str] = []
        cursor = 0
        number = 1

        for order, (start, end, kind) in enumerate(spans, start=1):
            pieces.append(source[cursor:start])
            while True:
                placeholder = f"[[PH_{number:04d}]]"
                number += 1
                if placeholder not in used:
                    break
            used.add(placeholder)
            original = source[start:end]
            tokens.append(
                ProtectedToken(
                    placeholder=placeholder,
                    original=original,
                    kind=kind,
                    order=order,
                )
            )
            pieces.append(placeholder)
            cursor = end
        pieces.append(source[cursor:])
        return PreparedText("".join(pieces), tokens)

    @staticmethod
    def observed_placeholders(text: str) -> list[str]:
        return PLACEHOLDER_LIKE_RE.findall(text)

    @staticmethod
    def restore(text: str, tokens: Iterable[ProtectedToken]) -> str:
        ordered = sorted(tokens, key=lambda token: token.order)
        expected = [token.placeholder for token in ordered]
        observed = PlaceholderEngine.observed_placeholders(text)
        if observed != expected:
            raise PlaceholderError(
                f"Placeholder sequence mismatch: expected {expected!r}, got {observed!r}"
            )
        restored = text
        for token in ordered:
            if restored.count(token.placeholder) != 1:
                raise PlaceholderError(
                    f"Placeholder {token.placeholder} must occur exactly once before restoration"
                )
            restored = restored.replace(token.placeholder, token.original, 1)
        return restored

    def round_trip(self, source: str) -> bool:
        prepared = self.protect(source)
        return self.restore(prepared.text, prepared.tokens) == source
