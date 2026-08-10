# Seori Translator v7.0 — Round 6 Generalization Reset

## Status

Approved design contract for the next implementation pass.

Round 6 is not an `a.txt` quality patch. It is a deliberate reset toward the actual product goal:

> **A reusable local Korean translation engine for unseen game text and fiction, especially Chinese/Japanese/English source text, without overfitting one work.**

`a.txt` remains a valuable regression fixture because it exposed real failure modes, but no default vocabulary, name, character setting, or prompt behavior may be specialized to that work.

---

## 1. Why Round 6 exists

The real local runs show a clear trend:

- Round 4: `VALID 233/235`, repaired 5, failed 2, risks 76.
- Round 5: `VALID 229/235`, repaired 6, failed 6, risks 14.
- Round 5.1: `VALID 228/235`, repaired 11, failed 7, risks 35.

Round 5 and 5.1 improved detection of real errors, but increasingly verbose terminology/register/name instructions made HY-MT less stable as a translator.

The decisive evidence is not only the aggregate count. In Round 5.1, HY-MT repeatedly ignored explicit corrective hints even after three attempts:

- a bedroom concept was still mistranslated as a kitchen even when the prompt explicitly provided the correct semantic class and prohibited the kitchen reading;
- an underwear concept was still emitted as an unrelated clothing concept even after the prompt explicitly prohibited that mapping;
- a female anatomy concept was still mapped to the wrong sex even after the prompt explicitly described the correct anatomy;
- a known device term still came back as mixed Korean+CJK despite explicit instructions;
- work-local name hints did not guarantee consistency and produced multiple malformed variants in normal VALID output.

At the same time, the Korean output developed more malformed prose and translation artifacts.

The conclusion is architectural:

> **HY-MT should translate. Python should own identity, deterministic mappings, validation, recovery, and correctness constraints. Do not try to turn HY-MT into a general instruction-following editor by adding more prose to the prompt.**

This is the same lesson that made Round 4 successful, now applied to terminology and style.

---

## 2. Product goal and non-goals

### Product goal

The engine must accept previously unseen source text and produce Korean for at least these use cases:

- novels / web novels / light-novel-like prose;
- adult fiction without arbitrary sanitization;
- game dialogue and game strings;
- general documents as a lower-priority mode.

The implementation must generalize across works. A work-specific success is not sufficient.

### Explicit non-goals for Round 6

Do **not** add:

- a larger `a.txt` vocabulary list;
- hardcoded characters from the current test work;
- automatic story-memory or character-memory systems;
- a Korean post-editor / second model pass;
- GUI / EXE work;
- Phase-B multi-Segment batching;
- HTML/Pixiv adapters;
- broad semantic guessing or automatic glossary invention;
- best-effort invalid-candidate output in place of the existing FAILED source fallback.

Those can be revisited only after the translation core is stable again.

---

## 3. Approved Round 6 decisions

All of the following are approved and should be implemented as the source of truth.

### 3.1 Return the default HY-MT prompt to a Round-4-style minimal translation prompt

The normal single-Segment prompt must stop injecting long source-triggered terminology/register/name sections.

Keep only the minimum contract needed for reliable translation:

1. translate exactly one source text into Korean;
2. output translation only;
3. preserve meaning, facts, numbers, polarity, state, names, and protected tokens;
4. use the supplied context only as reference and do not output the context;
5. preserve the source intensity rather than arbitrarily softening or strengthening it;
6. in novel mode, use natural Korean genre-fiction wording and avoid clinical/anatomical register unless the source context itself is clinical/medical.

The adult-fiction style instruction should be **one short global policy**, not a per-term glossary dump.

Recommended intent, not necessarily exact wording:

> Use natural Korean genre-fiction wording. Preserve the source's level of vulgarity, euphemism, playfulness, and explicitness. Unless the source is genuinely medical/clinical, prefer natural genre wording over clinical/anatomical prose.

Do not add dozens of target candidates or forbidden words to the normal prompt.

### 3.2 Remove Round-5.1 prompt-time glossary/register/name injection from the default path

The normal translation prompt must not contain sections equivalent to:

- `용어 의미 참고(...)`;
- `장르 문체 참고(...)`;
- `이름 참고(...)`;
- lists of preferred Korean candidates;
- lists of prohibited Korean translations;
- work-specific name instructions.

The same restriction applies to normal retries. Repair should remain concise and error-class-oriented.

The existing resources may be refactored into validation or language-pack data, but they must not behave as a large prompt glossary.

### 3.3 Remove work-specific defaults

The default `novel` profile must not contain names from the current test work.

In particular, no character name from `a.txt` may be hardcoded in `profiles/novel.json` or another global default resource.

