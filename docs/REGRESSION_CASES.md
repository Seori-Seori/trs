# Mandatory Regression Cases

이 문서의 케이스는 v6 실전 사용 중 실제로 발생한 문제다.
기능 추가/리팩터링 후에도 반드시 자동 테스트되어야 한다.

테스트 원칙:

- 정상 Segment를 다시 번역하지 않는지 함께 확인한다.
- parser 결과를 재파싱하지 않는다.
- ERROR와 RISK를 구분한다.
- 모델 없이 validator/parser 단위 테스트로 재현 가능한 버그는 반드시 deterministic test로 만든다.

## R01 - 중국어 문자 `的` 혼입

원문이 일본어/중국어이고 결과가 대부분 한국어지만 `的` 같은 CJK 원문 문자가 섞인다.

기대:
- Korean validator ERROR 또는 명시된 정책에 따른 repair 대상
- 해당 Segment만 재번역

## R02 - 줄바꿈 placeholder 삭제

expected:

```text
[[PH_0001]]
```

actual translation에서 placeholder가 사라짐.

기대:
- Placeholder validator ERROR
- 임의 위치 추정 복원 금지
- 해당 Segment만 repair

## R03 - 프롬프트 자체 번역/누출

결과에 다음 계열 문자열이 나타남.

```text
Translate the following
번역 규칙
output only
```

기대:
- prompt leak ERROR

## R04 - placeholder 예시 누출

모델이 실제 원문에 없던 다음 예시를 출력.

```text
[[CTRL_n]]
[[CTRL_1]]
```

기대:
- prompt/example leak ERROR
- 실제 등록된 placeholder와 구별

## R05 - 다음 행 ID와 번역이 현재 셀에 병합

```text
SEG_000001<TAB>첫 번역 SEG_000002 다음 번역
```

기대:
- row ID leak ERROR
- SEG_000001만 문제로 표시
- 이미 독립적으로 정상인 다른 Segment는 유지

## R06 - expected ID 누락

expected:

```text
SEG_1 SEG_2 SEG_3
```

actual:

```text
SEG_1 SEG_3
```

기대:
- missing SEG_2 ERROR
- SEG_1/SEG_3이 개별 검증 통과했다면 salvage
- SEG_2만 repair

## R07 - expected보다 +1 unexpected ID

expected:

```text
SEG_1 SEG_2 SEG_3
```

actual:

```text
SEG_1 SEG_2 SEG_3 SEG_4
```

기대:
- unexpected SEG_4 구조 ERROR/anomaly
- SEG_4는 절대 결과에 삽입하지 않음
- 정상 expected rows는 재번역하지 않음

이 케이스는 v6의 실제 검증 구멍이므로 중요도가 높다.

## R08 - Cyrillic 혼입

```text
안녕하세요 Привет
```

기대:
- 원문/허용목록에 없는 Cyrillic 탐지
- unexpected script ERROR

## R09 - Skip 의미 반전

source:

```text
Skip
```

bad translation:

```text
건너뛰지 마세요
```

기대:
- risk validator가 의미 반전 고위험으로 표시
- semantic QA 옵션 활성 시 선택 대상

단 일반 구조 validator가 이 번역의 의미를 100% 판정한다고 가정하지 않는다.

## R10 - 두 행짜리 한 문장의 첫 행 임의 완성

원문의 문장이 Segment 경계 때문에 이어지는데 첫 Segment를 모델이 자기 마음대로 완결시킨다.

기대:
- context를 통해 다음 Segment를 제공
- 문단 단위 segmentation으로 발생 빈도를 줄임
- 구조적으로 완전 자동 판정 불가하면 RISK/semantic QA 대상으로 남김

## R11 - 두 번째 행에 전혀 다른 문장 생성

기대:
- 지나친 원문 잔존/길이 이상/문맥 위험 등의 heuristic 가능
- 완전 의미 검증이 필요한 경우 선택적 semantic QA
- 정상 전체 AI 재검수는 금지

## R12 - 괄호/인용부호 손실

예:

```text
「Alice」
(Alice)
『Alice』
```

기대:
- pair/balance/signature 검사
- 언어별 인용부호 변환 정책은 profile로 허용 가능
- 열림/닫힘 자체가 사라지는 손상은 탐지

## R13 - RPG Maker 제어코드 훼손

예:

```text
\N[1]
\F[10]
```

기대:
- v7.3 이전에도 generic placeholder 엔진 단위 테스트로 존재
- protect -> translate mock -> restore round trip 100% 동일

## R14 - 영어 단어가 이상하게 섞인 신음/의성어

한국어 출력에 무작위 영어 토큰이 비정상적으로 혼합.

