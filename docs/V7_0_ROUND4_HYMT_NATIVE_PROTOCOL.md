# Seori Translator v7.0 Round 4 — HY-MT Native Protocol

## Status

Approved for implementation.

Round 4 is not another attempt to tweak row IDs. The architectural conclusion from two real local HY-MT runs is that model-visible row IDs are the wrong protocol for this model family.

## Real-world evidence

### Round 2 local run

Input: `C:\trstxt\a.txt`
Mode: `novel`
Model: `huihui_ai/hy-mt1.5-abliterated:7b`
Segments: 235
Batch size: 12

Observed failure summary before user stopped the run:

- FAILED: 20
- `DUPLICATE_ID`: 16
- `UNBALANCED_DELIMITERS`: 5
- `UNBALANCED_QUOTES`: 2

The old prompt exposed the same target ID multiple times in context/failure/target sections. HY-MT frequently reproduced the same ID more than once.

### Round 3 local run

Round 3 changed the prompt so each target ID was exposed only once.

Observed first batch:

- VALID: 0
- FAILED: 12
- all 12 failures: `MISSING_ID`

Every target in the first batch failed because the model response contained none of the requested `SEG_...` row IDs.

This is decisive enough to change the protocol rather than keep tuning ID formatting.

## Architectural conclusion

**Program IDs must become internal-only metadata.**

The model must not be responsible for copying, preserving, inventing, ordering, or returning `SEG_...`, `ADULT_...`, or any future Segment ID.

Python owns:

- Segment identity
- ordering
- source hash
- checkpoint mapping
- validation state
- repair state
- source/output reconstruction

HY-MT owns only:

- translation of the text presented to it

The transport/prompt layer must adapt the model to the core, not force the model to behave like a structured general-purpose instruction-following LLM.

## Non-negotiable invariants

These existing v7 invariants remain unchanged:

1. A VALID Segment is never retranslated.
2. `Segment.source` remains immutable.
3. Only failed/problem Segments may enter repair.
4. A model response is parsed/interpreted once per request and the result is reused.
5. Transport/server failures are job-level failures and do not consume semantic retry attempts.
6. Placeholder protection/restoration remains exact; never guess missing token positions.
7. Checkpoint/resume remains source-hash safe.
8. Failed output falls back to the original source in reconstructed TXT.
9. Adult content in `novel` mode is not softened or censored by program instructions; preserve source intensity.
10. Existing structure/language/risk validators remain active unless explicitly changed in a later review.

## Round 4 primary design

### Phase A — make single-Segment native translation the correctness baseline

First implement a protocol where one model request maps to exactly one target Segment.

The request must not contain any Segment ID.

Conceptual flow:

```text
Python Segment SEG_00000042
        |
        | internal mapping only
        v
HY-MT prompt:
  context (optional, clearly marked as reference only)
  source language / target Korean instruction
  exactly one target source string
        |
        v
model raw translation text only
        |
        v
Python attaches response back to SEG_00000042
        |
        v
placeholder restore + validators + VALID/repair/FAILED
```

The association between request and Segment is positional/in-memory: the caller already knows which Segment generated the request. No ID is needed in the model output.

### Single request prompt requirements

Use the simplest prompt compatible with the existing profile and HY-MT behavior.

Requirements:

- translate exactly one target into Korean
- return translation only
- no `ID<TAB>translation` requirement
- no model-visible Segment ID
- keep profile instructions such as novel tone and adult-intensity preservation
- context may be included, but it must be clearly reference-only and must not need to be reproduced
- do not feed the previous broken translation into repair prompts
- repair may include failure reason/hint, source, and context

The prompt builder should expose a dedicated single-target/native API rather than pretending this is the old batch row protocol.

Recommended separation:

```python
build_single_translation_prompt(segment, profile, mode=...)
```

Do not make callers parse the old batch prompt to infer behavior.

## Response handling for single translation

A single-target response is not a row table.

Do not use `ResponseParser` row-ID parsing as the primary parser for this request type.

Introduce a distinct response abstraction, for example:

```python
@dataclass(frozen=True)
class SingleTranslationResponse:
    translation: str
    raw_response: str
```

or an equivalent clean design.

Normalization may remove only safe wrapper noise that is explicitly supported, such as a single enclosing Markdown code fence if desired. Do not heuristically delete arbitrary lines simply because they look like explanations.

After extracting the candidate:

1. validate placeholder structure
2. restore placeholders
3. validate quote/bracket structure
4. validate Korean / script residue
5. validate semantic risks
6. mark VALID only if there are no ERRORs

