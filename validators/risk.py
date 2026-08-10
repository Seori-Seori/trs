from __future__ import annotations

import re
from collections.abc import Mapping

from core.config import ValidatorsConfig
from core.segment import ValidationResult, ValidationSeverity
from validators.policy import policy_severity


_NUMBER_RE = re.compile(r"(?<!\w)[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?!\w)")
_SOURCE_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|without|cannot|can't|don't|doesn't|do\s+not)\b|ない|ません|ぬ|無|非|不|没|别",
    re.IGNORECASE,
)
_KOREAN_NEGATION_RE = re.compile(r"않|아니|없|못|금지|불가|지\s*마|말(?:아|고|라|세요)")
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")


_CONCEPT_PAIRS = [
    (
        "YES_NO_FLIP_RISK",
        re.compile(r"\b(?:yes|ok|true)\b|はい|是|对", re.IGNORECASE),
        re.compile(r"\b(?:no|false)\b|いいえ|否|不是", re.IGNORECASE),
        re.compile(r"(?:^|\s)(?:예|네|맞습니다|확인)(?:$|[\s.!?])"),
        re.compile(r"아니|아니요|거절|틀렸"),
    ),
    (
        "UP_DOWN_FLIP_RISK",
        re.compile(r"\bup\b|上|위로?", re.IGNORECASE),
        re.compile(r"\bdown\b|下|아래로?", re.IGNORECASE),
        re.compile(r"위|상승|올리"),
        re.compile(r"아래|하강|내리"),
    ),
    (
        "LEFT_RIGHT_FLIP_RISK",
        re.compile(r"\bleft\b|左|왼쪽?", re.IGNORECASE),
        re.compile(r"\bright\b|右|오른쪽?", re.IGNORECASE),
        re.compile(r"왼쪽?|좌측"),
        re.compile(r"오른쪽?|우측"),
    ),
    (
        "BEFORE_AFTER_FLIP_RISK",
        re.compile(r"\bbefore\b|前|之前|전에?", re.IGNORECASE),
        re.compile(r"\bafter\b|後|后|之后|후에?", re.IGNORECASE),
        re.compile(r"전|이전|앞서"),
        re.compile(r"후|이후|뒤에"),
    ),
    (
        "ENABLE_DISABLE_FLIP_RISK",
        re.compile(r"\benable(?:d)?\b|有効|启用", re.IGNORECASE),
        re.compile(r"\bdisable(?:d)?\b|無効|禁用", re.IGNORECASE),
        re.compile(r"(?<!비)활성화|사용함|켜"),
        re.compile(r"비활성화|사용하지|꺼"),
    ),
    (
        "OPEN_CLOSE_FLIP_RISK",
        re.compile(r"\bopen\b|開|开|열", re.IGNORECASE),
        re.compile(r"\bclos(?:e|ed)\b|閉|关|닫", re.IGNORECASE),
        re.compile(r"열|개방"),
        re.compile(r"닫|폐쇄"),
    ),
    (
        "LOCK_UNLOCK_FLIP_RISK",
        re.compile(r"(?<!un)\block(?:ed)?\b|施錠|锁定", re.IGNORECASE),
        re.compile(r"\bunlock(?:ed)?\b|解錠|解锁", re.IGNORECASE),
        re.compile(r"잠금(?!\s*해제)"),
        re.compile(r"잠금\s*해제|해금"),
    ),
]


def validate_risks(
    source: str,
    translation: str,
    config: ValidatorsConfig,
    policy: Mapping[str, object] | None = None,
) -> ValidationResult:
    result = ValidationResult()

    if config.detect_number_risk:
        source_numbers = _NUMBER_RE.findall(source)
        translated_numbers = _NUMBER_RE.findall(translation)
        if source_numbers or translated_numbers:
            code = "NUMBER_MISMATCH_RISK" if source_numbers != translated_numbers else "NUMBER_PRESENT_RISK"
            severity = (
                policy_severity(
                    policy, "number_mismatch_severity", ValidationSeverity.RISK
                )
                if code == "NUMBER_MISMATCH_RISK"
                else ValidationSeverity.RISK
            )
            result.add(
                code,
                severity,
                "Numbers require meaning-preservation review",
                "risk",
                source_numbers=source_numbers,
                translation_numbers=translated_numbers,
            )

    if config.detect_negation_risk:
        source_negative = bool(_SOURCE_NEGATION_RE.search(source))
        translated_negative = bool(_KOREAN_NEGATION_RE.search(translation))
        if source_negative != translated_negative:
            result.add(
                "NEGATION_FLIP_RISK",
                policy_severity(
                    policy, "negation_flip_severity", ValidationSeverity.RISK
                ),
                "Source and translation may disagree on negation",
                "risk",
                source_negative=source_negative,
                translation_negative=translated_negative,
            )

    if config.detect_direction_risk or config.detect_state_flip_risk:
        for code, source_a, source_b, translated_a, translated_b in _CONCEPT_PAIRS:
            source_has_a = bool(source_a.search(source))
            source_has_b = bool(source_b.search(source))
            translation_has_a = bool(translated_a.search(translation))
            translation_has_b = bool(translated_b.search(translation))
            flipped = (source_has_a and translation_has_b and not translation_has_a) or (
                source_has_b and translation_has_a and not translation_has_b
            )
            if flipped:
                result.add(
                    code,
                    policy_severity(
                        policy, "concept_flip_severity", ValidationSeverity.RISK
                    ),
                    "A direction/state concept may have been reversed",
                    "risk",
                )

    proper_nouns = _PROPER_NOUN_RE.findall(source)
    missing_names = [name for name in proper_nouns if name not in translation]
    if missing_names:
        result.add(
            "PROPER_NOUN_CHANGE_RISK",
            ValidationSeverity.RISK,
            "Capitalized source names changed or were transliterated",
            "risk",
            names=missing_names,
        )
    return result
