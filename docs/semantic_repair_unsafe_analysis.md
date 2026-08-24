# PR5 Shadow Evaluation — Per-Case Unsafe Forensics

Companion to `docs/semantic_repair_pr5_claude_review.md`. That document covers
architecture; this one covers model quality only.

Evidence base (read directly from the working tree, verified against the corpus):

- `artifacts/semantic_shadow_results.json` — **Qwen3.8-27B**, `reachable=true`,
  `served_models=["Qwen/Qwen3.8-27B"]`, MTP `method=mtp`,
  `num_speculative_tokens=1`.
- `artifacts/semantic_shadow_results_qwen36_35b_a3b.json` — **second endpoint**,
  `served_models=["ai-balances-planner"]`,
  `configured_model="ai-balances-language"`. Neither field names
  Qwen3.6-35B-A3B; the filename is the only assertion of identity. Referred to
  below as "Model B" where precision matters.
- `tests/semantic_repair/corpus.json` — 23 cases (20 model cases + 3
  deterministic controls), 26 model invocations after repeats.

All aggregate numbers in the brief were re-derived from the JSON and match.

---

## 1. Executive summary

The headline framing — "Qwen3.8 is worse because 53.85 % of its accepted
proposals are unsafe" — is true but hides the finding that actually matters.

**Qwen3.8-27B is not making 14 independent mistakes. It is emitting one output
template.** Thirteen of its fourteen unsafe runs are the same proposal shape:
`set_operation=compare` + `set_comparison=<something>` +
`reference_prior_result`, with a `last_two_results` reference carrying
`selector="last"`. It emits this for "compare", for "what is the difference",
for "which is larger", for "the first of those two", for "take the second
option", and — most dangerously — for "compare them" when only one result
exists. This is mode collapse onto a single attractor, not noise. It is why
stability is 100 %: the model is stable because it has collapsed.

Underneath that collapse there is a genuinely clean island. **`swap_direction`
is 5/5 correct, 0 unsafe, with zero false positives across the 21 non-direction
runs.** The model never once proposed a direction swap where one was not wanted,
and never once missed one where it was. That is the single most useful result in
this evaluation, and it is invisible in the aggregate numbers.

On the two adversarial challenges:

1. *"Qwen3.8 may contain a small, stable, high-precision island."* —
   **CONFIRMED.** `swap_direction`, 5/5, plus `unsupported` at 3/3. The
   qualifier is sample size: three distinct phrasings, not thirty.
2. *"Model B's lower unsafe rate may be an artifact of validator rejection."* —
   **CONFIRMED, decisively.** 18 of its 26 outputs never reached semantic
   evaluation, and the rejections are overwhelmingly structural typing errors
   (`proposal_schema_invalid` 17, `mutation_value_forbidden` 13), not semantic
   caution. On the one comparison case where it did clear the schema, it
   produced the *identical* attractor template Qwen3.8 produces. It is not
   safer; it is quieter.

The other conclusion that changes the shape of a future PR6: **all eight
residual semantic errors trace to a prompt that enumerates a vocabulary without
ever defining what the vocabulary means.** The prompt lists `first`, `second`,
`last`, `same` nowhere at all, and lists `compare` and `calculate` without a
single word on when to use which. That is a cheap, general fix — not overfitting.

## 2. Qwen3.8-27B profile

| Metric | Value |
|---|---:|
| Invocations | 26 |
| Valid proposals | 26 |
| Validator rejections | 0 |
| Malformed | 0 |
| Typed output rate | 100.00 % |
| Semantic match | 34.62 % (9/26) |
| Precision among valid | 34.62 % |
| Correct patches | 6 |
| Unsafe patches | **14** |
| Unsafe transition rate | 53.85 % |
| Correct clarifications | **0** (of 4) |
| `unsupported` correct | 3 (of 3) |
| Repair coverage | 23.08 % |
| Stability | 100 % (3/3 repeated cases) |
| Latency avg / p50 / p95 | 7985 / 8972 / 10658 ms |
| Timeout / unavailable | 0 / 0 |

Character: **maximally compliant, minimally discriminating.** It always returns
well-formed typed JSON, always with high confidence (0.95–0.98), always stable.
It never once emitted a `CLARIFY` action in 26 runs — the action is effectively
absent from its behavioural repertoire.

A striking secondary signal: its correct outputs and its unsafe outputs occupy
**disjoint latency bands**. All 9 semantically correct runs finished in
4478–7709 ms. Thirteen of the 14 unsafe runs took 10234–10739 ms (the exception,
`same_for_first`, took 7690 ms). The over-specified three-mutation template is
systematically the slowest thing the model produces. This is reported as
evidence that the failures are a distinct generation mode, **not** as a proposed
gate — latency is not a semantic invariant, it varies with load, and building a
safety control on it would be fragile.

## 3. Model B (`ai-balances-planner`) profile

| Metric | Value |
|---|---:|
| Invocations | 26 |
| Valid proposals | 8 |
| Validator rejections | **18** |
| Malformed | 0 |
| Typed output rate | 30.77 % |
| Semantic match | 19.23 % (5/26) |
| Precision among valid | 62.50 % |
| Correct patches | 2 |
| Unsafe patches | 2 |
| Unsafe transition rate | 25.00 % |
| Correct clarifications | 1 (of 4) |
| `unsupported` correct | 2 (of 3) |
| Repair coverage | 7.69 % |
| Stability | **33.33 %** (1/3 repeated cases) |
| Latency avg / p50 / p95 | 2492 / 2718 / 3478 ms |
| Timeout / unavailable | 0 / 0 |

Character: **fast, semantically willing, structurally incompetent.** It is the
only one of the two that can produce a correct clarification, and it is 3.2×
faster. But it cannot reliably emit the contract: 69 % of its outputs are thrown
out for typing errors before anyone looks at their meaning.

## 4. Per-case unsafe table

All 16 unsafe runs. No sampling. `AS` = active state, `Hist` = relevant history.

### Qwen3.8-27B — 14 unsafe runs across 10 distinct cases

