# Seori Translator v7.0 — Round 3 HY-MT 실전 프로토콜 수정 명세

## 목적

Round 2 구현을 실제 로컬 HY-MT (`huihui_ai/hy-mt1.5-abliterated:7b`)로 소설 TXT에 적용한 첫 실전에서 구조적 실패가 확인되었다.

이 문서는 Work가 다음 수정안을 그대로 구현할 수 있도록 **원인, 불변조건, 구현 방향, parser 계약, prompt 계약, validator 정책, 회귀 테스트, 실전 재시험 절차**를 고정한다.

이번 수정은 새로운 기능 확장이 아니라 **실제 HY-MT 출력 습성에 맞춘 wire protocol 안정화**다.

---

## 1. 실전 관측 데이터

실행 파일:

- TXT: `C:\trstxt\a.txt`
- 모드: `novel`
- 총 문단: 235
- 기본 batch size: 12

초기 실행에서 첫 배치 결과가 다음처럼 나왔다.

```text
[Seori] 총 235개 문단: 재개 0, 한국어 유지 0, 번역 대상 235
[Seori] 배치 1/20 번역 중 (12개 문단)
[Seori] 배치 1/20 완료: VALID 1, FAILED 11
```

실행을 중단한 뒤 checkpoint의 FAILED Segment를 집계한 결과:

```text
FAILED = 20
ERROR CODES = Counter({
    'DUPLICATE_ID': 16,
    'UNBALANCED_DELIMITERS': 5,
    'UNBALANCED_QUOTES': 2
})
```

대표 실패:

```text
SEG_00000001 : DUPLICATE_ID
SEG_00000003 : DUPLICATE_ID
SEG_00000004 : DUPLICATE_ID
SEG_00000005 : DUPLICATE_ID
SEG_00000006 : DUPLICATE_ID
SEG_00000007 : DUPLICATE_ID
SEG_00000008 : DUPLICATE_ID
SEG_00000009 : UNBALANCED_QUOTES
SEG_00000010 : DUPLICATE_ID
SEG_00000011 : UNBALANCED_DELIMITERS
```

오류 코드 합계가 FAILED Segment 수보다 클 수 있는 것은 한 Segment가 여러 validation issue를 동시에 가질 수 있기 때문이다.

현재 우선순위는 명확하다. **DUPLICATE_ID가 압도적인 1차 장애이며 먼저 제거해야 한다.** 괄호/따옴표 검증 정책은 이번 프로토콜 수정 후 다시 실전 수치를 보고 조정한다.

---

## 2. 원인

현재 prompt는 같은 Segment ID를 한 요청 안에서 여러 번 노출한다.

batch 모드에서는 대략 다음 구조다.

```text
<<<CONTEXT>>>
SEG_00000001    {...context...}
...
<<<TARGETS>>>
SEG_00000001    ja    원문
```

repair/single에서는 실패 정보까지 추가되어 동일 ID가 세 번 이상 나타날 수 있다.

```text
<<<CONTEXT>>>
SEG_00000001    {...}

<<<FAILURES>>>
SEG_00000001    DUPLICATE_ID ...

<<<TARGETS>>>
SEG_00000001    ja    원문
```

실제 HY-MT는 이 반복 패턴을 출력 스키마의 반복 예시로 해석하는 경향을 보였고, 같은 ID 행을 두 번 출력하는 응답이 다수 발생한 것으로 판단한다.

parser는 현재 동일 ID가 두 번 등장하면 뒤 행의 내용과 무관하게 `DUPLICATE_ID`로 처리한다. parser 자체는 기존 계약대로 동작하지만, 실전에서는 **완전히 동일한 중복 행**까지 모두 재번역시키는 것은 불필요하다.

---

## 3. 절대 불변조건

Round 3에서도 아래 원칙은 바꾸지 않는다.

