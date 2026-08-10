from __future__ import annotations

import re
from collections.abc import Mapping

from core.config import ValidatorsConfig
from core.segment import ValidationResult, ValidationSeverity
from validators.policy import policy_severity


_NUMBER_RE = re.compile(r"(?<!\w)[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?!\w)")
_SOURCE_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|without|cannot|can't|don't|doesn't|do\s+not)\b|"
    r"(?:ではない|じゃない|ない|ません|できない|無い)|"
    r"(?:没有|沒有|不能|不会|不會|不要|不是|不想|不再|不敢|不可|不愿|不願|"
    r"不该|不該|从未|從未|别|別|无法|無法)",
    re.IGNORECASE,
)
_KOREAN_NEGATION_RE = re.compile(
    r"않|아니|없|못|금지|불가|(?:^|\s)안(?:\s|$)|지\s*마|말(?:아|고|라|세요)"
)
_SOURCE_EXPLICIT_AFFIRMATIVE_ACTION_RE = re.compile(
    r"\b(?:skip|go|continue|save|open|close|enable|allow|accept|confirm)\b|"
    r"(?:スキップ|続行|保存|有効|許可)|"
    r"(?:跳过|跳過|继续|繼續|保存|启用|啟用|允许|允許|可以|必须|必須)",
    re.IGNORECASE,
)
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")


_CONCEPT_PAIRS = [
    (
        "YES_NO_FLIP_RISK",
        re.compile(
            r"\b(?:yes|ok|true)\b|はい|"
            r"(?:^|[\s「『“\"，,:：])(?:是|对|對|好的?)(?=$|[\s」』”\"，,。.!?！？])",
            re.IGNORECASE,
        ),
        re.compile(r"\b(?:no|false)\b|いいえ|(?:不是|不对|不對|否定)", re.IGNORECASE),
        re.compile(r"(?:^|\s)(?:예|네|맞습니다|확인)(?:$|[\s.!?])"),
        re.compile(r"아니|아니요|거절|틀렸"),
    ),
    (
        "UP_DOWN_FLIP_RISK",
        re.compile(r"\bup\b|上へ|上方向|上がる|向上|上升|抬高", re.IGNORECASE),
        re.compile(r"\bdown\b|下へ|下方向|下がる|向下|下降|降低", re.IGNORECASE),
        re.compile(r"위로|위쪽|상승|올리"),
        re.compile(r"아래로|아래쪽|하강|내리"),
    ),
    (
        "LEFT_RIGHT_FLIP_RISK",
        re.compile(r"\bleft\b|左へ|左側|左边|左邊|左方|向左", re.IGNORECASE),
        re.compile(r"\bright\b|右へ|右側|右边|右邊|右方|向右", re.IGNORECASE),
        re.compile(r"왼쪽?|좌측"),
        re.compile(r"오른쪽?|우측"),
    ),
    (
        "BEFORE_AFTER_FLIP_RISK",
        re.compile(r"\bbefore\b|前に|以前|之前|此前|从前|從前", re.IGNORECASE),
        re.compile(r"\bafter\b|後に|以後|之后|之後|以后|以後|随后|隨後", re.IGNORECASE),
        re.compile(r"이전|전에|앞서|예전"),
        re.compile(r"이후|후에|뒤에|그 뒤"),
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
        re.compile(r"\bopen\b|開ける|打开|打開|开启|開啟", re.IGNORECASE),
        re.compile(r"\bclos(?:e|ed)\b|閉じる|关闭|關閉", re.IGNORECASE),
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
        explicit_positive_action = bool(
            _SOURCE_EXPLICIT_AFFIRMATIVE_ACTION_RE.search(source)
        )
        negation_mismatch = (
            source_negative and not translated_negative
        ) or (
            translated_negative
            and not source_negative
            and explicit_positive_action
        )
        if negation_mismatch:
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
