from __future__ import annotations

import json
from typing import Any

from core.segment import Segment, SegmentStatus, ValidationSeverity
from core.terminology import matching_novel_term_hints


class PromptBuildError(ValueError):
    pass


_REPAIR_HINTS: dict[str, str] = {
    "DUPLICATE_ID": "같은 출력 행을 반복하지 마십시오.",
    "MISSING_PLACEHOLDER": "모든 보호 토큰을 정확히 한 번씩 원래 순서로 유지하십시오.",
    "DUPLICATE_PLACEHOLDER": "보호 토큰을 복제하지 마십시오.",
    "UNEXPECTED_PLACEHOLDER": "원문에 없는 보호 토큰을 만들지 마십시오.",
    "PLACEHOLDER_ORDER_MISMATCH": "보호 토큰의 원래 순서를 유지하십시오.",
    "KNOWN_BAD_CJK_RESIDUE": "의도적으로 보호된 값을 제외하고 한국어만 출력하십시오.",
    "KANA_RESIDUE": "의도적으로 보호된 값을 제외하고 한국어만 출력하십시오.",
    "EXCESSIVE_CJK_RESIDUE": "의도적으로 보호된 값을 제외하고 한국어만 출력하십시오.",
    "ROW_ID_LEAK": "이 대상 ID와 번역 한 줄만 출력하고 다른 ID를 본문에 넣지 마십시오.",
    "MULTIPLE_ROWS_MERGED": "현재 대상의 번역만 한 줄로 출력하십시오.",
    "EMPTY_TRANSLATION": "원문의 내용을 생략하지 말고 한국어 번역을 출력하십시오.",
    "MISSING_ID": "요청된 대상 ID를 빠뜨리지 마십시오.",
    "PROMPT_LEAK": "지시문이나 설명을 복사하지 말고 번역만 출력하십시오.",
    "UNBALANCED_DELIMITERS": "원문의 괄호와 구분자 구조를 빠짐없이 균형 있게 유지하십시오.",
    "UNBALANCED_QUOTES": "원문의 인용부호 구조를 빠짐없이 균형 있게 유지하십시오.",
    "BRACKET_STRUCTURE_LOSS": "원문의 모든 괄호와 괄호 안 내용을 생략하지 말고 빠짐없이 번역하십시오.",
    "BRACKET_STRUCTURE_PARTIAL_LOSS": "원문의 모든 괄호와 괄호 안 내용을 빠짐없이 번역하십시오.",
    "PARENTHETICAL_CONTENT_LOSS": "각 괄호 안의 내용까지 생략하지 말고 모두 번역하십시오.",
    "QUOTE_STRUCTURE_LOSS": "원문에 있는 인용부호 쌍을 번역에서도 유지하십시오.",
    "NOVEL_CJK_RESIDUE": "고유명사 whitelist 외의 중국어 한자를 남기지 말고 완전한 한국어로 번역하십시오.",
    "TRUNCATED_OUTPUT": "문장을 중간에서 끊지 말고 원문의 끝까지 완전한 한국어 문장으로 번역하십시오.",
    "NOVEL_TERM_MISTRANSLATION": "현재 원문에 제시된 용어 참고를 따라 성별·대상·의미 범주를 정확히 번역하십시오.",
}


def _context_list(items: list[str]) -> str:
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def _profile_instructions(profile: dict[str, Any]) -> list[str]:
    instructions = profile.get("instructions", [])
    if not isinstance(instructions, list):
        raise PromptBuildError("Profile instructions must be a list")
    return [str(instruction) for instruction in instructions]


def _prepared_source(segment: Segment) -> str:
    return (
        segment.prepared_source
        if segment.prepared_source is not None
        else segment.source
    )


