from __future__ import annotations

import re

from core.config import ValidatorsConfig
from core.segment import ValidationResult, ValidationSeverity


_HANGUL_RE = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")
_KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LETTER_RE = re.compile(r"[A-Za-z\u3040-\u30ff\u3400-\u9fff\uac00-\ud7a3]")
_LATIN_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z'-]*\b")

_SCRIPT_PATTERNS: dict[str, re.Pattern[str]] = {
    "Cyrillic": re.compile(r"[\u0400-\u052f]"),
    "Greek": re.compile(r"[\u0370-\u03ff]"),
    "Arabic": re.compile(r"[\u0600-\u06ff]"),
    "Hebrew": re.compile(r"[\u0590-\u05ff]"),
    "Devanagari": re.compile(r"[\u0900-\u097f]"),
    "Thai": re.compile(r"[\u0e00-\u0e7f]"),
}


def validate_korean(
    source: str,
    translation: str,
    source_language: str,
    config: ValidatorsConfig,
    *,
    novel_mode: bool = False,
    cjk_whitelist: list[str] | None = None,
    protected_values: list[str] | None = None,
) -> ValidationResult:
    result = ValidationResult()
    hangul_count = len(_HANGUL_RE.findall(translation))
    source_letters = len(_LETTER_RE.findall(source))
    source_backed_whitelist = [
        term
        for term in (cjk_whitelist or [])
        if isinstance(term, str) and term and term in source and term in translation
    ]
    source_backed_protected = [
        value
        for value in (protected_values or [])
        if isinstance(value, str) and value and value in source and value in translation
    ]
    residue_text = translation
    for term in sorted(
        set(source_backed_whitelist + source_backed_protected),
        key=len,
        reverse=True,
    ):
        residue_text = residue_text.replace(term, "")

    if source_language != "ko" and source_letters >= 3 and hangul_count == 0:
        source_words = _LATIN_TOKEN_RE.findall(source)
        translation_words = _LATIN_TOKEN_RE.findall(translation)
        likely_name_or_symbol = (
            len(source_words) == 1
            and len(translation_words) == 1
            and source.strip() == translation.strip()
        )
        source_backed_only = (
            bool(source_backed_whitelist or source_backed_protected)
            and not _LETTER_RE.search(residue_text)
        )
        if likely_name_or_symbol or source_backed_only:
            result.add(
                "KOREAN_OUTPUT_ABSENT_RISK",
                ValidationSeverity.RISK,
                "An explicitly source-backed protected value/name/symbol was preserved without Hangul",
                "korean",
            )
        else:
            result.add(
                "KOREAN_OUTPUT_ABSENT",
                ValidationSeverity.ERROR,
                "Target text contains no Korean despite Korean-only output policy",
                "korean",
            )

    if config.detect_cjk_residue:
        kana = _KANA_RE.findall(residue_text)
        cjk = _CJK_RE.findall(residue_text)
        if "的" in residue_text:
            result.add(
                "KNOWN_BAD_CJK_RESIDUE",
                ValidationSeverity.ERROR,
                "Known untranslated CJK residue '的' remains",
                "korean",
                characters=["的"],
            )
        if kana:
            result.add(
                "KANA_RESIDUE",
                ValidationSeverity.ERROR,
                "Japanese kana remains in Korean output",
                "korean",
                characters=sorted(set(kana)),
            )
        cjk_without_known = [char for char in cjk if char != "的"]
        visible_length = max(1, len(re.sub(r"\s", "", translation)))
        if novel_mode and cjk_without_known:
            result.add(
                "NOVEL_CJK_RESIDUE",
                ValidationSeverity.ERROR,
                "Unprotected Chinese characters remain in novel-mode Korean output",
                "korean",
                characters=sorted(set(cjk_without_known)),
                count=len(cjk_without_known),
            )
        elif len(cjk_without_known) >= 3 or len(cjk_without_known) / visible_length >= 0.08:
            result.add(
                "EXCESSIVE_CJK_RESIDUE",
                ValidationSeverity.ERROR,
                "Too much untranslated CJK text remains",
                "korean",
                characters=sorted(set(cjk_without_known)),
                count=len(cjk_without_known),
            )
        elif cjk_without_known:
            result.add(
                "LOW_CJK_RESIDUE",
                ValidationSeverity.WARNING,
                "A small amount of CJK text remains; it may be an intentional name",
                "korean",
                characters=sorted(set(cjk_without_known)),
            )

    if config.detect_unexpected_scripts:
        for script_name, pattern in _SCRIPT_PATTERNS.items():
            output_chars = pattern.findall(translation)
            if output_chars and not pattern.search(source):
                result.add(
                    "UNEXPECTED_SCRIPT",
                    ValidationSeverity.ERROR,
                    f"Unexpected {script_name} script appears in the translation",
                    "korean",
                    script=script_name,
                    characters=sorted(set(output_chars)),
                )

    latin_tokens = _LATIN_TOKEN_RE.findall(translation)
    latin_chars = sum(len(token) for token in latin_tokens)
    visible_length = max(1, len(re.sub(r"\s", "", translation)))
    if len(latin_tokens) >= 3 and latin_chars / visible_length >= 0.25:
        result.add(
            "EXCESSIVE_LATIN_MIX_RISK",
            ValidationSeverity.RISK,
            "Korean output contains an unusually large amount of Latin text",
            "korean",
            tokens=latin_tokens,
        )
    return result
