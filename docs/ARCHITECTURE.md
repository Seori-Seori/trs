# Seori Translator Architecture

## 1. 핵심 설계 철학

Seori Translator는 파일별 번역 프로그램이 아니라 `Segment` 중심 번역 엔진이다.

```text
Input file
  -> Adapter
  -> Segment[]
  -> Core translation pipeline
  -> Segment.translation
  -> Adapter reconstruction
  -> Output file
```

Core는 TXT, HTML, RPG Maker, Excel 같은 파일 형식을 몰라야 한다.

## 2. 권장 디렉터리

```text
seori_translator/
├─ main.py
├─ config.json
├─ run_translation.bat
├─ core/
│  ├─ segment.py
│  ├─ pipeline.py
│  ├─ batching.py
│  ├─ parser.py
│  ├─ prompting.py
│  ├─ recovery.py
│  ├─ checkpoint.py
│  ├─ language.py
│  ├─ placeholders.py
│  └─ hashing.py
├─ adapters/
│  ├─ base.py
│  └─ text.py
├─ translators/
│  ├─ base.py
│  └─ ollama.py
├─ validators/
│  ├─ base.py
│  ├─ structure.py
│  ├─ korean.py
│  ├─ placeholders.py
│  └─ risk.py
├─ profiles/
│  └─ novel.json
├─ projects/
├─ output/
├─ backup/
└─ tests/
   ├─ unit/
   └─ regression/
```

## 3. Segment 계약

초기 구조는 다음 정보를 가진다.

```python
@dataclass
class Segment:
    id: str
    source: str
    source_language: str = "auto"
    target_language: str = "ko"
    speaker: str | None = None
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)
    protected_tokens: list[ProtectedToken] = field(default_factory=list)
    file_path: str | None = None
    location: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    prepared_source: str | None = None
    raw_translation: str | None = None
    translation: str | None = None
    status: SegmentStatus = SegmentStatus.PENDING
```

`source`는 원본이며 절대로 변형하지 않는다.

- `prepared_source`: placeholder가 적용되어 모델로 전송되는 텍스트
- `raw_translation`: 모델에서 막 받아 파싱된 결과
- `translation`: 검증 통과 + placeholder 복원까지 끝난 최종 결과

## 4. ProtectedToken 계약

단순 문자열 목록 대신 원본 값을 보존하는 구조체를 사용한다.

```python
@dataclass(frozen=True)
class ProtectedToken:
    placeholder: str
    original: str
    kind: str
    order: int
```

예:

```text
\N[1] -> [[PH_0001]]
%PLAYER% -> [[PH_0002]]
내부 줄바꿈 -> [[PH_0003]]
```

복원 전 반드시 placeholder의 값, 개수, 중복, 순서를 검증한다.

## 5. 상태 머신

권장 상태:

```text
PENDING
PREPARED
TRANSLATING
PARSED
VALID
REPAIR_PENDING
FAILED
```

핵심 불변조건:

```text
status == VALID
=> 모델 번역 요청의 대상이 될 수 없음
```

이 규칙은 caller가 아니라 pipeline 내부에서 강제한다.

## 6. Pipeline

```text
Adapter.load
 -> language detect
 -> protect placeholders
 -> checkpoint lookup
 -> context build
 -> batch build
 -> translator.translate
 -> parse response ONCE
 -> structure validation
 -> Korean validation
 -> placeholder validation
 -> risk detection
 -> accept valid segments immediately
 -> checkpoint valid segments immediately
 -> partial repair failed segments only
 -> split retry only when partial repair fails
 -> terminal failure recording
 -> Adapter.save
 -> QA report
```

정상 Segment를 실패 Segment와 함께 다시 모델에 보내면 안 된다.

## 7. v6에서 반드시 계승할 동작

기존 PowerShell v6에서 검증된 다음 동작은 Python에서도 유지한다.

- batch translation
- checkpoint/resume
- 완료 batch를 재검증한 뒤 skip
- 모델 응답 1회 parse
- 정상 행 salvage
- 문제 행만 partial repair
- 문제 비율이 클 때에만 batch retry 가능
- 긴 block 자동 split
- block 경계를 보존한 split
- single-row fallback에서도 주변 context 제공
- 최종 실패만 failure log
- expected ID / actual ID 비교
- 원본 SHA-256 기반 안전장치

## 8. v6에서 수정해야 하는 구멍

Python v7에서는 다음을 명시적으로 막는다.

### Unexpected ID

expected에 없는 ID가 하나라도 있으면 구조 오류다.

단 이미 정상 검증된 expected Segment는 버리지 않는다.

### Row ID leak

번역문 내부에 다른 Segment ID가 나타나면 구조 오류다.

예:

```text
SEG_0001    번역 SEG_0002 다음 행 번역
```

### Unexpected script

원문/설정상 허용되지 않은 Cyrillic 등 예상 외 Unicode script를 탐지한다.

### Meaning risk

숫자, 부정어, 방향, Yes/No, Skip, enable/disable 등은 risk validator가 표시한다.

## 9. Parser 계약

응답은 한 번만 parse한다.

권장 모델 출력 형식:

```text
SEG_00000001<TAB>한국어 번역
SEG_00000002<TAB>한국어 번역
```

Parser 결과 예:

```python
ParsedResponse(
    rows={...},
    duplicates=[...],
    malformed_lines=[...],
    raw_response="..."
)
```

Validator와 Recovery는 raw response를 다시 parse하면 안 된다.

## 10. Validator 결과

Validator는 최소 다음 심각도를 지원한다.

```text
PASS
WARNING
RISK
ERROR
```

- ERROR: 자동 복구 대상
- RISK: 번역을 반드시 폐기하지 않음. QA report/선택적 semantic QA 대상

## 11. Adapter 계약

모든 adapter는 대략 다음 책임을 가진다.

```text
load(path) -> Document + Segment[]
save(document, segments, output_path)
backup(path)
```

Adapter는 원본 파일의 비번역 구조를 기억해야 한다.
Core는 `location`을 opaque value로 취급하고 해석하지 않는다.

## 12. Translator 계약

Translator는 Ollama라는 구현 세부사항을 제외한 추상 인터페이스를 가진다.

```text
translate(prompt) -> raw string
health_check()
```

prompt 생성은 translator가 아니라 `core/prompting.py`에서 담당한다.

## 13. Checkpoint

v7.0부터 Python 표준 라이브러리 `sqlite3` 사용을 권장한다.

필수 개념:

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

재개 시 `source_hash`와 상태를 확인한다.

`VALID` + 동일 source hash이면 모델을 호출하지 않는다.

## 14. 확장 방향

v7.1부터 translation memory와 작품 memory를 추가하되 Core 인터페이스는 유지한다.

```text
TXT Adapter --------┐
HTML Adapter -------┤
RPGMaker Adapter ---┤ -> Segment[] -> Core -> Segment[]
Excel Adapter ------┤
EPUB Adapter -------┘
```

이 구조가 깨지는 기능 추가는 하지 않는다.
