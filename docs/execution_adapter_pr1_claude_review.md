# ExecutionAdapter PR1 — independent adversarial review

Reviewer: independent staff-level review, read-only on production code.
Branch reviewed: `feat/execution-adapter-pr1` @ `179a8a5`.
Baseline / merge-base: `790f3d702d37b6890e213054edcfd88d5c11eda3` (`main`).
Method: full read of the diff, full read of `processor.py`, `reducer.py`,
`contracts.py`, `service.py`, `store.py`, `api.py`, `bootstrap.py`,
`binding.py` (producer entry points), both new test files; repository-wide
symbol sweep; test suite executed on both `main` and PR head in the same
environment; targeted runtime probes of the adapter/reducer edge cases.

## 1. Verdict

**APPROVE WITH REQUIRED FIXES**

No blocker was found. The PR does what it claims for every reachable
production path, but it introduces one unmapped exception type at the
service boundary and leaves the fail-closed guarantee (I6) weaker than the
brief requires, which becomes dangerous the moment PR2 lands a PATCH
producer.

## 2. Executive summary

- No blocker. I ran the suite on both revisions in an identical
  environment: `main` = **228 passed / 0 failed**, PR head = **238 passed /
  0 failed**, identical failure set (empty diff), +10 new tests. No new
  failures, no removed tests.
- Grouping-vs-scalar routing is provably unchanged. The new
  `_dispatch_mutation` condition (`processor.py:132`) is character-identical
  to the two conditions it replaced, and for every converted
  `_execute_mutation` → `_dispatch_mutation` call site I traced the producer
  and confirmed it can never emit `Operation.GROUP` (the only `GROUP`
  constructor in `processor.py` is line 393, which already routed to the
  grouping executor before the PR).
- Execution is genuinely decoupled: exactly **one** production read of
  `mutation.replace_intent` remains outside `reducer.py` /
  `execution_adapter.py`, at `processor.py:1970`
  (`_enforce_role_separated_entities`), and it is a producer-side edit that
  runs *before* materialization at `processor.py:2036`. I2 holds.
- The adapter is genuinely thin (27 lines, imports only `contracts` and
  `reducer`, no domain knowledge, no mutation of inputs). `reduce_intent()`
  remains the single materialization semantics. I3 holds.
- "Executed == committed" holds for every reachable PR1 path, but largely
  *trivially*: all production producers are replace-only, so
  `reduce_intent()` is an identity round-trip. I verified empirically that
  `AnalysisIntent.model_validate(i.model_dump()) == i` for validated intents,
  which is what makes `_synchronize_effective_intent` safe today.
- **HIGH:** the fail-closed guard only rejects the *strictly* empty mutation.
  A patch that is semantically a no-op (e.g. a single
  `FieldMutation(action=KEEP)`) passes the guard and silently re-executes the
  previous active intent — the exact outcome I6 exists to prevent. Verified
  at runtime.
- **HIGH:** `_synchronize_effective_intent` (`processor.py:168-170`)
  overwrites `replace_intent` but **retains the original patch**. With a PR2
  patch producer, any normalization that rewrites a patched field turns a
  working turn into a hard `ContextReductionError`.
- **MEDIUM:** `ContextReductionError` is now reachable from
  `processor.process()` and is not mapped in `api.py:104-131` → HTTP 500.
  Before the PR the same error class could only surface from
  `store.commit()`, where `PostgresContextStore` (the production store, see
  `config.example.json:14`) wraps it into `ContextStoreError` → HTTP 503.
- **MEDIUM:** the canonical-unit correction branch
  (`processor.py:1104-1109`) is now a fail-closed trap rather than a
  correction, because a non-canonical unit cannot survive the reducer's
  `normalize_volume_unit` validator. Dead today; verified.
- **MEDIUM:** `_standalone()` (`processor.py:2661`) is a complete execution +
  commit path that never touches the adapter, so the design doc's claim
  "Every executable mutation is materialized through one processor helper"
  is not true. Not a regression, but the seam is not universal.
- Scope creep: none. The production diff touches 3 files and contains
  nothing outside the stated goal.

## 3. Blockers

None.

I specifically tried and failed to prove each of the listed blocker classes:

