# PR2a independent adversarial review — PATCH vs Normalization semantics

Reviewed ref: `feat/patch-normalization-pr2a` @ `e31c35b` ("docs: document PATCH normalization semantics")
Baseline: `5bcc745` (PR1a, previously reviewed/approved)
Diff scope: `git diff 5bcc745..e31c35b`
Scope of this review: PR2a correctness only, plus non-regression of PR1/PR1a invariants. PR1/PR1a findings are not re-litigated.

---

## 1. Verdict

**APPROVE**

---

## 2. Executive summary

PR2a does what it claims and nothing else. The double-PATCH-application ambiguity is eliminated:
when normalization changes the materialized effective intent, the mutation is collapsed to
`replace_intent = normalized_intent, patch = IntentPatch()`, so the reducer can no longer apply the
original patch a second time at commit. When normalization is a no-op, the original mutation object
is returned by identity, preserving patch provenance.

The diff is minimal and fully contained: 3 files (`docs/patch_normalization_pr2a.md` new,
`src/balance_chat/processor.py`, `tests/test_execution_commit_equivalence.py`). The production change
is one import, one helper rewrite, and four call sites. Every file the author claimed untouched is
verifiably untouched.

I independently verified every quantitative claim in `docs/patch_normalization_pr2a.md` — baseline 243,
PR2a 248, focused 9, regression subset 124, and the exact `-1 / +6` test delta. All correct.

Three things I probed hardest and cleared:

1. **The `is not` → `!=` change is behavior-preserving.** I exercised all four production normalizers
   across their reachable branches and confirmed no normalizer ever returns an object that is
   `is not intent` but `== intent` (nor the reverse). Each no-change branch returns the *same object*;
   each change branch guarantees a structural difference. So the refactor changes neither logging nor
   control flow in production.
2. **All four call sites pass a genuinely pre-normalization `previous_effective_intent`** that really is
   the materialization of the mutation they pass alongside it, including the two chained sites inside
   `_execute_mutation` where the second site consumes the first site's collapsed mutation.
3. **The helper never re-materializes the original patched mutation.** In the unchanged branch it
   materializes nothing at all; in the changed branch it materializes only the already-collapsed
   mutation. There is no path where the patch is applied during validation.

The most important empirical result: the exact scenario the now-deleted test asserted must raise
(`patch periods=SET[july]` + normalization moving periods to august) now succeeds and commits
`periods=['2025-08-01']` — the normalized value, not the patch value. That is the fix, demonstrated.

One important framing point for the reader: **there is no production non-empty PATCH producer today**
(verified repo-wide). Every production mutation is replace-only with the default empty patch. This means
PR2a's collapse is currently a no-op in production (`patch=IntentPatch()` overwrites an already-empty
patch) and therefore *cannot* regress live behavior — which is consistent with the observed 243 → 248
with zero changed/failed pre-existing tests. The collapse logic is forward-looking scaffolding for PR2b,
and it is correct scaffolding.

No blockers, no high findings. Five low-severity notes, all forward-looking rather than defects.

---

## 3. Blockers

None.

---

## 4. High findings

None.

---

## 5. Medium/Low findings

```
Finding: The fail-closed re-materialization guard is now effectively unreachable AND completely untested.
Severity: LOW
Evidence: src/balance_chat/processor.py:186-193 (`_synchronize_effective_intent`, the
  `!= normalized_effective_intent` -> TurnProcessingError branch).
  Coverage removed by this PR: tests/test_execution_commit_equivalence.py dropped
  `test_normalization_fails_closed_if_patch_would_change_committed_intent`, and also dropped
  `import pytest` and `from balance_chat.service import TurnProcessingError`.
  `grep -rn "would differ from committed" tests/` returns nothing; no `pytest.raises` remains in the file.
Why it matters: A defensive invariant with zero test coverage silently rots. If a future refactor makes
  the branch reachable and wrong, nothing catches it.
Reproduction / reasoning: After collapse the mutation is `replace_intent=N, patch=IntentPatch()`, so
  `reduce_intent` reduces to `AnalysisIntent.model_validate(N.model_dump(mode="python"))`. I verified this
  round-trip is exact — including `intent_id` — for SHOW, AGGREGATE, GROUP and COMPARE_PERIODS intents.
  A normalized intent that is structurally invalid does not reach this branch either: it raises
  `ContextReductionError` inside `reduce_intent`, which `_materialize_mutation` (processor.py:114-118)
  converts to `TurnProcessingError(code="context_reduction_failed")` first. So the `!=` branch cannot fire.
Minimal correction: None required for correctness. Optionally retain a test that drives the branch via a
  stubbed `execution_adapter` returning a divergent intent, purely to keep the guard alive.
```

