from __future__ import annotations

import json
from typing import Any

from core.segment import Segment, SegmentStatus


class PromptBuildError(ValueError):
    pass


def _context_block(segment: Segment) -> str:
    payload = {
        "before": segment.context_before,
        "after": segment.context_after,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


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

    profile_instructions = profile.get("instructions", [])
    if not isinstance(profile_instructions, list):
        raise PromptBuildError("Profile instructions must be a list")

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
    lines.extend(str(instruction) for instruction in profile_instructions)
    lines.append("<<<CONTEXT>>>")
    for segment in segments:
        lines.append(f"{segment.id}\t{_context_block(segment)}")
    lines.append("<<<END_CONTEXT>>>")
    lines.append("<<<TARGETS>>>")
    for segment in segments:
        prepared = segment.prepared_source if segment.prepared_source is not None else segment.source
        if "\n" in prepared or "\r" in prepared or "\t" in prepared:
            raise PromptBuildError(
                f"Prepared source for {segment.id} contains an unprotected tab/newline"
            )
        lines.append(f"{segment.id}\t{segment.source_language}\t{prepared}")
    lines.append("<<<END_TARGETS>>>")
    lines.append("이제 위 대상 ID와 번역만 출력하십시오.")
    return "\n".join(lines)