| Blocker class | Result | Why not |
|---|---|---|
| Observable regression | Not found | Identical test results on both revisions; routing conditions identical; planner input proven equal. |
| executed != committed | Not found | All producers are replace-only; round-trip identity verified; all four post-materialization mutation points go through `_synchronize_effective_intent`. |
| Silent re-execution (reachable) | Not found in PR1 | No production producer emits a patch-only or empty mutation (`binding.py:312,509` are the only compiler returns, both set `replace_intent`). Reachable only after PR2 — see H1. |
| Execution still depends on `replace_intent` | Not found | Single remaining read is producer-side (`processor.py:1970`). |
| Changed planner semantics | Not found | `planning.py` untouched; `_CapturingPlanner` test plus code trace confirm the planner receives an object equal to the pre-PR one. |
| Hidden state mutation | Not found | `reduce_intent` builds a fresh payload dict and `deepcopy`s every applied value (`reducer.py:45,71,80`). |

## 4. High severity findings

```
Finding: The fail-closed guard only rejects the strictly empty mutation; a
         semantically no-op patch silently re-executes the previous active
         intent.
Severity: HIGH
Evidence: src/balance_chat/execution_adapter.py:23-26
          (`if mutation.replace_intent is None and not any(
             field_mutation is not None for _, field_mutation in mutation.patch)`)
          src/balance_chat/reducer.py:92-93 (base falls back to
          `state.active_dialog_scope.intent`)
          src/balance_chat/contracts.py:308-325 (`MutationAction.KEEP` is a
          legal FieldMutation with `value=None`)
Why it matters: Invariant I6 exists so that a mutation carrying no executable
          intent cannot silently re-run the previous query against a new user
          message. The guard tests for *structural* emptiness only. Any patch
          whose every action is `KEEP` is structurally non-empty and
          semantically empty, so it passes the guard and `reduce_intent`
          returns the unchanged active intent. The turn then executes the
          previous question, commits it as a new turn, and the user sees a
          confident answer to a question they did not ask.
Reproduction / reasoning: Verified at runtime against the PR head:
          state = active scope with intent I;
          mutation = ContextMutation(turn_id="t2", user_message="repeat",
              patch=IntentPatch(operation=FieldMutation(action=MutationAction.KEEP)))
          ReducerExecutionAdapter().effective_intent(state, mutation) == I  -> True
          (no exception raised). An all-KEEP patch is exactly what a
          structured-output model produces when it decides "nothing changed",
          so this shape is highly likely once PR2 enables PATCH producers.
Minimal correction: Reject when the reduced intent is not distinguishable
          from a no-op over the active scope — e.g. widen the guard to
          "replace_intent is None and every present FieldMutation has
          action == KEEP", or require at least one non-KEEP action. One
          added condition in execution_adapter.py plus one test.
```

```
Finding: `_synchronize_effective_intent` overwrites `replace_intent` but
         retains the original patch, so any normalization that rewrites a
         patched field will hard-fail once a PATCH producer exists.
Severity: HIGH
Evidence: src/balance_chat/processor.py:162-175
          (`synchronized = mutation.model_copy(update={"replace_intent":
            effective_intent}, deep=True)` — `patch` is copied through
            unchanged, then re-applied by the equality check)
          Call sites: processor.py:1006, 1109 (grouping),
                      processor.py:2240, 2255 (scalar)
          Normalizer that rewrites `periods`: processor.py:3244-3276
          (`_normalize_same_scope_period_comparison_intent` sets
           `periods=[first.periods[0], second.periods[0]]`)
Why it matters: The synchronized mutation means "apply the patch on top of
          the already-normalized (already-patched) intent". That is not what
          was executed. The equality guard correctly refuses to commit the
          divergence, but the result is an uncaught `ContextReductionError`
          (see M1 → HTTP 500), not a degraded-but-correct turn. PR1 is safe
          because `patch` is always empty; PR2 makes this live.
Reproduction / reasoning: With a PR2 patch producer emitting
          `patch=IntentPatch(periods=FieldMutation(action=SET, value=[april]))`
          on a turn whose effective intent is COMPARE over two operands each
          holding one period, `_execute_mutation` normalizes to
          COMPARE_PERIODS with `periods=[p1,p2]`; `_synchronize_effective_intent`
          then reduces `replace_intent=normalized + patch(periods SET april)`
          to `periods=[april]`, which != normalized -> raise. The PR's own
          test `test_normalization_fails_closed_if_patch_would_change_committed_intent`
          (tests/test_execution_commit_equivalence.py:229-263) asserts exactly
          this failure as desired behaviour, which confirms the mechanism but
          not that a 500 is an acceptable user outcome.
Minimal correction: When synchronizing, drop the fields the normalizer
          rewrote from the retained patch (or clear the patch entirely, since
          `replace_intent` now carries the fully materialized intent).
          Whichever is chosen must be decided in PR2, but the choice should be
          written down in `docs/execution_adapter_design.md` now, because the
          current shape silently encodes "patch wins over normalization".
```

