# PR5 — Semantic Transition Proposal Contract + Qwen Shadow Mode

## 1. Goal

PR5 adds an opt-in, shadow-only semantic proposal boundary for hard contextual
turns that were not handled by the deterministic Period, GEO, or Business Entity
transition categories. The proposal is diagnostic evidence only. It is never
converted to `ContextMutation` or an executable `AnalysisIntent`.

Baseline: `f124040` (post-PR4a), 501 tests passed with one pre-existing warning.
Feature branch: `feat/semantic-repair-shadow-pr5`.

## 2. Architecture

The integration seam is after the authoritative commit in `BalanceChatService`:

```text
deterministic transition layer
        |
        +-- Period/GEO/Business PATCH --> existing processing --> commit
        |
        `-- NO_MATCH -------------------> existing processing --> commit
                                                               |
                                                               `--> shadow copy
                                                                    bounded context
                                                                    Qwen proposal
                                                                    validator
                                                                    structured log only
```

The response is built and request-cache entry is saved before shadow invocation.
Since PR5a, both the session lock and store-level turn reservation are released
before the bounded model call. Production latency is captured before reservation
release and logged separately from shadow latency. Shadow remains synchronous,
so it still contributes to current HTTP response wall time.

`processor.py` is unchanged. So are the transition classifier, deterministic
transition modules, reducer, normalization, `ExecutionAdapter`, planner,
executor, stores, and API models.

## 3. Why shadow mode

The earlier agentic PoC showed useful reasoning on some hard transitions, but it
also mixed semantic reasoning with tools and backend behavior. PR5 isolates one
question: can the model describe a safe typed state transition? It deliberately
provides no analytical tools and no authority over current processing.

## 4. Proposal contract

`SemanticTransitionProposal` has four actions:

- `patch`;
- `rebuild` (reserved and rejected by the PR5 validator);
- `clarify`;
- `unsupported`.

The PR5 mutation vocabulary is intentionally limited to:

- `set_operation` (`compare` or `calculate`);
- `set_comparison` (five bounded comparison meanings);
- `swap_direction`;
- `reference_prior_result`.

There are no Period, GEO, Business Entity, canonical-ID, SQL, or storage
mutations. The endpoint schema has `additionalProperties=false`, at most three
mutations, and at most four references.

## 5. Context contract

The serialized model input contains only:

- current active intent summarized semantically;
- at most four recent semantic turns;
- at most four recent addressable result summaries;
- current user message.

Entity labels and roles are included, but canonical entity IDs, result IDs,
database keys, SQL, credentials, connection strings, and raw full-session history
are excluded. Unit tests construct six committed turns and prove only the last
four turns/results are serialized. The live evaluation average was 796.85
characters per context.

## 6. Validator

`ProposalValidator` is deterministic and independent of the model. It rejects:

- unknown actions, mutation kinds, reference kinds, and selectors;
- PATCH without a mutation;
- CLARIFY without a question or with mutations;
- unsupported action/payload combinations;
- `rebuild` in the PR5 subset;
- more than three mutations or four references;
- duplicate mutation/reference kinds;
- `swap_direction` combined with another mutation;
- prior-result mutation without a bounded result reference;
- invalid operation/comparison values or confidence;
- canonical ID fields/tokens;
- output over 8192 characters;
- schema-invalid payloads.

Rejected, malformed, timed-out, and unavailable proposals have no production
effect.

## 7. Eligibility

Since PR5a, shadow eligibility is fail-closed:

```text
active analytical state
AND non-empty user message
AND interpretation mode == conversation_graph
```

Unknown, missing, empty, and future modes are skipped. Period, GEO, and Business
deterministic modes are also skipped. The committed corpus retains 26 model
invocations and three skipped deterministic controls. No new semantic classifier
or production routing branch was introduced.

## 8. Feature flag

The default is OFF. Bootstrap creates no runner unless the environment variable
named by `semantic_repair_shadow.enabled_env` is truthy. The default variable is:

```text
SEMANTIC_REPAIR_SHADOW_ENABLED
```