1. **VALID Segment는 절대 재번역하지 않는다.**
2. 모델 응답은 **정확히 한 번 parse**하고 그 ParsedResponse를 재사용한다.
3. 실패한 Segment만 repair/split/single 대상으로 보낸다.
4. 서로 다른 두 번역 후보 중 하나를 임의 선택하지 않는다.
5. placeholder 값/개수/순서가 불확실하면 추정 복구하지 않는다.
6. transport error와 validation error는 계속 분리한다.
7. checkpoint/resume 원칙을 유지한다.
8. output은 항상 한국어이며 현재 TXT adapter의 원본 보존 규칙을 유지한다.

---

## 4. Prompt protocol 재설계

### 핵심 계약

**프로그램이 생성한 각 target Segment ID는 한 prompt 안에서 정확히 한 번만 등장해야 한다.**

ID는 오직 실제 번역 대상 행에만 넣는다.

context, failure hint, 설명 블록에는 해당 Segment ID를 다시 쓰지 않는다.

### 권장 구조

batch의 각 대상은 독립 ITEM 블록으로 구성한다.

```text
<<<ITEM 1>>>
문맥은 번역 판단에만 사용하고 출력하지 마십시오.
앞 문맥: [...]
뒤 문맥: [...]
번역 대상:
SEG_00000001<TAB>ja<TAB>원문
<<<END_ITEM 1>>>

<<<ITEM 2>>>
문맥은 번역 판단에만 사용하고 출력하지 마십시오.
앞 문맥: [...]
뒤 문맥: [...]
번역 대상:
SEG_00000002<TAB>ja<TAB>원문
<<<END_ITEM 2>>>
```

`ITEM 1`, `ITEM 2`는 prompt 내부 위치 구분용 ordinal일 뿐이며 **출력 ID가 아니다.** 모델에게 ITEM 번호를 출력하라고 요구하지 않는다.

### repair/single

실패 이유는 같은 ITEM 안에 넣되 ID를 반복하지 않는다.

```text
<<<ITEM 1>>>
문맥은 번역 판단에만 사용하고 출력하지 마십시오.
앞 문맥: [...]
뒤 문맥: [...]
이전 응답 실패 이유: DUPLICATE_ID
수정 지침: 같은 출력 행을 반복하지 마십시오.
번역 대상:
SEG_00000001<TAB>ja<TAB>원문
<<<END_ITEM 1>>>
```

### 금지

다음 구조는 사용하지 않는다.

```text
SEG_00000001 <context>
SEG_00000001 <failure>
SEG_00000001 <target>
```

또한 prompt 설명에 예시용 가짜 `SEG_...` ID를 넣지 않는다.

### Prompt build assertion

가능하면 `build_prompt()` 또는 테스트 헬퍼에서 프로그램이 삽입한 target ID의 발생 횟수를 검증한다.

- 각 target ID: 정확히 1회
- 단, **원문/문맥 자체에 우연히 같은 문자열이 포함되는 경우는 시스템 생성 ID 반복과 구분**해야 한다.
- 따라서 단순 `prompt.count(id) == 1`만으로 프로덕션 오류를 내지 말고, builder 구조 수준 또는 테스트 수준에서 검증하는 방식을 우선한다.

---

## 5. Parser duplicate salvage 정책

현재 parser는 첫 행만 `rows`에 저장하고 같은 ID가 다시 나오면 `duplicates`에 기록한다.

Round 3에서는 **duplicate occurrence의 번역 내용까지 보존**할 수 있어야 한다.

권장 ParsedResponse 확장 예:

```python
rows: dict[str, str]
occurrences: dict[str, list[str]]
identical_duplicates: list[str]
conflicting_duplicates: list[str]
```

동등한 다른 구조도 허용하되 parser는 응답을 다시 parse하지 않아야 한다.

### 동일 duplicate

응답:

```text
SEG_1<TAB>나는 집에 갔다.
SEG_1<TAB>나는 집에 갔다.
```