## 5. Medium/Low findings

```
Finding: `ContextReductionError` is now reachable from `processor.process()`
         and is unmapped at the API boundary; the same error previously
         surfaced as HTTP 503 via the PostgreSQL store's blanket wrap.
Severity: MEDIUM
Evidence: src/balance_chat/processor.py:111 (materialization can raise),
          reached from processor.py:553, 827, 954, 1363, 2036 and from
          `_dispatch_mutation` at processor.py:131
          src/balance_chat/api.py:104-131 (no `ContextReductionError` clause;
          `TurnProcessingError` -> 422, `ContextStoreError` -> 503)
          src/balance_chat/store.py:650-651 (`except Exception as exc: raise
          ContextStoreError("could not commit PostgreSQL context transition")`)
          config.example.json:13-17 (production store type = postgresql)
Why it matters: An infrastructure refactor changed the error contract for a
          class of failures. Old path: reduce failure -> raised inside
          `store.commit` -> wrapped -> HTTP 503 `context_store_unavailable`.
          New path: reduce failure -> raised inside `process()` -> falls
          through every `except` in `api.py` -> HTTP 500 with no error code.
          It also means a genuine client/interpretation problem is now
          reported as an unhandled server fault instead of a coded 4xx.
Reproduction / reasoning: I could not find a reachable production mutation
          that fails `reduce_intent` today — every producer builds a validated
          `AnalysisIntent` (`binding.py:300-312,495-510`,
          `processor.py:392,1595,1679,1736,1782,1893,3269,3519,4566`), and the
          `model_copy(update=...)` sites that bypass validation
          (`processor.py:1234,1265,1328,4402`) are all guarded by
          `len(scope.intent.operands) != 1`. So this is latent, not live.
          But H1's and H2's failure modes both land here, so the mapping gap
          is the difference between "422 with a code" and "500".
Minimal correction: In `processor.py`, wrap materialization failures in
          `TurnProcessingError(..., code="context_reduction_failed")`, or add
          one `except ContextReductionError` clause in `api.py:104-131`.
```

```
Finding: The canonical-unit correction branch in grouping execution is now a
         fail-closed trap instead of a correction.
Severity: MEDIUM
Evidence: src/balance_chat/processor.py:1104-1109
          (`if canonical_unit and intent.operands[0].unit != canonical_unit:
             ... mutation = self._synchronize_effective_intent(state, mutation, intent)`)
          src/balance_chat/contracts.py:92-95 (`normalize_volume_unit`
          before-validator maps *any* non-empty unit to CANONICAL_VOLUME_UNIT)
          src/balance_chat/processor.py:4496-4503 (`_canonical_grouping_unit`
          currently returns CANONICAL_VOLUME_UNIT unconditionally)
Why it matters: The branch exists precisely to handle "the authoritative unit
          differs from the intent's unit". `operand.model_copy(update={"unit":
          X})` bypasses validation, but `_synchronize_effective_intent`
          re-validates through `reduce_intent`, which coerces X back to
          CANONICAL_VOLUME_UNIT — so the equality check fails and the whole
          turn raises. Before PR1 the same code silently stored X and let the
          commit-time reducer normalize it.
Reproduction / reasoning: Verified at runtime on the PR head:
          `AnalysisIntent.model_validate(bad.model_dump()) == bad` -> False,
          `rt.operands[0].unit == "тыс. м3"` for `bad` built with unit "т".
          Unreachable today because `_canonical_grouping_unit` is hard-wired
          to the canonical unit and every validated operand already carries
          it — i.e. the branch condition is currently always False. It becomes
          live the day that function is taught to return a second unit.
Minimal correction: None required for merge. Either delete the now-dead
          branch or add a comment recording that the reducer will coerce any
          non-volume unit, so this branch must not be revived as written.
```

