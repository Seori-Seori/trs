# v6 -> v7 Migration Notes

기존 v6 PowerShell은 버리는 코드가 아니라 검증된 동작의 참조 구현이다.
Python으로 줄 단위 포팅하지 않고 역할별로 분해한다.

## 재사용할 개념

### Batch generation

v6:
- `Flatten-Blocks`
- `New-Batches`

v7:
- `core/batching.py`

계승:
- block 경계 보존
- 최대 크기 기준 batch
- oversized block 처리

### Protected values

v6:
- `Safe-SourceText`
- `Protect-ControlCodes`
- `Restore-ProtectedControlCodes`

v7:
- `core/placeholders.py`

변경:
- 특정 게임 제어코드 중심에서 configurable generic placeholder 시스템으로 확대
- `[[LB]]` 위치 추정 복원 제거

### Prompt

v6:
- `Build-Prompt`
- `Build-SingleRowFallbackPrompt`

v7:
- `core/prompting.py`

계승:
- target row와 context 구분
- output 형식 강제

### Ollama

v6:
- `Ensure-Ollama`
- `Invoke-OllamaGenerate`

v7:
- `translators/ollama.py`

변경:
- HTTP transport와 prompt policy 분리

### Parse once

v6:
- `Parse-Response`
- `parsedOnce`를 validator와 salvage가 재사용

v7:
- `core/parser.py`

반드시 계승.

### Validation

v6:
- `Test-TranslationParsed`
- `Test-TranslationText`

v7:
- `validators/structure.py`
- `validators/korean.py`
- `validators/placeholders.py`
- `validators/risk.py`

추가 수정:
- unexpected ID를 명시적 오류로 처리
- 실제 row ID leak 탐지
- Cyrillic 등 unexpected script
- number/negation/direction/state risk
- quote/bracket structure

### Recovery

v6:
- `Try-FastSalvageBatch`
- `Try-TranslateSingleRowQuick`
- `Try-RepairCjkRow`
- recursive block split/fallback

v7:
- `core/recovery.py`

계승:
- good row 즉시 salvage
- failed row only repair
- 문제 비율이 높을 때 batch retry 가능
- recursive split
- single-row fallback with context

변경:
- CJK repair는 잘못된 번역문 rewrite보다 원문 + context 재번역을 기본값으로 함

### Resume/checkpoint

v6:
- 기존 결과 load
- validation 후 skip
- batch/TSV 저장

v7:
- `core/checkpoint.py`
- sqlite3

계승:
- 기존 결과를 무조건 믿지 않음
- hash + validation 기준 resume

### Terminal failure

v6:
- `Save-TerminalFailure`

v7:
- `projects/.../failures/` 또는 report storage

계승:
- 중간 retry 실패 로그 남발 금지
- 최종 실패만 별도 failure artifact

## v6에서 그대로 가져오면 안 되는 부분

1. PowerShell 전역 상태 의존
2. RPG Maker/Excel 구조가 core에 스며든 부분
3. `ADULT_#####` 같은 특정 ID prefix 하드코딩
4. `[[LB]]`를 번역문 길이에 따라 추정 복원하는 로직
5. 모델 출력의 Latin 전체를 금지하는 식의 과도한 규칙
6. 잘못 번역된 한국어+CJK 문자열만 다시 rewrite해서 의미 정확성을 가정하는 로직
7. TSV/Excel을 checkpoint 저장소 자체로 사용하는 구조

## v6에서 발견된 중요 검증 구멍

### Unexpected ID

`unexpected`를 계산하지만 최종 fail 조건에 포함되지 않는 경로가 존재했다.

v7 테스트 R07로 고정한다.

### Embedded next row ID

Parser가 line-start ID만 읽으면 현재 translation 안에 다음 ID가 섞이는 문제를 놓칠 수 있다.

v7 `ROW_ID_LEAK` 검사로 고정한다.

### Cyrillic

CJK 중심 검증만으로는 러시아/키릴 문자 혼입을 놓친다.

v7 Unicode script 검사로 확장한다.

## 원칙

v7 구현 중 v6와 동작이 다를 때 다음 우선순위를 따른다.

1. 데이터 안전성
2. 정상 번역 재사용
3. 구조 무결성
4. 의미 보존 위험 최소화
5. 속도

속도를 위해 정상 번역을 다시 돌리거나 구조 검증을 약화하지 않는다.