When disabled there is no state copy and zero extra model calls. The configured
pipeline `context` inference profile is reused. A dedicated client copy bounds
the shadow connect/read timeout and disables retries; credentials remain inside
the existing inference profile and are never logged.

## 9. Isolation guarantees

The service passes a deep copy of the pre-turn state to shadow after
`store.commit`, request-cache persistence, and reservation release. It does not
pass the production mutation or processed result. The proposal is logged only
after successful validation and is not added to the API response or canonical
state.

Tests prove that a fake shadow can mutate its state copy, propose direction
changes, propose a different clarification, return malformed JSON, time out,
report an unavailable endpoint, or raise an unexpected exception without
changing:

- production response;
- execution diagnostics/query payload;
- committed/reloaded intent;
- pending production clarification;
- session revision;
- production mutation.

## 10. Evaluation corpus

`tests/semantic_repair/corpus.json` contains 20 hard-tail cases and three
deterministic controls. Categories include direction reversal, comparison
continuation, calculation transformation, corrections, bounded result
references, ambiguity, and unsupported requests. It selectively reuses the
agentic PoC themes `reverse_business_direction`, `Сравни их`, and percentage
difference. `geo_switch`, Period, and Business transitions are controls and are
not sent to the model.

Three high-value cases request three temperature-zero runs. The prompt contains
two structural examples; it does not enumerate corpus phrases.

The `current_production_outcome` field is kept separate from expected proposal.
Some entries cite the earlier agentic PoC baseline; entries marked
`not_reexecuted` are not presented as fresh PR4a production measurements. The
explicit corpus expectation, not current production behavior, is ground truth.

## 11. Metrics

The evaluator reports invocation/status counts, exact and semantic match,
typed-output rate, proposal precision, correct/wrong PATCH and clarification,
unsafe accepted PATCH, repair coverage, latency, context size, repeated-run
stability, and tool availability. Definitions used here:

- unsafe transition rate = semantically wrong valid PATCH / valid proposals;
- semantic proposal precision = semantic matches / valid proposals;
- repair coverage = correct PATCH runs / eligible hard-tail runs;
- p95 latency uses nearest-rank over invoked runs.

Tools are N/A: PR5 shadow exposes no tool surface. Tool-call metric fields are
therefore `null`, not numeric zeros presented as measurements.

## 12. Results

Command:

```text
python scripts/run_semantic_shadow_eval.py \
  --corpus tests/semantic_repair/corpus.json \
  --output artifacts/semantic_shadow_results.json \
  --pipeline-root <pipeline-root> \
  --metadata-manifest <metadata-manifest> \
  --timeout 20 --max-tokens 512
```

Canonical successful Qwen3.8 run, observed on 2026-08-21 and preserved as
`artifacts/semantic_shadow_results_qwen38_27b_pr5.json`:

| Metric | Result |
|---|---:|
| Eligible runs | 26 |
| Shadow invocations | 26 |
| Valid proposals | 26 |
| Unavailable | 0 |
| Malformed / rejected / timeout | 0 / 0 / 0 |
| Valid typed-output rate | 1.0000 |
| Semantic proposal precision | 0.3462 |
| Exact / semantic match | 0.3462 / 0.3462 |
| Correct PATCH | 6 |
| Correct clarification | 0/4 |
| Unsafe accepted PATCH count | 14 |
| Unsafe transition rate | 0.5385 |
| Repair coverage | 0.2308 |
| Deterministic controls skipped | 3/3 |
| Repeated cases stable | 3/3 |
| Tools | N/A (not exposed) |

The endpoint health probe returned `reachable=true`; configured and served model
identity both report `Qwen/Qwen3.8-27B`. The deployment used native MTP with one
speculative token. `artifacts/semantic_shadow_results.json` remains the
compatibility path and contains the same successful evidence.

A second result is preserved as
`artifacts/semantic_shadow_results_qwen36_35b_a3b.json`. The artifact itself
reports configured alias `ai-balances-language` and served alias
`ai-balances-planner`; it does not independently prove the underlying checkpoint.
It is therefore described as **Model B / served alias
`ai-balances-planner`**. Exact checkpoint identity remains caveated despite the
provenance-oriented filename.

