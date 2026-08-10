from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from adapters.text import TextAdapter
from core.checkpoint import CheckpointStore
from core.config import ConfigError, load_config, load_profile
from core.pipeline import TranslationPipeline
from core.reporting import build_qa_report, write_json_atomic
from translators.base import TranslationTransportError
from translators.ollama import OllamaTranslator


PROJECT_ROOT = Path(__file__).resolve().parent
TRANSLATION_MODES = ("novel", "game", "document")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seori Translator v7.0 - UTF-8 TXT를 안전하게 한국어 TXT로 번역합니다."
    )
    parser.add_argument("input", type=Path, help="번역할 UTF-8 TXT 파일")
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config.json",
        help="설정 JSON 경로 (기본: 프로젝트의 config.json)",
    )
    parser.add_argument("--model", help="config의 Ollama 모델을 이번 실행에만 덮어씁니다")
    parser.add_argument("--output", type=Path, help="출력 한국어 TXT 경로")
    parser.add_argument(
        "--mode",
        choices=TRANSLATION_MODES,
        help="번역 목적: novel(소설), game(게임), document(일반 문서)",
    )
    parser.add_argument(
        "--profile",
        help="고급 설정: --mode의 기본 프로필 대신 사용할 프로필 이름 또는 JSON 경로",
    )
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument("--resume", dest="resume", action="store_true", default=True)
    resume_group.add_argument("--no-resume", dest="resume", action="store_false")
    return parser


def _default_paths(input_path: Path, suffix: str) -> tuple[Path, Path, Path]:
    output = input_path.with_name(f"{input_path.stem}{suffix}{input_path.suffix}")
    checkpoint = input_path.with_name(f"{input_path.stem}.seori.sqlite")
    report = input_path.with_name(f"{input_path.stem}.qa.json")
    return output, checkpoint, report


def _resolve_mode(requested: str | None) -> str:
    if requested is not None:
        return requested
    if not sys.stdin.isatty():
        return "novel"

    choices = {"1": "novel", "2": "game", "3": "document"}
    while True:
        print("번역 모드를 선택하세요.")
        print("1. 소설")
        print("2. 게임")
        print("3. 일반 문서")
        try:
            selected = input("선택: ").strip()
        except EOFError as exc:
            raise ConfigError("번역 모드를 선택할 수 없습니다. --mode를 지정하세요") from exc
        if selected in choices:
            return choices[selected]
        print("1, 2, 3 중 하나를 입력하세요.", file=sys.stderr)


def _preflight_ollama(translator: OllamaTranslator) -> None:
    try:
        model_available = translator.health_check()
    except TranslationTransportError as exc:
        raise RuntimeError(
            f"Ollama에 연결할 수 없습니다: {translator.base_url} ({exc})"
        ) from exc
    if not model_available:
        raise RuntimeError(f"Ollama에 모델이 없습니다: {translator.model}")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backup_for_run(
    adapter: TextAdapter,
    input_path: Path,
    checkpoint: CheckpointStore,
    source_sha256: str,
    *,
    resume: bool,
) -> Path:
    if resume:
        saved_backup = checkpoint.get_metadata("backup_file")
        if saved_backup:
            candidate = Path(saved_backup)
            try:
                if candidate.is_file() and _sha256_file(candidate) == source_sha256:
                    return candidate
            except OSError:
                pass
    backup_path = adapter.backup(input_path)
    checkpoint.set_metadata("backup_file", str(backup_path.resolve()))
    return backup_path


def run(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        print(f"오류: 입력 파일을 찾을 수 없습니다: {input_path}", file=sys.stderr)
        return 2
    if input_path.suffix.lower() != ".txt":
        print("오류: v7.0은 TXT 입력만 지원합니다.", file=sys.stderr)
        return 2

    try:
        mode = _resolve_mode(args.mode)
        config = load_config(args.config, model_override=args.model)
        profile_selector = args.profile or mode
        profile = load_profile(profile_selector)
        default_output, checkpoint_path, report_path = _default_paths(
            input_path, config.output.suffix
        )
        output_path = (args.output or default_output).expanduser().resolve()
        if output_path == input_path:
            raise ConfigError("출력 경로는 원본 TXT와 달라야 합니다")

        adapter = TextAdapter()
        document, segments = adapter.load(input_path)
        if not segments:
            raise ValueError("번역할 비어 있지 않은 문단이 없습니다")

        translator = OllamaTranslator(config.ollama)
        with CheckpointStore(
            checkpoint_path, enabled=config.checkpoint.enabled
        ) as checkpoint:
            checkpoint_source_sha = checkpoint.get_metadata("source_sha256")
            if (
                args.resume
                and checkpoint_source_sha is not None
                and checkpoint_source_sha != document.source_sha256
            ):
                raise ConfigError(
                    "checkpoint의 원본 SHA-256이 현재 TXT와 다릅니다. "
                    "안전한 재개를 거부합니다. --no-resume 또는 새 checkpoint를 사용하세요"
                )
            _preflight_ollama(translator)
            if not args.resume:
                checkpoint.clear_segments()

            checkpoint.set_metadata("source_file", str(input_path))
            checkpoint.set_metadata("source_sha256", document.source_sha256)
            checkpoint.set_metadata("model", config.ollama.model)
            checkpoint.set_metadata("mode", mode)
            checkpoint.set_metadata("profile", str(profile.get("name", profile_selector)))
            backup_path = (
                _backup_for_run(
                    adapter,
                    input_path,
                    checkpoint,
                    document.source_sha256,
                    resume=args.resume,
                )
                if config.output.create_backup
                else input_path
            )

            print(f"입력: {input_path}")
            print(f"모드: {mode}")
            print(f"프로필: {profile.get('name', profile_selector)}")
            print(f"모델: {config.ollama.model}")
            print(f"백업: {backup_path}")
            pipeline = TranslationPipeline(
                config,
                profile,
                translator,
                checkpoint,
                progress=lambda message: print(f"[Seori] {message}", flush=True),
            )
            result = pipeline.process(segments, resume=args.resume)

        adapter.save(document, result.segments, output_path)
        report = build_qa_report(
            result,
            source_path=input_path,
            source_sha256=document.source_sha256,
            output_path=output_path,
            model=config.ollama.model,
            backup_path=backup_path,
            mode=mode,
            profile=str(profile.get("name", profile_selector)),
        )
        if config.output.qa_report:
            write_json_atomic(report_path, report)

        summary = report["summary"]
        print(f"출력: {output_path}")
        if config.output.qa_report:
            print(f"QA: {report_path}")
        print(
            "완료: "
            f"VALID {summary['valid']}/{summary['total']}, "
            f"복구 {summary['repaired']}, 실패 {summary['failed']}, 위험 표시 {summary['risks']}"
        )
        return 1 if result.failed else 0
    except KeyboardInterrupt:
        print("\n중단되었습니다. 완료된 VALID 문단은 checkpoint에 저장되어 있습니다.", file=sys.stderr)
        return 130
    except TranslationTransportError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        print(
            "Ollama 전송 오류로 작업을 중단했습니다. 이미 완료된 VALID 문단은 "
            "checkpoint에 유지됩니다.",
            file=sys.stderr,
        )
        return 2
    except (ConfigError, UnicodeError, OSError, RuntimeError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
