# PR5a — Shadow Operational Hardening + Evaluation Evidence

## Goal

PR5a removes shadow inference from the authoritative turn-reservation window,
makes shadow eligibility fail closed, replaces unmeasured tool-call zeros with
N/A, and commits the successful PR5 evaluation evidence. It does not change the
proposal contract, prompt, validator, deterministic transitions, or active
production semantics.

## Baseline

```text
branch: feat/shadow-operational-hardening-pr5a
PR5 commit: 840471d7d37bcaae92cc9df72f3232213ab7ba9d
baseline full pytest: 535 passed, 1 warning
tests/semantic_repair: 34 passed
Golden validate-only: 8/8
P0 strict dry-run: 84/84
PR3 transition dry-run: 20/20
PR4 transition dry-run: 50/50
```

The working tree already contained the successful live artifacts and independent
Claude review evidence. They were preserved and incorporated rather than
discarded.

## Claude findings addressed

- M1: the store-level reservation remained held during synchronous shadow
  inference;
- M2: successful live evidence existed only outside committed history;
- LOW: eligibility was a deny-list and unknown/missing modes failed open;
- LOW: tool metrics were hardcoded numeric zeros although no tools were exposed;
- documentation still described the earlier unavailable endpoint run and used
  unexplained aggregate test labels.

## Reservation lifecycle before

```text
reserve
  -> session RLock
  -> processor
  -> commit
  -> response built
  -> save_request_result
  -> session RLock released
  -> shadow inference
  -> return response
  -> finally: release reservation
```

The session lock was not held during shadow, but the store reservation was.

## Reservation lifecycle after

```text
reserve
  -> session RLock
  -> processor
  -> commit
  -> response built
  -> save_request_result
  -> session RLock released
  -> release reservation
  -> shadow inference
  -> return response
```

The release helper is called once after a successful cache write. `finally`
releases only authoritative paths that exit before that point. The flag is
cleared before the release attempt, preventing a second attempt. If release
fails, the failure remains non-blocking as before, but shadow is skipped because
the boundary cannot prove that the reservation is gone.

## `release < shadow_start` proof

`test_same_session_turn_succeeds_while_previous_shadow_is_blocked` uses an
instrumented store and blocking shadow and records:

```text
reserve
commit
save_request_result
release
shadow_start
shadow_end
```

The test asserts the exact indexes, especially
`index(release) < index(shadow_start)`. Separate failure-path tests verify one
release for an acquired reservation on RevisionConflict, processor exception,
and commit exception. Shadow valid, timeout, unavailable, malformed, and
unexpected-exception paths also retain one release per completed turn.

## Same-session concurrency and idempotency

While Turn A shadow is blocked, Turn B uses revision `N+1`, commits, and returns
revision `N+2`; it does not receive `TurnInProgress`. The final state is revision
`N+2`, proving no lost update.

A retry of Turn A with the same `request_id` while its shadow is blocked returns
the cached authoritative response. It performs no second commit, reservation,
or shadow invocation. The observed current semantics are therefore one shadow
run for the original request and zero for the cached retry.

## Isolation and shadow failures

The hostile shadow still receives a deep copy of pre-turn state. It mutates the
metric, sets revision to 9999, and clears the conversation window. Committed and
reloaded state, production mutation, revision, and conversation history remain
unchanged.

Timeout, unavailable, malformed, and ordinary `Exception` outcomes happen only
after reservation release. The authoritative response and cache stay correct,
and a following same-session turn succeeds.

## HTTP latency semantics

Shadow remains synchronous before `execute_turn` returns. Therefore:

```text
Does shadow still delay the current HTTP response? YES
```

PR5a removes reservation holding, not user-visible shadow latency.
`production_latency_ms` remains the authoritative duration measured through
response-cache persistence and before reservation release. `shadow_latency_ms`
is logged separately. No broad telemetry rename/refactor was introduced.

## Eligibility change

Before PR5a, every non-empty active-state turn was eligible unless its mode was
one of three deterministic PATCH names. After PR5a, the explicit allow-list is:

```text
conversation_graph
```

Unknown, missing, empty, and `deterministic_future_patch` modes fail closed.
Period, GEO, and Business PATCH modes remain skipped. Fake-backend corpus smoke:

```text
eligible model invocations before: 26
eligible model invocations after:  26
deterministic controls skipped:      3/3
```

## Evaluation evidence committed

- `artifacts/semantic_shadow_results_qwen38_27b_pr5.json` is the immutable
  successful Qwen3.8 evidence;
- `artifacts/semantic_shadow_results.json` remains the compatible canonical path
  with the same successful run;
- `artifacts/semantic_shadow_results_qwen36_35b_a3b.json` preserves the second
  endpoint run;
- independent review and forensic evidence are preserved in
  `docs/semantic_repair_pr5_claude_review.md`,
  `docs/semantic_repair_unsafe_analysis.md`, and
  `docs/semantic_repair_unsafe_taxonomy.json`.

No new expensive live run was made because PR5a changes neither model input nor
semantic evaluation rules. Only the tool-metric representation was mechanically
updated in existing artifacts; raw proposal results and ground truth were not
changed.

## Qwen3.8 live metrics

```text
endpoint reachable:             true
configured/served model:        Qwen/Qwen3.8-27B
native MTP speculative tokens:  1
eligible/invoked:               26/26
valid proposals:                26
validator rejections:           0
valid typed output rate:        1.0000
semantic match/precision:       0.3462 / 0.3462
correct patches:                6
raw unsafe patches:             14
repair coverage:                0.2308
repeated-subset stability:      1.0000
average/p50/p95 latency ms:     7985.08 / 8971.50 / 10658
```

