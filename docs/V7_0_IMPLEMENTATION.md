# v7.0 Implementation Contract

이 문서는 Work 구현 시 따라야 할 순서와 완료 기준이다.

## Phase 1 - Data model

구현:

- `core/segment.py`
- Segment
- ProtectedToken
- SegmentStatus
- ValidationSeverity
- ValidationIssue
- ValidationResult

완료 기준:

- source는 immutable 취급
- prepared/raw/final translation 분리
- status transition 테스트

## Phase 2 - Config

구현:

- `config.json`
- config loader
- 기본값 + 사용자 override

필수 설정:

```text
ollama.base_url
ollama.model
ollama.timeout_seconds
translation.target_language = ko
translation.batch_size
translation.context_before
translation.context_after
recovery.partial_repair_max_ratio
recovery.max_attempts
recovery.split_on_failure
output.suffix
checkpoint.enabled
validators.*
placeholders.patterns
```

## Phase 3 - Placeholder engine

구현:

- `core/placeholders.py`

지원 범주:

```text
RPG control codes
printf placeholders
brace placeholders
${...}
markup-like tags
newline tokens
URLs
file paths
user-configured regex patterns
```

중요:
- RPG Maker 전용 하드코딩 엔진으로 만들지 말 것
- built-in pattern set + config custom pattern set
- protect/restore round-trip 테스트 필수

## Phase 4 - TXT adapter

구현:

- `adapters/base.py`
- `adapters/text.py`

필수:
- UTF-8/BOM
- CRLF/LF 보존
- paragraph segmentation
- separator 보존
- 내부 줄바꿈 보존
- output reconstruction
- backup

## Phase 5 - Parser

구현:

- `core/parser.py`

모델 출력 기본 형식:

```text
SEG_00000001<TAB>translation
```

응답 1회 parse 원칙을 코드 구조로 강제한다.

Parser가 반환할 정보:

```text
rows
row_order
duplicates
malformed_lines
raw_response
```

## Phase 6 - Validators

### structure.py

탐지:
- missing ID
- unexpected ID
- duplicate ID
- order mismatch
- row ID leak
- empty translation
- prompt leak
- multiple rows merged

### placeholders.py

탐지:
- missing placeholder
- unexpected placeholder
- duplicate placeholder
- order mismatch
- restoration mismatch

### korean.py

탐지:
- excessive source CJK residue
- known bad CJK residue such as `的`
- unexpected Cyrillic/other scripts
- Korean output absent when it should be Korean
- suspicious language mixing

Latin 전체 금지는 금지한다.

### risk.py

RISK 표시:
- not/don't/never
- 無/不/没/ない
- Skip/Don't Skip
- Yes/No
- numbers
- proper nouns
- up/down/left/right
- before/after
- enable/disable
- open/close
- lock/unlock

RISK는 자동 폐기와 동일하지 않다.

## Phase 7 - Ollama client

구현:

- `translators/base.py`
- `translators/ollama.py`

기본 endpoint:

```text
http://127.0.0.1:11434
```

역할:
- health check
- request
- timeout
- transport error
- raw response 반환

프롬프트 정책은 Ollama client에 넣지 않는다.

외부 패키지 없이 `urllib.request`로 시작 가능하다.

## Phase 8 - Prompting and batching

구현:

- `core/prompting.py`
- `core/batching.py`

원칙:
- target과 context 명확히 분리
- context는 번역 출력 대상이 아님
- output format을 엄격히 명시
- 실제 원문에 없는 placeholder/ID 예시가 결과로 누출되지 않도록 prompt 설계
- block/paragraph 경계를 가능한 한 보존

## Phase 9 - Checkpoint

구현:

- `core/checkpoint.py`
- sqlite3

최소 테이블은 구현자가 합리적으로 설계하되 다음 정보는 보존:

```text
segment_id
source_hash
source_text
status
translation
attempt_count
last_error
updated_at
```

핵심 테스트:
- VALID + 동일 hash -> translator 호출 0회

## Phase 10 - Recovery

구현:

- `core/recovery.py`

순서:

```text
batch response
 -> parse once
 -> validate each expected segment
 -> immediately accept/checkpoint good segments
 -> collect failed segments only
 -> partial repair failed only
 -> if still failing, split into smaller groups
 -> final single segment fallback
 -> terminal failure log only at the end
```

v6의 salvage 철학을 유지한다.

기존 잘못된 translation을 단순 rewrite하는 것보다 원문 + context에서 재번역하는 것을 기본 repair로 한다.

## Phase 11 - Pipeline

구현:

- `core/pipeline.py`

Pipeline이 불변조건을 강제한다.

특히:

```text
VALID Segment must never enter translator request
```

## Phase 12 - CLI

구현:

- `main.py`
- `run_translation.bat`

v7.0 최소 UX:

```bash
python main.py input.txt
```

선택 옵션 후보:

```text
--config
--model
--project
--resume
--no-resume
--output
--profile novel
```

처음부터 옵션을 과도하게 늘리지 않는다.

## Phase 13 - QA report

최종 report 예:

```json
{
  "total": 1420,
  "valid": 1417,
  "repaired": 3,
  "failed": 0,
  "risks": 7
}
```

가능하면 segment별 issue code도 남긴다.

## v7.0 Definition of Done

다음이 모두 되어야 v7.0이다.

1. TXT -> Korean TXT 실제 번역 가능
2. Ollama HY-MT 로컬 호출 가능
3. 중간 종료 후 resume 가능
4. VALID Segment 재번역 0회
5. partial repair 동작
6. split fallback 동작
7. placeholder round trip 보장
8. 필수 structure/korean/placeholder validators 동작
9. mandatory regression tests 통과
10. 원본 backup 생성
11. final QA report 생성
12. 원본 문단/빈 줄 구조 보존

이 범위가 끝나기 전에는 GUI, EPUB, 크롤러, RPG Maker 이식을 시작하지 않는다.