```
Finding: `_synchronize_effective_intent` trusts the caller's `previous_effective_intent` without
  verifying it is the materialization of the mutation it was handed.
Severity: LOW (MEDIUM as a PR2b pre-condition)
Evidence: src/balance_chat/processor.py:177-178 — `if normalized_effective_intent ==
  previous_effective_intent: return mutation` returns before any `_materialize_mutation` call.
Why it matters: The early-return is the *only* path that preserves a patch-only mutation. If a future
  call site passes a stale or re-derived `previous_effective_intent`, the helper returns a mutation whose
  committed intent differs from the executed intent, silently and with no error. This is precisely the
  class of bug PR2a exists to eliminate, and the guard against it is convention, not code.
Reproduction / reasoning: I audited all four current call sites (see section 7) and all four are correct,
  so this is latent, not live. It becomes load-bearing in PR2b when patch-only mutations actually reach
  these paths in production.
Minimal correction: None required now. If desired, a debug/assert-level check that
  `self._materialize_mutation(state, mutation) == previous_effective_intent` in the early-return branch
  would convert a future silent divergence into a loud failure.
```

```
Finding: Equality is structural over all fields including `intent_id`, whose default is `uuid4()`.
  A normalizer that *reconstructs* an `AnalysisIntent` instead of `model_copy`-ing it will register as
  "changed" even when semantically identical.
Severity: LOW
Evidence: src/balance_chat/contracts.py:143 (`intent_id: str = Field(default_factory=lambda: str(uuid4()))`).
  `_normalize_same_scope_period_comparison_intent` (src/balance_chat/processor.py:3311-3319) already uses
  the fresh-construction style and does emit a new `intent_id` (verified: `e3f4a214 -> cdc43cdd`).
Why it matters: A future normalizer copying that construction style would needlessly collapse a
  patch-only mutation to a replacement, destroying patch provenance for that turn.
Reproduction / reasoning: I verified two independently-constructed identical intents compare unequal,
  while `model_copy(deep=True)` compares equal. Crucially the direction of failure is safe: it can only
  over-collapse (still guaranteeing executed == committed), never under-collapse. `intent_id` also has no
  execution effect — it is provenance only (`planning.py:85` etc.) and is explicitly excluded from the
  execution payload (`execution.py:208`, `exclude={"intent_id"}`).
Minimal correction: None. Worth a one-line comment in the helper noting that `==` is intentionally
  stricter than semantic equality.
```

```
Finding: `model_copy(update=..., deep=True)` inserts update values by reference, so the collapsed
  mutation's `replace_intent` is the *same object* as the intent handed to the planner.
Severity: LOW (informational; pre-existing, not introduced by PR2a)
Evidence: src/balance_chat/processor.py:179-185. Verified empirically:
  `synchronized.replace_intent is normalized_effective_intent` -> True. Pydantic v2 deep-copies the model
  then applies `update` into `__dict__` without copying the update values.
Why it matters: The committed mutation aliases the executed intent. If any downstream code mutated the
  planner's intent in place, the committed mutation would change silently.
Reproduction / reasoning: Not exploitable today — I found no in-place attribute assignment on any intent
  anywhere in `src/`. The old (PR1a) code had the identical aliasing, so this is not a PR2a regression.
  Arguably it *strengthens* I4 by making executed and committed the same object.
Minimal correction: None.
```

