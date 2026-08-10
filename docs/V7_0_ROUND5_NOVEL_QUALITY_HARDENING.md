# Seori Translator v7.0 — Round 5 Novel Quality Hardening

## Status
Approved follow-up after the first successful real HY-MT native single-Segment run.

Real run result on `C:\trstxt\a.txt`:
- total 235
- VALID 233
- repaired 5
- FAILED 2
- risks 76
- warnings 3
- protocol `hy-mt-native-single`

Round 4 solved the transport/protocol problem. Do **not** redesign the native single-Segment protocol again. Round 5 is quality/validation hardening only.

---

## 1. Non-negotiable invariants

Preserve all current hard invariants:
- HY-MT never sees `SEG_*` / `ADULT_*` bookkeeping IDs.
- One source Segment maps to one Python-owned identity.
- `Segment.source` is immutable.
- VALID is terminal and must never be retranslated in the same job/resume.
- Transport failures remain separate from content/validation failures.
- Repair receives immutable source + reference context + failure reasons, never the broken candidate as source truth.
- Placeholder protection/restoration remains exact.
- Checkpoint/resume behavior remains source-hash safe.
- Failed terminal Segments still fall back to original source in final reconstruction.
- Novel mode keeps no-softening / no-strengthening and preserves adult-content intensity from source.

Do not add GUI yet. Do not reintroduce row-ID batching.

---

## 2. What the real run proved

The core engine is now stable enough to finish a 235-Segment real file. Remaining problems are translation quality and validator policy.

### 2.1 Two FAILED Segments were not general translation failures

`SEG_00000030`
- source is a fully quoted dialogue line.
- HY-MT produced a Korean translation but dropped the outer quote pair.
- final error: `QUOTE_STRUCTURE_LOSS`.
- after 3 attempts the original Chinese source was used as fallback.

`SEG_00000202`
- source contains outer quotes plus two parenthetical asides.
- HY-MT translated the main sentence but omitted the parenthetical contents.
- final error: `BRACKET_STRUCTURE_LOSS`.
- fallback therefore left a full Chinese line in final output.

Conclusion: quote-mark-only loss and semantic parenthetical-content loss must not be treated identically.

### 2.2 Valid output still contained Chinese residue

Observed valid residue examples included mixed forms such as:
- `主卧` partially left untranslated as `주卧`
- `自慰棒` partially left untranslated as `자위棒`

Current `LOW_CJK_RESIDUE` WARNING is too permissive for normal novel prose.

### 2.3 Valid output contained truncation that current validators missed

Observed examples ended abruptly in Korean, e.g. clauses equivalent to:
- `...귀에 울`
- `...진동 소리가 울`

These were marked VALID.

This is a higher-priority correctness problem than many current heuristic RISK flags.

### 2.4 Terminology/semantic mistranslations were not caught

The file exposed repeated Chinese web-novel / adult-fiction terminology errors. These are semantic errors, not censorship.

Observed source concepts and wrong output classes:

- `阴蒂` = female clitoris. The real output repeatedly translated this as the male organ `음경`. This must be prevented.
- `爱液` = female arousal/lubrication fluid. The real output sometimes rendered this as semen `정액`. This must be prevented.
- `内裤` = underwear/panties. The real output repeatedly used `내복`, which is wrong in context.
- `主卧` = master bedroom/main bedroom. Output included `주卧` and at least one contextually wrong room term.
- `自慰棒` = masturbation toy / dildo / vibrator depending context. Do not leave a mixed Chinese-Korean form such as `자위棒`.
- `小穴`, `花壶`, `骚屄`, `穴口`, `阴户` and similar euphemistic/slang references are contextual genital terms in this genre. Do not transliterate or literally map them to unrelated everyday nouns such as `소혈구`, `화분`, or `꽃병`.
- `高开叉` describes a high slit in clothing; avoid malformed literal compounds.

The implementation must preserve source intensity but should not become more clinical than the source unless the source itself is clinical.

Important: do **not** implement blind final-text string replacement. That would be unsafe and overfit.

---

## 3. Round 5 design

### 3.1 Add source-triggered novel terminology hints

Introduce a small, explicit, testable terminology-hint layer for `novel` mode.

Recommended structure:
- a JSON/data file under `profiles/` or `resources/`, e.g. `profiles/novel_zh_terms.json`
- each entry has:
  - source term or pattern
  - semantic meaning / disallowed mistranslation class
  - optional preferred Korean candidates
  - scope/language (`zh`, novel)