## Raw 14 vs forensic 9

The machine-readable evaluator result remains 14 unsafe accepted PATCHes. The
independent forensic layer found that five are contract/over-specification
artifacts and nine of 26 remain genuinely semantically unsafe. The original
corpus ground truth and raw score were not rewritten.

The observed clean island is `swap_direction`: 5/5 correct, zero unsafe, zero
false-positive emissions over 21 non-direction runs, and three distinct positive
phrasings. This is not sufficient for activation; PR5-EVAL2 must run a dedicated
adversarial shadow experiment.

## Model B identity caveat

The second artifact reports:

```text
configured_model = ai-balances-language
served_models = [ai-balances-planner]
```

Those aliases do not independently establish a physical checkpoint. It is
therefore reported as **Model B / served alias `ai-balances-planner`**. The
provenance-oriented filename is not treated as identity proof.

## Tool metric cleanup

PR5 exposes no tools. Evaluator output now records:

```json
{
  "tools_exposed": false,
  "tool_call_count": null,
  "repeated_tool_calls": null,
  "tool_errors": null
}
```

The redundant `eligible_hard_tail` expression was simplified to the measured
eligible-turn count without changing its value.

## Production differential

The shadow-OFF versus shadow-ON integration test compares the complete response
and reloaded state after removing only timestamps. It covers status, result,
clarification, diagnostics/execution payload, effective committed intent,
revision, mutation isolation, and request-cache behavior. The lifecycle tests
then compare PR5 pre-fix behavior with PR5a ordering while preserving those
authoritative values.

```text
semantic mismatches: 0
```

This is code-level differential evidence. No live DB-backed differential is
claimed.

## Regression

```text
pytest -q
  553 passed, 1 warning

pytest tests/semantic_repair -q
  52 passed

pytest tests/test_period_patch.py -q
  34 passed

pytest tests/test_geo_patch.py -q
  103 passed

pytest tests/transitions/test_business_entity.py tests/test_business_entity_patch.py -q
  79 passed (66 + 13)

pytest tests/transitions -q
  103 passed

pytest tests/test_reducer.py -q
  8 passed

pytest tests/test_execution_commit_equivalence.py -q
  9 passed

pytest tests/test_postgres_store.py -q
  3 passed

pytest tests/test_golden_queries.py -q
  14 passed

python scripts/run_golden.py --validate-only
  8/8 validated

P0 strict dry-run
  84/84

PR3 transition strict dry-run
  20/20

PR4 transition strict dry-run
  50/50
```

The warning is the pre-existing Starlette/httpx deprecation warning. Dry-runs
prove catalog coverage, not live database behavior.

## Deferred LOW findings and PR5-EVAL2

Explicitly deferred:

- prompt files are not declared package data for a non-editable wheel;
- `except Exception` does not catch `KeyboardInterrupt` or `SystemExit`.

PR5a reduces the latter consequence because reservation and authoritative cache
are already complete before shadow begins. The synchronous HTTP call can still
be interrupted, and a same-request retry returns the cached response.

Also deferred to PR5-EVAL2: G1, G2, G3, G4, prompt definitions/examples,
selector rules, contract changes, validator semantic changes, and all active
repair behavior.

## Merge recommendation

**YES — PR5 is ready for merge after PR5a.** Operational reservation authority,
fail-closed eligibility, evaluator evidence representation, documentation, and
regression requirements are satisfied. **Active semantic repair remains NOT
READY** because nine of 26 runs remain genuinely unsafe after forensic review.

## Mandatory final answers

1. **Q1 — Does model inference run while authoritative turn reservation is held? NO.**
2. **Q2 — Can a slow shadow cause the next same-session turn to receive TurnInProgress solely because prior shadow is running? NO.**
3. **Q3 — Is session RLock held during shadow? NO.**
4. **Q4 — Does synchronous shadow still delay the current HTTP response? YES.**
5. **Q5 — Can shadow affect execution? NO.**
6. **Q6 — Can shadow affect persisted state/revision? NO.**
7. **Q7 — Can shadow failure corrupt a completed authoritative turn? NO.**
8. **Q8 — Is turn reservation released exactly once on all paths? YES.** An acquired reservation receives one release attempt; a conflict raised before acquisition receives none.
9. **Q9 — Are unknown/missing modes shadowed? NO.**
10. **Q10 — Are Period/GEO/Business deterministic PATCH modes shadowed? NO.**
11. **Q11 — Do intended hard-tail PR5 cases remain eligible? YES.** Before 26, after 26; three deterministic controls remain skipped.
12. **Q12 — Are tool metrics still emitted as fake numeric zeros? NO.**
13. **Q13 — Is successful Qwen3.8 live evidence committed? YES.**
14. **Q14 — Are raw evaluator 14 unsafe and forensic 9 genuinely semantic unsafe kept separate? YES.**
15. **Q15 — Is Model B identity caveated unless independently proven? YES.**
16. **Q16 — Did PR5a change the SemanticTransitionProposal contract? NO.**
17. **Q17 — Did PR5a change prompt semantics? NO.**
18. **Q18 — Were G1/G2/G3/G4 implemented? NO.**
19. **Q19 — Was active semantic repair enabled? NO.**
20. **Q20 — Full pytest? 553 passed, 1 warning.**
21. **Q21 — Production differential semantic mismatch? NO.**
22. **Q22 — Is PR5 ready for merge after PR5a? YES.** Active repair remains out of scope and not ready.