```
Finding: No end-to-end (execute -> commit -> reload) test exercises a real production normalizer changing
  the same field a patch had set — the only configuration where a retained patch is non-idempotent.
Severity: LOW
Evidence: tests/test_execution_commit_equivalence.py:285 (scenario B) covers same-field change but only
  as a direct unit call to `_synchronize_effective_intent`. tests/test_execution_commit_equivalence.py:389
  (scenario F) is the only true end-to-end test, and its normalization changes `grain` while the patch set
  `periods` — an unrelated field.
Why it matters: In the unrelated-field case a retained patch is idempotent (the patch's SET value is
  already reflected in the effective intent), so `reduce_intent(...) == normalized` holds *even under the
  buggy retain-the-patch implementation*. In test F only the explicit
  `assert processed.mutation.patch == IntentPatch()` catches the bug — the commit/reload assertions do not.
Reproduction / reasoning: Under the old implementation, test F's `committed.active_dialog_scope.intent`
  would still equal `normalized`; only the patch-emptiness assertion fails. The genuinely divergent case
  (B) is verified at unit level only. This is currently unreachable in production (no patch producer), but
  it is exactly the PR2b scenario.
Minimal correction: When PR2b lands, add an end-to-end test with a period patch plus a normalizer that
  rewrites periods, asserting `reloaded.active_dialog_scope.intent == normalized`.
```

```
Finding: `test_changed_replace_only_normalization_commits_normalized_replacement` does not commit.
Severity: LOW (naming/coverage nit)
Evidence: tests/test_execution_commit_equivalence.py:366-386 — no `store.commit` / `store.get` call
  despite "commits" in the name; it asserts on `reduce_intent(state, synchronized)` instead.
Why it matters: Scenario E's "committed" half is asserted by proxy. `reduce_intent` is the same function
  `apply_context_transition` uses (reducer.py:152), so the proxy is sound — but the name overstates it.
Minimal correction: Rename, or add the two-line store round-trip.
```

---

## 6. Invariant table

| Invariant | PASS / FAIL / UNCERTAIN | Evidence |
|---|---|---|
| No-op normalization preserves original patch | PASS | `processor.py:177-178` early-returns the original object. Verified: helper returns `is mutation`, `replace_intent is None`, `patch` unchanged. Asserted at unit level by `tests/test_execution_commit_equivalence.py:258-282` (which passes a `model_copy(deep=True)`, i.e. `is not` but `==`, so an identity-based implementation fails it) and end-to-end by the pre-existing `tests/test_execution_commit_equivalence.py:152-205` (`processed.mutation == mutation`, `replace_intent is None`, `adapter.calls == 1`). |
| Changed normalization collapses to replacement | PASS | `processor.py:179-185`. Asserted by tests at lines 285, 321, 366, 389. Verified empirically for the same-field, unrelated-field, and replace-only cases. |
| Collapsed mutation has empty patch | PASS | `"patch": IntentPatch()` at `processor.py:182`. Asserted at test lines 317, 344, 385, 430. `IntentPatch()` has all six fields `None` (`contracts.py:328-334`), and `reduce_intent` skips `None` field mutations (`reducer.py:97-99`). |
| Patch cannot apply twice | PASS | Directly demonstrated: the scenario the deleted test required to raise now commits `periods=['2025-08-01']` (normalized) rather than `2025-07-01` (patch value). `reduce_intent(state, synchronized) == normalized` asserted at test lines 318, 345, 386. |
| Executed == committed == reloaded | PASS | `tests/test_execution_commit_equivalence.py:389-432` drives the real `_normalize_series_reduction_intent` through real `_dispatch_mutation` -> real planner/executor -> real `InMemoryContextStore.commit` -> `store.get`, asserting `planner.intent == normalized`, `committed.active_dialog_scope.intent == normalized`, `reloaded... == normalized`. `adapter.calls == 2` pins that the collapse branch actually executed. |
| Replace-only behavior unchanged | PASS | For a replace-only mutation the collapse writes `IntentPatch()` over an already-empty patch, and the no-op path returns the original object — both identical to PR1a. Confirmed by 243 -> 248 with zero pre-existing test failures and zero modifications to the three pre-existing tests in the touched file (`git diff` shows only helpers added, one test replaced, six added). |
| No production PATCH producer | PASS | `grep -rn "IntentPatch(\|FieldMutation(" src/` yields only `contracts.py:308,328` (declarations) and `processor.py:182` (the empty collapse). `grep -rn "patch=" src/` yields nothing — no production mutation is ever constructed with a patch. |
| Planner/executor unchanged | PASS | `git diff 5bcc745..e31c35b -- src/balance_chat/planning.py` and `execution.py` are both empty, as are `reducer.py`, `contracts.py`, `store.py`, `api.py`, `interpretation.py`, `binding.py`, `execution_adapter.py`, `grouping.py`, `conversation.py`, `domain.py`, `service.py`, `gating.py`. Their *inputs* are also unchanged in the no-op case, and in the changed case the planner receives the normalized intent exactly as before PR2a. |

