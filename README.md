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

v7.0은 TXT 소설 번역을 완성하는 최소 핵심 엔진이다.

```text
TXT
 -> TextAdapter
 -> Segment[]
 -> language detection
 -> placeholder protection
 -> context build
 -> batching
 -> Ollama translation
 -> parse once
 -> validators
 -> partial repair only for failed segments
 -> split fallback
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
run_translation.bat novel.txt
```

또는

```bash
python main.py novel.txt
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

- v7.0: Python core + TXT + Ollama + validators + checkpoint + partial repair
- v7.1: glossary + character memory + story memory + translation memory
- v7.2: HTML + Pixiv 저장 페이지
- v7.3: RPG Maker adapter
- v7.4: JSON / CSV / Excel
- 이후: EPUB / GUI

상세 구현 계약은 `docs/` 문서를 따른다.
