# PR1a fixes — short re-review

Scope: verify the two required fixes from `execution_adapter_pr1_claude_review.md`
against commits `179a8a5..HEAD` (`b69e7f1`, `c5df242`, `ca0911d`, `1d35ded`).

## Fix A — ContextReductionError mapping (was M1)

`processor.py:108-117`: `_materialize_mutation` now catches `ContextReductionError`
and re-raises `TurnProcessingError(code="context_reduction_failed")`.
`_synchronize_effective_intent` (`processor.py:175-183`) does the same instead of
raising `ContextReductionError` directly. `api.py:128-130` already maps
`TurnProcessingError` → HTTP 422 with `{code}` — confirmed unchanged.

New test `test_context_reduction_failure_returns_controlled_http_422_without_commit`
(`tests/test_api.py:400`) injects a materialization failure end-to-end through the
API and asserts: HTTP 422, code `context_reduction_failed`, zero store commits,
revision stays 0. This is the right test — it closes the gap directly instead of
only unit-testing the exception type.

**Status: FIXED.** Verified.

## Fix B — no-op KEEP patch fail-closed (was H1)

`execution_adapter.py:26-31`: the guard now checks
`field_mutation.action != MutationAction.KEEP`, not just `is not None`. A patch
whose every present field is `KEEP` is correctly rejected as empty; `SET` and
`REFERENCE` are correctly still accepted.

New tests in `tests/test_execution_adapter.py`: single-KEEP rejected, multi-KEEP
rejected, `SET` executable, `REFERENCE` reaches the reducer and resolves from
`last_successful_scope`. Covers the exact shape from the original finding
(structured-output model emitting all-KEEP) plus the two positive cases that
must keep working.

**Status: FIXED.** Verified.

## Regression check

Ran the full suite myself (`conda run -n ai_env python -m pytest -q`):
**243 passed, 0 failed** — matches the fix commit's own claim exactly.
`test_normalization_fails_closed_if_patch_would_change_committed_intent`
(`tests/test_execution_commit_equivalence.py:257-264`) was correctly updated to
expect `TurnProcessingError` instead of `ContextReductionError`.

## Not fixed, correctly deferred

H2 (`_synchronize_effective_intent` retains the original patch through
normalization) was **not** touched — the fix docs explicitly scope this out to
PR2 (`docs/execution_adapter_pr1a_review_fixes.md`, "Deferred to PR2"), which
matches the original review's own classification: H2 was a PR2 blocker, not a
PR1 merge blocker. No new PATCH producer, no routing/planner/executor change —
confirmed by diff, scope is exactly the two fixes.

## Verdict

**APPROVE.** Both required fixes are correctly implemented, each has a targeted
regression test that would fail on the old code, and the full suite is green.
No new scope creep introduced by the fix commits.
