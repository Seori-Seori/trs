# Seori Translator v7.0 Round 2 Review / Work Handoff

This document records the approved second-round fixes after reviewing the first full TXT implementation on PR #1.

## Goal

Do not redesign the project. Keep the existing v7.0 architecture and invariants, then harden the implementation for the first real local HY-MT smoke test.

The next milestone is:

```text
real Japanese/Chinese TXT
-> user selects translation mode
-> local Ollama/HY-MT
-> Korean TXT
-> resume/checkpoint
-> QA
```

After this round, the user intends to run real Pixiv-style novel TXT files (typically about 70k-100k characters) on the target Windows machine.

## Do not implement in this round

The following ideas are intentionally deferred because they are unnecessary for the current personal-use workload:

- global job fingerprint across model/profile/prompt versions
- massive-file streaming architecture
- multi-million-segment optimization
- glossary / character memory / story memory / translation memory (v7.1)
- HTML / RPG Maker / JSON / CSV / Excel
- GUI

The user will not intentionally change model/mode halfway through one translation job. Keep checkpoint safety practical, not enterprise-grade.

---

# A. Required fixes before the real smoke test

## A1. Ollama preflight check

`OllamaTranslator.health_check()` already exists. Use it before starting translation.

Expected behavior:

1. Check that Ollama is reachable.
2. Check that the configured model is visible.
3. If either fails, stop immediately with a clear Korean error message.
4. Do not create a storm of Segment repair attempts when the transport itself is unavailable.

Examples:

```text
오류: Ollama에 연결할 수 없습니다: http://127.0.0.1:11434
```

or

```text
오류: Ollama에 모델이 없습니다: huihui_ai/hy-mt1.5-abliterated:7b
```

A transport/server failure is not a translation-quality failure.

## A2. Separate transport failure from Segment validation failure

Current recovery treats HTTP/connection failures too similarly to bad model output.

Change the policy:

```text
TRANSPORT ERROR
- connection refused
- timeout
- HTTP failure
- invalid Ollama response envelope

=> job/request-level failure
=> do not split the batch
=> do not partial-repair individual Segments
=> do not consume Segment semantic/validation retry budget merely because Ollama is offline
```

But these remain Segment recovery failures:

```text
- missing ID
- duplicate ID
- CJK residue
- placeholder loss
- malformed translation row
- prompt leak
- row merge
```

Keep the hard invariant: VALID Segments never re-enter translation.

## A3. Batch by actual prompt size, not target text alone

`max_batch_chars` currently counts prepared target text, but the actual prompt also contains repeated before/after context and instructions.

Add a real prompt-budget guard.

Recommended simple v7.0 solution:

- add `translation.max_prompt_chars`
- before sending a group, build/estimate the actual prompt
- if it exceeds the configured limit, reduce/split the target group before calling Ollama
- never split a protected token
- preserve current long-Segment safe split behavior

No tokenizer dependency is required. Character-count budgeting is sufficient for this round.

The purpose is to prevent context duplication from making a seemingly small 12k target batch into an unexpectedly huge model request.

## A4. Fix Windows-path placeholder greediness

Review the built-in Windows path regex.

It must not accidentally protect the Korean/Japanese/Chinese sentence following an unquoted path.

Bad example to avoid:

```text
파일은 C:\game\data.txt 에 저장되어 있습니다.
```

The protected token must be only the path, not:

```text
C:\game\data.txt 에 저장되어 있습니다.
```

Suggested policy:

- ordinary unquoted paths stop at whitespace
- quoted paths may contain spaces and should be handled separately
- keep UNC path support

Add regression tests.

## A5. Give repair prompts the validation failure reason

Partial repair currently retranslates from source/context, which is correct. Improve the repair instruction by including validation error codes for the failed Segment.

Do NOT feed the broken translated sentence back as the semantic source of truth.

Repair input should conceptually contain:

```text
source
context
failure codes
```

Examples:

```text
MISSING_PLACEHOLDER
KNOWN_BAD_CJK_RESIDUE
ROW_ID_LEAK
KANA_RESIDUE
EMPTY_TRANSLATION
```