A name mapping belongs to a **job-local mapping**, not a global novel profile.

Two different files must be allowed to map the same source spelling differently without changing global configuration.

### 3.4 Introduce a generic three-class term policy

Do not treat all terminology alike. Classify interventions by confidence and context dependence.

#### Class A — deterministic mapping/protection

Use Python-owned deterministic mapping only when all of the following are true:

- the source token has an effectively context-independent identity for the current job;
- the target rendering has been explicitly fixed for that job or language pack;
- replacing/protecting it will not require the model to infer a different semantic reading;
- the mapping is safe enough that Python can prove the restoration rather than guess it.

Typical examples:

- user/job-supplied character names;
- fixed named entities;
- game item/UI names deliberately locked by the user or project;
- very small, exceptionally high-confidence language-pack entries if their use is demonstrably context-independent.

Implementation principle:

1. protect the source occurrence with a typed mapped placeholder such as `[[NAME_0001]]` / `[[MAP_0001]]`;
2. HY-MT translates the surrounding sentence while preserving the token;
3. Python restores the **preselected Korean mapping**;
4. exact token count/order rules remain mandatory;
5. no output-string guessing or fuzzy replacement is allowed.

Do not overuse Class A. If hiding the source term from the model would remove semantic information needed to translate the sentence naturally, the item does not belong in Class A.

#### Class B — semantic guard, model chooses wording

Use this for concepts whose semantic category is stable but whose Korean wording is context/style dependent.

Examples of the kind of distinction intended:

- bedroom versus kitchen;
- male versus female anatomy;
- underwear versus unrelated garments;
- a device versus a literal unrelated object.

For Class B:

- the model sees and translates the original source term normally;
- no preferred Korean candidate list is injected into the normal prompt;
- validators may reject only **high-confidence catastrophic meaning swaps**;
- repair instructions should describe the semantic error class briefly, not dump a dictionary entry;
- if repeated repair still fails, preserve existing FAILED behavior rather than inventing a translation.

The language pack may encode semantic classes and narrow impossible/conflicting classes, but it should not become a work-specific phrasebook.

#### Class C — context-dependent slang, metaphor, euphemism, voice

Use this for expressions whose correct Korean rendering depends strongly on tone, scene, speaker, genre, or metaphor.

For Class C:

- never force a deterministic target replacement;
- never add a work-specific candidate just because one regression file used it;
- let HY-MT translate from context under the short global genre policy;
- validators may still catch structural failure, untranslated source residue, and genuinely provable semantic inversion;
- awkward style alone should remain RISK/QA, not become repeated semantic repair.

Adult slang and euphemism often belong here unless the semantic identity is genuinely unambiguous.

---

## 4. Adult-fiction register policy

The target is not clinical Korean when the source is genre fiction.

For `novel` mode:

- preserve the source meaning exactly;
- preserve the source intensity rather than censoring or sanitizing it;
- when the source is explicit, natural explicit Korean genre wording is allowed and preferred over clinical/anatomical phrasing;
- when the source is euphemistic or playful, preserve that level rather than converting it to a medical explanation;
- when the source is vulgar, a comparable Korean vulgar register is allowed;
- do not intensify a neutral source merely to make it more provocative;
- genuine medical/clinical scenes may still use medical terminology.

This is a **register selection policy**, not a fixed adult-word substitution table.

Do not create a giant adult glossary merely to optimize `a.txt`.

---

## 5. Job-local name mapping

Keep the idea of deterministic per-job names, but remove all built-in work-specific mappings.

### Requirements

- Global profiles ship with an empty/default name map.
- A job may optionally provide explicit source→Korean mappings.
- The mapping must be persisted with the job/checkpoint if used so resume behavior is stable.
- The same source spelling may map differently in another job.
- Unknown names must not be automatically guessed into the permanent map during Round 6.
- If no job-local mapping exists, HY-MT translates/transliterates normally and QA may report inconsistency, but Python must not invent a canonical name.

Automatic name discovery can be designed later as a separate feature.

---

## 6. Validation: keep the good parts from Round 5

Round 5/5.1 validators exposed real problems and should not be thrown away merely because prompt injection failed.

Preserve or improve these deterministic/high-confidence checks:

- exact placeholder preservation/restoration;
- prompt leakage;
- CJK/kana/Cyrillic residue rules by source/target mode;
- novel-mode unprotected CJK residue as ERROR;
- obvious Korean mid-clause truncation as ERROR;
- source quote/delimiter structure;
- deterministic full-span outer-quote restoration when provably safe;
- parenthetical content loss;
- high-confidence catastrophic semantic class swaps;
- source hash / checkpoint / resume invariants;
- transport-error separation;
- VALID-never-retranslate.

