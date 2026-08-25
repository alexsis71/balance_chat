# PR5-EVAL2: semantic contract v2 and adversarial shadow evaluation

## 1. Goal

PR5-EVAL2 measures whether a smaller, explicit proposal contract improves
Qwen3.8 shadow reliability and whether `swap_direction` survives a deliberately
adversarial corpus. It does not activate semantic repair.

## 2. PR5 baseline

The immutable PR5 Qwen3.8 artifact remains
`artifacts/semantic_shadow_results_qwen38_27b_pr5.json`. Its raw result was 26
invocations, 26 accepted typed proposals, 34.62% semantic match, 6 correct
PATCHes, and 14 raw evaluator-unsafe PATCHes. The independent forensic review
classified 5/14 as contract/over-specification artifacts and 9/26 as genuinely
unsafe. Neither figure is rewritten by EVAL2.

## 3. Contract v2 delta

- `ReferenceSelector` retains only `first` and `second`.
- `last_two_results` accepts null, `first`, or `second`; every other reference
  kind requires a null selector. `last_two_results/last` is not representable.
- `first` means the older member of the latest pair (`recency=2`); `second`
  means the newer member (`recency=1`). The most recent result is represented
  by `previous_result`, not a `last` selector. Current state is represented by
  `active_state`, not a `same` selector.
- CLARIFY carries one bounded `clarification_reason`: `missing_second_result`,
  `ambiguous_result_reference`, `ambiguous_direction`, or
  `entity_role_ambiguous`.
- Shadow results retain raw payload/validation, normalized proposal/actions,
  and contextual gate status/reasons.
- The endpoint output-contract name changed from v1 to v2. Production intent,
  mutation, execution, API, and store contracts did not change.

## 4. Prompt v2 delta

The prompt now defines recency and pair ordering, all retained reference forms,
the compare/calculate boundary, and every retained comparison value. It states
that `last_two_results` and `named_periods` are scopes, while
`larger_value`, `absolute_difference`, and `percent_difference` describe the
requested operation over that scope. It also defines CLARIFY versus UNSUPPORTED
precedence and tells the model not to emit redundant
`reference_prior_result`. Four structural examples are used; no evaluation
phrase is copied as a phrase-specific rule.

## 5. Context v2 delta

The context remains bounded to four turns and four results and still excludes
canonical IDs. It adds deterministically derived:

- `addressable_result_count`;
- `relative_position` (`most_recent`, `previous`, `older`);
- `directed_relation_count`;
- `direction_ambiguous`.

Directed relations recognize both canonical shapes reachable in this codebase:
an explicit `source+destination` pair and the full-balance
`balance+(source|destination)+article` representation for incoming/distribution.

## 6. Structural validator

`ProposalValidator` remains deterministic, payload-only, model-independent,
and context-independent. It enforces selector product space and bounded
CLARIFY reasons in addition to the PR5 rules. Context cardinality and active
direction are not inspected here. The v1 replay had 12/60 structural rejects;
the v2 corpus had 45/180. All responses were JSON/schema-shaped, but the rejected
payloads violated semantic field constraints: forbidden mutation values (27),
duplicate mutation kinds (15), or invalid operation values (3) on v2.

## 7. Normalization

G1 removes `reference_prior_result` only when a proposal already has a
`set_operation`/`set_comparison` mutation and an explicit bounded result
reference. It never changes a selector or referent. Both raw and normalized
forms are preserved. It affected 0 outputs in both live runs: prompt v2 removed
the PR5 redundant-reference attractor before normalization was needed.

## 8. Contextual semantic gates

The separate contextual gate implements:

- G3: `last_two_results` requires at least two addressable results;
- G4: `swap_direction` requires exactly one unambiguous directed relation;
- G5: PATCH with unresolved mentions is rejected.

G4 affected 18 v2 outputs, blocked 18 unsafe swaps, and blocked 0 correct
outputs. G1, G2, G3, and G5 affected no live output. G1–G4 are **KEEP** because
they express stable representation/state invariants. G5 is **NEEDS MORE DATA**;
it is unambiguous but had no observed effect. No gate is phrase-specific.

## 9. Corpus v2

`tests/semantic_repair/corpus_v2.json` preserves v1 and adds 60 unique model
cases: 16 positive swaps, 24 swap near-misses, 10 reference cases, and 10
compare/calculate cases. State shapes cover zero, one, two, incomplete, explicit,
and full-balance directed relations plus zero, one, two, and four addressable
results. Every case ran three times at temperature zero.

