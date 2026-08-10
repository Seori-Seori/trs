# Seori Translator v7.0 — Round 5.1 Novel Register / Style Fix

Status: approved follow-up after the real Round 5 `a.txt` run.

## Real-run evidence

Round 5 real output:

- total: 235
- VALID: 229
- repaired: 6
- FAILED: 6
- RISK: 14

This is not an engine-protocol regression. Round 4's native single-Segment architecture remains the correct default. The remaining problem is output register and lexical naturalness in `novel` mode.

Observed failure classes from the real run:

- `NOVEL_CJK_RESIDUE`: untranslated/mixed Chinese survives and correctly becomes ERROR.
- `NOVEL_TERM_MISTRANSLATION`: meaning-class guards catch some catastrophic lexical swaps.
- `BRACKET_STRUCTURE_LOSS`: parenthetical content can still disappear.

Observed quality defects even in VALID output:

- Korean prose frequently drifts into clinical/anatomical register even though the Chinese source is vulgar, colloquial, euphemistic, playful, or pornographic fiction rather than medical writing.
- Chinese euphemisms are sometimes translated literally as object nouns rather than converted to a natural Korean fiction equivalent.
- Some ordinary setting/garment words are mistranslated or over-literal.
- Some Chinese-first-person / self-reference constructions remain stiff (`본 아가씨`, overly formal self-narration, literal Chinese syntax).
- Some sentences are semantically understandable but read like MT rather than Korean genre fiction.

Examples from the real run that motivated this document include: excessive `유방`, `대퇴부`, `여성의 외부 성기`, `음경 입구` style wording; literal or malformed sexual euphemisms; and ordinary lexical errors such as bedroom terminology drifting toward `주방`. These examples are diagnostics, not a request for global blind replacement.

## Core decision

Do **not** add another global post-translation replacement table.

Instead, split novel quality control into two layers:

1. **Semantic terminology layer** — prevents objectively wrong meaning-class swaps.
2. **Novel register layer** — controls how a correct meaning is expressed in Korean fiction.

The second layer is new in Round 5.1.

## Hard invariants

Do not change these:

- HY-MT sees no `SEG_*` identity.
- one default request maps to one Python-owned Segment.
- immutable `Segment.source`.
- VALID is terminal and is never regenerated during normal resume.
- only the bad Segment may repair.
- transport failures are separate from content failures.
- placeholder exactness remains strict.
- source fallback remains the terminal-failure output policy.
- no broad batching optimization yet.
- no final-text blind string replacement.

## 1. Add a `novel` register/style resource

Create a compact resource, e.g. `profiles/novel_register.json` or equivalent code structure.

It must describe **semantic class + preferred Korean register**, not force one universal Korean word.

Each entry may have fields conceptually like:

```json
{
  "source_terms": ["..."],
  "meaning_class": "...",
  "preferred_register": "colloquial_novel",
  "preferred_korean": ["...", "..."],
  "avoid_korean": ["..."],
  "notes": "Choose a natural Korean adult-fiction/web-novel expression matching the source intensity; do not become clinical unless source is clinical."
}
```

Only entries whose source term occurs in the immutable current Segment may be injected into the prompt.

Never dump the entire register glossary into every request.

## 2. Source register must control target register

For `novel` mode, add one short global style rule and source-triggered hints.

Global rule should be minimal so Round 4 prompt simplicity is preserved:

> Use natural contemporary Korean genre-fiction wording. Match the source's level of vulgarity, euphemism, playfulness, and explicitness. Do not replace colloquial or erotic source wording with medical/clinical terminology unless the source itself is clinical.

Do not add a long essay to every prompt.

Source-triggered hints may then clarify individual high-risk terms.

### Register mapping policy

- vulgar/slang source -> natural Korean vulgar/slang register of comparable strength
- erotic colloquial source -> natural Korean erotic colloquial register
- playful euphemism -> natural Korean euphemism, not literal object translation
- neutral everyday term -> neutral everyday Korean
- explicitly anatomical/medical source -> anatomical Korean is allowed

The translation must preserve meaning and intensity without sanitizing or intensifying it.

## 3. Keep semantic correctness guards separate from style guards

The existing catastrophic guards are good and should remain, but expand them carefully.

Examples of semantic constraints from the real Chinese test corpus:

- `阴蒂` must never become male genital anatomy. Its semantic class is clitoris.
- `爱液` must not become semen when the source does not introduce semen.
- `内裤` is underwear/panties, not thermal underwear.
- `主卧` is master/main bedroom, never kitchen.
- `自慰棒` is a sex toy/dildo/vibrator class according to context; do not leave mixed CJK.
- `小穴`, `阴户`, `骚屄`, `肉洞`, `花壶` and similar adult-fiction expressions belong to distinct vulgar/euphemistic genital-reference classes. Do not translate them as unrelated literal objects, pseudo-medical nonsense, or male anatomy.

These are semantic classes. The preferred Korean wording can vary by source tone and sentence context.

Do not implement a single fixed output token for every occurrence.

## 4. Add a conservative `NOVEL_REGISTER_MISMATCH` diagnostic

We need to catch outputs that are semantically plausible but stylistically wrong.

Do not simply ban every anatomical Korean word.

Trigger only when all are true:

1. mode is `novel`;
2. current source contains a known colloquial/vulgar/euphemistic source term or construction;
3. translation contains a known overly clinical alternative for that source class;
4. source context itself is not medical/clinical.

