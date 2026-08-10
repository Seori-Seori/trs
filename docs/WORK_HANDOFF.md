# Work Handoff

이 저장소를 구현하는 Work/코딩 모델은 먼저 다음 문서를 읽는다.

1. `README.md`
2. `docs/ARCHITECTURE.md`
3. `docs/TXT_IO_SPEC.md`
4. `docs/V7_0_IMPLEMENTATION.md`
5. `docs/REGRESSION_CASES.md`
6. `docs/V6_MIGRATION.md`
7. `docs/V7_0_ROUND4_HYMT_NATIVE_PROTOCOL.md`

Round별 승인 문서는 초기 설계를 보정한다. 현재 기본 HY-MT wire protocol은 Round 4의
ID 없는 단일 Segment native 요청이며, 이전 문서의 row-ID batching 설명보다 우선한다.

## 구현 목표

첫 목표는 거대한 범용 프로그램이 아니다.

**v7.0 = TXT를 넣으면 안전하게 한국어 TXT를 뱉는 로컬 번역기**를 완성한다.

```text
python main.py input.txt
```

가 실제 Ollama HY-MT 모델과 연결되어 동작해야 한다.

## 구현 시 임의 변경 금지 원칙

- target language는 한국어 고정
- core가 파일 형식을 알아서는 안 됨
- 정상 VALID Segment 재번역 금지
- response parse 1회
- failed Segment만 partial repair
- placeholder 손상 시 임의 추정 복원 금지
- 모든 문장 AI QA 금지
- risk 문장만 선택적 semantic QA 구조
- 원본 overwrite 기본 금지
- backup 필수
- failure log는 terminal failure만

## 권장 작업 방식

한 번에 전체 프로젝트를 생성하지 말고 Phase 단위로 구현하고 테스트한다.

### PR/commit 1

- package/folder skeleton
- Segment/ProtectedToken/validation models
- config loader
- tests skeleton

### PR/commit 2

- placeholder engine
- placeholder unit tests
- regression R02/R04/R13/R19/R20

### PR/commit 3

- TXT adapter
- paragraph/separator/newline preservation tests

### PR/commit 4

- parser
- structure validators
- regression R03/R05/R06/R07/R15/R16/R17/R18/R21

### PR/commit 5

- Korean/unicode/risk validators
- regression R01/R08/R09/R12/R14

### PR/commit 6

- Ollama client
- prompting
- batching
- mock translator tests

### PR/commit 7

- sqlite checkpoint
- resume tests including R22

### PR/commit 8

- recovery
- partial repair/split
- R23

### PR/commit 9

- pipeline
- CLI/BAT
- QA report
- end-to-end mock test

### PR/commit 10

- local Ollama real integration smoke test
- TXT -> Korean TXT final validation

실제 작업 환경에 따라 commit 수는 달라도 되지만 의존 순서는 가능한 한 유지한다.

## 테스트 우선

가능하면 translator를 mock/fake로 주입할 수 있게 설계한다.

그러면 모델이 없어도 다음을 결정적으로 테스트할 수 있다.

- parser
- validators
- checkpoint
- resume
- partial repair
- retry/split
- normal rows never retranslating

실제 Ollama 테스트는 마지막 integration layer에서 한다.

## 외부 의존성

v7.0은 가능하면 Python 표준 라이브러리만 사용한다.

권장:

```text
argparse
pathlib
json
urllib.request
sqlite3
hashlib
re
shutil
datetime
logging
dataclasses
enum
```

테스트에서 `pytest`가 필요하다고 판단하면 추가 가능하지만, 런타임 자체는 외부 패키지 0개를 우선한다.

## 완료 보고 시 반드시 포함

Work는 구현 완료 시 다음을 보고한다.

- 생성/수정된 파일 목록
- 실행 방법
- 테스트 명령
- 테스트 통과 수
- 아직 구현하지 않은 범위
- 설계 문서와 다르게 판단한 부분이 있다면 그 이유

문서 요구사항을 조용히 생략하지 않는다.