def build_single_translation_prompt(
    segment: Segment,
    profile: dict[str, Any],
    *,
    mode: str = "single",
) -> str:
    """Build the default HY-MT prompt without exposing program Segment identity."""
    if segment.status == SegmentStatus.VALID:
        raise RuntimeError(
            f"Invariant violation: VALID segment {segment.id} entered prompt builder"
        )

    mode_instruction = {
        "single": "아래 원문 하나를 문맥을 참고하여 한국어로 번역하십시오.",
        "repair": "이전 응답은 검증에 실패했습니다. 아래 원문 하나를 새로 번역하십시오.",
        "split": "긴 문단에서 안전하게 분리된 아래 원문 조각 하나를 번역하십시오.",
    }.get(mode, "아래 원문 하나를 한국어로 번역하십시오.")

    lines = [
        "Translate exactly one source text into Korean.",
        "당신은 일본어·중국어·영어를 자연스러운 한국어로 옮기는 전문 번역가입니다.",
        mode_instruction,
        "목표 언어는 반드시 한국어입니다.",
        "원문의 의미, 사실, 수치, 고유명사, 부정과 상태를 추가·삭제·반전하지 마십시오.",
        "대괄호 두 개로 감싼 보호 토큰은 글자 하나도 바꾸거나 옮기거나 복제하지 마십시오.",
        "문맥은 번역 판단에만 사용하고 문맥 문장은 출력하지 마십시오.",
        "번역문만 출력하고 설명, 머리말, 표식, Markdown 코드 블록을 출력하지 마십시오.",
    ]
    lines.extend(_profile_instructions(profile))
    terminology_hints = matching_novel_term_hints(
        segment.source, segment.source_language, profile
    )
    if terminology_hints:
        lines.append("용어 참고(현재 원문에 실제 등장한 항목만):")
        lines.extend(
            f"- {hint.source}: {hint.prompt_hint}"
            for hint in terminology_hints
        )
    lines.extend(
        [
            "<<<REFERENCE_CONTEXT>>>",
            f"앞 문맥: {_context_list(segment.context_before)}",
            f"뒤 문맥: {_context_list(segment.context_after)}",
            "<<<END_REFERENCE_CONTEXT>>>",
        ]
    )

    if mode == "repair":
        codes = sorted(
            {
                issue.code
                for issue in segment.validation_issues
                if issue.severity == ValidationSeverity.ERROR
            }
        )
        if codes:
            lines.append(f"이전 응답 실패 이유: {','.join(codes)}")
            hints = list(
                dict.fromkeys(
                    _REPAIR_HINTS[code]
                    for code in codes
                    if code in _REPAIR_HINTS
                )
            )
            if hints:
                lines.append(f"수정 지침: {' '.join(hints)}")

    lines.extend(
        [
            f"원문 언어: {segment.source_language}",
            "<<<SOURCE>>>",
            _prepared_source(segment),
            "<<<END_SOURCE>>>",
            "위 원문의 한국어 번역문만 출력하십시오.",
        ]
    )
    return "\n".join(lines)


def build_prompt(
    segments: list[Segment],
    profile: dict[str, Any],
    *,
    mode: str = "batch",
) -> str:
    if not segments:
        raise PromptBuildError("Cannot build a prompt without target segments")
    if any(segment.status == SegmentStatus.VALID for segment in segments):
        ids = [segment.id for segment in segments if segment.status == SegmentStatus.VALID]
        raise RuntimeError(f"Invariant violation: VALID segments entered prompt builder: {ids}")

    profile_instructions = _profile_instructions(profile)

    mode_instruction = {
        "batch": "아래 번역 대상만 번역하십시오.",
        "repair": "이전 응답에서 검증에 실패한 대상만 원문과 문맥을 바탕으로 새로 번역하십시오.",
        "single": "아래 한 대상만 원문과 문맥을 바탕으로 번역하십시오.",
        "split": "긴 문단에서 분리된 아래 조각만 번역하십시오.",
    }.get(mode, "아래 번역 대상만 번역하십시오.")

    lines = [
        "Translate the following segments into Korean, without additional explanation.",
        "당신은 일본어·중국어·영어를 자연스러운 한국어로 옮기는 전문 번역가입니다.",
        mode_instruction,
        "목표 언어는 반드시 한국어입니다.",
        "원문의 의미, 사실, 수치, 고유명사, 부정과 상태를 추가·삭제·반전하지 마십시오.",
        "대괄호 두 개로 감싼 보호 토큰은 글자 하나도 바꾸거나 옮기거나 복제하지 마십시오.",
        "문맥은 번역 판단에만 사용하고 문맥 문장은 출력하지 마십시오.",
        "각 대상마다 정확히 한 줄만 출력하십시오.",
        "출력 형식은 대상 ID, 탭 문자 1개, 한국어 번역입니다.",
        "설명, 머리말, Markdown 코드 블록을 출력하지 마십시오.",
    ]
    lines.extend(profile_instructions)
    for ordinal, segment in enumerate(segments, start=1):
        prepared = _prepared_source(segment)
        if "\n" in prepared or "\r" in prepared or "\t" in prepared:
            raise PromptBuildError(
                f"Prepared source for {segment.id} contains an unprotected tab/newline"
            )

        lines.append(f"<<<ITEM {ordinal}>>>")
        lines.append("문맥은 번역 판단에만 사용하고 출력하지 마십시오.")
        lines.append(f"앞 문맥: {_context_list(segment.context_before)}")
        lines.append(f"뒤 문맥: {_context_list(segment.context_after)}")
        if mode in {"repair", "single"}:
            codes = sorted(
                {
                    issue.code
                    for issue in segment.validation_issues
                    if issue.severity == ValidationSeverity.ERROR
                }
            )
            if codes:
                lines.append(f"이전 응답 실패 이유: {','.join(codes)}")
                hints = list(
                    dict.fromkeys(
                        _REPAIR_HINTS[code]
                        for code in codes
                        if code in _REPAIR_HINTS
                    )
                )
                if hints:
                    lines.append(f"수정 지침: {' '.join(hints)}")
        lines.append("번역 대상:")
        lines.append(f"{segment.id}\t{segment.source_language}\t{prepared}")
        lines.append(f"<<<END_ITEM {ordinal}>>>")
    lines.append("이제 위 대상 ID와 번역만 출력하십시오.")
    return "\n".join(lines)
