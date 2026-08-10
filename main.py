from __future__ import annotations

import argparse
import sys
from pathlib import Path

from adapters.text import TextAdapter
from core.checkpoint import CheckpointStore
from core.config import ConfigError, load_config, load_profile
from core.pipeline import TranslationPipeline
from core.reporting import build_qa_report, write_json_atomic
from translators.ollama import OllamaTranslator


PROJECT_ROOT = Path(__file__).resolve().parent


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
        "--profile", default="novel", help="profiles의 프로필 이름 또는 JSON 경로"
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
        config = load_config(args.config, model_override=args.model)
        profile = load_profile(args.profile)
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
        backup_path = adapter.backup(input_path) if config.output.create_backup else input_path

        print(f"입력: {input_path}")
        print(f"모델: {config.ollama.model}")
        print(f"백업: {backup_path}")

        translator = OllamaTranslator(config.ollama)
        with CheckpointStore(
            checkpoint_path, enabled=config.checkpoint.enabled
        ) as checkpoint:
            checkpoint.set_metadata("source_file", str(input_path))
            checkpoint.set_metadata("source_sha256", document.source_sha256)
            checkpoint.set_metadata("model", config.ollama.model)
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
    except (ConfigError, UnicodeError, OSError, RuntimeError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