Map common failure codes to short targeted instructions, for example:

- `MISSING_PLACEHOLDER` -> preserve every protection token exactly once and in order
- `KNOWN_BAD_CJK_RESIDUE` / `KANA_RESIDUE` -> return Korean only except intentionally protected values
- `ROW_ID_LEAK` -> output only this target's translation

Keep the prompt minimal. The old v5/v6 experience showed that verbose examples can leak into model output.

## A6. Resume must not silently reuse a checkpoint from a different source file state

Do not add a broad job fingerprint.

Do add a narrow source-file safety check:

- when a checkpoint already contains `source_sha256`, compare it with the current input file SHA-256 before resume
- if different, do not silently overwrite metadata and reuse old rows
- safest v7.0 behavior: refuse resume with a clear message and tell the user to use `--no-resume` / a fresh checkpoint

This is only a source-file integrity guard, not model/profile versioning.

---

# B. Translation modes - approved

The user explicitly wants to choose the translation purpose instead of automatic classification.

Implement three modes:

```text
1. novel
2. game
3. document
```

Do not auto-detect the mode.

File format and translation mode are separate concepts. TXT can be used with any of these modes.

## B1. CLI behavior

Recommended interface:

```bash
python main.py input.txt --mode novel
python main.py input.txt --mode game
python main.py input.txt --mode document
```

Windows BAT must forward the option unchanged.

For friendly personal use, if `--mode` is omitted and stdin is interactive, show:

```text
번역 모드를 선택하세요.
1. 소설
2. 게임
3. 일반 문서
선택: 
```

For non-interactive execution, either require `--mode` or retain `novel` as a documented fallback. Prefer deterministic behavior over guessing.

`--profile` may remain as an advanced override if useful, but normal users should choose `--mode`.

## B2. `novel` mode

Primary goal:

```text
context + natural Korean + character voice
```

Default style:

- natural Korean web-novel / light-novel prose
- paragraph is the basic TXT Segment
- use previous/next paragraphs as context
- preserve meaning and intensity
- no censorship or invented content
- protected tokens remain strict
- semantic number/direction/state checks usually remain RISK rather than automatic ERROR

Keep existing `profiles/novel.json` behavior as the base.

## B3. `game` mode

Primary goal:

```text
structure + exact functional meaning + terminology consistency
```

Game mode must be stricter than novel mode for:

- placeholders/control codes
- Yes / No
- Skip / Don't Skip
- enable / disable
- open / close
- lock / unlock
- left / right / up / down
- numbers used as gameplay values
- short menu/UI strings

Do not make game mode artificially literary.

For TXT game strings, keep the same core engine but use a game profile/policy. Future RPG Maker adapters should plug into this same mode.

## B4. `document` mode

Primary goal:

```text
conservative information-preserving Korean
```

Policy:

- less stylistic rewriting than novel mode
- preserve numbers, URLs, paths, names and technical values carefully
- no invented explanations
- natural but relatively faithful Korean

Create:

```text
profiles/novel.json
profiles/game.json
profiles/document.json
```

The core pipeline must not contain `if mode == game` spaghetti where profile/config policy can express the difference cleanly.

---

# C. Validator hardening

## C1. Row-ID leak detection must use actual expected IDs

Do not rely only on hard-coded `SEG_` / `ADULT_` regexes.

For every response, also scan each translation for any other expected Segment ID from the current request.

This keeps the core reusable when future adapters use other ID shapes.

Hard-coded known legacy patterns may remain as an additional heuristic.

## C2. Improve quote/bracket preservation slightly

Current validator detects total disappearance and imbalance, but partial pair loss can still pass.

Make it conservative without demanding identical punctuation style across Japanese/Chinese/Korean.

Desired behavior:

- translated delimiters must be balanced
- Japanese quotes may become Korean/typographic quotes
- if source contains multiple structural quote/bracket pairs and translation loses a meaningful number of them, report ERROR or at minimum a strong RISK according to mode

Do not require exact character-for-character quote style preservation in novel mode.

## C3. Preserve unexpected-script checks

Keep Cyrillic/Greek/etc unexpected-script detection.

