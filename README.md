# Seori Translator

개인용 로컬 범용 번역기 프로젝트.

## 최종 목표

- 입력 언어: 한국어, 일본어, 중국어, 영어, 자동 감지
- 출력 언어: 항상 한국어
- 로컬 Ollama 사용
- 기본 모델: HY-MT 계열 (`huihui_ai/hy-mt1.5-abliterated:7b` 등)
- Windows 11 + Python 환경에서 복잡한 설치 없이 실행
- RPG Maker/게임, 웹소설/픽시브 소설, TXT/HTML/JSON/CSV/Excel을 순차 지원
- 향후 EPUB, 다른 게임 엔진, GUI 확장

## v7.0 범위

v7.0은 TXT 번역을 완성하는 최소 핵심 엔진이다. 파일 형식과 번역 목적은 분리되어
있으며 같은 TXT를 `novel`, `game`, `document` 모드로 번역할 수 있다.

```text
TXT
 -> TextAdapter
 -> Segment[]
 -> language detection
 -> placeholder protection
 -> context build
 -> HY-MT native single-Segment request (internal ID is not exposed)
 -> Ollama translation text
 -> parse once
 -> validators
 -> partial repair only for failed segments
 -> safe split only for long segments
 -> checkpoint
 -> TextAdapter reconstruction
 -> Korean TXT
 -> QA report
```

## 가장 중요한 불변조건

1. 번역 엔진은 파일 형식을 모른다. 파일 형식은 Adapter만 안다.
2. 모든 입력은 Adapter에서 `Segment[]`로 변환한다.
3. 목표 언어는 항상 한국어다.
4. 정상 판정된 Segment는 절대로 재번역하지 않는다.
5. 모델 응답은 한 번만 파싱하고 그 결과를 모든 검증/복구 단계에서 재사용한다.
6. 구조 오류는 문제 Segment만 부분복구한다.
7. 부분복구 실패 시에만 더 작은 블록으로 분할한다.
8. 끝까지 실패한 경우만 failure log에 기록한다.
9. placeholder 개수/값/순서가 틀리면 번역 실패다.
10. 모든 문장을 별도 AI 검수하지 않는다. 규칙 기반 검증이 기본이며 의미상 위험한 문장만 선택적 semantic QA 대상으로 표시한다.
11. 원본은 항상 backup한다.
12. 원본 데이터는 번역 대상 텍스트 이외에는 변경하지 않는다.

## v7.0 첫 사용자 경험

가장 먼저 완성해야 할 형태는 단순하다.

```bat
run_translation.bat novel.txt --mode novel
```

또는

```bash
python main.py novel.txt --mode novel
```

결과 예:

```text
novel.txt
novel.ko.txt
novel.seori.sqlite
novel.qa.json
backup/novel.<timestamp>.txt
```

입력 TXT의 문단/빈 줄/줄바꿈 구조는 가능한 한 유지하면서 텍스트만 한국어로 번역한다.

## 개발 순서

- v7.0: Python core + TXT + novel/game/document 모드 + Ollama + validators + checkpoint + partial repair
- v7.1: glossary + character memory + story memory + translation memory
- v7.2: HTML + Pixiv 저장 페이지
- v7.3: RPG Maker adapter
- v7.4: JSON / CSV / Excel
- 이후: EPUB / GUI

상세 구현 계약은 `docs/` 문서를 따른다.

## v7.0 실행

필요 환경:

- Windows 11
- Python 3.10 이상
- 로컬 Ollama
- 기본 모델 `huihui_ai/hy-mt1.5-abliterated:7b`

런타임 Python 외부 패키지는 사용하지 않는다.

```bash
ollama pull huihui_ai/hy-mt1.5-abliterated:7b
python main.py novel.txt --mode novel
```

Windows에서는 다음 BAT도 사용할 수 있다.

```bat
run_translation.bat novel.txt --mode novel
```

게임 문자열과 일반 문서는 각각 다음처럼 실행한다.