## 10. v1 replay results

Prompt/Contract/Context/Gates v2 replayed the original PR5 corpus with all 20
model cases repeated three times. There were 60/60 calls, no timeout or
unavailable result, 100% run stability, 65.00% raw semantic match, 80.00%
structural acceptance, 18 correct PATCHes, 12 correct CLARIFY results, 9 correct
UNSUPPORTED results, and 6 unsafe PATCHes.

On the original 26-invocation shape, 9 of 17 PR5 failures became correct, 8
remained failures, and 1 formerly correct case regressed (`compare_named_seasons`
was changed to a latest-pair comparison). Fixed families/cases were the three
`compare_pronoun` repeats, bare/latest-pair comparison, both correction cases,
and both insufficient/ambiguous-reference clarification cases.

## 11. v2 adversarial results

There were 180/180 calls, no timeout or unavailable result, and 60/60 stable
unique cases. Raw semantic match was 50.00%; structural acceptance was 75.00%.
There were 54 correct PATCHes, 30 correct CLARIFY results, 6 correct UNSUPPORTED
results, 39 raw unsafe PATCHes, and 21 unsafe PATCHes after contextual gates.
The gates reduced unsafe PATCHes but did not make the accepted set safe enough.

## 12. Per-kind matrix

Metrics below use post-gate accepted emissions for precision/recall; runs are
the denominator, while expected cases are also shown as unique counts.

| Kind | Expected unique | Emissions | TP | FP | FN | Validator rejects | Gate rejects | Precision | Recall | Stability |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| swap_direction | 16 | 75 raw | 48 | 9 | 0 | 0 | 18 | 84.21% | 100.00% | 100.00% |
| set_operation | 7 | 12 | 6 | 6 | 15 | 12 | 0 | 50.00% | 28.57% | 100.00% |
| set_comparison | 7 | 18 | 6 | 12 | 15 | 12 | 0 | 33.33% | 28.57% | 100.00% |
| reference_prior_result | 7 | 0 accepted | 0 | 0 | 21 | 21 | 0 | n/a | 0.00% | 100.00% |
| clarify | 21 | 36 | 30 | 6 | 33 | 6 | 0 | 83.33% | 47.62% | 100.00% |
| unsupported | 9 | 6 | 6 | 0 | 21 | 6 | 0 | 100.00% | 22.22% | 100.00% |

## 13. swap_direction analysis

| Metric | Result |
|---|---:|
| Positive unique cases | 16 |
| Positive total runs | 48 |
| Correct swaps | 48 |
| False negatives | 0 |
| Non-direction unique cases | 44 |
| Non-direction total runs | 132 |
| False-positive swap emissions, raw | 27 |
| False-positive swap emissions, post-gate | 9 |
| Precision, post-gate | 84.21% |
| Recall | 100.00% |
| G4 rejects | 18 |
| Stability | 100.00% |

The three post-gate false-positive case families were
`near_n04_ask_availability`, `near_n11_new_query`, and
`near_n12_correction`, each stable across three repeats. They all had one valid
directed relation, so G4 correctly could not distinguish intent from state
alone. This misses the predeclared 95% precision and zero-false-positive bar.

## 14. False-positive analysis

The dominant v2 error is no longer the PR5 redundant-reference proposal. It is
a raw PATCH containing only `swap_direction`: 27 occurrences across the
`swap_near_miss` category. G4 blocks 18 cases with invalid direction state, but
the remaining 9 are genuinely unsafe semantic false positives in valid states.
Adding message regex gates would overfit and is explicitly rejected.

## 15. Clarification analysis

Qwen produced the correct CLARIFY action and reason on 30/63 expected runs
(10/21 unique cases), for 47.62% correctness. Correct free-form questions
addressed the recorded ambiguity. Failures were mostly unsafe swap proposals,
comparison proposals despite missing operands, and structurally invalid result
references. CLARIFY behavior improved substantially over PR5's 0 emissions but
does not approach the 80% general-repair bar.

## 16. Reference analysis

No accepted `reference_prior_result` proposal was produced for 21 expected
runs: accepted-emission precision is undefined and recall is 0.00%. Raw attempts
were structurally rejected, mainly because the model placed a non-null value on
the reference mutation. Pair ordering is now explicit and invalid
`last_two_results/last` is impossible, but reference selection did not become a
reliable capability.

## 17. Compare/calculate analysis