| # | Case | AS / Hist | Message | Expected | Actual proposal | Validator | Why accepted | Class | Sev. if active | Det. gate? | Prompt fix? | Kinds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `compare_bare` | show/distribution, Ростовская обл.; **2** results (апрель, май) | "Сравни" | patch `[set_operation=compare, set_comparison=last_two_results]`, ref `last_two_results/null` | patch `[set_operation=compare, set_comparison=last_two_results, reference_prior_result]`, ref `last_two_results/`**`last`** | VALID | every field is in-enum; validator does not forbid a redundant 3rd mutation, nor `selector=last` on a pair reference | **U11** contract mismatch | LOW — core intent correct, over-specified | **YES** (normalize) | n/a | set_operation, set_comparison, reference_prior_result |
| 2–4 | `compare_pronoun` ×3 | same, 2 results | "Сравни их" | same as #1 | identical to #1, all 3 runs byte-identical | VALID | as #1 | **U11** | LOW | **YES** | n/a | same |
| 5 | `compare_last_two` | same, 2 results | "Сравни последние два" | same as #1 | identical to #1 | VALID | as #1 | **U11** | LOW | **YES** | n/a | same |
| 6 | `absolute_difference` | same, 2 results | "Какова разница?" | patch `[set_operation=`**`calculate`**`, set_comparison=`**`absolute_difference`**`]`, ref `last_two_results/null` | patch `[compare, last_two_results, reference_prior_result]`, ref `/last` | VALID | `compare` and `last_two_results` are both legal enum values | **U8** calculation semantics | **HIGH** — user asked for a computed difference, would get a side-by-side view | NO | **YES** — prompt never says when `calculate` applies | set_operation, set_comparison, reference_prior_result |
| 7–9 | `percent_difference` ×3 | same, 2 results | "Какова разница в процентах?" | patch `[`**`calculate`**`, percent_difference]`, ref `/null` | patch `[`**`compare`**`, percent_difference, reference_prior_result]`, ref `/last` — all 3 identical | VALID | both values legal | **U8** | **HIGH** — comparison value is right, operation is wrong | NO | **YES** — same root cause | same |
| 10 | `larger_value` | same, 2 results | "Что больше?" | patch `[compare, `**`larger_value`**`]`, ref `/null` | patch `[compare, `**`last_two_results`**`, reference_prior_result]`, ref `/last` | VALID | both values legal | **U7** comparison semantics | **HIGH** — "which is larger" degraded to a generic comparison | NO | **YES** — prompt never maps phrasing to `larger_value` | same |
| 11 | `reference_first` | same, 2 results | "Первый из тех двух" | patch `[reference_prior_result]`, ref `last_two_results/`**`first`** | patch `[compare, last_two_results, reference_prior_result]`, ref `last_two_results/`**`last`** | VALID | selector `last` is legal on `last_two_results` | **U5** + **U9** wrong selection + overreach | **HIGH** — selects the wrong operand *and* converts a selection into a comparison | Partial | **YES** — prompt never defines selectors | same |
| 12 | `reference_second` | same, 2 results | "Возьми второй вариант" | patch `[reference_prior_result]`, ref `last_two_results/`**`second`** | identical to #11 | VALID | as #11 | **U5** + **U9** | **HIGH** | Partial | **YES** | same |
| 13 | `same_for_first` | same, 2 results | "То же для первого" | patch `[reference_prior_result]`, ref `last_two_results/`**`first`** | patch `[reference_prior_result]`, ref **`previous_result/last`** | VALID | `previous_result` satisfies `prior_result_reference_required` | **U5** wrong reference | **HIGH** — correct mutation kind, wrong result chosen | NO | **YES** | reference_prior_result |
| 14 | `ambiguous_pronoun_single_result` | show/balance, ГП ТГ Ухта; **1** result only | "Сравни их" | **clarify** | patch `[compare, last_two_results, reference_prior_result]`, ref `last_two_results/last` | VALID | validator has no view of how many results actually exist | **U1** abstention failure | **CRITICAL** — proposes comparing "the last two" when only one result exists; references a non-existent operand | **YES** (cardinality) | Partial | same |

### Model B — 2 unsafe runs

| # | Case | AS / Hist | Message | Expected | Actual proposal | Validator | Why accepted | Class | Sev. if active | Det. gate? | Prompt fix? | Kinds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 15 | `reverse_business_direction` run1 | directed flow Томск→Сургут; **1** result | "А теперь наоборот" | patch `[swap_direction]`, refs `[]` | patch `[swap_direction]`, ref **`last_two_results/last`** (spurious; only 1 result exists) | VALID | `swap_direction` alone passes the multi-field rule; a stray reference is not cross-checked against result cardinality | **U9** unnecessary reference | MEDIUM — core mutation correct, reference meaningless | **YES** (cardinality) | **YES** | swap_direction |
| 16 | `compare_pronoun` run2 | 2 results | "Сравни их" | patch `[compare, last_two_results]`, ref `/null` | patch `[compare, last_two_results, reference_prior_result]`, ref `/last` — **identical to Qwen3.8's attractor** | VALID | as #1 | **U11** | LOW | **YES** (normalize) | n/a | set_operation, set_comparison, reference_prior_result |

Runs 15 and 16 are the *only* two Model B outputs that both cleared the schema
and were wrong. Note run 16: when Model B does clear the contract on a
comparison case, it makes Qwen3.8's exact mistake.

## 5. Unsafe taxonomy

