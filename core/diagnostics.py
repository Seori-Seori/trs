from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.segment import Segment, ValidationSeverity


def _bounded(value: str, limit: int = 8000) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    marker = "\n... [중간 생략] ...\n"
    remaining = max(0, limit - len(marker))
    head = (remaining * 3) // 4
    tail = remaining - head
    return value[:head] + marker + (value[-tail:] if tail else ""), True


class FailureDebugStore:
    """Persist only the latest bounded failed exchange for each Segment."""

    def __init__(self, path: str | Path, *, mode: str) -> None:
        self.path = Path(path)
        self.mode = mode
        self.entries: dict[str, dict[str, Any]] = {}
        self.last_write_error: str | None = None

    def reset(self) -> None:
        self.entries.clear()
        self._write()

    def record(self, segment: Segment) -> None:
        source, source_truncated = _bounded(segment.source)
        error_codes = sorted(
            {
                issue.code
                for issue in segment.validation_issues
                if issue.severity == ValidationSeverity.ERROR
            }
        )
        self.entries[segment.id] = {
            "segment_id": segment.id,
            "status": segment.status.value,
            "attempt_count": segment.attempt_count,
            "mode": self.mode,
            "request_mode": segment.last_request_mode,
            "source": source,
            "source_truncated": source_truncated,
            "rendered_prompt": segment.last_prompt,
            "prompt_truncated": segment.last_prompt_truncated,
            "raw_model_response": segment.last_raw_response,
            "raw_response_truncated": segment.last_raw_response_truncated,
            "error_codes": error_codes,
        }
        self._write()

    def clear(self, segment: Segment) -> None:
        if self.entries.pop(segment.id, None) is not None:
            self._write()

    def _write(self) -> None:
        value = {
            "version": "7.0",
            "protocol": "hy-mt-native-single",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode,
            "failed_exchanges": list(self.entries.values()),
        }
        temporary_name: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temporary_name = handle.name
            Path(temporary_name).replace(self.path)
            self.last_write_error = None
        except OSError as exc:
            self.last_write_error = str(exc)
        finally:
            if temporary_name is not None:
                temporary = Path(temporary_name)
                try:
                    if temporary.exists():
                        temporary.unlink()
                except OSError:
                    pass