```
Finding: `_standalone()` is a full execution+commit path that never goes
         through the ExecutionAdapter, contradicting the design doc.
Severity: MEDIUM
Evidence: src/balance_chat/processor.py:2661-2768 — builds `intent` from
          `self.translator.intent(envelope, ...)` (line 2705), constructs
          `ContextMutation(..., replace_intent=intent)` (line 2711), uses
          `intent` directly for `_grouped_result_reference` (2722) and
          `_memory_write` (2728), and returns a `TurnProcessResult` that the
          service commits. `_materialize_mutation` is never called.
          Entered from processor.py:266-274, 430-437, 625-633.
          docs/execution_adapter_design.md:80-82 claims "Every executable
          mutation is materialized through one processor helper."
Why it matters: The seam is not universal, so "the effective intent is the
          execution source of truth" is only true for the dispatched paths.
          It is not a PR1 regression (this path was never coupled to
          `mutation.replace_intent` — it owns its intent locally) and a
          patch-only mutation can never reach it, so PR2 is not blocked. But
          the documentation overstates coverage, and a future reader will
          assume an invariant that does not hold.
Reproduction / reasoning: Static trace; `_standalone` appears in neither the
          `_dispatch_mutation` nor the `_materialize_mutation` call graph
          (repo-wide grep of both symbols returns only processor.py:131,553,
          827,954,1006,1109,1363,2036,2240,2255 and the two test files).
Minimal correction: Correct the sentence in
          docs/execution_adapter_design.md:80-82 to "every *dispatched*
          mutation", and list `_standalone` (and the two clarification
          returns at processor.py:500-510 and 767-777) as intentionally
          outside the seam.
```

```
Finding: `IntentPatch` cannot express `formula` or `ranking`, so a PATCH that
         changes `operation` away from CALCULATE/RANK produces an invalid
         intent.
Severity: MEDIUM (PR2 constraint, not a PR1 defect)
Evidence: src/balance_chat/contracts.py:328-334 (`IntentPatch` fields:
          operation, operands, periods, grouping, grain, comparison — no
          formula, no ranking)
          src/balance_chat/contracts.py:183-202 (validator: `formula` is
          valid only for CALCULATE, `ranking` only for RANK)
Why it matters: A patch-only follow-up over an active RANK or CALCULATE scope
          that changes `operation` cannot clear the now-illegal spec, so
          `reduce_intent` raises. This is not "А за апрель?" (which patches
          only `periods`), but it is the first adjacent case PR2 will hit.
Reproduction / reasoning: Reading the contract; `_apply_field`
          (reducer.py:54-86) can only touch fields present in `IntentPatch`.
Minimal correction: None for PR1. Note it as a known PR2 boundary.
```

```
Finding: The `_dispatch_mutation` routing predicate is redundant.
Severity: LOW
Evidence: src/balance_chat/processor.py:132
          (`if intent.grouping or intent.operation == Operation.GROUP:`)
          src/balance_chat/contracts.py:179-182 (validator makes
          `grouping != []` and `operation == GROUP` mutually implied)
Why it matters: Nothing — it is copied verbatim from the two sites it
          replaced, which is the correct choice for a no-behavior-change PR.
          Worth recording only so a future reader does not "simplify" it in a
          way that changes semantics for an unvalidated intent.
Minimal correction: None.
```

```
Finding: The constructor default `execution_adapter or ReducerExecutionAdapter()`
         means a missing wiring would never be detected.
Severity: LOW
Evidence: src/balance_chat/processor.py:91; src/balance_chat/bootstrap.py:49
Why it matters: Verified that the composition root *does* pass an explicit
          instance, so no wiring defect is masked today. `ReducerExecutionAdapter`
          is stateless (no `__init__`, no fields), so the per-processor
          instance is neither a singleton nor a shared-mutable-state risk.
          There is however no test asserting that `build_application` wires
          it, so the explicit wiring could be deleted without any test failing.
Minimal correction: None required.
```

## 6. Invariant verification

