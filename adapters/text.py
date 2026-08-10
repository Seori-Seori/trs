from __future__ import annotations

import codecs
import hashlib
import re
import shutil
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.segment import Segment, SegmentStatus

from .base import Adapter


_NEWLINE_TOKEN = r"(?:\r\n|(?<!\r)\n|\r(?!\n))"
_NEWLINE_RE = re.compile(_NEWLINE_TOKEN)
_PARAGRAPH_SEPARATOR_RE = re.compile(
    rf"({_NEWLINE_TOKEN}[ \t]*{_NEWLINE_TOKEN}(?:(?:[ \t]*){_NEWLINE_TOKEN})*)"
)


@dataclass(frozen=True)
class TextPart:
    segment_id: str
    prefix: str = ""
    suffix: str = ""


@dataclass(frozen=True)
class Separator:
    text: str


@dataclass
class TextDocument:
    path: Path
    parts: list[TextPart | Separator]
    has_bom: bool
    newline_style: str
    source_sha256: str


class TextAdapter(Adapter):
    def load(self, path: str | Path) -> tuple[TextDocument, list[Segment]]:
        input_path = Path(path)
        raw = input_path.read_bytes()
        has_bom = raw.startswith(codecs.BOM_UTF8)
        try:
            text = raw.decode("utf-8-sig" if has_bom else "utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise UnicodeError(
                f"{input_path} is not valid UTF-8. v7.0 does not guess CP949/Shift-JIS."
            ) from exc

        newline_style = self._detect_newline_style(text)
        raw_parts = _PARAGRAPH_SEPARATOR_RE.split(text)
        parts: list[TextPart | Separator] = []
        segments: list[Segment] = []

        for raw_part in raw_parts:
            if raw_part == "":
                continue
            if _PARAGRAPH_SEPARATOR_RE.fullmatch(raw_part) or not raw_part.strip():
                parts.append(Separator(raw_part))
                continue

            prefix_match = re.match(r"^[ \t]*", raw_part)
            suffix_match = re.search(r"[ \t]*$", raw_part)
            prefix = prefix_match.group(0) if prefix_match else ""
            suffix = suffix_match.group(0) if suffix_match else ""
            end = len(raw_part) - len(suffix) if suffix else len(raw_part)
            source = raw_part[len(prefix):end]
            if not source:
                parts.append(Separator(raw_part))
                continue

            segment_id = f"SEG_{len(segments) + 1:08d}"
            part_index = len(parts)
            segment = Segment(
                id=segment_id,
                source=source,
                file_path=str(input_path),
                location=part_index,
                metadata={
                    "adapter": "text",
                    "part_index": part_index,
                    "prefix": prefix,
                    "suffix": suffix,
                },
            )
            segments.append(segment)
            parts.append(TextPart(segment_id=segment_id, prefix=prefix, suffix=suffix))

        return (
            TextDocument(
                path=input_path,
                parts=parts,
                has_bom=has_bom,
                newline_style=newline_style,
                source_sha256=hashlib.sha256(raw).hexdigest(),
            ),
            segments,
        )

    @staticmethod
    def _detect_newline_style(text: str) -> str:
        crlf = text.count("\r\n")
        without_crlf = text.replace("\r\n", "")
        lf = without_crlf.count("\n")
        cr = without_crlf.count("\r")
        if crlf >= lf and crlf >= cr and crlf:
            return "\r\n"
        if lf >= cr and lf:
            return "\n"
        if cr:
            return "\r"
        return "\n"

    def save(
        self,
        document: TextDocument,
        segments: list[Segment],
        output_path: str | Path,
    ) -> Path:
        output = Path(output_path)
        if output.resolve() == document.path.resolve():
            raise ValueError("Refusing to overwrite the source TXT in v7.0")
        current_hash = hashlib.sha256(document.path.read_bytes()).hexdigest()
        if current_hash != document.source_sha256:
            raise RuntimeError(
                "Source TXT changed after it was loaded; refusing unsafe reconstruction"
            )
        by_id = {segment.id: segment for segment in segments}
        pieces: list[str] = []
        for part in document.parts:
            if isinstance(part, Separator):
                pieces.append(part.text)
                continue
            segment = by_id[part.segment_id]
            value = (
                segment.translation
                if segment.status == SegmentStatus.VALID and segment.translation is not None
                else segment.source
            )
            pieces.append(part.prefix + value + part.suffix)

        encoded = "".join(pieces).encode("utf-8")
        if document.has_bom:
            encoded = codecs.BOM_UTF8 + encoded
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=output.parent, prefix=f".{output.name}.", suffix=".tmp", delete=False
            ) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                temporary_name = handle.name
            Path(temporary_name).replace(output)
        finally:
            if temporary_name is not None:
                temporary = Path(temporary_name)
                if temporary.exists():
                    temporary.unlink()
        return output

    def backup(self, path: str | Path, backup_dir: str | Path | None = None) -> Path:
        source = Path(path)
        destination_dir = Path(backup_dir) if backup_dir else source.parent / "backup"
        destination_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = destination_dir / f"{source.stem}.{stamp}{source.suffix}"
        shutil.copy2(source, destination)
        return destination