Only include a hint in the prompt when the current immutable source Segment actually contains that source term/pattern.

Prompt behavior example concept:

```text
용어 참고(현재 원문에 실제 등장한 항목만):
- 阴蒂: 여성의 클리토리스 의미. 남성 성기 의미로 번역하지 말 것.
- 爱液: 여성의 흥분성 분비액/애액 의미. 정액으로 번역하지 말 것.
- 内裤: 속옷/팬티 의미. 내복으로 번역하지 말 것.
```

Keep this section short. Do not dump a global dictionary into every request.

Rules:
- source-triggered only
- no post-hoc global `str.replace`
- no output rewriting after validation unless it is deterministic punctuation restoration described below
- preserve the source register/intensity
- prefer natural Korean web-novel wording over anatomy-textbook wording when source is slang/euphemistic
- do not soften adult wording merely because it is adult
- do not strengthen a euphemism into a materially harsher term unless necessary for correct meaning

### 3.2 Add terminology regression checks

Add deterministic semantic guardrails for known catastrophic mistranslation classes when the source contains an unambiguous term.

At minimum:
- if source contains `阴蒂`, translation must not contain a male-organ mistranslation.
- if source contains `爱液`, translation must not map it to semen unless independent source text explicitly introduces semen.
- if source contains `内裤`, translation should not use `내복` as the corresponding garment.
- if source contains `主卧`, translation must not retain the Chinese `卧` residue.

These may be ERROR or targeted repair triggers in `novel` mode. Keep checks conservative and source-anchored to avoid false positives.

Do not attempt broad semantic QA for every possible word in Round 5.

### 3.3 Strengthen CJK residue policy for novel mode

Current real run had 3 `LOW_CJK_RESIDUE` warnings and valid mixed-language text.

For `novel` mode:
- unprotected/unwhitelisted CJK residue in a translation should normally be ERROR and repair-eligible.
- allow explicit whitelist classes for intentional names/brands if already supported.
- a single residual Chinese character inside an otherwise Korean common word must not automatically be accepted as a name.
- partial mixed strings like `주卧`, `자위棒` must fail.

For `game`/`document`, keep mode-specific policy configurable rather than globally changing all modes.

### 3.4 Add robust truncation detection

Introduce `TRUNCATED_OUTPUT` or equivalent ERROR for obvious incomplete Korean output.

Use conservative evidence rather than one weak heuristic.

Possible combined signals:
- source is a complete sentence/paragraph ending in normal terminal punctuation or a closing quote;
- translation does not end with terminal punctuation/closing quote when expected;
- translation ends in a bare Korean verb stem / incomplete connective-like fragment (`울`, `하`, `되`, etc.) with no plausible sentence completion;
- source length is materially larger than translation and translation stops mid-clause;
- final character pattern strongly suggests generation cutoff.

Do not fail legitimate short titles, labels, fragments, sound effects, or dialogue intentionally lacking final punctuation.

Create regression tests from the actual real-output shapes equivalent to:
- `...귀에 울`
- `...진동 소리가 울`

These must no longer become VALID.

### 3.5 Deterministic outer punctuation restoration

For a source Segment that is structurally just one complete outer quote pair around the whole content:
- if HY-MT returns otherwise valid Korean content but drops only the matching outer quote pair,
- Python may deterministically restore that outer pair before final validation.

Example:
- source: `「...」`
- candidate: Korean content with no outer quotes
- safe normalization: `「<candidate>」`

Conditions must be strict:
- source has exactly one matching full-span outer quote pair;
- candidate itself is otherwise structurally valid;
- no inner content is added/deleted by Python;
- only deterministic punctuation wrapper restoration is performed.

This should salvage cases like real `SEG_00000030` without 3 model retries and Chinese fallback.

Do **not** use this mechanism for missing parenthetical contents, missing inner quotes, reordered punctuation, or ambiguous structures.

### 3.6 Keep parenthetical-content loss as real failure

For real `SEG_00000202`, HY-MT removed the contents of two source parenthetical asides.

That is semantic loss, not punctuation-only loss.

Therefore:
- keep `BRACKET_STRUCTURE_LOSS` repair-eligible.
- repair prompt should explicitly say that the contents inside each parenthetical aside must also be translated/preserved, not merely the bracket characters.
- if repeated repair still removes the aside meaning, terminal FAILED/source fallback is safer than silently accepting it.