Only 6/21 expected compare/calculate runs were exact semantic matches. Accepted
`set_operation` precision was 50.00%; accepted `set_comparison` precision was
33.33%. Absolute/percentage/larger-value failures from PR5 remain, and named
period comparison regressed on v1 replay. The clarified prompt improved simple
latest-pair comparison but did not make this family safe.

## 18. Mode-collapse analysis

The old 13/14 redundant-reference/selector attractor disappeared. It was
replaced by the simpler `swap_direction` attractor on 27 erroneous runs. The
answer is therefore **PARTIALLY**: the old signature is gone, but mode collapse
remains and moved to the candidate island under test.

## 19. Gate effectiveness

| Gate | Outputs affected | Unsafe blocked | Correct blocked | Decision |
|---|---:|---:|---:|---|
| G1 redundant reference normalization | 0 | 0 | 0 | KEEP invariant; more live evidence needed |
| G2 selector product space | 0 invalid emissions | 0 | 0 | KEEP |
| G3 result cardinality | 0 | 0 | 0 | KEEP |
| G4 unique directed relation | 18 | 18 | 0 | KEEP |
| G5 unresolved PATCH | 0 | 0 | 0 | NEEDS MORE DATA |

No implemented gate appears corpus-overfit: all are representation/state
invariants and none matches user phrases. G4 is useful but insufficient.

## 20. Stability

All 23 v1 corpus cases and all 60 v2 corpus cases were stable across three
repeats. Stability is 100%, but it includes stable wrong and unsafe proposals;
it is not evidence of correctness by itself.

## 21. Latency and confidence

| Corpus/outcome | Average ms | P50 ms | P95 ms |
|---|---:|---:|---:|
| v1 all | 7,720.25 | 7,735.0 | 10,685 |
| v1 correct | 6,808.69 | 6,451 | 9,127 |
| v1 unsafe | 9,255.17 | 9,259.0 | 9,392 |
| v2 all | 7,274.26 | 6,609.0 | 10,970 |
| v2 correct | 6,471.31 | 6,540.5 | 9,356 |
| v2 unsafe | 7,215.28 | 6,582 | 9,442 |

V2 confidence ranged only from 0.90 to 0.95. Mean confidence was 0.9467 for
correct, 0.9450 for incorrect, and 0.9423 for unsafe outputs; point-biserial
correlation with correctness was 0.0603. Confidence remains uncalibrated and is
not used as a gate.

## 22. Counterfactual active outcome

If every structurally accepted post-gate v2 proposal were active, 117 proposals
would pass: 54 correct repairs, 21 unsafe transitions, and 42 operationally safe
abstentions (36 semantically correct and 6 semantically wrong). Eighteen unsafe
PATCHes would be blocked by G4 and 45 payloads would be structurally rejected.
This simulation never invoked mutation, execution, or persistence.

## 23. Regressions

| Suite | Result |
|---|---:|
| Full pytest | 565 passed, 1 pre-existing warning |
| Semantic repair | 64 passed |
| Period PATCH | 34 passed |
| GEO PATCH | 103 passed |
| Business Entity PATCH | 79 passed |
| Transitions | 103 passed |
| Reducer + execution/commit + PostgreSQL store | 20 passed (8 + 9 + 3) |
| Golden pytest | 14 passed |
| Golden validate-only | 8/8 |
| P0 strict dry-run | 84/84 |
| PR3 strict dry-run | 20/20 |
| PR4 strict dry-run | 50/50 |

PR5a collected 553 tests; EVAL2 collects 565. Removed test nodes: 0. Added
test nodes: 12. The shadow-on/off service differential remains identical in
response and persisted state, so production differential mismatches are 0.
Dry-runs prove catalog coverage, not live database semantics.

## 24. Known limitations

- Corpus wording and expected proposals remain human-authored ground truth.
- The modest 60-case adversarial corpus cannot prove production safety.
- Structured generation still permits payloads rejected by semantic field
  constraints; its JSON envelope rate is 100%, but structural acceptance is
  75% on v2.
- G1, G2, G3, and G5 had no live effect in this run.
- No live DB-backed acceptance was run because EVAL2 is shadow-only and the
  requested acceptance evidence is non-destructive dry-run coverage.
- English clarification questions are permitted by the free-form contract but
  would need product-language policy before any active integration.

## 25. PR6 recommendation

**Decision C: contract/prompt improved general quality, but no kind meets the
active safety bar. Continue shadow evaluation.** General active repair is not
ready. A narrow swap-direction PR6 is **NO** for this evidence set: precision is
84.21%, false-positive accepted transitions are 9, typed structural acceptance
is 75%, and only stability meets the threshold.