```bash
python main.py strings.txt --mode game
python main.py manual.txt --mode document
```

대화형 터미널에서 `--mode`를 생략하면 소설/게임/일반 문서 선택 메뉴가 표시된다.
비대화형 실행에서는 자동 추측하지 않고 호환성을 위해 `novel`을 결정적으로 사용한다.

기본 생성 파일:

```text
novel.ko.txt          한국어 결과
novel.seori.sqlite    문단별 checkpoint/resume 데이터
novel.qa.json         validator/RISK/terminal failure 보고서
backup/novel.*.txt    실행 전 원본 백업
```

사용 가능한 최소 옵션:

```text
--config PATH
--model MODEL
--output PATH
--mode novel|game|document
--profile NAME|PATH
--resume / --no-resume
--debug-failures
```

`--profile`은 선택한 모드의 기본 프로필을 바꾸는 고급 옵션이다.
`--debug-failures`는 실패한 단일 요청의 bounded source/prompt/원시 응답과 최종
오류 코드를 `<입력명>.seori-debug.json`에 기록한다. 성공 응답은 기록하지 않는다.

입력은 UTF-8 또는 UTF-8 BOM TXT만 지원한다. CP949/Shift-JIS를 임의 추측하지
않으며 원본 TXT와 같은 경로로 출력하는 것도 거부한다.

## 안전 복구 동작

- 시작 전에 Ollama 연결과 모델 설치 여부를 검사하고 실패하면 즉시 중단한다.
- HTTP/연결/timeout/응답 envelope 오류는 작업 수준 오류이며 Segment repair/split을 하지 않는다.
- `translation.max_prompt_chars`는 문맥과 지시문까지 포함한 실제 프롬프트 크기를 제한한다.
- 기본 HY-MT 요청은 한 번에 Segment 하나만 보내며 프로그램 내부 ID를 prompt/응답 규약에 노출하지 않는다.
- 모델 응답마다 parser는 정확히 한 번만 실행된다.
- 검증을 통과한 Segment는 즉시 SQLite checkpoint에 저장된다.
- 실패한 Segment만 source·context·오류 코드로 단건 partial repair 요청을 보낸다.
- repair 프롬프트에는 원문·문맥·검증 실패 코드만 포함하며 깨진 번역을 기준으로 삼지 않는다.
- 일반 Segment는 설정된 단건 재시도 횟수를 소진하면 FAILED가 되며, 긴 Segment만 안전한 자식 조각으로 분할한다.
- placeholder 값·개수·순서가 틀리면 원래 위치를 추정하지 않고 실패 처리한다.
- 동일 ID와 동일 source SHA-256의 `VALID` checkpoint는 재검증 후 모델 호출 없이 재사용한다.
- checkpoint의 source SHA-256이 현재 TXT와 다르면 resume을 거부하고 `--no-resume`을 안내한다.
- 번역 도중 원본 TXT가 바뀌면 SHA-256 안전장치가 결과 재삽입을 거부한다.
- 동일 작업의 일반 resume은 검증된 기존 backup을 재사용한다.
- 끝까지 실패한 문단만 QA의 `terminal_failures`에 실패 코드와 제한된 마지막 원시 응답을 기록하며 결과 TXT에는 해당 원문을 안전하게 유지한다.

## 테스트

Ollama 모델 없이도 parser, validator, checkpoint, partial repair와 split을 결정적으로
검사할 수 있다. 표준 라이브러리 `unittest`만 사용한다.

```bash
python -m unittest discover -s tests -v
```

`tests/regression/test_regression_cases.py`에는 레거시 parser 호환 회귀를 포함해
`R01`부터 Round 4 네이티브 프로토콜의 `R42`까지 각각의 회귀 테스트가 존재한다.
통합 테스트는 로컬 임시 HTTP 서버로 Ollama API 계약을 재현하여
`main.py input.txt`부터 백업, 한국어 TXT, SQLite, QA JSON 생성까지 검증한다.