---

## 7. Normalization call-site audit

### The four synchronized sites

| # | File:line | Function | Normalizer | Before -> after | Sync behavior |
|---|---|---|---|---|---|
| 1 | `processor.py:1020-1040` | `_execute_grouping_mutation` | `_normalize_temporal_grouping_intent` (`processor.py:3175`) | `grain`, `grouping[0].aggregate_type` | `previous_effective_intent=intent` (the exact normalizer input); `intent = normalized_intent` after the sync; `log_event` gated on the precomputed bool and reads the already-reassigned `intent` — same values the old code logged. Correct. |
| 2 | `processor.py:1129-1140` | `_execute_grouping_mutation` | inline canonical-unit rewrite via `_canonical_grouping_unit` | `operands[0].unit` | `previous_intent = intent` is captured at line 1130 *before* the two `model_copy` calls, then passed as `previous_effective_intent`. Correct. Guarded by `intent.operands[0].unit != canonical_unit`, so the change is always real. |
| 3 | `processor.py:2268-2286` | `_execute_mutation` | `_normalize_same_scope_period_comparison_intent` (`processor.py:3287`) | `operation` COMPARE -> COMPARE_PERIODS, operands 2 -> 1, periods lifted to global | `previous_effective_intent=intent` = `effective_intent` param. Correct. |
| 4 | `processor.py:2287-2310` | `_execute_mutation` | `_normalize_series_reduction_intent` (`processor.py:3227`) | `grain` | `previous_effective_intent=intent`, where `intent` is the *post-site-3* value (line 2276). Chaining is correct: if site 3 collapsed, the mutation now materializes to exactly `normalized_comparison`, so `previous` remains a truthful materialization. Correct. |

**Is `previous_effective_intent` always a real pre-normalization materialization of the mutation passed alongside it?** Yes, at all four sites. The chain of custody is:
`_dispatch_mutation` (`processor.py:136-138`) either receives `effective_intent` or computes `self._materialize_mutation(state, mutation)`; callers that supply it (`processor.py:572-590`, `846-864`, `2067-2084`) compute it from the same mutation immediately beforehand. `_execute_grouping_mutation` forwards the (possibly synchronized) `mutation` together with the matching normalized `intent` when it delegates to `_execute_mutation` at `processor.py:1045-1058`. No site re-derives or reuses a stale value.

### Identity vs. equality — the `is not` -> `!=` change

I exercised every normalizer across its reachable branches and compared `(r is not intent)` against `(r != intent)`:

| Normalizer | Cases exercised | Divergence found |
|---|---|---|
| `_normalize_temporal_grouping_intent` | `''`, `'по месяцам'`, `'среднее по дням'`, `'итого'`, `'распределение'` | none |
| `_normalize_series_reduction_intent` | `''`, `'среднемесячное'`, `'среднесуточное'`, `'распределение'`, `'среднегодовой'` | none |
| `_normalize_same_scope_period_comparison_intent` | changing and non-changing | none |
| canonical-unit rewrite | guarded inline by `unit != canonical_unit` | none (structural change guaranteed) |

The reason is structural, not accidental: every no-change branch `return intent` returns the *same object*
(`processor.py:3183, 3213, 3233, 3236, 3269, 3299, 3309`), and every change branch is guarded so that at
least one field must differ. `_normalize_same_scope_period_comparison_intent` builds a fresh
`AnalysisIntent` (new `intent_id`) but also always flips `operation` to `COMPARE_PERIODS`, so `!=` holds
regardless of the id. **Conclusion: the refactor affects neither logging nor correctness today.**

### Post-materialization transformations that are *not* synchronized — and why each is safe

- **`_repair_incomplete_evidence` (`processor.py:876-1001`).** Structurally different and already safe. It
  either returns the input `(mutation, effective_intent)` pair untouched (line 899), or it compiles a
  *brand-new* `repaired_mutation` and derives `repaired_intent = self._materialize_mutation(state,
  repaired_mutation)` at line 973 — the pair is consistent by construction. It never mutates an intent
  while retaining a stale mutation, so it needs no collapse treatment.