## Mandatory comparison table

Rates use model invocations as denominator unless stated otherwise. Raw typed
output is the JSON/schema-shaped envelope rate; structural acceptance is shown
separately because v2 semantic field rules reject otherwise typed payloads.

| Metric | PR5 Qwen3.8 | EVAL2 v1 replay | EVAL2 v2 corpus |
|---|---:|---:|---:|
| Corpus/model invocations | 26 | 60 | 180 |
| Raw typed output | 100.00% | 100.00% | 100.00% |
| Raw semantic match | 34.62% | 65.00% | 50.00% |
| Post-normalization match | n/a | 65.00% | 50.00% |
| Structural reject rate | 0.00% | 20.00% | 25.00% |
| Context-gate reject rate | n/a | 0.00% | 10.00% |
| Unsafe raw | 14 (forensic: 9 genuine) | 6 | 39 |
| Unsafe post-gate | n/a | 6 | 21 |
| Correct PATCH | 6 | 18 | 54 |
| Correct CLARIFY | 0 | 12 | 30 |
| Repair coverage | 23.08% | 30.00% | 30.00% |
| Stability | 100% repeated subset | 100% whole corpus | 100% whole corpus |
| P50 latency | 8,971.5 ms | 7,735.0 ms | 6,609.0 ms |
| P95 latency | 10,658 ms | 10,685 ms | 10,970 ms |

## Mandatory verification answers

1. Did EVAL2 change production semantics? **NO**.
2. Did EVAL2 enable active semantic repair? **NO**.
3. Can proposals reach `ContextMutation` or `AnalysisIntent`? **NO**.
4. Did deterministic Period/GEO/Business transitions change? **NO**.
5. LLM calls for deterministic PATCH paths? **0**.
6. Is unknown/missing shadow mode still fail closed? **YES**.
7. Is structural validation deterministic/model-independent? **YES**.
8. Are G3/G4 separated from structural validation? **YES**.
9. Contract v2 changes: selector product space, bounded clarification reason,
   layered shadow evidence fields, and v2 output-contract name; exact details
   are in section 3.
10. Prompt v2 changes: selector/recency semantics, compare/calculate and all
    comparison meanings, redundant-reference rule, and CLARIFY/UNSUPPORTED
    precedence; exact details are in section 4.
11. Semantic context changes: result count/relative positions and directed
    relation count/ambiguity; exact details are in section 5.
12. Does `last_two_results/last` remain valid? **NO**.
13. Can a two-result reference pass with one result? **NO**, G3 rejects it.
14. Can swap pass G4 with zero relations? **NO**.
15. Can swap pass G4 with two relations? **NO**.
16. PR5 failures fixed on the original 26-run shape: **9 of 17**.
17. PR5 failures remaining on that shape: **8**; one prior success regressed.
18. Did the 13/14 attractor disappear? **PARTIALLY**; the old signature
    disappeared, but a new 27-run swap attractor appeared.
19. Qwen3.8 correct clarification rate: **30/63 = 47.62%**.
20. Reference selection precision: **n/a (0 accepted emissions); 0/21 correct,
    0.00% recall**.
21. Compare/calculate precision: **33.33% for accepted set_comparison emissions
    (6/18); exact expected-case recall 6/21 = 28.57%**.
22. swap_direction precision: **48/(48+9) = 84.21% post-gate**.
23. swap_direction false-positive count: **27 raw, 9 post-gate**.
24. swap_direction stability: **16/16 unique cases = 100%**.
25. Correct swaps blocked by deterministic gates: **0**.
26. Does any gate look overfit? **NO**; G5 needs more data, but no gate uses
    phrases or corpus-specific strings.
27. Full pytest: **565 passed, 1 warning**.
28. Production differential mismatch: **0**.
29. General active repair ready? **NO**.
30. Narrow swap_direction PR6 worth designing now? **NO**; precision and
    false-positive requirements fail despite perfect recall/stability.

## Provenance

Both EVAL2 artifacts record configured and served model
`Qwen/Qwen3.8-27B`, temperature 0, max tokens 512, MTP method `mtp`, one
speculative token, prompt/schema/corpus SHA-256 hashes, and evaluated branch SHA
`2f1151a9d374e5c0fc8f1ab552514ef0a0b4c127`. Raw PR5 evidence remains a
separate immutable artifact.