| Class | Qwen3.8 | Model B | Description |
|---|---:|---:|---|
| **U1** should CLARIFY, model PATCHed | **1** | 0 | `ambiguous_pronoun_single_result` |
| **U2** should UNSUPPORTED, model PATCHed | 0 | 0 | — |
| **U3** wrong mutation kind | 0 | 0 | no case has a purely wrong kind |
| **U4** correct kind, wrong value | 0 | 0 | absorbed into U7/U8 |
| **U5** wrong prior-result / reference selection | **3** | 0 | `reference_first`, `reference_second`, `same_for_first` |
| **U6** source/destination or role error | 0 | 0 | **none observed** — notable, see §11 |
| **U7** comparison semantics error | **1** | 0 | `larger_value` |
| **U8** calculation semantics error | **4** | 0 | `absolute_difference`, `percent_difference` ×3 |
| **U9** unnecessary multi-mutation / overreach | 0 (co-occurs with U5) | **1** | Model B #15 |
| **U10** state-history misunderstanding | 0 | 0 | — |
| **U11** prompt/contract mismatch | **5** | **1** | the over-specification + selector cluster |
| **U12** corpus expectation questionable | 0 | 0 | see below |
| **Total** | **14** | **2** | |

### Model error vs evaluation-contract error

I checked every mismatch against "is the ground truth clearly justified by state
plus message?" and did **not** find a case where the corpus is wrong. No case is
classified `AMBIGUOUS_GROUND_TRUTH`. The corpus expectations are defensible:
"первый из тех двух" unambiguously means the first of the pair; "какова
разница" unambiguously asks for a computed value, not a view.

However, **the U11 cluster (6 of 16 unsafe runs) is not a semantic error at
all**, and counting it as "unsafe" overstates the unsafe rate. In those cases
the model got the operation and the comparison meaning exactly right and
differed from ground truth only by:

- adding a redundant `reference_prior_result` mutation alongside a
  `set_comparison=last_two_results` that already implies it, and
- setting `selector="last"` on a `last_two_results` reference where the corpus
  expects `null`.

Applied to canonical state, `compare` + `last_two_results` + a redundant
prior-result marker is what the user asked for. It is over-specified, not wrong.
And `last_two_results` + `selector=last` is *semantically incoherent* — "last"
of a reference that already denotes a pair — which is a defect in the contract's
`(kind, selector)` product space, not in the model's understanding. The
validator accepts it (`validator.py:131-135` only rejects `first`/`second` on
non-pair kinds).

**Adjusted view: Qwen3.8's genuinely unsafe count is 9 of 26 (34.6 %), not 14 of
26 (53.85 %) — still far too high to activate, but the distinction matters for
deciding what to fix.** Model B's genuinely unsafe count is 1 of 8.

## 6. Abstention failures

| | Qwen3.8 | Model B |
|---|---:|---:|
| expected CLARIFY → actual PATCH | **1** | **0** |
| expected UNSUPPORTED → actual PATCH | **0** | **0** |
| expected PATCH → wrong PATCH | **13** | **2** |

**Qwen3.8's high unsafe rate is NOT primarily an abstention failure.** 13 of 14
unsafe runs are on cases where a patch was genuinely wanted and the model
produced the wrong patch. Only one is a failure to abstain.

But abstention is broken in a different, quieter way. Qwen3.8 **never emitted a
single `CLARIFY` in 26 runs.** On the four clarification cases it produced
`unsupported` three times and a dangerous `patch` once. Degrading CLARIFY into
UNSUPPORTED is safe but useless — the user gets "I can't do that" instead of a
question that would have unblocked them. Combined with the U1 case, this means
Qwen3.8 has no working mechanism for expressing uncertainty at all: it is either
confidently right or confidently wrong, at confidence 0.95–0.98 in both cases.

That is the property that most disqualifies it from an unsupervised active role.

## 7. Mutation-type matrix

**This is the deliverable that determines what a PR6 could touch.**

### By expected mutation kind / action (the activation-relevant view)

**Qwen3.8-27B**

| Kind / action | Runs | Valid | Correct | Unsafe | Rejected | Precision (of valid) | Coverage (of runs) |
|---|---:|---:|---:|---:|---:|---:|---:|
| **`swap_direction`** | 5 | 5 | **5** | **0** | 0 | **100 %** | **100 %** |
| **`unsupported`** | 3 | 3 | **3** | **0** | 0 | **100 %** | **100 %** |
| `set_operation`+`set_comparison` | 11 | 11 | 1 | **10** | 0 | **9 %** | 9 % |
| `reference_prior_result` | 3 | 3 | 0 | **3** | 0 | **0 %** | 0 % |
| `clarify` | 4 | 4 | 0 | 1 | 0 | **0 %** | 0 % |
| *deterministic control* | 3 | — | 3 | 0 | 0 | n/a | 100 % |

**Model B**

| Kind / action | Runs | Valid | Correct | Unsafe | Rejected | Precision (of valid) | Coverage (of runs) |
|---|---:|---:|---:|---:|---:|---:|---:|
| `swap_direction` | 5 | 3 | 2 | 1 | 2 | 67 % | 40 % |
| `unsupported` | 3 | 3 | 2 | 0 | 0 | 67 % | 67 % |
| `set_operation`+`set_comparison` | 11 | 1 | 0 | 1 | 10 | 0 % | 0 % |
| `reference_prior_result` | 3 | 0 | 0 | 0 | 3 | n/a | 0 % |
| `clarify` | 4 | 1 | **1** | 0 | 3 | **100 %** | 25 % |
| *deterministic control* | 3 | — | 3 | 0 | 0 | n/a | 100 % |

### By actually emitted mutation kind (the false-positive view)

**Qwen3.8-27B**

| Emitted kind | Times emitted | In a correct run | In an unsafe run | Precision |
|---|---:|---:|---:|---:|
| **`swap_direction`** | **5** | **5** | **0** | **100 %** |
| `set_operation` | 14 | 1 | 13 | 7 % |
| `set_comparison` | 14 | 1 | 13 | 7 % |
| `reference_prior_result` | 14 | **0** | **14** | **0 %** |
| action `unsupported` | 6 | 3 | 0 | 50 % |
| action `clarify` | **0** | — | — | never emitted |

**Model B**

| Emitted kind | Times emitted | In a correct run | In an unsafe run | Precision |
|---|---:|---:|---:|---:|
| `swap_direction` | 3 | 2 | 1 | 67 % |
| `set_operation` | 1 | 0 | 1 | 0 % |
| `set_comparison` | 1 | 0 | 1 | 0 % |
| `reference_prior_result` | 1 | 0 | 1 | 0 % |
| action `clarify` | 2 | 1 | 0 | 50 % |
| action `unsupported` | 2 | 2 | 0 | 100 % |