The existing validators should be reused rather than duplicated.

## Recovery behavior

For the correctness baseline, recovery is per Segment.

Example:

```text
initial single request
  -> valid: checkpoint immediately
  -> invalid: repair single request using source + context + error codes
  -> still invalid: remaining configured single retries
  -> exhausted: FAILED
```

There is no batch split for a request that already contains one Segment.

Long Segment splitting remains valid: safe-split a long Segment into child pieces, translate children one-by-one, validate, join, then validate the parent.

Never route a VALID sibling into a retry because another Segment failed.

## Performance tradeoff

Round 4 correctness baseline is allowed to be slower than the existing 12-row batch implementation.

Correctness comes first. Do not reintroduce model-visible IDs simply to recover throughput.

Once the single-target path is proven on the real local file, throughput optimization can be attempted separately.

## Phase B — optional HY-MT-native tagged batching experiment

Only after Phase A is fully working, Work may add an experimental multi-segment batching path if it is strictly safer than the removed row-ID protocol.

The preferred experiment is model-native/simple structural tags, not internal Segment IDs.

Conceptual example only:

```text
<sn>source paragraph A</sn>
<sn>source paragraph B</sn>
<sn>source paragraph C</sn>
```

Python maps the N input tag positions to N Segment objects by order.

The model must not see `SEG_...` IDs.

### Tagged batch acceptance criteria

A tagged batch is usable only when all of the following are deterministically true:

1. output contains exactly N target units
2. tag order is preserved
3. no units are missing
4. no extra units are generated
5. each output unit can be mapped to exactly one input position
6. placeholders validate per unit
7. each mapped Segment passes all normal validators independently

If structural extraction fails, **do not assign uncertain outputs to Segments**.

Fallback must be the single-Segment native path for only the affected/requested Segments.

Do not guess alignment by semantic similarity, length, punctuation, or nearest position after a missing/extra unit.

### Tagged batch is optional for this Round

If implementing tagged batching substantially complicates Round 4 or cannot be proved with tests, defer it. A correct single-Segment implementation is a valid Round 4 completion.

## Parser compatibility

The existing row-oriented `ResponseParser` may remain for legacy tests or future adapters, but the HY-MT native single path must not depend on row IDs.

Do not delete useful row parser tests simply to make Round 4 pass. Instead separate protocols explicitly.

Suggested protocol concepts:

```text
single/native protocol -> raw translation text -> known Segment
optional tagged batch   -> ordered structural units -> ordered Segments
legacy row protocol     -> retained only where explicitly needed, not HY-MT default
```

## Prompt policy by mode

Existing manual modes remain:

- `novel`
- `game`
- `document`

`novel` must continue to include the current intent:

- do not arbitrarily soften or intensify source meaning
- preserve adult expression intensity
- preserve facts, numbers, proper nouns, negation, state
- natural Korean web-novel/light-novel style
- preserve character speech/honorific consistency when context allows

Round 4 must not accidentally drop these profile instructions when simplifying the wire prompt.

## Placeholder and special-character requirements

The current real test file intentionally includes mixed punctuation/symbols and is intended to exercise this path.

Round 4 must preserve the existing placeholder system and must continue to handle, where present:

- internal newlines
- hearts and emoji/symbols
- Japanese/Chinese quote marks
- parentheses/brackets
- numbers
- Latin proper nouns / labels
- game control codes via configured patterns

Do not create a blanket rule that all punctuation or Unicode symbols must be escaped into placeholders. Protect only what requires exact preservation under the existing configurable placeholder policy.

## Diagnostics requirement

The previous debugging cycle was slower because aborted runs did not make the last raw model response easy to inspect.

Round 4 should improve diagnostics without creating huge logs.

Required:

- on terminal Segment failure, retain bounded `last_raw_response` as already designed
- add an explicit debug-friendly way to inspect a failed request/response pair for a single Segment
- do not dump every successful model response by default
- do not store unbounded raw responses in SQLite

A config/CLI debug option is acceptable, but keep normal operation quiet.

If adding a debug artifact, it should include enough to diagnose protocol behavior:

- Segment ID (program-side only)
- mode
- source text
- rendered prompt
- raw model response
- final error codes

It must not alter translation behavior.

## Test requirements

Keep all existing applicable tests passing. Tests that assert the old HY-MT row-ID wire protocol should be replaced with tests for the new explicit native protocol rather than preserved as false requirements.

Add deterministic tests covering at least:

### R33 — native single prompt contains no Segment ID