### Register/style findings

`NOVEL_REGISTER_MISMATCH` or an equivalent style QA signal may remain, but:

- it must remain non-blocking RISK;
- it must never trigger repeated translation attempts by itself;
- it must not cause large corrective glossary sections to be added to the repair prompt.

### Risk validators

Keep risk signals conservative. A small number of meaningful RISK findings is useful; broad keyword heuristics that flood QA should be avoided.

---

## 7. Repair behavior

Repair remains Segment-local.

A repair request should contain:

- the immutable source;
- bounded reference context;
- the same minimal translation contract;
- a short description of the failed error class(es).

Examples of acceptable repair intent:

- untranslated source characters remain — produce complete Korean;
- the output ended mid-clause — translate through the end;
- a source parenthetical was omitted — preserve its content;
- a semantic category changed — preserve the original category.

Do **not** include:

- the broken previous candidate;
- long dictionaries;
- multiple preferred Korean alternatives;
- lists of unrelated prohibited terms;
- global work lore;
- internal Segment IDs.

A repair prompt must stay much closer to Round 4 than Round 5.1.

---

## 8. FAILED output policy

Keep the current safe behavior for Round 6:

> If a Segment exhausts repair and remains invalid, the final TXT uses the immutable source text for that Segment and QA records the failure.

Do not silently output a known-invalid Korean candidate merely to make the file look fully translated.

A future GUI may offer an explicit opt-in `best effort invalid candidate` mode, but that is outside Round 6.

---

## 9. Korean polishing is deferred

The Round-5.1 real output contained malformed Korean, awkward calques, name corruption, and typographical-looking artifacts that can pass semantic validators.

Do not solve this by adding a second Korean-polish model in Round 6.

Reason: we first need to know whether the base translator is stable. Adding a post-editor now would make error attribution ambiguous again.

Future architecture may be:

```text
source
  -> translation
  -> deterministic validation/recovery
  -> optional Korean polish
  -> final validation
```

But Round 6 ends before `optional Korean polish`.

---

## 10. Language-pack architecture

Refactor terminology toward reusable source-language packs rather than one-work dictionaries.

A language pack may contain small, conservative metadata such as:

- source spelling/aliases;
- source language;
- semantic class;
- whether the item is eligible for Class A deterministic mapping;
- narrow impossible/conflicting semantic classes for Class B validation;
- medical/context exceptions where truly necessary;
- validator metadata.

It should **not** contain:

- names from one novel;
- plot-specific vocabulary;
- dozens of target candidates copied from one regression output;
- rules whose only justification is that they make `a.txt` score higher.

If a newly discovered failure is added to a language pack, the implementation/review must answer:

> Is this a reusable source-language phenomenon, or are we memorizing one work?

If the answer is uncertain, do not add it as a global rule.

---

## 11. Game mode must remain first-class

Round 6 must not turn `novel` specialization into a regression for `game`.

Requirements:

- game mode keeps exact placeholders, IDs/tokens/formatting protections, and source structure safeguards;
- novel adult-register policy must not leak into game/document mode unless the user explicitly uses a compatible profile;
- job-local mappings should be usable for game character/item/UI names without requiring novel-specific code;
- core mapping/protection infrastructure should be mode-agnostic; only policy/resources differ.

The long-term architecture should look like:

```text
                    +-------------------+
source -----------> | common pipeline   |
                    +-------------------+
                       |    |       |
                     mode language  job-local
                     pack   pack      map
                       \     |        /
                        minimal prompt
                             |
                           HY-MT
                             |
                   deterministic validators
                             |
                      repair/checkpoint
```

No single test work owns the core.

---

## 12. Regression requirements

Preserve all valid invariants and prior regression coverage. Do not delete earlier tests merely because the implementation architecture changes.

Add at least the following Round-6 cases (numbering may continue from R66):

### R67 — default single prompt is minimal
A novel Segment with terminology present must not emit terminology/register/name glossary sections in the normal model prompt.

### R68 — no work-specific default names
The shipped `novel` profile contains no `a.txt` character names or other work-local mappings.

### R69 — no unrelated dictionary dump
A source Segment must never receive unrelated vocabulary entries in its prompt.

### R70 — job-local mapped name round-trip
An explicitly configured job-local name is replaced by a typed placeholder, preserved once, restored to the configured Korean spelling, and then validated.

### R71 — mapped placeholder count/order safety
Missing, duplicated, unexpected, or reordered mapped placeholders fail deterministically; Python never guesses placement.

### R72 — same source name may differ between jobs
Two jobs can map the same source spelling to two different Korean renderings with no global-state bleed.

