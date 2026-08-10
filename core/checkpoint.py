from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.hashing import sha256_text
from core.segment import Segment, SegmentStatus


@dataclass(frozen=True)
class CheckpointRecord:
    segment_id: str
    source_hash: str
    source_text: str
    status: str
    translation: str | None
    attempt_count: int
    last_error: str | None
    repaired: bool
    issues_json: str
    updated_at: str


class CheckpointStore:
    def __init__(self, path: str | Path, *, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self._connection: sqlite3.Connection | None = None
        if enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.path)
            self._connection.row_factory = sqlite3.Row
            self._initialize()

    def _initialize(self) -> None:
        assert self._connection is not None
        self._connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS segments (
                segment_id TEXT PRIMARY KEY,
                source_hash TEXT NOT NULL,
                source_text TEXT NOT NULL,
                status TEXT NOT NULL,
                translation TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                repaired INTEGER NOT NULL DEFAULT 0,
                issues_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_segments_hash_status
                ON segments(source_hash, status);
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._connection.commit()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "CheckpointStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def load_valid(self, segment: Segment) -> CheckpointRecord | None:
        if not self.enabled or self._connection is None:
            return None
        row = self._connection.execute(
            """
            SELECT segment_id, source_hash, source_text, status, translation,
                   attempt_count, last_error, repaired, issues_json, updated_at
            FROM segments
            WHERE segment_id = ? AND source_hash = ? AND status = 'VALID'
            """,
            (segment.id, sha256_text(segment.source)),
        ).fetchone()
        if row is None or row["translation"] is None:
            return None
        return CheckpointRecord(
            segment_id=row["segment_id"],
            source_hash=row["source_hash"],
            source_text=row["source_text"],
            status=row["status"],
            translation=row["translation"],
            attempt_count=int(row["attempt_count"]),
            last_error=row["last_error"],
            repaired=bool(row["repaired"]),
            issues_json=row["issues_json"],
            updated_at=row["updated_at"],
        )

    def save_segment(self, segment: Segment) -> None:
        if not self.enabled or self._connection is None:
            return
        issues = [
            {
                "code": issue.code,
                "severity": issue.severity.value,
                "message": issue.message,
                "validator": issue.validator,
                "details": issue.details,
            }
            for issue in segment.validation_issues
        ]
        updated_at = datetime.now(timezone.utc).isoformat()
        self._connection.execute(
            """
            INSERT INTO segments (
                segment_id, source_hash, source_text, status, translation,
                attempt_count, last_error, repaired, issues_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(segment_id) DO UPDATE SET
                source_hash=excluded.source_hash,
                source_text=excluded.source_text,
                status=excluded.status,
                translation=excluded.translation,
                attempt_count=excluded.attempt_count,
                last_error=excluded.last_error,
                repaired=excluded.repaired,
                issues_json=excluded.issues_json,
                updated_at=excluded.updated_at
            """,
            (
                segment.id,
                sha256_text(segment.source),
                segment.source,
                segment.status.value,
                segment.translation,
                segment.attempt_count,
                segment.last_error,
                int(segment.was_repaired),
                json.dumps(issues, ensure_ascii=False),
                updated_at,
            ),
        )
        self._connection.commit()

    def valid_count(self) -> int:
        if not self.enabled or self._connection is None:
            return 0
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM segments WHERE status = 'VALID'"
        ).fetchone()
        return int(row["count"])

    def set_metadata(self, key: str, value: str) -> None:
        if not self.enabled or self._connection is None:
            return
        self._connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        self._connection.commit()
