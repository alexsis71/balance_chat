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
The session lock is released before the bounded model call. Production latency is
captured before shadow starts and logged separately from shadow latency.

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

Shadow eligibility is:

```text
active analytical state
AND non-empty user message
AND interpretation mode is not one of:
    deterministic_period_patch
    deterministic_geo_patch
    deterministic_business_entity_patch
```

In an active session the existing processor invokes the deterministic transition
layer before any other contextual path. Therefore any other resulting mode means
that the three transition categories returned `NO_MATCH`. No new semantic
classifier or production routing branch was introduced.

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
`store.commit`. It does not pass the production mutation or processed result.
The proposal is logged only after successful validation and is not added to the
API response or canonical state.

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
stability, and tool counts. Definitions used here:

- unsafe transition rate = semantically wrong valid PATCH / valid proposals;
- semantic proposal precision = semantic matches / valid proposals;
- repair coverage = correct PATCH runs / eligible hard-tail runs;
- p95 latency uses nearest-rank over invoked runs.

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

Observed on 2026-08-21:

| Metric | Result |
|---|---:|
| Eligible runs | 26 |
| Shadow invocations | 26 |
| Valid proposals | 0 |
| Unavailable | 26 |
| Malformed / rejected / timeout | 0 / 0 / 0 |
| Valid typed-output rate | 0.0000 |
| Semantic proposal precision | N/A (no valid proposal) |
| Exact / semantic match | 0.0000 / 0.0000 |
| Correct PATCH | 0 |
| Correct clarification | 0/4 |
| Unsafe accepted PATCH count | 0 |
| Unsafe transition rate | N/A (no valid proposal) |
| Repair coverage | 0.0000 |
| Deterministic controls skipped | 3/3 |
| Repeated cases stable | 3/3 (all unavailable) |
| Tool calls | 0 |

The configured endpoint health probe returned `reachable=false`. All calls
failed as `unavailable` before a model response, so this is an integration
failure result, not a Qwen quality result. The served model identity could not be
observed and is intentionally not inferred from the configured alias.

## 13. Unsafe transition analysis

No unsafe proposal passed the validator because no proposal was returned. The
count is zero, but the unsafe transition rate is **not measurable**, not 0%.
This run provides no evidence that Qwen meets an activation safety threshold.

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

Unavailable-call wall times were:

```text
average: 2038.19 ms
p50:     2039.00 ms
p95:     2055 ms
```

These values measure endpoint failure latency, not Qwen inference latency.
Production latency is captured before shadow invocation and logged separately.

## 16. Regression results and known limitations

```text
Full pytest:                    535 passed, 1 warning
Period PATCH:                   34 passed
GEO PATCH:                     103 passed
Business Entity PATCH:          79 passed
Transition package:            103 passed
Persistence/reducer:            20 passed
Golden pytest catalog:          14 passed
P0 strict dry-run:              84/84
PR3 transition dry-run:         20/20
PR4 transition dry-run:         50/50
Semantic repair focused:        34 passed
```

The single warning is the pre-existing Starlette/httpx deprecation warning.
Dry-runs prove catalog coverage only. The live Qwen quality evaluation and actual
served model identity remain blocked by the unavailable inference endpoint.

## 17. Recommendation for PR6

**A. Do not proceed to active repair.**

The architectural boundary and production isolation are code-tested, but there
is no valid live model output from this run. Restore the configured endpoint,
confirm the actual served model, rerun the committed corpus, and review unsafe
accepted proposals before considering either a narrower active subset or an
active feature flag.

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
16. Valid typed-output rate? **0.0000 (endpoint unavailable).**
17. Semantic proposal precision? **N/A (0 valid proposals).**
18. Unsafe accepted transition rate? **N/A (0 valid proposals); count 0.**
19. Correct clarification rate? **0.0000 (0/4; no model response).**
20. Repair coverage? **0.0000.**
21. Qwen latency? **Not measured.** Unavailable-call latency was
    **2038.19 ms average / 2039.00 ms p50 / 2055 ms p95**.
22. Production differential semantic mismatch? **NO.**
23. PR6 recommendation? **A — do not proceed to active repair.**
