from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .mappings import JobMappings
from .segment import ProtectedToken


PLACEHOLDER_LIKE_RE = re.compile(r"\[\[[A-Za-z][A-Za-z0-9_]*(?:_[A-Za-z0-9]+)*\]\]")
GENERATED_PLACEHOLDER_RE = re.compile(r"\[\[(?:PH|NAME|MAP)_\d{4,}\]\]")


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


@dataclass(frozen=True)
class _ProtectedSpan:
    start: int
    end: int
    kind: str
    mapped_target: str | None = None
    placeholder_prefix: str = "PH"


class PlaceholderEngine:
    def __init__(
        self,
        custom_patterns: Iterable[Any] | None = None,
        *,
        protect_internal_newlines: bool = True,
        job_mappings: JobMappings | None = None,
    ) -> None:
        self._patterns = self._build_patterns(
            list(custom_patterns or []), protect_internal_newlines=protect_internal_newlines
        )
        self._mapping_entries = tuple((job_mappings or JobMappings()).entries)

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
                r"<<<(?:CONTEXT|END_CONTEXT|TARGETS|END_TARGETS|"
                r"REFERENCE_CONTEXT|END_REFERENCE_CONTEXT|SOURCE|END_SOURCE)>>>",
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
                "quoted_windows_path",
                r'"(?:[A-Za-z]:\\|\\\\)[^"\r\n<>|?*]+"'
                r"|'(?:[A-Za-z]:\\|\\\\)[^'\r\n<>|?*]+'",
            ),
            (
                "windows_path",
                r"(?:[A-Za-z]:\\|\\\\)[^\s\r\n<>|\"?*]+",
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

    def _selected_spans(self, source: str) -> list[_ProtectedSpan]:
        candidates: list[
            tuple[int, int, int, str, str | None, str]
        ] = []
        for spec in self._patterns:
            for match in spec.regex.finditer(source):
                if match.start() == match.end():
                    continue
                candidates.append(
                    (match.start(), match.end(), spec.priority, spec.kind, None, "PH")
                )

        mapping_priority = len(self._patterns)
        for offset, entry in enumerate(self._mapping_entries):
            start = 0
            while True:
                start = source.find(entry.source, start)
                if start < 0:
                    break
                end = start + len(entry.source)
                candidates.append(
                    (
                        start,
                        end,
                        mapping_priority + offset,
                        f"mapped_{entry.kind}",
                        entry.target,
                        entry.placeholder_prefix,
                    )
                )
                start += 1
        candidates.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))

        selected: list[_ProtectedSpan] = []
        occupied_until = -1
        for start, end, _priority, kind, mapped_target, prefix in candidates:
            if start < occupied_until:
                continue
            selected.append(
                _ProtectedSpan(start, end, kind, mapped_target, prefix)
            )
            occupied_until = end
        return selected

    def protected_spans(self, source: str) -> list[tuple[int, int, str]]:
        return [
            (span.start, span.end, span.kind)
            for span in self._selected_spans(source)
        ]

    def protect(self, source: str) -> PreparedText:
        spans = self._selected_spans(source)
        if not spans:
            return PreparedText(source, [])

        existing_markers = set(PLACEHOLDER_LIKE_RE.findall(source))
        used = set(existing_markers)
        tokens: list[ProtectedToken] = []
        pieces: list[str] = []
        cursor = 0
        numbers = {"PH": 1, "NAME": 1, "MAP": 1}

        for order, span in enumerate(spans, start=1):
            pieces.append(source[cursor:span.start])
            prefix = span.placeholder_prefix
            while True:
                placeholder = f"[[{prefix}_{numbers[prefix]:04d}]]"
                numbers[prefix] += 1
                if placeholder not in used:
                    break
            used.add(placeholder)
            original = source[span.start:span.end]
            tokens.append(
                ProtectedToken(
                    placeholder=placeholder,
                    original=original,
                    kind=span.kind,
                    order=order,
                    mapped_target=span.mapped_target,
                )
            )
            pieces.append(placeholder)
            cursor = span.end
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
            restored = restored.replace(token.placeholder, token.restored_value, 1)
        return restored

    def round_trip(self, source: str) -> bool:
        prepared = self.protect(source)
        return self.restore(prepared.text, prepared.tokens) == source