Default severity:

- use `RISK` for merely stiff/clinical-but-correct wording;
- use `ERROR` only for a known meaning/register mapping that materially damages meaning or produces an obviously wrong concept.

RISK must not consume retries.

This lets the report surface prose-quality problems without repeating the Round 5 mistake of turning every style preference into terminal failure.

## 5. Do not overuse hard terminology ERRORs

The Round 5 run produced 6 failures because several newly strict checks correctly rejected bad output, but a translation system must still terminate successfully when the candidate is readable and semantically safe.

Use ERROR for:

- untranslated non-whitelisted CJK;
- cross-anatomy meaning swap;
- wrong ordinary concept (`main bedroom` -> `kitchen`);
- missing protected content;
- missing parenthetical meaning;
- genuine truncation;
- source-triggered known catastrophic terminology mapping.

Use RISK/WARNING for:

- clinical wording that is accurate but ugly;
- awkward Korean register;
- literal-sounding but understandable prose;
- optional genre-style preferences.

## 6. Improve repair prompt for terminology/register failures

When repair is caused by a semantic terminology error, include only:

- immutable source;
- normal reference context;
- the matched source term(s);
- semantic meaning class;
- short preferred register hint;
- short list of known disallowed meanings from the failed validation.

Do not include the broken previous candidate.

When repair is caused only by a style/RISK issue, do **not** automatically retry in v7.0. Keep it visible in QA first. We can later add an optional polish phase if necessary.

## 7. Preserve Chinese adult-fiction euphemism as Korean fiction, not literal nouns

Chinese erotic web fiction often uses metaphorical/euphemistic nouns that are not meant literally. The system must translate the intended referent and tone, not the dictionary surface object.

Implementation rule:

- semantic glossary entry identifies intended referent/class;
- prompt tells HY-MT the intended meaning and register;
- model produces the actual Korean wording;
- validator rejects only known category errors;
- Python does not substitute a final phrase.

This prevents recurrence of literal flower/container/object translations while avoiding a brittle hardcoded pornography thesaurus.

## 8. Korean prose register guidance

For `novel`, prefer ordinary Korean fiction syntax over Chinese calques.

Watch for patterns such as:

- repeated `본 아가씨`, `이 아가씨`, `본 소녀` when normal first-person/third-person Korean would be smoother;
- unnecessary `~하였다`/formal explanatory phrasing inside playful internal monologue;
- literal Chinese modifiers that produce phrases like `정직한 여자`, `주인의 투정`, `접근거리 카메라`;
- anatomical/technical noun stacking when the scene uses casual narration;
- unnatural pronoun/name drift (`鱼鱼` should remain a consistent chosen Korean name form within the work).

Do not blindly rewrite every occurrence. Give the model a concise fiction-register instruction and use source-triggered hints for high-risk cases.

## 9. Name consistency

The current run still shows variation around `鱼鱼` transliteration/self-reference.

Add a lightweight per-job name map for obvious repeated proper names/nicknames discovered from source or provided glossary.

For v7.0 this may remain deterministic/config-driven rather than story-memory AI.

At minimum, once `鱼鱼` is resolved to the selected Korean form for the job, later Segments should receive that mapping as a short source-triggered name hint.

Do not turn this into broad story memory yet.

## 10. Acceptance test using the same `a.txt`

Round 5.1 is not accepted merely by a higher VALID count.

Required manual/automatic checks:

- no unprotected Chinese residue in final Korean output;
- no terminal source fallback caused by a fixable style preference;
- no main-bedroom/kitchen type ordinary semantic swap;
- no cross-sex anatomy swap;
- no semen/body-fluid category swap unless source actually changes category;
- no thermal-underwear mapping for ordinary underwear;
- no literal flower/container noun where the source is a sexual euphemism;
- no obvious mid-sentence truncation;
- outer quote restoration still works;
- parenthetical content remains preserved;
- no censorship/softening relative to source;
- Korean prose should read like contemporary genre fiction rather than a medical description.

Target metrics for the same stress file:

- FAILED: ideally 0; acceptable only if remaining failures are genuine source-preservation failures that cannot be safely repaired.
- CJK residue: 0.
- catastrophic terminology swaps: 0.
- RISK: low enough to inspect manually; do not chase zero by deleting useful signals.

## 11. Regression tests

Add at least R56-R66 or equivalent:

- colloquial erotic source does not map to known clinical-only alternative when a natural preferred register is supplied;
- explicitly medical source still permits anatomical Korean terminology;
- source-triggered register hints appear only when the term exists in current source;
- no global glossary dump;
- no output string replacement;
- `主卧` cannot become kitchen;
- `阴蒂` cannot map to male anatomy;
- `爱液` cannot map to semen absent source support;
- Chinese euphemistic genital term cannot map to literal unrelated object when glossary class is known;
- style-only mismatch is RISK and does not consume retry;
- catastrophic semantic terminology mismatch remains ERROR and repairs only current Segment.

## 12. Important: do not redesign the engine

Round 5.1 is a quality patch, not Round 6 architecture.

Do not add:

- model-visible Segment IDs;
- row batching;
- story-memory architecture;
- second-model reviewer;
- full-file LLM polish;
- GUI;
- blind output replacements.

First make the native single-Segment translator produce accurate, natural Korean novel prose on the existing Chinese stress test. Japanese testing comes after this passes.