| Invariant | PASS / FAIL / UNCERTAIN | Evidence |
|---|---|---|
| Zero observable behavior change | PASS | Suite on identical env: `main` 228 passed/0 failed, PR 238 passed/0 failed, identical failure sets. Routing predicate at `processor.py:132` is identical to the replaced `processor.py:489,773` conditions; every converted call site traced to a producer that cannot emit `Operation.GROUP`. Gate input changed from `mutation.replace_intent` to `effective_intent`, which are equal under empty patch (round-trip identity verified at runtime). Only caveat is the *unreachable* error-code change in M1. |
| Execution decoupled from replace_intent | PASS | Repo-wide grep: the only production read outside `reducer.py`/`execution_adapter.py` is `processor.py:1970` (`_enforce_role_separated_entities`), which runs before materialization at `processor.py:2036`. Full classification in §7. |
| Adapter is thin | PASS | `execution_adapter.py` is 27 lines; imports only `.contracts` and `.reducer`; no GEO/period/planner/interpreter symbols; does not touch `state` or `mutation`; delegates unconditionally to `reduce_intent`. Its only added semantics is the empty-mutation precondition — a guard, not a second materialization. |
| Executed == committed intent | PASS (for PR1) | All four post-materialization intent rewrites (`processor.py:1006,1109,2240,2255`) go through `_synchronize_effective_intent`; I found no fifth. `_repair_incomplete_evidence` returns `repaired_intent = _materialize_mutation(state, repaired_mutation)` (`processor.py:954,982`), so the returned pair is consistent by construction. Commit re-reduces the same mutation (`store.py:117,257,587` → `reducer.py:152`) and, with `replace_intent` set and an empty patch, `reduce_intent` does not read state at all, so store-side re-deserialization cannot diverge. Caveat: `_execute_full_balance_mutation` executes a *derived* `source_intent` (`processor.py:2498-2502`, built by `processor.py:3516-3531`) while committing `intent` — pre-existing, unchanged by this PR, and outside what the new guard covers. |
| No PATCH producer activated | PASS | `IntentPatch(` / `FieldMutation(` occur only in `contracts.py` (definitions) and in `tests/`. `ContextMutation.patch` keeps its `default_factory=IntentPatch` empty default (`contracts.py:340`), which is not functional PATCH usage. |
| Empty mutation fails closed | PASS (literal) / gap | The literal invariant ("`replace_intent is None` AND patch empty") is enforced at `execution_adapter.py:23-26` and covered by `tests/test_execution_adapter.py:94-99`. Two gaps: (a) a no-op KEEP patch bypasses it — see H1, verified at runtime; (b) the guard exists only at the execution boundary — `apply_context_transition`/`reduce_intent` still silently reuse the active intent for a strictly empty mutation (verified: `reduce_intent(state, ContextMutation(turn_id="t3", user_message="repeat")) == active intent` -> True), so any future path that commits without materializing is unguarded. |
| Planner semantics unchanged | PASS | `src/balance_chat/planning.py` is not in the diff. The planner is called at `processor.py:2290` with `intent`, which is `effective_intent` after the same two normalizers in the same order as before. `tests/test_execution_commit_equivalence.py:118` asserts `planner.intent == intent` including `intent_id`. |
| Executor semantics unchanged | PASS | `src/balance_chat/execution.py` is not in the diff. The executor is still called at `processor.py:2343` with the plan produced from that intent; `tests/test_execution_commit_equivalence.py:172` asserts `executor.plan == planner.output_plan`. |

## 7. Remaining `replace_intent` dependencies

Full list of production occurrences (`src/`, `scripts/`, `run_*.py`); tests
omitted and classified TEST.

| File:line | Symbol / context | Classification | Notes |
|---|---|---|---|
| `contracts.py:339` | `ContextMutation.replace_intent` field | OTHER | Contract definition. |
| `reducer.py:90-91` | `reduce_intent` base selection | OTHER | The single source of materialization semantics; must remain. |
| `reducer.py:95` | `"initial transition requires replace_intent"` | VALIDATION | Reducer-level fail-closed for patch-without-base. |
| `execution_adapter.py:23` | empty-mutation guard | VALIDATION | Execution-boundary precondition only; delegates immediately. |
| `binding.py:312` | `_compile_draft` return | PRODUCER | Always sets a validated full replacement. |
| `binding.py:509` | `_compile_graph` return | PRODUCER | Always sets a validated full replacement. |
| `conversation.py:120` | `materialize_window` old-session bridge | PRODUCER | Synthesizes a mutation for a turn frame; never executed. |
| `processor.py:169` | `_synchronize_effective_intent` write | NORMALIZATION-SYNC | Write, not read. See H2. |
| `processor.py:392` | deterministic context grouping | PRODUCER | The only `Operation.GROUP` producer in the processor. |
| `processor.py:505, 772` | clarification result mutation | PRODUCER | Commits the unchanged active intent; no execution. |
| `processor.py:538, 807` | `CanonicalRelationNotFound` attempt | PRODUCER | Feeds `_reverse_no_data`. |
| `processor.py:1245, 1277, 1348` | geo / reverse deterministic mutations | PRODUCER | Unreachable in the current control flow (see §12). |
| `processor.py:1595, 1679, 1757, 1812, 1911` | deterministic flow / comparison / balance producers | PRODUCER | All construct validated `AnalysisIntent`. |
| **`processor.py:1970`** | **`_enforce_role_separated_entities`: `intent = mutation.replace_intent`** | **PRODUCER** | **The only remaining direct read.** Runs at `processor.py:2031`, i.e. *before* `_materialize_mutation` at `processor.py:2036`. It raises `TurnProcessingError` if `replace_intent is None`, so it would need rework before a patch-only producer could reach it — but no such producer can, since the mutation is constructed locally at `processor.py:2025`. Not an execution dependency. |
| `processor.py:1995` | role-separated write-back | PRODUCER | Write. |
| `processor.py:2029, 2106, 2715` | role-separated / peer-entity / standalone producers | PRODUCER | Local construction. |
| `processor.py:4414, 4447, 4579` | period / extremum deterministic producers | PRODUCER | Unreachable in the current control flow (see §12). |