두 translation 문자열이 **정확히 동일**하면:

- canonical translation은 한 번만 사용
- Segment를 `DUPLICATE_ID` ERROR로 죽이지 않음
- `IDENTICAL_DUPLICATE_ID` WARNING을 남겨도 좋음
- 이후 placeholder/Korean/structure validator는 정상적으로 전부 실행
- 최종 검증을 통과하면 VALID
- 이미 VALID가 된 뒤 duplicate 때문에 repair하지 않음

여기서 동일성은 parser가 추출한 translation 문자열의 **exact equality**를 기본으로 한다. 임의 trim, 문장 정규화, 의미 비교를 하지 않는다.

### conflicting duplicate

응답:

```text
SEG_1<TAB>나는 집에 갔다.
SEG_1<TAB>나는 집으로 돌아갔다.
```

한 글자라도 다르면:

- 기존 `DUPLICATE_ID` ERROR 유지
- 어느 번역도 임의 선택하지 않음
- 해당 Segment만 repair

### 세 번 이상 등장

모든 occurrence가 정확히 동일하면 identical duplicate salvage 가능.

하나라도 다르면 conflicting duplicate로 처리한다.

---

## 6. Validator 계약

`validate_structure()`는 parser의 duplicate 분류 결과를 사용한다.

- identical duplicate → WARNING 또는 진단 메타데이터
- conflicting duplicate → `DUPLICATE_ID` ERROR

기존 R16의 의미는 삭제하지 않는다. 대신 **R16은 conflicting duplicate를 검출하는 회귀 테스트**로 명확히 정의한다.

Global error 때문에 다른 정상 expected row까지 버리면 안 된다. 기존 “good rows survive” 원칙을 유지한다.

---

## 7. Recovery 계약

프로토콜 수정 후에도 recovery 순서는 유지한다.

```text
initial batch
→ 실패 Segment만 repair
→ 필요 시 실패 집합 split
→ 마지막 single
```

단, identical duplicate를 salvage하여 모든 validation을 통과한 Segment는 initial response에서 바로 VALID가 되어야 하며 repair에 들어가면 안 된다.

conflicting duplicate만 repair한다.

repair prompt에서도 target ID는 한 번만 노출한다.

---

## 8. 괄호/따옴표 오류는 이번에 완화하지 않는다

실전에서 다음도 관측되었다.

```text
UNBALANCED_DELIMITERS: 5
UNBALANCED_QUOTES: 2
```

하지만 현재는 DUPLICATE_ID로 인한 반복 repair가 다수 발생한 상태이므로 이 수치만으로 validator가 과민하다고 결론 내리지 않는다.

Round 3에서는:

- 기존 `UNBALANCED_DELIMITERS` ERROR 유지
- 기존 `UNBALANCED_QUOTES` ERROR 유지
- partial loss 정책도 현재 profile 정책 유지

프로토콜 수정 후 동일 소설을 다시 깨끗하게 실행하고 실제 잔여 실패 데이터를 보고 별도 조정한다.

---

## 9. 필수 테스트

기존 R01-R23 및 Round 2 테스트는 모두 통과해야 한다.

추가로 최소 다음 테스트를 만든다.

### R24 — prompt target ID single exposure

2개 이상의 Segment batch prompt에서 시스템이 생성한 각 target ID는 번역 대상 행에 한 번만 배치된다.

context block과 failure metadata에 ID를 반복하지 않는다.

### R25 — repair prompt target ID single exposure

repair/single prompt에서도 target ID는 번역 대상 행에만 한 번 등장한다.

실패 코드는 유지하되 ID를 별도 failure row에 반복하지 않는다.

### R26 — identical duplicate salvage

응답:

```text
SEG_00000001<TAB>정상 번역
SEG_00000001<TAB>정상 번역
```

기대:

- parser parse 1회
- conflicting duplicate 아님
- `DUPLICATE_ID` ERROR 없음
- 정상 validator를 모두 통과하면 VALID
- repair 요청 없음