## 13. Unsafe transition analysis

The raw evaluator result remains **14 unsafe valid PATCHes out of 26
invocations**. Independent post-hoc forensic review classified five of those 14
as contract/over-specification artifacts and nine of 26 as genuinely
semantically unsafe. These are intentionally separate layers; the machine result
is not rewritten retrospectively.

The observed clean island was `swap_direction`: 5/5 correct, zero unsafe, and
zero false-positive emissions across 21 non-direction runs, covering three
distinct positive phrasings. This is insufficient evidence for activation and
requires a dedicated adversarial shadow experiment in PR5-EVAL2.

## 14. Production differential

The test harness runs the same authoritative processor/store flow with shadow
OFF and ON and compares the complete response and reloaded state after removing
only nondeterministic timestamps. Status, interpretation diagnostics, result,
clarification, execution diagnostics, active state, and revision are identical.

Baseline/test delta:

```text
baseline nodes: 501
current nodes:  535
removed:          0
added:            34
existing test files/parameter expectations changed: 0
```

This is code-level production differential evidence. Live DB-backed differential
acceptance was not run and is not claimed.

## 15. Latency

Successful Qwen3.8 inference latency was:

```text
average: 7985.08 ms
p50:     8971.50 ms
p95:     10658 ms
```

Authoritative processing time is captured before reservation release and logged
separately. Shadow is still synchronous and therefore delays the current HTTP
response, but it no longer extends the same-session `TurnInProgress` window.

## 16. Regression results and known limitations

```text
PR5 baseline full pytest:       535 passed, 1 warning
Period PATCH:                   34 passed
GEO PATCH:                     103 passed
Business transition file:       66 passed
Business processor file:        13 passed
Transition package:            103 passed
Reducer file:                     8 passed
Execution/commit file:            9 passed
Postgres store file:              3 passed
Golden pytest catalog:          14 passed
P0 strict dry-run:              84/84
PR3 transition dry-run:         20/20
PR4 transition dry-run:         50/50
PR5 semantic repair focused:    34 passed
```

The Business aggregate is reproducibly 66 + 13 = 79. Persistence-related files
are listed separately instead of retaining an unexplained aggregate label. The
single warning is the pre-existing Starlette/httpx deprecation warning. Dry-runs
prove catalog coverage only.

## 17. Recommendation for PR6

**A. Do not proceed to active repair.**

The architectural boundary and production isolation are code-tested and live
quality evidence is available, but raw and forensic unsafe rates remain far
above the general activation bar. The narrow `swap_direction` island requires a
dedicated adversarial experiment before any active subset is considered.

## Mandatory answers

1. Production user-visible semantics changed? **NO.**
2. Can a proposal affect current execution? **NO.**
3. Can a proposal affect persisted state? **NO.**
4. Can it affect the clarification shown to the user? **NO.**
5. Are deterministic Period/GEO/Business PATCHes shadowed? **NO.**
6. Extra shadow LLM calls for successful deterministic PATCHes? **0.**
7. Is shadow default OFF? **YES.**
8. Does the accepted proposal contract allow canonical database IDs? **NO.**
9. Is the proposal typed and bounded? **YES.**
10. Is validation deterministic? **YES.**
11. Are malformed/timeout/unavailable calls non-blocking? **YES.**
12. Can shadow increment the semantic revision? **NO.**
13. Does semantic repair live outside deterministic transitions? **YES.**
14. Did reducer/normalization/ExecutionAdapter semantics change? **NO.**
15. Were analytical execution tools exposed? **NO.**
16. Valid typed-output rate? **1.0000.**
17. Semantic proposal precision? **0.3462.**
18. Unsafe accepted transition rate? **0.5385; raw count 14.** Forensic review
    separately retains **9/26 genuinely semantic unsafe**.
19. Correct clarification rate? **0.0000 (0/4).**
20. Repair coverage? **0.2308.**
21. Qwen latency? **7985.08 ms average / 8971.50 ms p50 / 10658 ms p95**.
22. Production differential semantic mismatch? **NO.**
23. PR6 recommendation? **A — do not proceed to active repair.**