**Conclusion:** zero EXECUTION-semantic reads of `mutation.replace_intent`
remain. Zero ROUTING reads remain (`processor.py:489` and `:773` on `main`
are gone). Zero LOGGING reads remain (`processor.py:921`, `:1317`, `:1985`,
`:1988` on `main` now read `effective_intent`). I2 is achieved.

## 8. Test adequacy

The ten new tests are real tests, not tautologies, but they are concentrated
on the *unreachable* half of the surface.

What is genuinely covered, and would catch a wrong implementation:

| Concern | Test | Would a wrong impl pass? |
|---|---|---|
| replace-only equivalence | `test_execution_adapter.py:42` | No — asserts full model equality including `intent_id`. |
| replace + patch precedence | `test_execution_adapter.py:56` | No. |
| patch without base | `test_execution_adapter.py:75` | No — asserts the reducer's own message, so a swallow-and-default impl fails. |
| fail-closed empty mutation | `test_execution_adapter.py:94` | No for the strictly-empty shape; **yes** for a no-op KEEP patch (H1 is invisible to this suite). |
| reducer error propagation | `test_execution_adapter.py:102` | No. |
| immutability of state/mutation | `test_execution_adapter.py:119` | Reasonable — compares full `model_dump` before/after. |
| single materialization | `test_execution_commit_equivalence.py:117,170` (`adapter.calls == 1`) | No — this is the strongest test in the set; it would catch a double-materialize regression. |
| planner input identity | `test_execution_commit_equivalence.py:118,171` | No. |
| grouping routing from effective intent | `test_execution_commit_equivalence.py:179` | No — monkeypatches `_execute_mutation` to throw. |
| executed == committed == reloaded | `test_execution_commit_equivalence.py:161-176` | No, for the patch-only case. |
| normalization fail-closed | `test_execution_commit_equivalence.py:229` | No — but it asserts the *raise* is desirable, which is the design question raised in H2. |

Missing, in rough order of importance:

1. **No end-to-end regression test for the actual production shape.** Every
   new equivalence test uses a *patch-only* mutation, which no production
   producer emits. The replace-only production path is only covered by
   `test_replace_only_dispatch_preserves_intent_and_original_mutation`, which
   calls `_dispatch_mutation` directly and never exercises `process()`. The
   "NO OBSERVABLE BEHAVIOR CHANGE" claim rests entirely on the pre-existing
   `test_pipeline_wiring.py` / `test_golden_queries.py` suites, not on
   anything this PR added.
2. **No test for the no-op-patch case** (H1) — the single most valuable
   missing test.
3. **No coverage of the two `_synchronize_effective_intent` call sites inside
   `_execute_grouping_mutation`** (`processor.py:1006`, `:1109`). Only the
   scalar-side mechanism is tested, and only via a direct call to the helper.
4. **No test for `_reverse_no_data`** after its conversion from
   `@staticmethod` to an instance method that materializes
   (`processor.py:1353-1363`). Three production call sites
   (`processor.py:204,540,801`) are uncovered by any new test.
5. **No test for `_repair_incomplete_evidence`'s new three-tuple contract**
   (`processor.py:954,982`) — specifically that `repaired_mutation` and
   `repaired_intent` stay mutually consistent.
6. **No test asserting `bootstrap.build_application` injects the adapter.**
   The explicit wiring at `bootstrap.py:49` could be deleted and the suite
   would stay green (the constructor default absorbs it).
7. **No negative test that a `ContextReductionError` from materialization
   produces a sane API response** — which is exactly the M1 gap.

`test_pipeline_wiring.py:1532` (`_execute_mutation` → `_dispatch_mutation`)
was a *forced* change: `_execute_mutation` now takes a required keyword-only
`effective_intent`. Retargeting it at the dispatcher is the right call, but
it means the repo no longer has any test that calls the scalar executor
directly with an explicit intent.

## 9. Scope creep

