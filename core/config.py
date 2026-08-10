from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "ollama": {
        "base_url": "http://127.0.0.1:11434",
        "model": "huihui_ai/hy-mt1.5-abliterated:7b",
        "timeout_seconds": 180,
    },
    "translation": {
        "target_language": "ko",
        "source_language": "auto",
        "batch_size": 12,
        "context_before": 3,
        "context_after": 2,
        "max_segment_chars": 6000,
        "max_batch_chars": 12000,
    },
    "recovery": {
        "partial_repair_max_ratio": 0.25,
        "max_attempts": 3,
        "split_on_failure": True,
    },
    "checkpoint": {"enabled": True},
    "output": {
        "suffix": ".ko",
        "create_backup": True,
        "qa_report": True,
        "overwrite_source": False,
    },
    "validators": {
        "strict_id_order": True,
        "detect_prompt_leak": True,
        "detect_row_id_leak": True,
        "detect_unexpected_scripts": True,
        "detect_cjk_residue": True,
        "detect_number_risk": True,
        "detect_negation_risk": True,
        "detect_direction_risk": True,
        "detect_state_flip_risk": True,
    },
    "placeholders": {
        "protect_internal_newlines": True,
        "custom_patterns": [],
    },
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str
    model: str
    timeout_seconds: int


@dataclass(frozen=True)
class TranslationConfig:
    target_language: str
    source_language: str
    batch_size: int
    context_before: int
    context_after: int
    max_segment_chars: int
    max_batch_chars: int


@dataclass(frozen=True)
class RecoveryConfig:
    partial_repair_max_ratio: float
    max_attempts: int
    split_on_failure: bool


@dataclass(frozen=True)
class CheckpointConfig:
    enabled: bool


@dataclass(frozen=True)
class OutputConfig:
    suffix: str
    create_backup: bool
    qa_report: bool
    overwrite_source: bool


@dataclass(frozen=True)
class ValidatorsConfig:
    strict_id_order: bool
    detect_prompt_leak: bool
    detect_row_id_leak: bool
    detect_unexpected_scripts: bool
    detect_cjk_residue: bool
    detect_number_risk: bool
    detect_negation_risk: bool
    detect_direction_risk: bool
    detect_state_flip_risk: bool


@dataclass(frozen=True)
class PlaceholdersConfig:
    protect_internal_newlines: bool
    custom_patterns: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class AppConfig:
    ollama: OllamaConfig
    translation: TranslationConfig
    recovery: RecoveryConfig
    checkpoint: CheckpointConfig
    output: OutputConfig
    validators: ValidatorsConfig
    placeholders: PlaceholdersConfig
    raw: dict[str, Any] = field(repr=False)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _require_positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _require_nonnegative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"{name} must be a non-negative integer")
    return value


def load_config(path: str | Path | None = None, *, model_override: str | None = None) -> AppConfig:
    override: dict[str, Any] = {}
    if path is not None:
        config_path = Path(path)
        try:
            override = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"Config file not found: {config_path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid JSON in config file {config_path}: {exc}") from exc
        if not isinstance(override, dict):
            raise ConfigError("Config root must be a JSON object")

    data = _deep_merge(DEFAULT_CONFIG, override)
    if model_override:
        data["ollama"]["model"] = model_override

    if data["translation"]["target_language"] != "ko":
        raise ConfigError("translation.target_language is fixed to 'ko' in v7.0")
    if data["output"]["overwrite_source"]:
        raise ConfigError("output.overwrite_source=true is not supported in v7.0")
    if not data["output"]["create_backup"]:
        raise ConfigError("output.create_backup=false is not supported in v7.0")

    ratio = data["recovery"]["partial_repair_max_ratio"]
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not 0 <= ratio <= 1:
        raise ConfigError("recovery.partial_repair_max_ratio must be between 0 and 1")

    custom_patterns = data["placeholders"]["custom_patterns"]
    if not isinstance(custom_patterns, list):
        raise ConfigError("placeholders.custom_patterns must be a list")

    base_url = str(data["ollama"]["base_url"]).rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError("ollama.base_url must start with http:// or https://")

    return AppConfig(
        ollama=OllamaConfig(
            base_url=base_url,
            model=str(data["ollama"]["model"]),
            timeout_seconds=_require_positive_int(
                data["ollama"]["timeout_seconds"], "ollama.timeout_seconds"
            ),
        ),
        translation=TranslationConfig(
            target_language="ko",
            source_language=str(data["translation"]["source_language"]),
            batch_size=_require_positive_int(
                data["translation"]["batch_size"], "translation.batch_size"
            ),
            context_before=_require_nonnegative_int(
                data["translation"]["context_before"], "translation.context_before"
            ),
            context_after=_require_nonnegative_int(
                data["translation"]["context_after"], "translation.context_after"
            ),
            max_segment_chars=_require_positive_int(
                data["translation"]["max_segment_chars"], "translation.max_segment_chars"
            ),
            max_batch_chars=_require_positive_int(
                data["translation"]["max_batch_chars"], "translation.max_batch_chars"
            ),
        ),
        recovery=RecoveryConfig(
            partial_repair_max_ratio=float(ratio),
            max_attempts=_require_positive_int(
                data["recovery"]["max_attempts"], "recovery.max_attempts"
            ),
            split_on_failure=bool(data["recovery"]["split_on_failure"]),
        ),
        checkpoint=CheckpointConfig(enabled=bool(data["checkpoint"]["enabled"])),
        output=OutputConfig(
            suffix=str(data["output"]["suffix"]),
            create_backup=bool(data["output"]["create_backup"]),
            qa_report=bool(data["output"]["qa_report"]),
            overwrite_source=False,
        ),
        validators=ValidatorsConfig(**{
            key: bool(data["validators"][key])
            for key in ValidatorsConfig.__dataclass_fields__
        }),
        placeholders=PlaceholdersConfig(
            protect_internal_newlines=bool(
                data["placeholders"]["protect_internal_newlines"]
            ),
            custom_patterns=custom_patterns,
        ),
        raw=data,
    )


def load_profile(name_or_path: str | Path = "novel") -> dict[str, Any]:
    candidate = Path(name_or_path)
    if candidate.suffix.lower() != ".json" and candidate.parent == Path("."):
        candidate = Path(__file__).resolve().parent.parent / "profiles" / f"{candidate.name}.json"
    try:
        profile = json.loads(candidate.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Profile not found: {candidate}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in profile {candidate}: {exc}") from exc
    if not isinstance(profile, dict):
        raise ConfigError("Profile root must be a JSON object")
    if profile.get("target_language", "ko") != "ko":
        raise ConfigError("Profile target_language is fixed to 'ko'")
    return profile