**Reading of the matrix.**

`swap_direction` for Qwen3.8 is clean on both views. It was emitted exactly 5
times, on exactly the 5 runs where it was wanted, and it appears in **zero** of
the 14 unsafe runs. Across the 21 runs where a direction swap was *not* wanted,
it was never proposed — a false-positive rate of 0/21. Three distinct phrasings
were covered: "А теперь наоборот", "Покажи обратное направление", "Обратно",
one of them verified stable over 3 temperature-zero runs.

`reference_prior_result` is the mirror image and must be **closed off entirely**:
emitted 14 times, present in 0 correct runs and 14 unsafe runs. It is the single
component most responsible for the attractor template.

`set_operation` / `set_comparison` at 7 % precision must also be **closed off**.
Even granting the U11 adjustment, the model cannot distinguish `compare` from
`calculate` and cannot select the right comparison meaning.

`clarify` is unusable for Qwen3.8 (never emitted) and unproven for Model B
(1/1, sample of one).

## 8. Validator effectiveness

| | Qwen3.8 | Model B |
|---|---:|---:|
| Raw attempts | 26 | 26 |
| Rejected by validator | **0** | **18** |
| Reached semantic evaluation | 26 | 8 |
| Unsafe among accepted | **14 (53.85 %)** | 2 (25 %) |
| Semantic errors caught by validator | **0** | **0** |

Model B rejection reasons (18 runs, errors non-exclusive):

| Reason | Count | Nature |
|---|---:|---|
| `proposal_schema_invalid` | 17 | structural |
| `mutation_value_forbidden` | 13 | structural — non-null `value` on a kind requiring null |
| `prior_result_reference_required` | 3 | consistency |
| `invalid_operation_value` | 2 | structural |
| `invalid_comparison_value` | 1 | structural |
| `duplicate_mutation_kind` | 1 | structural |
| `patch_cannot_clarify` | 1 | consistency |

**The validator provides structural safety only.** This is now demonstrated
rather than argued: for Qwen3.8 it rejected nothing at all while 14 of the 26
outputs it waved through were semantically unsafe — a 0 % catch rate on semantic
error. Every Model B rejection is a typing or consistency violation; not one is
a rejection of a well-formed proposal for meaning the wrong thing.

**Adversarial challenge 2 resolved: Model B is VALIDATOR-FILTERED, not
semantically safer.** Three independent pieces of evidence:

1. 69 % of its outputs never reach semantic scoring; the comparison
   between a 25 % unsafe rate over 8 samples and a 53.85 % rate over 26 is not
   a like-for-like comparison.
2. The rejections are dominated by `mutation_value_forbidden` — putting a value
   on `swap_direction` or `reference_prior_result` — which is a contract-typing
   mistake, evidence of *less* competence, not more caution.
3. Decisively: on `compare_pronoun` run 2, the one comparison case where Model B
   did clear the schema, it emitted the byte-identical attractor template that
   Qwen3.8 emits. When the filter is removed, the same error appears.