| File | Change | Necessary | Risk |
|---|---|---|---|
| `src/balance_chat/execution_adapter.py` | New file: protocol + reducer-backed impl + empty guard | YES | Low. The empty guard is arguably new *policy* rather than pure infrastructure, but it implements a stated invariant (I6) and is documented. |
| `src/balance_chat/processor.py` | `execution_adapter` ctor param + default (`:81,91`) | YES | Low. |
| `src/balance_chat/processor.py` | `_materialize_mutation`, `_dispatch_mutation`, `_synchronize_effective_intent` (`:106-175`) | YES | Medium — `_dispatch_mutation` centralizes a previously duplicated condition; verified identical. |
| `src/balance_chat/processor.py` | 15 × `_execute_mutation`/`_execute_grouping_mutation` → `_dispatch_mutation` | YES | Medium — each site now runs a grouping check it did not run before. Verified unreachable-for-GROUP at every site. |
| `src/balance_chat/processor.py` | `effective_intent` made a **required** keyword-only param of `_execute_mutation` / `_execute_grouping_mutation` (`:989,2225`) | YES | Low — both are private, all call sites updated. Making it required (rather than defaulting to `None`) is the safer choice. |
| `src/balance_chat/processor.py` | `_reverse_no_data`: `@staticmethod` → instance method + `state` param (`:1353-1356`) | YES | Low — required to materialize; the only behavioural read (`operation.value`) is provably equal. |
| `src/balance_chat/processor.py` | `_repair_incomplete_evidence` returns a 3-tuple (`:880,982`) | YES | Low. |
| `src/balance_chat/processor.py` | 4 × `mutation.model_copy(update={"replace_intent": ...})` → `_synchronize_effective_intent` | YES | Medium — adds a new raise path. Unreachable in PR1 (round-trip identity verified); see H2 and M2. |
| `src/balance_chat/bootstrap.py` | Import + explicit `ReducerExecutionAdapter()` (`:14,49`) | YES | Low. |
| `tests/test_pipeline_wiring.py` | 1 line, `_execute_mutation` → `_dispatch_mutation` | YES | Low — forced by the signature change. |
| `docs/*.md` | 2 new documents | YES | None (see M3 for one inaccurate sentence). |

**No unrelated cleanup, renames, formatting churn, condition reordering,
parser/binder changes, error-handling rewrites, or normalization changes were
found.** The diff is unusually disciplined for a change touching a 4600-line
file; I looked specifically for opportunistic edits and found none.

## 10. PR2 readiness

**READY AFTER FIXES**

A single patch-only producer for "А за апрель?" can be added without touching
`planning.py` or `execution.py`. I traced the whole path with a patch-only
mutation:

- producer (`binding.py` compile) → `ContextMutation(replace_intent=None,
  patch=IntentPatch(periods=SET))`
- `processor.py:553/827` `_materialize_mutation` → effective intent over the
  active scope ✔
- `processor.py:875-880` evidence gate consumes `effective_intent` ✔
- `processor.py:131-160` dispatch routes from `effective_intent` ✔
- `processor.py:2290` planner receives `effective_intent` ✔ (unchanged)
- `processor.py:2343` executor receives the derived plan ✔ (unchanged)
- `service.py:208` commits the *original* patch-only mutation; `store.py:117/
  257/587` → `reducer.py:152` re-reduces against the same-revision state ✔
  (covered by `test_patch_only_execution_and_commit_use_the_same_effective_intent`)

Concrete blockers to clear first:

1. **H1** — an all-KEEP or otherwise no-op patch will silently re-execute the
   previous intent. This must be closed before any producer can emit patches.
2. **H2** — decide and encode what happens to a retained patch when
   normalization rewrites a patched field. Today: hard failure.
3. **M1** — `ContextReductionError` must produce a coded 4xx, not a 500,
   because H1/H2/reducer-validation failures all land there and patch-only
   mutations make that path genuinely reachable.
4. **M4 (`IntentPatch` has no `formula`/`ranking`)** — bounds which
   `operation` transitions a patch can legally express. Not needed for
   "А за апрель?", needed for the next case.

Not blockers, but note: `_standalone` (M3) and `_execute_full_balance_mutation`'s
derived `source_intent` remain outside the seam; neither can be reached by a
patch-only mutation, so neither blocks PR2.

## 11. Required fixes before merge

Only two, and only one is strictly about PR1 as merged:

1. **Map `ContextReductionError` at the service/API boundary** (M1). Either
   wrap it in `processor.py` as
   `TurnProcessingError(..., code="context_reduction_failed")` or add an
   `except ContextReductionError` clause to `api.py:104-131`. Without this,
   PR1 has silently converted a (latent) 503 into an unhandled 500, and it
   leaves the H1/H2 failure modes surfacing as unhandled server faults.

