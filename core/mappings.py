from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.checkpoint import CheckpointStore


_MAPPING_METADATA_KEY = "job_mappings_v1"
_MAPPING_HASH_METADATA_KEY = "job_mappings_sha256"
_SUPPORTED_KINDS = {"name", "map"}


class JobMappingError(ValueError):
    pass


@dataclass(frozen=True)
class JobMappingEntry:
    source: str
    target: str
    kind: str

    @property
    def placeholder_prefix(self) -> str:
        return "NAME" if self.kind == "name" else "MAP"


@dataclass(frozen=True)
class JobMappings:
    entries: tuple[JobMappingEntry, ...] = ()

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for entry in self.entries:
            if entry.kind not in _SUPPORTED_KINDS:
                raise JobMappingError(f"Unsupported mapping kind: {entry.kind!r}")
            if not entry.source.strip() or not entry.target.strip():
                raise JobMappingError("Job mapping source and target must be non-empty")
            if any(character in entry.source for character in "\r\n\t"):
                raise JobMappingError(
                    "Job mapping source must not contain tabs or newlines"
                )
            if any(character in entry.target for character in "\r\n\t"):
                raise JobMappingError(
                    "Job mapping target must not contain tabs or newlines"
                )
            if entry.source in seen:
                raise JobMappingError(
                    f"Job mapping source must be unique: {entry.source!r}"
                )
            seen.add(entry.source)

    @property
    def is_empty(self) -> bool:
        return not self.entries

    def to_data(self) -> dict[str, Any]:
        names = {
            entry.source: entry.target
            for entry in self.entries
            if entry.kind == "name"
        }
        mappings = {
            entry.source: entry.target
            for entry in self.entries
            if entry.kind == "map"
        }
        return {"version": 1, "names": names, "mappings": mappings}

    def to_json(self) -> str:
        return json.dumps(
            self.to_data(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_data(cls, raw: object) -> "JobMappings":
        if not isinstance(raw, dict):
            raise JobMappingError("Job mapping root must be a JSON object")
        unknown = set(raw).difference({"version", "names", "mappings"})
        if unknown:
            raise JobMappingError(
                f"Unknown job mapping fields: {sorted(unknown)!r}"
            )
        version = raw.get("version", 1)
        if version != 1:
            raise JobMappingError("Job mapping version must be 1")

        entries: list[JobMappingEntry] = []
        for field, kind in (("names", "name"), ("mappings", "map")):
            values = raw.get(field, {})
            if not isinstance(values, dict):
                raise JobMappingError(f"Job mapping {field!r} must be an object")
            for source, target in values.items():
                if not isinstance(source, str) or not isinstance(target, str):
                    raise JobMappingError(
                        f"Job mapping {field!r} must map strings to strings"
                    )
                entries.append(JobMappingEntry(source, target, kind))

        entries.sort(key=lambda entry: (entry.kind, entry.source))
        return cls(tuple(entries))

    @classmethod
    def from_json(cls, text: str) -> "JobMappings":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise JobMappingError(f"Invalid job mapping JSON: {exc}") from exc
        return cls.from_data(raw)


def load_job_mappings(path: str | Path) -> JobMappings:
    mapping_path = Path(path)
    try:
        text = mapping_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise JobMappingError(f"Job mapping file not found: {mapping_path}") from exc
    except OSError as exc:
        raise JobMappingError(f"Cannot read job mapping file {mapping_path}: {exc}") from exc
    return JobMappings.from_json(text)


def resolve_job_mappings(
    checkpoint: CheckpointStore,
    provided: JobMappings | None,
    *,
    resume: bool,
) -> JobMappings:
    """Bind mappings to a checkpoint so resume cannot silently change them."""
    stored_json = checkpoint.get_metadata(_MAPPING_METADATA_KEY)
    stored = JobMappings.from_json(stored_json) if stored_json is not None else None

    if not resume:
        resolved = provided or JobMappings()
    elif stored is None:
        resolved = provided or JobMappings()
        if not resolved.is_empty and checkpoint.segment_count() > 0:
            raise JobMappingError(
                "Existing checkpoint has no persisted job mapping. "
                "Use --no-resume before adding one."
            )
    elif provided is None:
        resolved = stored
    elif provided.to_json() != stored.to_json():
        raise JobMappingError(
            "Provided job mapping differs from the checkpoint mapping. "
            "Use the original mapping or start with --no-resume."
        )
    else:
        resolved = provided

    checkpoint.set_metadata(_MAPPING_METADATA_KEY, resolved.to_json())
    checkpoint.set_metadata(_MAPPING_HASH_METADATA_KEY, resolved.sha256)
    return resolved
