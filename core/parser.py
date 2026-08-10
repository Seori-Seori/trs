from __future__ import annotations

import re
from dataclasses import dataclass, field


_ROW_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_:-]*)\t(.*)$")
_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*\s*$")


@dataclass(frozen=True)
class MalformedLine:
    line_number: int
    text: str


@dataclass(frozen=True)
class ParsedResponse:
    rows: dict[str, str]
    row_order: list[str]
    duplicates: list[str]
    malformed_lines: list[MalformedLine]
    raw_response: str
    code_fence_removed: bool = False
    blank_line_count: int = 0


class ResponseParser:
    """Parse one model response once into an immutable reusable result."""

    def parse(self, raw_response: str) -> ParsedResponse:
        raw = raw_response.lstrip("\ufeff")
        enumerated = list(enumerate(raw.splitlines(), start=1))
        nonblank_indexes = [index for index, (_number, line) in enumerate(enumerated) if line.strip()]
        code_fence_removed = False

        if len(nonblank_indexes) >= 2:
            first = nonblank_indexes[0]
            last = nonblank_indexes[-1]
            if _FENCE_RE.fullmatch(enumerated[first][1].strip()) and enumerated[last][1].strip() == "```":
                enumerated = enumerated[:first] + enumerated[first + 1:last] + enumerated[last + 1:]
                code_fence_removed = True

        rows: dict[str, str] = {}
        row_order: list[str] = []
        duplicates: list[str] = []
        malformed: list[MalformedLine] = []
        blank_line_count = 0

        for line_number, line in enumerated:
            if not line.strip():
                blank_line_count += 1
                continue
            match = _ROW_RE.fullmatch(line)
            if not match:
                malformed.append(MalformedLine(line_number=line_number, text=line))
                continue
            row_id, translation = match.groups()
            row_order.append(row_id)
            if row_id in rows:
                duplicates.append(row_id)
                continue
            rows[row_id] = translation

        return ParsedResponse(
            rows=rows,
            row_order=row_order,
            duplicates=duplicates,
            malformed_lines=malformed,
            raw_response=raw_response,
            code_fence_removed=code_fence_removed,
            blank_line_count=blank_line_count,
        )