- **`_enforce_role_separated_entities` (`processor.py:1993-2027`).** Rewrites `mutation.replace_intent`
  *before* materialization (materialization happens afterwards at line 2067), on a mutation constructed
  two lines earlier with `replace_intent=` and the default empty patch. Pre-materialization, therefore safe.
- **`_full_balance_source_intent` (`processor.py:3559-3574`), used at `processor.py:2541-2545`.** Derives a
  broader balance-snapshot intent used *only* to issue the DB query. It is never committed — the committed
  mutation is the one passed in (`processor.py:2695-2696`). This is an execution-scoped derivation, not a
  canonicalization of the user's intent, so it correctly does not collapse.
- **Intent construction sites** at `processor.py:1265, 1296, 1359, 1766, 1812, 1923, 2112-2127, 2748, 4444,
  4477, 4608`. All build an intent and *immediately* wrap it in a fresh `ContextMutation(replace_intent=...)`
  with the default empty patch, before any materialization. None are post-materialization rewrites.
- **`_normalize_full_balance_envelope` (`processor.py:3661`)** operates on the result envelope, not on an intent.

**Central question — is there any reachable path where the intent changes after materialization but the
mutation does not collapse correctly?** No. All four such paths route through
`_synchronize_effective_intent` with correct arguments; every other post-materialization derivation is
execution-scoped and never committed.

---

## 8. Test adequacy

The touched test file now holds 9 tests (3 pre-existing, unmodified + 6 new). Per-scenario assessment,
including "could a wrong implementation still pass?":

**A — patch-only + no-op normalization -> original patch preserved.**
`test_unchanged_normalization_preserves_original_patch_only_mutation` (line 258).
Asserts `synchronized is mutation`, `replace_intent is None`, `patch == mutation.patch`.
*Discriminating power: strong.* It deliberately passes `effective.model_copy(deep=True)` as the normalized
intent — an object that is `is not` but `==` the previous one. An implementation comparing by identity, or
one that always collapses, fails on `synchronized is mutation`. Additionally covered end-to-end by the
pre-existing `test_patch_only_execution_and_commit_use_the_same_effective_intent` (line 152), which asserts
`processed.mutation == mutation` and `adapter.calls == 1` through the real dispatch path.

**B — patch-only + normalization changes the patched field -> full normalized replacement + empty patch.**
`test_changed_patched_field_collapses_to_normalized_replacement` (line 285).
Patch sets `periods=[july]`; normalization moves periods to `[august]`.
*Discriminating power: strongest test in the suite.* This is the only test where a retained patch is
non-idempotent. `assert reduce_intent(state, synchronized) == normalized` (line 318) fails under the
old retain-the-patch behavior, which would yield `periods=[july]`. It also pins metadata preservation
(`turn_id`, `user_message`, `normalized_message`). Limitation: unit-level only, no store round-trip.

**C — patch-only + normalization changes an unrelated field -> same collapse.**
`test_any_normalization_change_collapses_even_when_patch_touched_another_field` (line 321).
Patch sets `periods`; normalization changes `grain`.
*Discriminating power: adequate, but narrower than it looks.* Because the patch's SET value is already
reflected in the effective intent, a retained patch here is **idempotent** — `reduce_intent(...) ==
normalized` would hold even under the buggy implementation. The only assertion that catches the bug is
`assert synchronized.patch == IntentPatch()` (line 344). It is present, so the test does discriminate.

**D — replace-only + no-op -> no behavioral change.**
`test_unchanged_replace_only_normalization_preserves_original_mutation` (line 348).
Single assertion `synchronized is mutation`. Minimal but exactly sufficient: any always-collapse
implementation returns a copy and fails. Also covered end-to-end by the pre-existing
`test_replace_only_dispatch_preserves_intent_and_original_mutation` (line 124).

**E — replace-only + normalization change -> committed normalized replacement.**
`test_changed_replace_only_normalization_commits_normalized_replacement` (line 366).
Asserts `replace_intent == normalized`, `patch == IntentPatch()`, `reduce_intent(...) == normalized`.
Sound, but does not actually commit (see LOW finding). Since `apply_context_transition` calls the same
`reduce_intent` (`reducer.py:152`), the proxy is valid.

