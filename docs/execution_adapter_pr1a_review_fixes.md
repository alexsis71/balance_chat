# ExecutionAdapter PR1a review fixes

PR1a applies only the two required corrective fixes from the independent
adversarial review. It does not start PR2 and does not add a PATCH producer.

## Fix A — ContextReductionError mapping

`PipelineV2TurnProcessor._materialize_mutation()` now catches
`ContextReductionError` from `ExecutionAdapter.effective_intent()` and raises
the existing application-level `TurnProcessingError` with:

```text
code = context_reduction_failed
```

The existing API mapping returns HTTP 422 with that code. No API endpoint,
response model, exception hierarchy, reducer behavior, or store behavior was
changed. The existing post-normalization divergence guard uses the same
controlled error contract rather than allowing `ContextReductionError` to
escape processing.

Test evidence:

- `test_context_reduction_failure_returns_controlled_http_422_without_commit`
  injects a materialization-time `ContextReductionError`;
- the response is HTTP 422 with `context_reduction_failed`;
- the tracking store records zero commit calls;
- the persisted session revision remains zero.

## Fix B — semantic no-op patch guard

For a mutation without `replace_intent`, execution materialization now requires
at least one present field mutation whose action is not `KEEP`.

Rejected shapes:

1. patch with no field mutations;
2. patch whose present field mutations are all `KEEP`.

Meaningful actions remain the existing contract actions:

```text
SET
ADD
REMOVE
CLEAR
REFERENCE
```

Their semantics are not reimplemented in the adapter; accepted mutations are
delegated to `reduce_intent()`. This is structural no-op detection only and is
not a generic comparison between the reduced intent and the active intent.

Test evidence covers empty, one `KEEP`, multiple `KEEP`, allowed `SET`, and
allowed `REFERENCE`. Invalid reducer references continue to propagate from the
adapter and are mapped only when crossing processor materialization.

## Deferred to PR2

Before enabling the first production PATCH producer, normalization versus
retained-patch synchronization semantics must be resolved. Current
`_synchronize_effective_intent()` retains the original patch while adding a
normalized replacement and fails closed if re-reduction would diverge. PR1a
does not clear, rewrite, or compile that patch.

`IntentPatch` cannot currently mutate `formula` or `ranking`. Operation
transitions requiring those fields remain a PR2+ contract boundary. PR1a does
not extend the contract.

The pre-existing `_standalone()` path is also intentionally outside the
dispatched mutation seam and was not refactored.

## Regression results

PR1 head before corrective fixes:

```text
238 passed, 1 warning
```

PR1a after corrective fixes:

```text
conda run -n ai_env python -m pytest -q
243 passed, 1 warning
```

The five-test increase consists of four ExecutionAdapter guard cases and one
controlled API/application error test. There are no new failures.

Relevant PR1 regression matrix:

```text
conda run -n ai_env python -m pytest tests/test_execution_adapter.py tests/test_execution_commit_equivalence.py tests/test_reducer.py tests/test_interpretation.py tests/test_pipeline_wiring.py tests/test_api.py tests/test_golden_queries.py tests/test_acceptance_runner.py -q
151 passed, 1 warning
```

## Scope confirmation

```text
No PATCH producer added.
No routing change.
No planner change.
No executor change.
No reducer semantics change.
```

Interpreter, prompts, binder, normalization semantics, stores, and API models
are unchanged. Existing reachable replace-only behavior remains unchanged.
