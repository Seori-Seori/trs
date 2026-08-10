from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


_TERM_RESOURCE = (
    Path(__file__).resolve().parent.parent / "profiles" / "novel_zh_terms.json"
)


@dataclass(frozen=True)
class NovelTermHint:
    source: str
    scope: str
    language: str
    meaning: str
    prompt_hint: str
    preferred_korean: tuple[str, ...] = ()
    disallowed_korean: tuple[str, ...] = ()
    exception_source_terms: tuple[str, ...] = ()


def is_novel_profile(profile: dict[str, Any] | None) -> bool:
    if not profile:
        return False
    name = str(profile.get("name", "")).strip().lower()
    style = str(profile.get("style", "")).strip().lower()
    return name == "novel" or "novel" in style


def _string_tuple(value: object, *, field: str, source: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(
            f"Novel terminology {source!r} field {field!r} must be a string list"
        )
    return tuple(value)


@lru_cache(maxsize=1)
def load_novel_term_hints() -> tuple[NovelTermHint, ...]:
    try:
        raw = json.loads(_TERM_RESOURCE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load novel terminology resource: {exc}") from exc
    entries = raw.get("terms") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError("Novel terminology resource must contain a terms list")

    hints: list[NovelTermHint] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each novel terminology entry must be an object")
        source = entry.get("source")
        scope = entry.get("scope")
        language = entry.get("language")
        meaning = entry.get("meaning")
        prompt_hint = entry.get("prompt_hint")
        if not all(
            isinstance(value, str) and value
            for value in (source, scope, language, meaning, prompt_hint)
        ):
            raise ValueError(
                "Novel terminology source/scope/language/meaning/prompt_hint must be strings"
            )
        if scope != "novel":
            raise ValueError(f"Unsupported novel terminology scope: {scope!r}")
        hints.append(
            NovelTermHint(
                source=source,
                scope=scope,
                language=language,
                meaning=meaning,
                prompt_hint=prompt_hint,
                preferred_korean=_string_tuple(
                    entry.get("preferred_korean"),
                    field="preferred_korean",
                    source=source,
                ),
                disallowed_korean=_string_tuple(
                    entry.get("disallowed_korean"),
                    field="disallowed_korean",
                    source=source,
                ),
                exception_source_terms=_string_tuple(
                    entry.get("exception_source_terms"),
                    field="exception_source_terms",
                    source=source,
                ),
            )
        )
    return tuple(hints)


def matching_novel_term_hints(
    source: str,
    source_language: str,
    profile: dict[str, Any] | None,
) -> list[NovelTermHint]:
    if not is_novel_profile(profile):
        return []
    return [
        hint
        for hint in load_novel_term_hints()
        if hint.source in source
        and source_language in {hint.language, "auto", "unknown"}
    ]