**F — execute -> commit -> reload -> exact same normalized intent.**
`test_normalized_patch_execution_matches_committed_and_reloaded_intent` (line 389).
The strongest end-to-end test. Notably it does **not** hand-inject a normalized intent: it seeds an
AGGREGATE/avg intent and a message containing "среднемесячное", so the real
`_normalize_series_reduction_intent` fires and rewrites `grain` `total -> month` inside the real
`_execute_mutation`. It then asserts `adapter.calls == 2` (proving the collapse branch ran and
re-materialized), `planner.intent == normalized` (executed), `processed.mutation.replace_intent ==
normalized` and `patch == IntentPatch()` (collapsed), and `committed`/`reloaded` active scope intents both
`== normalized` (committed and reloaded, through the real store).
*Discriminating power: good.* Under the old implementation the patch-emptiness assertion fails. Note the
commit/reload assertions alone would not fail, for the idempotency reason described under C.

**Could a wrong implementation pass all six?** I worked through the plausible wrong variants:
collapse-but-forget-to-clear-patch fails B, C, E, F; clear-patch-but-forget-`replace_intent` fails B, C, E, F
(the reducer would fall back to the active scope intent); always-collapse fails A and D; identity-based
comparison fails A and D; drop turn metadata fails B. No single wrong variant survives. Coverage is
genuine, not ceremonial.

**Coverage removed.** One test was deleted:
`test_normalization_fails_closed_if_patch_would_change_committed_intent`. Its removal is **legitimate**:
it asserted `TurnProcessingError("executed effective intent would differ from committed intent")` for
exactly the patch-only + changed-periods scenario that PR2a deliberately converts into a successful
collapse. Keeping it would mean asserting the bug. Scenario B is its direct successor and covers the same
input with the new correct expectation. The only residual loss is coverage of the `raise` branch itself
(see LOW finding 1).

---

## 9. Regression results

All runs on the same interpreter (`C:\Users\alexs\miniforge3\envs\ai_env\python.exe`, pytest 9.1.1,
pydantic 2.12.4).

| Run | Result |
|---|---|
| Baseline `5bcc745`, full suite | **243 passed, 0 failed, 1 warning** |
| PR2a `e31c35b`, full suite | **248 passed, 0 failed, 1 warning** |
| PR2a focused (`tests/test_execution_commit_equivalence.py`) | **9 passed** |
| PR2a regression subset (execution_adapter + reducer + pipeline_wiring + api + execution_commit_equivalence) | **124 passed, 0 failed, 1 warning** |

Every number in `docs/patch_normalization_pr2a.md` is confirmed independently.

The baseline was measured by extracting `git archive 5bcc745` into a throwaway sibling directory and
running pytest there — no ref was ever checked out over another worktree, and the temp directory was
deleted afterwards.

**Exact test delta (via `--collect-only` set difference, 243 -> 248 ids):**

Removed (1):
- `tests/test_execution_commit_equivalence.py::test_normalization_fails_closed_if_patch_would_change_committed_intent`

Added (6):
- `test_unchanged_normalization_preserves_original_patch_only_mutation`
- `test_changed_patched_field_collapses_to_normalized_replacement`
- `test_any_normalization_change_collapses_even_when_patch_touched_another_field`
- `test_unchanged_replace_only_normalization_preserves_original_mutation`
- `test_changed_replace_only_normalization_commits_normalized_replacement`
- `test_normalized_patch_execution_matches_committed_and_reloaded_intent`

Net `+5`, matching the doc's arithmetic (`-1 + 6`). **New failures: none. Removed coverage: only the
superseded fail-closed assertion.** The six additions are genuine new coverage of distinct scenarios, not
a rename of the removed test. The three pre-existing tests in the file are byte-identical to baseline — no
assertion was weakened to make PR2a pass.

