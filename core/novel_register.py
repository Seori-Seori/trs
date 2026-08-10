from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.terminology import is_novel_profile


_REGISTER_RESOURCE = (
    Path(__file__).resolve().parent.parent / "profiles" / "novel_register.json"
)


@dataclass(frozen=True)
class NovelRegisterHint:
    source_terms: tuple[str, ...]
    language: str
    meaning_class: str
    source_register: str
    preferred_register: str
    preferred_korean: tuple[str, ...]
    avoid_korean: tuple[str, ...]
    notes: str

    def matched_source_terms(self, source: str) -> tuple[str, ...]:
        return tuple(term for term in self.source_terms if term in source)


@dataclass(frozen=True)
class NovelRegisterResource:
    medical_context_terms: tuple[str, ...]
    entries: tuple[NovelRegisterHint, ...]


@dataclass(frozen=True)
class NovelNameHint:
    source: str
    korean: str


class NovelJobNameMap:
    """Small deterministic name map instantiated once for one translation job."""

    def __init__(self, mappings: dict[str, str] | None = None) -> None:
        self._mappings = tuple(
            (source, korean) for source, korean in (mappings or {}).items()
        )

    @classmethod
    def from_profile(cls, profile: dict[str, Any] | None) -> "NovelJobNameMap":
        if not is_novel_profile(profile):
            return cls()
        raw = (profile or {}).get("name_map", {})
        if not isinstance(raw, dict):
            raise ValueError("Novel profile name_map must be an object")
        mappings: dict[str, str] = {}
        for source, korean in raw.items():
            if not isinstance(source, str) or not source.strip():
                raise ValueError("Novel profile name_map keys must be non-empty strings")
            if not isinstance(korean, str) or not korean.strip():
                raise ValueError("Novel profile name_map values must be non-empty strings")
            mappings[source] = korean
        return cls(mappings)

    def matching(self, source: str, source_language: str) -> list[NovelNameHint]:
        if source_language not in {"zh", "auto", "unknown"}:
            return []
        return [
            NovelNameHint(source=source_term, korean=korean)
            for source_term, korean in self._mappings
            if source_term in source
        ]


def _string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"Novel register field {field!r} must be a string list")
    return tuple(value)


@lru_cache(maxsize=1)
def load_novel_register_resource() -> NovelRegisterResource:
    try:
        raw = json.loads(_REGISTER_RESOURCE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load novel register resource: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Novel register resource root must be an object")
    medical_context_terms = _string_tuple(
        raw.get("medical_context_terms"), field="medical_context_terms"
    )
    entries = raw.get("entries")
    if not isinstance(entries, list):
        raise ValueError("Novel register resource must contain an entries list")

    hints: list[NovelRegisterHint] = []
    seen_source_terms: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each novel register entry must be an object")
        source_terms = _string_tuple(entry.get("source_terms"), field="source_terms")
        duplicates = seen_source_terms.intersection(source_terms)
        if duplicates:
            raise ValueError(
                f"Novel register source terms must be unique: {sorted(duplicates)!r}"
            )
        seen_source_terms.update(source_terms)
        language = entry.get("language")
        meaning_class = entry.get("meaning_class")
        source_register = entry.get("source_register")
        preferred_register = entry.get("preferred_register")
        notes = entry.get("notes")
        if not all(
            isinstance(value, str) and value
            for value in (
                language,
                meaning_class,
                source_register,
                preferred_register,
                notes,
            )
        ):
            raise ValueError(
                "Novel register language/meaning/register/notes fields must be strings"
            )
        hints.append(
            NovelRegisterHint(
                source_terms=source_terms,
                language=language,
                meaning_class=meaning_class,
                source_register=source_register,
                preferred_register=preferred_register,
                preferred_korean=_string_tuple(
                    entry.get("preferred_korean"), field="preferred_korean"
                ),
                avoid_korean=_string_tuple(
                    entry.get("avoid_korean"), field="avoid_korean"
                ),
                notes=notes,
            )
        )
    return NovelRegisterResource(medical_context_terms, tuple(hints))


def source_context_is_medical(source: str, context: list[str] | None = None) -> bool:
    resource = load_novel_register_resource()
    combined = "\n".join([source, *(context or [])])
    return any(term in combined for term in resource.medical_context_terms)


def matching_novel_register_hints(
    source: str,
    source_language: str,
    profile: dict[str, Any] | None,
    *,
    context: list[str] | None = None,
) -> list[NovelRegisterHint]:
    if not is_novel_profile(profile) or source_context_is_medical(source, context):
        return []
    return [
        hint
        for hint in load_novel_register_resource().entries
        if hint.matched_source_terms(source)
        and source_language in {hint.language, "auto", "unknown"}
    ]