기대:
- Latin 문자 전체 금지 금지
- 고유명사/의도적 영문은 허용
- 과도한 혼합은 heuristic WARNING/RISK
- source와 glossary 정보를 고려

## R15 - 한 행에 여러 행 번역을 합침

하나의 expected row translation이 비정상적으로 다른 row 내용까지 포함.

기대:
- row ID leak가 있으면 ERROR
- ID가 없더라도 비정상 길이/다중 출력 패턴 heuristic 적용 가능
- 문제 Segment만 repair

# 추가 필수 구조 테스트

## R16 - duplicate ID

동일 ID 두 번 출력 -> ERROR.

## R17 - output order mismatch

설정에서 strict order 사용 시 expected order와 다르면 ERROR.

## R18 - translation empty

빈 문자열/공백만 존재 -> ERROR.

## R19 - placeholder duplicate

원본 1개, 결과 2개 -> ERROR.

## R20 - placeholder order changed

순서 보존이 필요한 placeholder의 순서가 바뀜 -> ERROR.

## R21 - prompt response code fence

모델이 ``` 등을 덧붙여도 parser가 정책에 맞게 거부/정규화하고 구조 검증한다.

## R22 - resume never retranslates VALID

mock translator 호출 횟수를 검사하여 VALID + 동일 source hash Segment가 재호출되지 않는지 확인한다.

## R23 - partial repair never retranslates good rows

10개 중 2개 ERROR 시 mock translator에 repair 대상으로 정확히 2개만 전달되는지 확인한다.

# Round 6 일반화 재설계 회귀

## R67 - 기본 prompt 최소화

일반 번역 요청에 용어집, register 목록, 후보 번역, 금지어, 작품별 이름이 포함되지 않는다.

## R68 - 작품별 기본 이름 제거

shipped profile에 특정 작품 이름 매핑이 없으며 비어 있지 않은 legacy `name_map`은 거부한다.

## R69 - dictionary dump 금지

Class B language pack의 항목이 source 등장 여부와 무관하게 prompt에 노출되지 않는다.

## R70 - mapped name round trip

명시적 이름 매핑은 `[[NAME_0001]]`로 보호되고 정확한 목표 이름으로 복원된다.

## R71 - mapped placeholder 무결성

누락, 중복, unexpected placeholder, 순서 변경은 모두 ERROR이며 위치를 추정 복원하지 않는다.

## R72 - 작업별 mapping 격리

같은 source 이름이라도 서로 다른 작업은 서로 다른 목표 이름으로 매핑할 수 있다.

## R73 - mapping 부재 시 자동 정규화 금지

명시적 mapping이 없으면 고유명사를 발견하거나 canonicalize하지 않는다.

## R74 - Class B validator-side only

고신뢰 의미 범주 충돌은 validator가 source-anchored ERROR로 잡되 해당 규칙은 prompt에 없다.

## R75 - Class C 전역 치환 금지

문맥 의존 비속어·은유는 결정적 replacement나 전역 hard ERROR가 아니다.

## R76 - 짧은 범용 성인소설 정책

novel profile은 작품별 지식 없이 장르 강도와 자연스러운 한국어를 지시하는 짧은 정책만 쓴다.

## R77 - 의료 문맥의 임상 표현 허용

의료 문맥에서는 임상 용어가 novel register RISK를 잘못 발생시키지 않는다.

## R78 - bounded repair prompt

repair에는 source, context, 오류 코드와 최대 3개의 짧은 의미 범주만 들어가며 깨진 번역,
후보 사전, 금지어 목록, 내부 Segment ID는 들어가지 않는다.

## R79 - terminal failure source fallback

bounded repair가 끝까지 실패하면 출력에는 immutable source를 남기고 QA에 terminal failure를 기록한다.

## R80 - mapped checkpoint/resume

동일 source와 저장된 mapping의 VALID checkpoint는 모델 호출 없이 재사용한다. 다른 mapping으로
resume하면 거부한다.

## R81 - game mapping 격리

game 모드에서도 명시적 mapping은 작동하지만 novel register 정책이 prompt에 새지 않는다.

## R82 - document 정책 격리

document 모드 prompt에는 novel register 정책과 작품별 지식이 없다.

# 완료 조건

R01~R23은 초기 필수 회귀 집합이다. 이후 승인된 프로토콜 회귀는 Round별 문서에
정의되며, 현재 구현은 `V7_0_ROUND6_GENERALIZATION_RESET.md`의 R67~R82를 포함해
R01~R82가 자동 테스트로 존재하고 통과해야 한다. R26~R29의 row-ID 검사는 레거시
parser 호환성에만 적용되며 기본 HY-MT native 경로의 wire contract가 아니다.