**Note on environment:** running the suite from inside `.claude/worktrees/<agent>/` produces 26 spurious
failures because several tests resolve a sibling `pipeline/` directory via
`Path(__file__).resolve().parents[2]`, which from worktree depth points at
`C:\#work\sl\balance_chat\.claude\worktrees\pipeline` instead of `C:\#work\sl\pipeline`. All 26 fail with
`PipelineRuntimeError: invalid pipeline root` (17) or the corresponding `FileNotFoundError` on
`manifest.json` (9), in test files PR2a does not touch. This is a worktree-depth artifact, not a PR2a
regression; the authoritative runs above were done at correct depth.

---

## 10. Scope creep

**None found.** The full diff `5bcc745..e31c35b` is three files:

| File | Change | Assessment |
|---|---|---|
| `docs/patch_normalization_pr2a.md` | new, +97 | In scope — the PR's own design note. Claims verified accurate. |
| `src/balance_chat/processor.py` | +50/-12 | In scope. Exactly: one `IntentPatch` import (line 30), the `_synchronize_effective_intent` rewrite (169-194), and four call-site updates (1020-1040, 1129-1140, 2268-2286, 2287-2310). Nothing else. |
| `tests/test_execution_commit_equivalence.py` | +187/-23 | In scope. Two helpers added, two imports adjusted, one superseded test replaced, six added. |

Explicitly checked and **absent**: no production period PATCH; no NEW/PATCH/REINTERPRET classifier; no
contextual routing change; no interpreter, prompt, binder, reducer, planner, executor, API or store change;
no unrelated processor cleanup, dead-code removal, or drive-by refactor. Verified by empty diffs for
`planning.py`, `execution.py`, `reducer.py`, `contracts.py`, `store.py`, `api.py`, `interpretation.py`,
`binding.py`, `execution_adapter.py`, `grouping.py`, `conversation.py`, `domain.py`, `service.py`,
`gating.py`.

The author resisted the obvious temptation to land a period PATCH producer alongside the semantics fix.
That separation is correct and makes this PR reviewable.

---

## 11. PR2b readiness

**YES.**

A first production period-only PATCH producer — `IntentPatch(periods=FieldMutation(action=SET,
value=[april]))` for "А за апрель?" — can now be added without planner or executor changes. Traced end to end:

1. The compiler emits a patch-only `ContextMutation`. `ReducerExecutionAdapter.effective_intent`
   (`execution_adapter.py:28-34`) accepts it: `replace_intent is None` but the patch contains a non-KEEP
   action, so the "empty executable mutation" guard passes and `reduce_intent` overlays `periods` onto
   `state.active_dialog_scope.intent`.
2. `_dispatch_mutation` materializes it once and routes on the *effective* intent — already covered by the
   pre-existing `test_patch_only_grouping_routes_from_the_effective_intent` (line 208).
3. **No normalization change** (the common "А за апрель?" case): `_synchronize_effective_intent` early-returns
   the original mutation, the patch survives to commit, and `reduce_intent` at commit produces exactly the
   intent that executed. Covered end-to-end today by `test_patch_only_execution_and_commit_use_the_same_effective_intent`.
4. **Normalization change** (e.g. "среднемесячное за апрель"): the mutation collapses to
   `replace_intent=normalized, patch=IntentPatch()`, so the period patch cannot reapply. Executed ==
   committed == reloaded, demonstrated by scenario F.
5. The planner receives an `AnalysisIntent` either way and never sees a `ContextMutation` — `planning.py`
   and `execution.py` are structurally indifferent to how the intent was produced.

Two non-blocking recommendations to carry into PR2b, both restated from section 5:

- Add the end-to-end test that scenario B currently covers only at unit level: a period patch plus a
  normalizer that rewrites `periods`, asserted through commit and reload. That is the one configuration
  where a retained patch is non-idempotent, and it is the configuration PR2b creates.
- Keep `previous_effective_intent` honest at any new call site. The early-return does not self-verify, so
  the pre-normalization materialization must be passed, never a re-derived or cached value.

Also worth knowing, though not a blocker: `reduce_intent` propagates the *active scope's* `intent_id` into
a patch-derived effective intent, so a patched turn commits under the previous turn's `intent_id`. This is
pre-existing, and `intent_id` is provenance-only — excluded from execution payloads at `execution.py:208`.

---

## Final Q&A

