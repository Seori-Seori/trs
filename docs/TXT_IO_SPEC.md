# TXT Input / Output Specification (v7.0)

## 목표

v7.0의 첫 완성형은 `TXT -> 한국어 TXT`다.

TXT는 웹소설, Pixiv 저장본, 복사한 본문, 일반 문서 등 어디서든 쉽게 만들 수 있으므로 가장 먼저 안정화한다.

## 기본 사용

```bash
python main.py input.txt
```

BAT 예:

```bat
run_translation.bat input.txt
```

기본 출력:

```text
input.txt
input.ko.txt
input.seori.sqlite
input.qa.json
backup/input.<timestamp>.txt
```

출력 파일 이름은 config로 변경 가능하게 설계한다.

## 입력 인코딩

v7.0 기본:

1. UTF-8 BOM 감지
2. UTF-8 시도
3. 실패 시 명시적 오류

초기 버전에서 임의로 CP949/Shift-JIS를 추측해 잘못 해석하지 않는다.
향후 encoding detection 옵션을 추가할 수 있다.

## 줄바꿈

원본의 주 줄바꿈 스타일을 감지한다.

- CRLF (`\r\n`)
- LF (`\n`)

가능하면 출력에도 동일 스타일을 사용한다.

## 문단 분리

기본 번역 단위는 물리적 한 줄이 아니라 문단이다.

빈 줄로 나뉜 텍스트 블록을 기본 paragraph 후보로 사용한다.

예:

```text
첫 문단 첫 줄
첫 문단 둘째 줄

두 번째 문단

세 번째 문단
```

Adapter는 다음처럼 관리할 수 있다.

```text
TextPart(paragraph)
Separator("\r\n\r\n")
TextPart(paragraph)
Separator("\r\n\r\n")
TextPart(paragraph)
```

Core에 전달하는 것은 paragraph Segment뿐이다.
Separator는 Adapter가 보존한다.

## 문단 내부 줄바꿈

문단 내부의 줄바꿈은 의미가 있을 수 있으므로 삭제하지 않는다.

예:

```text
「待って！」
彼女は叫んだ。
```

v7에서는 구조적 줄바꿈을 protected token으로 치환할 수 있다.

```text
「待って！」[[PH_0001]]彼女は叫んだ。
```

모델이 `[[PH_0001]]`를 삭제/복제/순서변경하면 ERROR다.

v6처럼 번역문 길이를 기준으로 줄바꿈 위치를 추정하여 억지 복원하지 않는다.

## 빈 줄

빈 줄은 번역 대상 Segment로 만들지 않는다.
원본 위치 그대로 보존한다.

연속 빈 줄도 가능한 한 개수를 보존한다.

## whitespace

문단 앞뒤 whitespace가 파일 구조상 의미 있을 수 있으므로 Adapter가 보존한다.
Core의 번역 대상 문자열과 reconstruction metadata를 분리한다.

예:

```text
"    대사"
```

에서 들여쓰기를 번역 모델에 맡기지 않고 metadata로 보호하는 방식을 허용한다.

## Segment ID

파일 내 안정적인 순번 ID를 생성한다.

```text
SEG_00000001
SEG_00000002
SEG_00000003
```

ID는 모델 출력 매핑용이며 최종 TXT에는 삽입하지 않는다.

## 문맥

현재 문단 번역 시 주변 문단을 context로 제공한다.

기본 profile 후보:

```text
context_before: 3
context_after: 2
```

하지만 모델 출력은 target Segment만 반환해야 한다.

context 문단을 번역 출력에 섞으면 구조 오류다.

## 긴 문단

한 문단이 모델 제한을 넘는 경우 Adapter가 임의로 문장을 잘라서는 안 된다.
Core의 safe split 전략을 사용한다.

우선순위 예:

1. 문장 경계
2. 구두점 경계
3. whitespace 경계
4. 마지막 fallback

일본어/중국어 구두점도 고려한다.

- 일본어: `。！？「」『』…`
- 중국어: `。！？“”`
- 영어/한국어: `.!?` 등

Split된 조각은 원래 하나의 Segment라는 parent metadata를 유지하여 재결합한다.

## 번역 완료 조건

각 Segment는 다음을 만족해야 최종 TXT에 들어갈 수 있다.

- expected ID 존재
- unexpected/duplicate ID 없음
- translation non-empty
- prompt leak 없음
- row ID leak 없음
- placeholder 정확히 보존
- 허용되지 않은 대량 CJK 잔존 없음
- 예상 외 script 없음
- 필수 구조 검사 통과

RISK만 있는 경우 정책에 따라 번역을 유지하면서 QA report에 기록할 수 있다.

## 원본 보존

번역 시작 전에 원본 backup을 만든다.

출력은 원본 파일을 기본적으로 overwrite하지 않는다.

`--overwrite` 같은 옵션은 나중에 추가할 수 있지만 v7.0 기본값은 새 파일 생성이다.

## Resume

같은 입력 파일을 다시 실행하면 checkpoint를 조회한다.

동일 `source_hash`이고 status가 VALID인 문단은 재번역하지 않는다.

원본 일부만 수정된 경우 수정된 Segment만 PENDING으로 돌릴 수 있도록 설계하되, v7.0 첫 구현에서는 파일/Segment hash 정책을 보수적으로 시작해도 된다.

## 성공 기준

v7.0 TXT adapter는 다음 시나리오가 가능해야 한다.

```text
1000개 문단 TXT 입력
 -> 중간에 프로그램 종료
 -> 재실행
 -> 완료된 VALID 문단 재호출 없음
 -> 실패 문단만 복구
 -> 원래 문단/빈 줄 구조 유지
 -> 최종 한국어 TXT 생성
```

이 시나리오가 안정적으로 동작한 뒤 HTML/RPG Maker adapter로 확장한다.