### R73 — absent job map means no forced canonicalization
Without an explicit job mapping, the source name is not rewritten by a hidden global default.

### R74 — Class B rule is validator-side, not prompt glossary
A high-confidence semantic guard can reject a catastrophic category swap while the normal prompt remains minimal.

### R75 — Class C slang is not deterministic replacement
A context-dependent slang/metaphor entry is not replaced with a fixed Korean output by Python.

### R76 — adult novel register is one short policy
Novel mode includes the approved source-intensity/nonclinical genre policy without per-term candidate lists.

### R77 — medical context may remain clinical
A genuinely clinical source/context is not rejected merely for using correct clinical Korean terminology.

### R78 — repair prompt remains bounded
Repair contains source/context/error-class guidance but no broken candidate, no large dictionary, and no internal Segment ID.

### R79 — terminal failure still falls back to source
Known-invalid Korean is not silently promoted to final output.

### R80 — VALID resume still causes zero model calls
Round 6 must not weaken the existing terminal VALID/checkpoint guarantee.

### R81 — game mode unaffected by novel policy
A game-mode translation does not receive novel/adult register instructions or novel-specific terminology behavior.

### R82 — document mode unaffected by novel policy
A document-mode translation remains neutral and does not receive fiction-specific register instructions.

Add targeted unit/integration tests as needed beyond these cases.

---

## 13. Real acceptance strategy

### Stage 1 — current Chinese stress regression

Run the same `a.txt` cleanly after implementation.

Purpose:

- verify that Round-4-level translation stability returns;
- verify CJK/truncation/structure detection remains active;
- verify work-specific names are no longer globally hardcoded;
- verify the result does not become more clinical due to validator design;
- inspect actual Korean readability rather than only VALID count.

This file is a **regression dataset**, not the vocabulary source of truth.

### Stage 2 — unseen language/work probe

After Stage 1 is acceptable, test an unseen work, preferably Japanese fiction as the next probe.

Do not add rules from the Japanese test directly into global resources unless they generalize to Japanese source behavior.

### Stage 3 — game probe

Before declaring v7.0 generally useful, test unseen game text using `--mode game` and verify exact token/format preservation plus Korean output quality.

### Success is not `235/235` alone

Assess:

- terminal failures;
- untranslated source residue;
- structural loss;
- catastrophic semantic inversion;
- name/token consistency where explicitly mapped;
- natural Korean readability;
- absence of obvious work-specific overfitting.

---

## 14. Implementation guidance for existing Round-5.1 code

The current branch contains `core/novel_register.py`, `profiles/novel_register.json`, `profiles/novel_zh_terms.json`, name-map prompt logic, terminology validators, and register validators.

Do not assume these files must be deleted wholesale. Refactor according to responsibility:

- **Prompting:** remove large per-term/per-name/per-register injection from the default HY-MT request.
- **Profiles:** remove work-specific names from shipped defaults; keep only short mode policy.
- **Terminology/language pack:** retain only generalizable, high-confidence source-language metadata and semantic guards.
- **Register:** keep QA-oriented non-blocking detection if useful, but do not use it as a prompt-expansion engine.
- **Name map:** convert to optional job-local deterministic mapping/protection; no global current-work defaults.
- **Placeholder engine:** extend carefully for typed mapped placeholders while preserving existing exact restoration invariants.
- **Recovery:** use short error-class repair hints; never reintroduce a glossary dump.

Prefer deleting complexity that does not prove its value over preserving Round-5.1 behavior for compatibility.

---

## 15. Hard invariants that still apply

Round 6 must preserve all core safety/correctness invariants:

1. `Segment.source` is immutable.
2. A VALID Segment is terminal for the same job/resume and is never retranslated.
3. Parse each model response once and reuse the parsed result.
4. Transport failures are not semantic/content failures and do not trigger split/repair behavior.
5. Repair only the failed Segment; never resend healthy siblings.
6. Placeholder/mapped-token placement is never guessed.
7. Checkpoint source hash must match before resume.
8. Resumed VALID content is revalidated according to existing policy.
9. Terminal invalid Segment falls back to source in final TXT.
10. Output target is Korean.
11. Model-visible internal persistent Segment IDs remain forbidden in the native single-Segment protocol.
12. No arbitrary censorship or sanitization of adult source intensity.

---

## 16. The Round-6 principle

> **Do not teach HY-MT one novel. Build a system that constrains what must be exact, validates what can be proven, and leaves genuine translation choices to the translator.**

Or, operationally:

> **Minimal prompt. Generic language/mode policy. Job-local deterministic mappings. High-precision validators. Segment-local repair. No work-specific overfitting.**