Do not convert `EXCESSIVE_LATIN_MIX_RISK` into a blanket ban: names, acronyms and intentional English can be valid.

## C4. Mode-aware risk severity

Where practical, allow profile/mode to influence risk severity.

Examples:

```text
Novel:
"3 years" -> "삼 년" can be stylistically valid
number change => usually RISK

Game:
"HP +30"
30 changed/missing => should be much stricter
```

Likewise for directional/state UI concepts.

Do not add universal second-pass AI review.

---

# D. Checkpoint / reporting quality-of-life fixes

These are useful but should stay simple.

## D1. Batch checkpoint writes where practical

Current per-Segment SQLite commits are safe but unnecessarily chatty.

Prefer committing the VALID rows of one translated batch in one transaction while keeping completed work durable after each batch.

Do not sacrifice resume safety for micro-optimization.

## D2. Avoid duplicate backups on ordinary resume

If the same source SHA already has a valid existing backup for the same checkpoint/job, reusing that backup is acceptable instead of copying the same TXT every resume run.

This is a convenience optimization, not a blocker. Keep the rule that an original source must have a backup before translation/reconstruction.

## D3. Keep QA useful for debugging

Terminal failures should contain enough information to diagnose real HY-MT problems.

For terminally failed Segments, record the last raw model response or a safely bounded representation of it, in addition to:

- source
- failure codes
- attempt count
- last error

Do not dump every successful raw response into QA.

For current 70k-100k character novels, full per-Segment QA metadata is acceptable. No streaming/huge-file QA redesign is needed yet.

---

# E. Keep these existing behaviors unchanged

The following first-round behavior is approved and must not regress:

1. `Segment.source` is immutable.
2. VALID Segment can never re-enter translation/batching/prompt/recovery.
3. Parse each model response exactly once.
4. Salvage valid expected rows even if another row fails.
5. Unexpected extra IDs are detected but do not force valid expected rows to be retranslated.
6. Partial repair requests only failed rows.
7. Split/single fallback happens only after failed-row repair.
8. Placeholder count/order/value mismatch is ERROR; never guess missing positions.
9. Source TXT is never overwritten.
10. Source SHA is checked again before reconstruction.
11. Failed final Segments fall back to original source in the output and are listed in QA.
12. Output language remains Korean only.
13. No universal AI re-review pass.
14. Existing R01-R23 regression tests must continue passing.

---

# F. Tests to add for Round 2

At minimum add deterministic tests for:

1. Ollama unavailable -> fail fast before translation loop.
2. Transport failure does not trigger Segment split/partial-repair storm.
3. Transport failure does not incorrectly consume semantic retry budget.
4. Actual prompt budget causes a target group to be reduced/split before send.
5. Unquoted Windows path stops at whitespace.
6. Quoted Windows path with spaces round-trips correctly.
7. Repair prompt receives `MISSING_PLACEHOLDER` failure reason without using broken translation as source.
8. Repair prompt receives CJK-residue failure reason.
9. Existing checkpoint source SHA differs -> resume is refused safely.
10. `--mode novel` loads novel policy.
11. `--mode game` loads game policy.
12. `--mode document` loads document policy.
13. Another current expected ID embedded inside a translation is `ROW_ID_LEAK` even if the ID prefix is not `SEG_` or `ADULT_`.
14. Game-mode number/state reversal receives stricter treatment than novel mode where configured.
15. Existing R01-R23 still pass.
16. Full mock HTTP TXT integration test still passes for all three modes where appropriate.

---

# G. Definition of Round 2 done

Round 2 is done when:

```text
python -m unittest discover -s tests -v
```

passes, compile checks pass, and the following real-user path is ready:

```text
1. Start Ollama.
2. Confirm HY-MT model is installed.
3. Put a real Japanese or Chinese novel into UTF-8 TXT.
4. Run Seori Translator in novel mode.
5. Receive .ko.txt + checkpoint + QA.
6. Interrupt/re-run once to verify resume.
7. Inspect actual translation quality and any validator false positives.
```

Do not add more architecture before this real smoke test unless a test exposes a concrete correctness bug.
