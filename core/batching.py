from __future__ import annotations

import re
from dataclasses import dataclass

from core.placeholders import PlaceholderEngine
from core.segment import Segment, SegmentStatus


@dataclass(frozen=True)
class SplitPiece:
    text: str
    separator_after: str = ""


def build_batches(
    segments: list[Segment],
    *,
    batch_size: int,
    max_batch_chars: int,
) -> list[list[Segment]]:
    batches: list[list[Segment]] = []
    current: list[Segment] = []
    current_chars = 0

    for segment in segments:
        if segment.status == SegmentStatus.VALID:
            raise RuntimeError(f"Invariant violation: VALID segment {segment.id} entered batching")
        segment_chars = len(segment.prepared_source or segment.source)
        would_overflow = current and (
            len(current) >= batch_size or current_chars + segment_chars > max_batch_chars
        )
        if would_overflow:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(segment)
        current_chars += segment_chars

    if current:
        batches.append(current)
    return batches


_BOUNDARY_RE = re.compile(
    r"(?P<punct>[。！？!?…]+|(?<!\d)[.!?](?!\d)|[，、,;；:：])(?P<after>[ \t]+)?|(?P<space>[ \t]+)"
)


def _inside_protected(position: int, spans: list[tuple[int, int, str]]) -> tuple[int, int] | None:
    for start, end, _kind in spans:
        if start < position < end:
            return start, end
    return None


def safe_split_with_separators(
    text: str,
    max_chars: int,
    placeholder_engine: PlaceholderEngine,
) -> list[SplitPiece]:
    """Split long text without cutting protected values and retain exact boundary whitespace."""
    if len(text) <= max_chars:
        return [SplitPiece(text)]

    spans = placeholder_engine.protected_spans(text)
    pieces: list[SplitPiece] = []
    start = 0

    while len(text) - start > max_chars:
        limit = min(len(text), start + max_chars)
        protected = _inside_protected(limit, spans)
        if protected:
            protected_start, protected_end = protected
            if protected_start - start >= max(1, max_chars // 3):
                limit = protected_start
            else:
                limit = protected_end

        candidates = []
        for match in _BOUNDARY_RE.finditer(text, start, limit + 1):
            boundary = match.end()
            if boundary - start < max(1, max_chars // 3):
                continue
            if _inside_protected(boundary, spans):
                continue
            candidates.append(match)

        separator = ""
        if candidates:
            match = candidates[-1]
            if match.group("space") is not None:
                text_end = match.start()
                next_start = match.end()
                separator = match.group("space")
            else:
                after = match.group("after") or ""
                text_end = match.end() - len(after)
                next_start = match.end()
                separator = after
        else:
            text_end = limit
            next_start = limit

        if text_end <= start:
            text_end = min(len(text), max(start + 1, limit))
            next_start = text_end
            separator = ""

        pieces.append(SplitPiece(text=text[start:text_end], separator_after=separator))
        start = next_start

    if start < len(text):
        pieces.append(SplitPiece(text=text[start:]))
    if not pieces:
        return [SplitPiece(text)]
    reconstructed = "".join(piece.text + piece.separator_after for piece in pieces)
    if reconstructed != text:
        raise AssertionError("Safe split failed to preserve source text exactly")
    return pieces