### R27 — conflicting duplicate remains error

응답:

```text
SEG_00000001<TAB>번역 A
SEG_00000001<TAB>번역 B
```

기대:

- `DUPLICATE_ID` ERROR
- 임의 선택 없음
- 해당 Segment만 repair

### R28 — triple identical duplicate salvage

같은 ID와 같은 translation이 세 번 출력되어도 하나로 canonicalize하고 WARNING 후 정상 검증한다.

### R29 — one conflicting occurrence poisons duplicate set

세 occurrence 중 둘이 같고 하나가 다르면 conflicting duplicate로 처리한다.

### R30 — valid sibling survives conflicting duplicate

한 batch에서 A는 conflicting duplicate, B는 정상 단일 행일 때:

- B는 즉시 VALID/checkpoint
- repair에는 A만 들어감
- B 재번역 금지

### R31 — repair hint without broken translation

repair prompt에는:

- 원문
- 문맥
- 실패 코드/힌트

만 포함한다.

깨진 이전 번역문은 포함하지 않는다.

### R32 — parser remains parse-once

identical/conflicting duplicate 분류를 추가해도 같은 raw model response를 parser에 두 번 전달하지 않는다.

---

## 10. 실전 재시험 절차

Work 구현과 자동 테스트 완료 후 사용자가 로컬에서 같은 파일을 다시 실행한다.

기존 checkpoint에는 Round 2 실패 상태가 섞여 있으므로 **이번 실전 비교는 clean run으로 한다.**

권장 명령:

```powershell
cd C:\trspro
git pull
py -3 main.py "C:\trstxt\a.txt" --mode novel --no-resume
```

필요하면 기존 `a.seori.sqlite`, `a.qa.json`, `a.ko.txt`를 별도 보관하거나 삭제해 비교한다.

첫 목표는 번역 문체 평가가 아니라 프로토콜 안정성이다.

성공 기준:

1. 첫 12문단 batch에서 `VALID 1 / FAILED 11` 같은 대량 구조 실패가 사라질 것
2. `DUPLICATE_ID`가 실질적으로 0에 가깝게 감소할 것
3. 동일 duplicate가 발생하더라도 안전하게 salvage되어 repair 낭비가 없을 것
4. conflicting duplicate는 계속 잡힐 것
5. VALID sibling은 repair에 다시 들어가지 않을 것
6. 그 뒤 남는 `UNBALANCED_DELIMITERS`, `UNBALANCED_QUOTES`, CJK residue 등의 실전 비율을 새로 측정할 것

---

## 11. 이번 Round 3에서 하지 않는 것

다음은 scope 밖이다.

- GUI/UI 구현
- glossary / character memory / story memory
- 초대형 streaming adapter
- job fingerprint 확장
- HTML/Pixiv adapter
- 괄호/따옴표 validator 임의 완화
- 모델 교체
- batch size를 무조건 1로 낮추는 회피책

batch protocol을 실제 HY-MT에 맞게 고치는 것이 우선이다.

---

## 12. Work 완료 조건

Work는 다음을 모두 만족한 뒤 완료로 보고한다.

- prompt 구조 수정
- parser duplicate occurrence 보존
- identical duplicate salvage
- conflicting duplicate ERROR 유지
- repair/single에서도 ID 1회 노출
- R01-R23 유지
- Round 2 테스트 유지
- R24-R32 추가 및 통과
- 전체 unittest 통과
- `compileall` 통과
- `git diff --check` 통과
- 변경 요약과 실제 테스트 결과를 PR에 기록

**핵심 원칙: 모델의 단순 반복은 안전하게 살리되, 서로 다른 후보 중 하나를 추측해서 선택하지 않는다. 그리고 한번 VALID가 된 Segment는 어떤 recovery 단계에도 다시 들어가지 않는다.**
