from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


_ZH_LANGUAGE_PACK = (
    Path(__file__).resolve().parent.parent / "profiles" / "zh_language_pack.json"
)


@dataclass(frozen=True)
class SemanticTermRule:
    source_terms: tuple[str, ...]
    language: str
    meaning_class: str
    meaning: str
    conflicting_korean: tuple[str, ...]
    guarded_korean: tuple[str, ...] = ()
    exception_source_terms: tuple[str, ...] = ()

    def matched_source_terms(self, source: str) -> tuple[str, ...]:
        return tuple(term for term in self.source_terms if term in source)


def is_novel_profile(profile: dict[str, Any] | None) -> bool:
    if not profile:
        return False
    name = str(profile.get("name", "")).strip().lower()
    style = str(profile.get("style", "")).strip().lower()
    return name == "novel" or "novel" in style


def _string_tuple(
    value: object,
    *,
    field: str,
    required: bool = False,
) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"Chinese language-pack field {field!r} must be a string list")
    if required and not value:
        raise ValueError(f"Chinese language-pack field {field!r} must not be empty")
    return tuple(value)


@lru_cache(maxsize=1)
def load_zh_semantic_rules() -> tuple[SemanticTermRule, ...]:
    try:
        raw = json.loads(_ZH_LANGUAGE_PACK.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load Chinese language pack: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("language") != "zh":
        raise ValueError("Chinese language pack root/language is invalid")
    entries = raw.get("semantic_guards")
    if not isinstance(entries, list):
        raise ValueError("Chinese language pack must contain semantic_guards")

    rules: list[SemanticTermRule] = []
    seen_source_terms: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each Chinese semantic guard must be an object")
        source_terms = _string_tuple(
            entry.get("source_terms"), field="source_terms", required=True
        )
        duplicates = seen_source_terms.intersection(source_terms)
        if duplicates:
            raise ValueError(
                f"Chinese semantic source terms must be unique: {sorted(duplicates)!r}"
            )
        seen_source_terms.update(source_terms)
        meaning_class = entry.get("meaning_class")
        meaning = entry.get("meaning")
        if not isinstance(meaning_class, str) or not meaning_class:
            raise ValueError("Chinese semantic meaning_class must be a string")
        if not isinstance(meaning, str) or not meaning:
            raise ValueError("Chinese semantic meaning must be a string")
        rules.append(
            SemanticTermRule(
                source_terms=source_terms,
                language="zh",
                meaning_class=meaning_class,
                meaning=meaning,
                conflicting_korean=_string_tuple(
                    entry.get("conflicting_korean"),
                    field="conflicting_korean",
                    required=True,
                ),
                guarded_korean=_string_tuple(
                    entry.get("guarded_korean"), field="guarded_korean"
                ),
                exception_source_terms=_string_tuple(
                    entry.get("exception_source_terms"),
                    field="exception_source_terms",
                ),
            )
        )
    return tuple(rules)


def matching_semantic_term_rules(
    source: str,
    source_language: str,
) -> list[SemanticTermRule]:
    if source_language not in {"zh", "auto", "unknown"}:
        return []
    return [
        rule
        for rule in load_zh_semantic_rules()
        if rule.matched_source_terms(source)
    ]