Build a single translation prompt and assert the Segment ID does not appear anywhere in the rendered model prompt.

### R34 — single raw translation maps to known Segment

Given one Segment and raw response `정상 번역`, verify it is evaluated and attached to that Segment without requiring an ID prefix.

### R35 — native single success becomes VALID in one request

Mock translator returns a valid Korean translation. Assert exactly one translator call, one validation path, VALID state, checkpoint callback invoked.

### R36 — one failed Segment repair never retranslates a VALID sibling

Two Segments: one becomes VALID, one fails validation. Assert only the failed Segment receives repair calls.

### R37 — repair prompt contains no Segment ID and no broken candidate

Assert repair prompt may contain source/context/error codes/hints but contains neither the program Segment ID nor previous broken translation.

### R38 — transport failure remains job-level

Single translation transport exception must not consume semantic attempt count or change Segment into semantic FAILED.

### R39 — placeholders survive native single protocol

Protect a newline/control token/synthetic configured placeholder, translate, restore, and validate exact preservation.

### R40 — adult-intensity novel instruction survives prompt simplification

Load `novel` profile and assert its no-softening/adult-intensity instruction is still included in the native novel prompt.

### R41 — single terminal failure retains bounded raw response

Force validation exhaustion and verify final diagnostic state contains the last raw response with configured truncation behavior.

### R42 — resume never calls translator for existing VALID Segment

Same source hash and validated checkpoint result -> zero new generation calls.

### Optional tagged-batch tests, only if Phase B is implemented

- exact N-in/N-out ordered mapping
- missing tag -> no guessing, fallback to singles
- extra tag -> no guessing, fallback to singles
- reordered units -> reject/fallback unless protocol guarantees a deterministic safe order mapping
- one invalid mapped unit -> valid siblings remain VALID and only failed unit repairs
- placeholders validated independently per mapped unit

## Integration test requirements

Mock Ollama HTTP integration must cover at least:

1. one TXT document -> single/native translations -> reconstructed Korean TXT
2. novel/game/document profile selection
3. checkpoint/resume with zero calls for already VALID Segments
4. Ctrl+C/partial completion safety where practical at pipeline level
5. one invalid Segment among multiple does not cause retranslation of completed VALID Segments

## Real local acceptance test

After Work implementation, user will run on Windows against real Ollama/HY-MT:

```powershell
cd C:\trspro
git pull
py -3 main.py "C:\trstxt\a.txt" --mode novel --no-resume
```

The test file has 235 paragraphs and intentionally exercises adult Chinese prose, quotes, punctuation, symbols, numbers, mixed Latin text, and internal newlines.

### Acceptance expectations

For the single/native baseline, the old batch display may change because requests are no longer 12-row model batches. Progress reporting should remain understandable, e.g. Segment count/progress rather than pretending row batches still exist.

Success criteria:

- no `MISSING_ID` / `DUPLICATE_ID` failures from the HY-MT default protocol, because IDs are no longer model-visible
- completed VALID Segments checkpoint immediately
- failed Segment does not poison completed siblings
- translated output preserves structure/symbols according to validators
- novel prompt preserves non-softening/adult-intensity policy

Do not claim complete quality success from VALID count alone; user will inspect Korean output and QA report afterward.

## Explicitly out of scope

Do not add these in Round 4 unless required by the protocol refactor itself:

- GUI
- glossary
- character memory
- story memory
- HTML/Pixiv adapter
- massive-file streaming
- job fingerprint
- model replacement
- broad quote/bracket validator relaxation
- censorship/softening logic
- semantic similarity alignment guesses

## Work completion checklist

Work should not mark Round 4 complete until:

- [ ] default HY-MT path no longer requires model-visible Segment IDs
- [ ] single/native response maps directly to known Segment
- [ ] repair uses only failed Segment source/context/reasons
- [ ] VALID-never-retranslate invariant remains enforced
- [ ] placeholders/checkpoint/validators remain integrated
- [ ] `novel` no-softening/adult-intensity instructions remain present
- [ ] R33-R42 pass
- [ ] all still-applicable previous regression tests pass
- [ ] mock Ollama integration passes
- [ ] `python -m compileall -q .` passes
- [ ] `git diff --check` passes
- [ ] PR body documents that real Round 2 produced duplicate IDs and real Round 3 produced 12/12 missing IDs, motivating the protocol removal

## Final principle

**Do not ask HY-MT to preserve Seori Translator's internal bookkeeping.**

The translator translates. Python owns identity and correctness.