One genuine credit: its single `clarify` on `ambiguous_first_without_pair` was
correct and well-phrased ("Какой из двух предыдущих результатов вы имеете в
виду?"). That is a real capability Qwen3.8 lacks.

Also worth flagging: the harness scores `clarify` and `unsupported` on the
**action only** (`evaluation.py:197-198` returns `True` as soon as the action
matches). A "correct clarification" is not verified to be the *right* question.
Model B's happened to be good; the metric would not have noticed if it were not.

## 9. Stability analysis

Three cases run 3× at temperature 0.

**Qwen3.8 — 100 % stable (3/3), and stability holds specifically for unsafe output:**

| Case | Runs | Distinct signatures | Unsafe | Stable |
|---|---:|---:|---:|---|
| `reverse_business_direction` | 3 | **1** | 0/3 | yes |
| `compare_pronoun` | 3 | **1** | **3/3** | yes |
| `percent_difference` | 3 | **1** | **3/3** | yes |

Both repeated cases that produced unsafe output produced **byte-identical**
unsafe output on all three runs — same mutations, same references, same
`reason_code`, same confidence. The wrong answers are stable in the same wrong
direction. This is systematic misunderstanding, not inference noise.

**Model B — 33.33 % stable (1/3):**

| Case | Runs | Distinct signatures | Outcome per run |
|---|---:|---:|---|
| `reverse_business_direction` | 3 | 2 | valid+unsafe / rejected / rejected |
| `compare_pronoun` | 3 | 2 | rejected / valid+unsafe / rejected |
| `percent_difference` | 3 | 1 | rejected ×3 (stable) |

Its instability changes **schema validity**, not semantics: the same case flips
between "passes the contract" and `proposal_schema_invalid` /
`mutation_value_forbidden` across runs. It is not oscillating between different
meanings; it is oscillating between being able and unable to express itself.
That is a contract-compliance defect, and it also means its 62.5 % precision
figure rests on a non-deterministic sample.

## 10. Clarification analysis

Every clarification ground-truth case, both models:

| Case | Qwen3.8 | Model B |
|---|---|---|
| `correction_geo` — "Нет, я имел в виду Нижний Новгород" | `unsupported` (`ENTITY_REPLACEMENT_NOT_ALLOWED`) — safe, unhelpful | **rejected** (`invalid_operation_value`, `invalid_comparison_value`, `proposal_schema_invalid`) |
| `correction_business` — "Нет, только Ухта" | `unsupported` (`ENTITY_FILTER_NOT_ALLOWED`) — safe, unhelpful | **rejected** — had wanted to clarify but emitted `action=patch` with duplicated `set_operation=show` *and* a clarification question (`duplicate_mutation_kind`, `patch_cannot_clarify`) |
| `ambiguous_pronoun_single_result` — "Сравни их", 1 result | **`patch` — UNSAFE** | **rejected** (`proposal_schema_invalid`) |
| `ambiguous_first_without_pair` — "Первый из тех двух", 1 result | `unsupported`, correctly listing the unresolved mention | **`clarify` — CORRECT**, good question |
| **Correct** | **0 / 4** | **1 / 4** |

Two observations that matter more than the counts.

Qwen3.8's three `unsupported` responses on clarification cases are *safe
failures* — it abstains rather than acting. Its reason codes
(`ENTITY_REPLACEMENT_NOT_ALLOWED`, `ENTITY_FILTER_NOT_ALLOWED`) show it
correctly recognised that entity replacement is outside the PR5 vocabulary. It
then chose the wrong abstention channel. Arguably `unsupported` is a defensible
reading of "this correction cannot be represented by the allowed vocabulary" —
the prompt itself says "If it cannot be represented by the allowed vocabulary,
use UNSUPPORTED". **The corpus expects `clarify` for a request the contract
genuinely cannot express, which is the one place the ground truth is debatable.**
I still do not classify it `AMBIGUOUS_GROUND_TRUTH`, because asking the user is
more useful than refusing; but the prompt is pushing the model the other way.

Model B's `correction_business` output is the most informative single failure in
either run: it *understood* the case (its question was "Please specify the entity
type for 'Ухта'"), but expressed it as a PATCH carrying a clarification question
plus a duplicated bogus mutation. The semantics were there; the contract
encoding was not.

## 11. Reference errors

Three ground-truth cases require selecting a specific prior result. Qwen3.8
failed all three; Model B never produced a valid proposal for any of them.

| Case | Message | Expected reference | Qwen3.8 actual |
|---|---|---|---|
| `reference_first` | "Первый из тех двух" | `last_two_results` / `first` | `last_two_results` / **`last`** + spurious comparison |
| `reference_second` | "Возьми второй вариант" | `last_two_results` / `second` | `last_two_results` / **`last`** + spurious comparison |
| `same_for_first` | "То же для первого" | `last_two_results` / `first` | **`previous_result`** / `last` |

**Root cause identified: the system prompt never defines the selectors.**
`src/balance_chat/prompts/semantic_transition_shadow.md` mentions
`reference_prior_result` and the reference kinds, but the words `first`,
`second`, `last`, and `same` appear **nowhere in it**. The model was handed a
four-value enum with no semantics and no worked example, and defaulted to
`last` in every case.

The context representation is not the limiting factor here — ordering *is*
present, via `recency: 2` (апрель) and `recency: 1` (май) in
`recent_addressable_results`. But nothing tells the model that "first of those
two" maps to the higher `recency` number. The inversion between an ordinal in
the user's language and a recency counter in the state is exactly the kind of
mapping a prompt must state explicitly.

**No U6 role/direction errors were observed in either model.** Given that the
`directed_flow` context carries explicit `role: source` / `role: destination`
labels and both models handled direction correctly, operand-role representation
appears adequate. This is a point in favour of the current context design.

## 12. Comparison / calculation errors

Five Qwen3.8 unsafe runs turn on choosing the wrong operation or comparison
value.

| Case | Message | Expected | Actual | Error |
|---|---|---|---|---|
| `absolute_difference` | "Какова разница?" | `calculate` / `absolute_difference` | `compare` / `last_two_results` | both wrong |
| `percent_difference` ×3 | "Какова разница в процентах?" | `calculate` / `percent_difference` | `compare` / `percent_difference` | **comparison right, operation wrong** |
| `larger_value` | "Что больше?" | `compare` / `larger_value` | `compare` / `last_two_results` | **operation right, comparison wrong** |

The `percent_difference` result is the most diagnostic in the whole evaluation.
The model correctly identified the comparison semantics — it picked
`percent_difference` out of a five-value enum, three times identically — and
then paired it with `set_operation=compare` instead of `calculate`. It
understands *what* is being asked and not *which operation category* that
belongs to.

**Root cause: the prompt lists `set_operation with value compare or calculate`
and never says what distinguishes them.** There is no rule, no example, and no
statement that "разница" implies a computed scalar while "что больше" implies a
comparison. `set_operation` and `set_comparison` also overlap semantically —
`last_two_results` is arguably a *scope*, not a comparison meaning, and it sits
in the same enum as `absolute_difference` and `percent_difference`, which are
genuinely comparison meanings. That conflation is a contract-design issue and
plausibly contributes to the collapse onto `compare`/`last_two_results` as the
default pair.

## 13. Context / contract issues

Correlating unsafe cases against what the bounded context contains:

| Needed information | Present in context? | Correlated failures |
|---|---|---|
| Operand roles (source/destination) | **Yes** (`role` field) | none — U6 count is 0 |
| Result ordering | **Yes** (`recency`) | 3 reference errors, but from missing *prompt semantics*, not missing data |
| Prior-result identity | **Partial** — results are described by `state` summary, `row_count`, `fact_count`; there is no stable handle | contributes to reference errors |
| Result cardinality | **Implicit** — length of `recent_addressable_results` | the U1 critical case: model proposed `last_two_results` against a 1-element list |
| Direction of an existing flow | **Yes** | none — `swap_direction` is 100 % |
| Comparison operands | **Weak** — no explicit statement of what two things would be compared | contributes to comparison/calculation errors |
| Pending clarification | **Absent** from the context contract | not exercised by this corpus |

The context is genuinely well-designed for the cases it gets right, and the
failures are mostly **not** attributable to missing context. Two exceptions
worth naming: there is no stable identifier for an addressable result (making
"the first one" hard to express unambiguously), and result cardinality is
implicit rather than stated.

Contract-design issues, identified but not automatically recommended for
expansion:

1. `(last_two_results, selector=last)` is meaningless yet accepted — the
   `(kind, selector)` matrix should be constrained.
2. `reference_prior_result` is redundant when `set_comparison=last_two_results`
   is present; the contract permits both and the model always emits both.
3. `set_comparison` mixes a scope value (`last_two_results`, `named_periods`)
   with genuine comparison meanings (`absolute_difference`, `percent_difference`,
   `larger_value`).
4. The prompt's instruction "If it cannot be represented by the allowed
   vocabulary, use UNSUPPORTED" competes directly with the corpus's expectation
   of `clarify` on correction cases, and Qwen3.8 followed the prompt.

## 14. Model vs prompt vs validator attribution

| Failure class | Runs (Q3.8) | Primary attribution |
|---|---:|---|
| U11 over-specification + selector | 5 | **validator/contract** — accepts redundant and incoherent combinations |
| U8 operation `compare` vs `calculate` | 4 | **prompt** — enum listed, semantics never stated |
| U7 wrong comparison value | 1 | **prompt** — no phrasing-to-value guidance |
| U5 wrong reference selector | 3 | **prompt** — selectors never mentioned at all |
| U1 patched instead of clarifying | 1 | **validator/gate** — no cardinality precondition; secondarily model capability |

**Not one of the 14 requires enumerating corpus-specific phrases to fix.** The
fixes are: constrain a contract product space, define what four enum values
mean, define when two operation values apply, and add a cardinality
precondition. All are general statements about the vocabulary, none is a phrase
list. Nothing here is OVERFIT.

Genuine model-capability limits do exist and are not prompt-fixable: the total
absence of `CLARIFY` from Qwen3.8's repertoire, and its uniform 0.95–0.98
confidence regardless of correctness, are behavioural properties, not
instruction gaps.

## 15. Deterministic gate opportunities

Candidate gates, simulated against the actual unsafe runs.

| # | Gate | Classification | Effect on Qwen3.8's 14 |
|---|---|---|---|
| **G1** | Drop `reference_prior_result` when `set_comparison`/`set_operation` is also present (or reject the combination) | **GOOD GATE** — small invariant over the contract | part of the 5 normalization fixes |
| **G2** | On a `last_two_results` reference, `selector` must be `null`, `first`, or `second`; reject or canonicalize `last` | **GOOD GATE** — removes a meaningless state from the product space | part of the 5 normalization fixes |
| **G3** | Reject any `last_two_results` reference when fewer than 2 addressable results exist | **GOOD GATE** — cardinality precondition, exactly the brief's "cardinality == 1" idea | **blocks the CRITICAL U1 case** |
| **G4** | Allow `swap_direction` only when exactly one directed relation exists in the active state | **GOOD GATE** | 0 (no swap_direction failures to catch) — but it is what would make an active `swap_direction` island safe |
| **G5** | Forbid PATCH when `unresolved_mentions` is non-empty | **GOOD GATE** | 0 here — Qwen3.8 returned empty mentions on every unsafe run, so this gate never fires. Worth having; ineffective against this failure mode |
| G6 | Require `set_operation=calculate` whenever `set_comparison` ∈ {`absolute_difference`, `percent_difference`} | **BORDERLINE** — it is a real contract invariant, but it encodes a semantic judgement | would fix 3 (`percent_difference` ×3) |
| G7 | Map specific Russian phrasings to specific comparison values | **BAD GATE — OVERFIT** | not recommended |

Simulated outcome for Qwen3.8's 14 unsafe runs, applying G1+G2+G3:

```
5  fixed by normalization  (compare_bare, compare_pronoun x3, compare_last_two)
1  blocked by cardinality  (ambiguous_pronoun_single_result — the critical one)
8  residual genuine errors (absolute_difference, percent_difference x3,
                            larger_value, reference_first, reference_second,
                            same_for_first)
```

For Model B's 2: 1 fixed by normalization, 1 blocked by cardinality, **0
residual**.

Adding the borderline G6 would fix 3 more (the `percent_difference` runs),
leaving 5 residual. I do not recommend G6 as a gate — it belongs in the prompt
and in the contract's shape, not in a post-validator rule engine.

## 16. Potential active-repair islands

**`swap_direction` (Qwen3.8-27B) is the only candidate.**

Evidence for:

- 5/5 correct, 0 unsafe, 100 % precision on the expected-kind view;
- 5 emissions, all on the 5 runs where it was wanted — **0 false positives across
  21 non-direction runs**;
- 3 distinct phrasings covered, including the bare adverb "Обратно";
- one case verified byte-identical over 3 temperature-zero runs;
- the validator already enforces `unsupported_multi_field_combination`, so a
  `swap_direction` proposal structurally cannot smuggle another mutation
  alongside it;
- G4 (exactly one directed relation in active state) is a clean precondition;
- the corresponding production baseline was `status: no_data` — this is a case
  production currently gets wrong, so the upside is real.

Evidence against, and why it still fails the bar:

- three distinct cases is a thin base for an activation decision;
- Model B achieves only 67 % on the same kind, so the result is model-specific
  and would need re-validation on any model change;
- the corpus contains no *adversarial* direction cases — no message that
  superficially resembles a reversal but is not one — so the 0/21 false-positive
  figure has not been stress-tested;
- most importantly, activating any island inside a model that **cannot abstain**
  (0/4 clarifications, never emits `CLARIFY`, uniform 0.95+ confidence) means
  out-of-island inputs get a confident answer rather than a refusal.

`unsupported` is also 3/3 for Qwen3.8, but it is an abstention, not a repair —
"correctly declining" produces no state transition and so has no activation
value beyond suppressing a bad answer.

Everything else is **closed**: `reference_prior_result` (0 %, 14/14 unsafe),
`set_operation`/`set_comparison` (7 %), `clarify` (never emitted).

## 17. Qwen3.8 vs Model B recommendation

**Qwen3.8-27B → KEEP FOR NARROW ACTIVE CANDIDATE (`swap_direction` only, after a
confirmatory experiment).**
It is the only model with a clean, false-positive-free mutation kind, 100 %
typed output, and full determinism. Its weaknesses — comparison/calculation
confusion, reference selection, and total absence of CLARIFY — are concentrated
in kinds that would simply not be activated.

**Model B → KEEP FOR SHADOW ONLY.**
It cannot be a primary proposer: 69 % of its output fails the contract, its
stability is 33 %, and it is worse than Qwen3.8 on the one kind that works. Its
one genuine asset is that it can produce a correct clarification, which Qwen3.8
cannot do at all. That is worth keeping in shadow to see whether it holds up.

No single winner is forced. They fail differently and the difference is
informative.

### Critic / veto architecture

The proposal "Qwen3.8 candidate → Model B semantic veto → YES/NO/CLARIFY" is
**not supported by this evidence**, for three reasons drawn from the data rather
than from principle:

1. **Correlated failure.** On the one comparison case where Model B cleared the
   contract, it produced Qwen3.8's byte-identical wrong answer. A critic whose
   errors correlate with the proposer's cannot veto them.
2. **The critic cannot express itself.** A veto must be reliably parseable.
   Model B fails schema validation 69 % of the time; its verdict would be
   unavailable on most turns, and its instability (33 %) means the same input
   yields different verdicts.
3. **It would veto the wrong things.** Model B rejects most comparison-family
   proposals *by failing the schema*, not by judging them wrong — and it is
   worse than Qwen3.8 precisely on `swap_direction`, the one kind worth
   activating, so it would most likely veto the good island while passing
   through nothing else.

Following the stated preference order — small deterministic invariant > single
model proposal > second-model critic > multi-agent loop — the deterministic
gates in §15 deliver more safety than a critic would, at a fraction of the cost
and with no added latency. **No agent framework, planner agent, or multi-agent
loop is justified by anything in this data.**

### Against the PR6 activation bar

| Criterion | Bar | Qwen3.8 (overall) | Qwen3.8 (`swap_direction` only) | Model B |
|---|---|---|---|---|
| Semantic precision | ≥ 90 % | 34.62 % ✗ | **100 %** ✓ | 62.5 % ✗ |
| Unsafe accepted rate | ≤ 2 % | 53.85 % ✗ | **0 %** ✓ | 25 % ✗ |
| Typed output rate | ≥ 95 % | **100 %** ✓ | **100 %** ✓ | 30.77 % ✗ |
| Correct clarification | ≥ 80 % | 0 % ✗ | 0 % ✗ | 25 % ✗ |
| Coverage | may be low | 23.08 % — | 100 % — | 7.69 % — |

The `swap_direction` subset passes three of four criteria and fails only on
clarification. That failure is not incidental: the clarification criterion
exists because an active system must be able to decline, and Qwen3.8 cannot.
For a kind-restricted island the criterion could reasonably be replaced by
"zero false-positive emissions of the activated kind", which Qwen3.8 currently
satisfies at 0/21 — but that substitution needs to be earned by a larger
experiment, not assumed.

## 18. PR6 recommendation

**MAYBE — needs another shadow experiment, scoped to `swap_direction`.**

Do not open a PR6 that activates semantic repair on the current evidence. Do
open a follow-up shadow experiment that can answer the one open question:
whether `swap_direction`'s perfect record survives contact with cases designed
to break it.

Concretely, before any activation:

1. Expand the corpus with 15–20 direction cases across varied phrasing, **plus
   adversarial near-misses** — messages that mention direction, reversal, or
   "обратно" in contexts where a swap is *not* the right transition, and
   contexts with zero or multiple directed relations. The 0/21 false-positive
   figure is the number that must survive.
2. Implement gates G1, G2, G3, G4 in the validator/contract. G1 and G2 are
   contract defects that should be fixed regardless of any PR6.
3. Fix the prompt gaps identified in §11 and §12 — define the four selectors,
   define `compare` vs `calculate`, and resolve the UNSUPPORTED/CLARIFY conflict
   for correction cases.
4. Re-run and re-measure. If `swap_direction` holds at ≥ 95 % precision with
   zero false positives on the adversarial set, a kind-restricted PR6 becomes
   defensible.

## 19. Suggested next experiment

Single highest-value run, combining the cheap general fixes with the
discriminating test:

- **Prompt v2** — add selector semantics (`first`/`second` relative to
  `recency`), a `compare`-vs-`calculate` rule, one worked reference example, and
  a clarified precedence between CLARIFY and UNSUPPORTED for requests outside the
  vocabulary.
- **Contract v2** — constrain the `(kind, selector)` matrix (G2), forbid
  redundant `reference_prior_result` (G1), add the cardinality precondition (G3).
- **Corpus v2** — direction cases expanded to ~20 with adversarial near-misses;
  reference cases expanded; add explicit result identity/cardinality to the
  context contract.
- Re-run **Qwen3.8-27B** with 3 repeats on every case, not just three of them,
  so stability is measured across the whole corpus rather than 12 % of it.

The measurement that decides PR6: `swap_direction` precision and false-positive
rate on the adversarial set. Everything else is secondary.

---

## Final answers

**Q13 — How many Qwen3.8 unsafe proposals?**
**14**, independently confirmed, across 10 distinct cases (4 of the 14 are
repeat runs of 2 cases). Adjusted for the U11 contract-artifact cluster, the
genuinely semantically unsafe count is **9**.

**Q14 — How many Model B unsafe accepted proposals?**
**2**, confirmed, across 2 distinct cases. One is a spurious reference on an
otherwise-correct `swap_direction`; one is the shared attractor template.

**Q15 — How many unsafe cases are primarily "should CLARIFY → PATCH"?**
Qwen3.8: **1 of 14**. Model B: **0 of 2**. Neither model's unsafe rate is
primarily an abstention failure — 13 of Qwen3.8's 14 are wrong patches on cases
where a patch was genuinely wanted. (Separately, Qwen3.8 emitted **zero**
CLARIFY actions in 26 runs, which is a distinct and serious limitation.)

**Q16 — Breakdown by error type (Qwen3.8's 14, by primary driver):**

| Driver | Count |
|---|---:|
| Wrong mutation value (operation or comparison) | 5 |
| Wrong reference (kind or selector) | 3 |
| Over-specification / contract artifact only | 5 |
| Abstention failure (should clarify) | 1 |
| Wrong mutation kind (pure) | 0 |
| Role / direction error | 0 |
| Other | 0 |

Model B's 2: 1 over-specification, 1 unnecessary reference.

**Q17 — Is Qwen3.8's 100 % stability also true for its UNSAFE outputs?**
**YES.** Both repeated cases that produced unsafe output (`compare_pronoun`,
`percent_difference`) produced byte-identical unsafe proposals on all 3 runs —
one distinct signature each, same mutations, references, reason_code, and
confidence. The wrong answers are stable in the same wrong direction.

**Q18 — Do Qwen3.8 failures look MOSTLY SYSTEMATIC / MOSTLY STOCHASTIC / MIXED?**
**MOSTLY SYSTEMATIC.** One distinct signature per repeated case, and 13 of 14
unsafe runs share a single output template across six semantically different
prompts. This is mode collapse, not sampling noise.

**Q19 — Does evidence suggest MTP contributes to semantic errors?**
**NO EVIDENCE.** 100 % valid JSON, 0 malformed outputs, 0 validator rejections,
and byte-identical repeats at temperature 0. Decoding corruption from
speculative decoding would present as malformed output or run-to-run variation;
neither occurs. The errors are coherent, well-formed, internally consistent
proposals with sensible `reason_code` values. **MTP is very likely irrelevant to
the observed semantic failures.** No further claim is made.

**Q20 — Is Model B safer semantically, or mainly validator filtering?**
**VALIDATOR-FILTERED.** 18 of 26 outputs never reached semantic evaluation; the
rejections are dominated by structural typing errors (`proposal_schema_invalid`
17, `mutation_value_forbidden` 13), not semantic caution; and on the single
comparison case where it did pass validation it emitted Qwen3.8's identical
wrong answer. It is quieter, not safer.

**Q21 — Highest-precision mutation kind for Qwen3.8?**
**`swap_direction` — 5 runs, 5 valid, 5 correct, 0 unsafe, 100 % precision**,
with 0 false positives across the 21 non-direction runs. (`unsupported` as an
action is also 3/3.)

**Q22 — Highest-precision mutation kind for Model B?**
Among mutation kinds, **`swap_direction` — 3 valid, 2 correct, 1 unsafe, 67 %**.
Among actions, `unsupported` 2/2 and `clarify` 1/1 (sample of one each).

**Q23 — Any mutation kind with 0 unsafe accepted AND enough positive cases?**
**Qwen3.8 `swap_direction`: yes on the criterion, borderline on "enough".**
5 runs / 3 distinct phrasings / 0 unsafe / 0 false positives. Also `unsupported`
at 3/3/0. No other kind for either model qualifies. Three distinct cases is
suggestive, not sufficient for activation.

**Q24 — Credible narrow active-repair island?**
**YES — `swap_direction` (Qwen3.8-27B) — but NEEDS MORE DATA before activation.**
It is the only kind that is clean on both the expected-kind and the
false-positive views. The gap is an adversarial direction corpus, not a
correction to the model.

**Q25 — Could small deterministic preconditions eliminate a substantial fraction
of Qwen3.8's 14 unsafe cases?**
**PARTIALLY — 6 of 14.** Simulated: 5 fixed by normalization (G1 redundant
mutation + G2 selector canonicalization) and 1 blocked by a cardinality
precondition (G3) — and that 1 is the critical `ambiguous_pronoun_single_result`
case. **8 residual genuine semantic errors** would remain. (A borderline
operation/comparison consistency rule would fix 3 more, but that belongs in the
prompt and contract, not a rule engine.)

**Q26 — Would fixing unsafe cases require phrase-specific rules?**
**NO.** Every identified fix is a general statement: constrain a `(kind,
selector)` product space, forbid a redundant mutation, add a cardinality
precondition, define what four enum selectors mean, define when `calculate`
applies. None enumerates corpus phrases. Nothing recommended here is OVERFIT.
A phrase-to-value mapping table *was* considered and is explicitly rejected as a
BAD GATE.

**Q27 — Is a second-model critic justified by current evidence?**
**NO** (at most EXPERIMENT ONLY, and low priority). Three evidence-based
reasons: Model B's errors are correlated with Qwen3.8's on the shared case; it
fails schema validation 69 % of the time so its verdict is usually unavailable;
and it is worse than Qwen3.8 on `swap_direction`, so it would veto the one good
island. Deterministic gates deliver more safety at lower cost.

**Q28 — Which model should remain primary shadow candidate?**
**Qwen3.8-27B.** It has the only clean mutation kind, 100 % typed output, and
full determinism, and its failures are concentrated in kinds that would be
closed off anyway. Model B stays in shadow purely to track its one distinctive
capability — producing a correct clarification — which Qwen3.8 entirely lacks.
Note its identity caveat: the second artifact does not self-identify as
Qwen3.6-35B-A3B.

**Q29 — Active-repair readiness?**
**NOT READY.** With every accepted PATCH applied, Qwen3.8 would produce 6 correct
repairs and 14 unsafe state transitions (70 % of applied patches wrong), and the
validator would catch none of them. The `swap_direction` island is real and is
the reason this is "not ready" rather than "abandon", but three distinct cases
and an untested false-positive rate cannot carry an activation decision, and the
model's total inability to abstain makes any unsupervised island risky.

**Q30 — Recommended next step, ranked:**

- **Primary: C — prompt/context refinement and rerun.** All 8 residual semantic
  errors trace to a prompt that enumerates a vocabulary without defining it:
  the selectors `first`/`second`/`last`/`same` appear nowhere in the prompt, and
  `compare` vs `calculate` is never distinguished. This is the cheapest,
  most general, least overfit intervention available, and it is a prerequisite
  for interpreting any future run. Bundle the G1/G2/G3 contract fixes into it —
  those are contract defects, not experiments.
- **Secondary: B — narrower shadow experiment by mutation kind**, scoped to
  `swap_direction` with adversarial near-miss cases, to test whether the 0/21
  false-positive rate survives.

Explicitly not recommended now: **A** (PR6 active repair — precision far below
bar), **E** (model-critic veto — evidence argues against it), **F** (abandon —
premature; there is a real island and a clean, zero-risk shadow harness to keep
measuring it with).