### 3.7 Reduce noisy RISK heuristics

Real run produced 76 RISK flags; `NEGATION_FLIP_RISK` alone appeared 53 times.

These heuristics are currently too noisy to guide actual quality review.

Round 5 rule:
- RISK remains non-blocking.
- do not promote these broad lexical heuristics to ERROR.
- tighten `NEGATION_FLIP_RISK`, `UP_DOWN_FLIP_RISK`, `YES_NO_FLIP_RISK`, `BEFORE_AFTER_FLIP_RISK` so they require stronger source/translation evidence.
- prioritize precision over recall for RISK reporting.
- do not spend repair attempts on RISK-only Segments.

Acceptance target is not zero RISK; target is materially fewer false positives while still surfacing obvious reversals.

---

## 4. Prompt changes

Keep the Round 4 HY-MT-native single request shape.

Do not reintroduce IDs or structured output rows.

The prompt may gain only two narrowly scoped quality sections:
1. source-triggered terminology hints
2. stronger repair hint for specific structural/semantic loss

Keep prompt concise because HY-MT performs best when asked mainly to translate.

For novel mode, preserve existing instructions:
- natural Korean web-novel/light-novel style
- preserve character voice/honorifics
- no arbitrary softening or strengthening
- adult expressions retain source intensity

Add a register instruction such as:
- choose natural Korean genre wording appropriate to the source register; avoid unnecessary clinical/anatomy-textbook wording when the source uses slang, euphemism, or colloquial narration.

Do not add this if it makes the prompt substantially longer than the actual source for tiny Segments; keep reusable instructions compact.

---

## 5. Tests — add Round 5 regression set

Suggested R43-R55:

R43 — outer full-span quote loss is deterministically restored and VALID without model retry when candidate is otherwise valid.

R44 — missing inner/partial quote structure is not auto-restored.

R45 — parenthetical content loss remains ERROR and repair-eligible.

R46 — novel unprotected residual CJK mixed string is ERROR.

R47 — whitelisted intentional proper-name CJK behavior remains configurable/safe.

R48 — obvious Korean mid-clause truncation is `TRUNCATED_OUTPUT` ERROR.

R49 — legitimate short title/fragment is not falsely marked truncated.

R50 — source-triggered terminology hints appear only when matching source terms occur.

R51 — terminology hints do not expose internal Segment IDs and do not include unrelated global dictionary entries.

R52 — source `阴蒂` cannot validate with the observed male-organ mistranslation.

R53 — source `爱液` cannot validate with the observed semen mistranslation unless separate source semantics justify semen.

R54 — source `内裤` + output `내복` is rejected/repair-triggered in novel Chinese profile.

R55 — risk-only Segment is never retranslated; VALID terminal/checkpoint behavior remains intact.

Also retain R01-R42.

---

## 6. Real acceptance test after implementation

Use the same real file as the regression benchmark, preferably as a new clean benchmark job/checkpoint so behavior can be compared intentionally.

Round 5 acceptance goals:

### Hard goals
- 235/235 or near-235 VALID with no Chinese fallback caused by punctuation-only loss.
- zero unprotected mixed Chinese residue in normal Korean prose.
- zero obvious mid-sentence truncations.
- no observed catastrophic semantic terminology swaps listed above.
- special symbols/hearts/emojis/numbers remain preserved as before.
- no regression in no-softening adult-intensity behavior.
- no regression in checkpoint/repair/VALID-never-retranslate.

### Quality goals
- Korean narration/dialogue should read as one coherent web-novel register instead of alternating between clinical anatomy wording, literal Chinese compounds, and ordinary prose.
- fewer noisy RISK flags; do not chase zero.

After this real rerun, inspect `a.ko.txt` and `a.qa.json` again before starting GUI work.

---

## 7. Explicitly out of scope for Round 5

Do not implement:
- GUI / EXE packaging
- HTML/Pixiv adapters
- story memory / character memory
- broad automatic post-editing with another LLM
- blind output replacement dictionary
- reintroduction of multi-row ID protocol
- Phase B tagged batching
- massive-file streaming
- job fingerprinting
- broad model replacement

Round 5 is the last planned engine-quality pass before the simple desktop GUI unless the real rerun reveals another correctness-class bug.
