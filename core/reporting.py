from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.hashing import sha256_text
from core.pipeline import PipelineResult
from core.segment import Segment, ValidationIssue, ValidationSeverity


def _issue_to_dict(issue: ValidationIssue) -> dict[str, Any]:
    return {
        "code": issue.code,
        "severity": issue.severity.value,
        "message": issue.message,
        "validator": issue.validator,
        "details": issue.details,
    }


def build_qa_report(
    pipeline_result: PipelineResult,
    *,
    source_path: str | Path,
    source_sha256: str,
    output_path: str | Path,
    model: str,
    backup_path: str | Path,
    mode: str = "novel",
    profile: str = "novel",
) -> dict[str, Any]:
    segments = pipeline_result.segments
    risk_count = sum(
        issue.severity == ValidationSeverity.RISK
        for segment in segments
        for issue in segment.validation_issues
    )
    warning_count = sum(
        issue.severity == ValidationSeverity.WARNING
        for segment in segments
        for issue in segment.validation_issues
    )
    failed = pipeline_result.failed
    return {
        "version": "7.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_file": str(Path(source_path).resolve()),
        "source_sha256": source_sha256,
        "output_file": str(Path(output_path).resolve()),
        "backup_file": str(Path(backup_path).resolve()),
        "model": model,
        "mode": mode,
        "profile": profile,
        "summary": {
            "total": len(segments),
            "valid": len(pipeline_result.valid),
            "repaired": sum(segment.was_repaired for segment in segments),
            "failed": len(failed),
            "risks": risk_count,
            "warnings": warning_count,
            "resumed": pipeline_result.resumed,
            "already_korean": pipeline_result.already_korean,
        },
        "global_issues": [_issue_to_dict(issue) for issue in pipeline_result.global_issues],
        "segments": [
            {
                "id": segment.id,
                "source_hash": sha256_text(segment.source),
                "source_language": segment.source_language,
                "status": segment.status.value,
                "attempt_count": segment.attempt_count,
                "repaired": segment.was_repaired,
                "issues": [_issue_to_dict(issue) for issue in segment.validation_issues],
            }
            for segment in segments
        ],
        "terminal_failures": [
            {
                "id": segment.id,
                "source": segment.source,
                "attempt_count": segment.attempt_count,
                "last_error": segment.last_error,
                "failure_codes": sorted(
                    {
                        issue.code
                        for issue in segment.validation_issues
                        if issue.severity == ValidationSeverity.ERROR
                    }
                ),
                "last_raw_response": segment.last_raw_response,
                "last_raw_response_truncated": segment.last_raw_response_truncated,
                "issues": [_issue_to_dict(issue) for issue in segment.validation_issues],
            }
            for segment in failed
        ],
    }


def write_json_atomic(path: str | Path, value: dict[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_name = handle.name
        Path(temporary_name).replace(destination)
    finally:
        if temporary_name is not None:
            temporary = Path(temporary_name)
            if temporary.exists():
                temporary.unlink()
    return destination