2. **Either close the no-op-patch hole in the guard, or record the deferral
   in writing** (H1). If the team accepts that PR1 only guarantees I6 for the
   strictly-empty shape, that limitation belongs in
   `docs/execution_adapter_design.md` next to the fail-closed claim, and in
   `docs/execution_adapter_pr1.md` where the answer to Question 4 currently
   reads an unqualified "YES". Shipping the current text implies a guarantee
   the code does not provide.

H2 (retained patch during normalization) is a **required fix before PR2**,
not before PR1 — it is unreachable while every producer is replace-only.

## 12. Optional observations

- **Pre-existing dead code, relevant to reviewing this PR.** `process()`
  delegates to `_process_contextual()` whenever `state.active_dialog_scope is
  not None` (`processor.py:190-199`). Everything after that line therefore
  runs only with a `None` scope, which makes
  `_deterministic_reverse_mutation` (`:1252-1254`),
  `_deterministic_extremum_comparison` (`:4539-4540`),
  `_deterministic_grouping_query` (`:4605-4606`),
  `_deterministic_period_mutation` (`:4394-4396`) and
  `_deterministic_geo_mutation` (`:1211-1213`) unconditionally return `None`
  at the `process()` call sites (`:200,220,385,413,416`). This is not the
  PR's doing, but it is why five of the fifteen `_dispatch_mutation`
  conversions are unreachable and untestable — worth knowing before someone
  reads coverage numbers as reassurance.
- `_execute_full_balance_mutation` executes `_full_balance_source_intent(intent)`
  (`processor.py:2498-2502`, `:3516-3531`) — a synthesized SHOW/balance intent
  — while committing `intent`. That is a deliberate execution-plan derivation,
  not a semantic divergence, and it predates the PR. It does mean the phrase
  "executed effective intent" is not literally the intent handed to the
  runtime on this path.
- The performance cost of the seam is one extra `model_dump` +
  `model_validate` of an `AnalysisIntent` per turn (two more per
  normalization). Negligible relative to interpretation and SQL.
- `ReducerExecutionAdapter` is stateless, so the per-processor instance
  created by the constructor default is not a shared-mutable-state hazard,
  and `_CapturingAdapter` in the tests subclasses it cleanly.
- Both new documents are accurate on the numbers I could independently
  reproduce (`228` → `238`), which is more than usual for a self-review. The
  one inaccurate sentence is design doc lines 80-82 (see M3).

## Final Q&A

### Q1 — Can PR1 be safely merged? (YES / NO)

**YES**, with the two fixes in §11. No blocker was found, no regression was
reproduced, and the suite is green on both revisions. The architectural seam
is real: the execution path takes its intent from `ExecutionAdapter`, not from
`mutation.replace_intent`.

### Q2 — Is there an observable regression? (YES / NO / NOT PROVEN)

**NO.** Suite executed on both revisions in one environment: `main`
228 passed / 0 failed, PR head 238 passed / 0 failed, identical (empty)
failure diff, +10 new tests. Routing predicate proven identical; every
converted dispatch site traced to a producer that cannot emit `Operation.GROUP`;
planner input proven equal via round-trip identity. The only behavior
difference I could construct is the HTTP 503 → 500 change for reduce-time
failures (M1), and I could not find any reachable production mutation that
triggers it.

### Q3 — Is execution fully decoupled from `mutation.replace_intent`? (YES / PARTIALLY / NO)

**YES.** One production read remains (`processor.py:1970`,
`_enforce_role_separated_entities`) and it is producer-side, executing before
materialization at `processor.py:2036`. All routing, evidence-validation,
grouping, scalar-execution, no-data and operation-dependent logging reads now
consume the effective intent.

### Q4 — Is "executed effective intent == committed effective intent" confirmed? (YES / NO / NOT PROVEN)

**YES for PR1's reachable paths.** All four post-materialization intent
rewrites route through `_synchronize_effective_intent`; I searched for a fifth
and found none. Commit re-reduces the same mutation, and with `replace_intent`
set and an empty patch `reduce_intent` does not read state, so store-side
re-deserialization cannot diverge. Two scope caveats: the guarantee is
currently trivial (identity round-trip of a full replacement), and it does not
cover the derived `source_intent` used by `_execute_full_balance_mutation`.

### Q5 — Is the code ready for one production period PATCH without changing planner/executor? (YES / PARTIALLY / NO)

**PARTIALLY.** The mechanical path works end to end and is tested. Three
things must be settled first: the no-op-patch fail-closed hole (H1), the
retained-patch-vs-normalization semantics (H2), and the unmapped
`ContextReductionError` (M1) that both of those failure modes surface through.