### Q1 — At no-op normalization, is the original patch preserved? (YES / NO / NOT PROVEN)
**YES.** `processor.py:177-178` returns the original mutation object by identity before any copy or
materialization. Verified directly: returned object `is` the original, `replace_intent is None`, `patch`
unchanged. Asserted by `tests/test_execution_commit_equivalence.py:258-282` (unit, using an equal-but-distinct
normalized intent so an identity-based implementation fails) and by the pre-existing end-to-end test at
line 152.

### Q2 — At actual normalization change, does the mutation collapse to normalized replace_intent + empty patch? (YES / NO / NOT PROVEN)
**YES.** `processor.py:179-185` sets `replace_intent = normalized_effective_intent` and `patch =
IntentPatch()` in a single `model_copy`, then validates by re-materializing the collapsed mutation.
Asserted at test lines 316-318, 343-345, 384-386, 429-430. Turn metadata (`turn_id`, `user_message`,
`normalized_message`, `assistant_summary`) verified to survive the copy.

### Q3 — Can the original patch apply a second time after normalization? (expected: NO)
**NO.** The patch is emptied in the same `model_copy` that installs the normalized `replace_intent`, and
`reduce_intent` skips `None` field mutations (`reducer.py:97-99`). Demonstrated on the precise scenario that
previously failed closed: patch `periods=SET[july]` with normalization to august now commits
`periods=['2025-08-01']`, not `2025-07-01`. The validation step never materializes the original patched
mutation — in the unchanged branch it materializes nothing, in the changed branch only the collapsed one.

### Q4 — Is executed intent == committed intent == reloaded intent confirmed? (YES / NO / NOT PROVEN)
**YES.** `tests/test_execution_commit_equivalence.py:389-432` drives the real
`_normalize_series_reduction_intent` through real dispatch, planner, executor, `InMemoryContextStore.commit`
and `store.get`, asserting `planner.intent == normalized` (executed), `committed.active_dialog_scope.intent
== normalized`, and `reloaded.active_dialog_scope.intent == normalized`. `adapter.calls == 2` confirms the
collapse branch actually executed rather than the test passing vacuously.

### Q5 — Is there a production non-empty PATCH producer? (expected: NO)
**NO.** `grep -rn "IntentPatch(\|FieldMutation(" src/` returns only the two class declarations in
`contracts.py:308,328` and the empty `IntentPatch()` at `processor.py:182`. `grep -rn "patch=" src/`
returns nothing — no production code path constructs a mutation with a patch. All non-empty patch
construction is confined to `tests/`.

### Q6 — Is there an observable regression vs PR1a? (YES / NO / NOT PROVEN)
**NO.** Baseline 243 passed / 0 failed -> PR2a 248 passed / 0 failed, both measured by me. No pre-existing
test changed or broke; the three pre-existing tests in the touched file are byte-identical. The only removed
test asserted behavior PR2a deliberately corrects. Since production emits no non-empty patches, the collapse
writes an empty patch over an already-empty one, and the `is not` -> `!=` change is provably equivalent for
all four production normalizers — so PR2a is behavior-preserving in production by construction, not merely
by test evidence.

### Q7 — Is the pipeline ready for PR2b (first period PATCH)? (YES / PARTIALLY / NO)
**YES.** Both branches of the period-patch flow are correct and tested: no-op normalization preserves the
patch through to commit, and changed normalization collapses so nothing reapplies. No planner, executor,
reducer or contract change is required. The two recommendations in section 11 are quality improvements to
land with PR2b, not blockers.

---

## Reviewer's note on git state

The isolated review worktree was created from `origin/main` (`8a5bbe6`), a **divergent** line that does not
contain `e31c35b` at all (`git merge-base --is-ancestor e31c35b HEAD` -> false). Since
`feat/patch-normalization-pr2a` is checked out in the primary checkout, this worktree was moved to a
**detached HEAD at `e31c35b`** — the exact ref under review. The working tree was clean before and after.

No other ref was checked out at any point. The PR1a baseline was measured by extracting
`git archive 5bcc745` into a throwaway sibling directory (`C:\#work\sl\pr2a_baseline_tmp`, since deleted),
never by checking out a different ref in any worktree. The primary checkout `C:\#work\sl\balance_chat` was
verified to remain on `feat/patch-normalization-pr2a` @ `e31c35b` with a clean status.

Final state of this worktree: `HEAD = e31c35b`, clean except for this report file.
