# PR6: runtime intent / execution / state consistency

## Goal and baseline

PR6 adds fail-closed assertions between authoritative typed artifacts. It does
not infer, repair, or rewrite intent, plan, execution, or persisted state.

- Baseline: `a25dc4dd76611f93bf7017da078a36ae510ee175`
  (PR5/PR5a plus EVAL2; no Guard Spike wiring).
- Feature branch: `feat/runtime-consistency-pr6`.
- Baseline collection/result: `567 collected`, `567 passed`, one existing
  Starlette/httpx deprecation warning.
- PR6 collection/result: `603 collected`, `603 passed`, the same warning.
- Collection delta: 36 added, 0 removed.

## Architecture seam

### Intent to execution

The native analytical path now has this order:

```text
effective AnalysisIntent
  -> NativeMultiOperandPlanner.plan()
  -> assert_intent_execution_consistent(intent, plan)
  -> RoutingEvidenceGate.validate_plan()
  -> native or full-balance execution
```

The assertion is in `PipelineV2TurnProcessor._execute_mutation`, after the
existing planner and before any analytical executor or database call. A
supported mismatch raises `IntentExecutionConsistencyError`, logs
`INTENT_EXECUTION_MISMATCH`, and is translated to the existing
`TurnProcessingError` boundary with code `intent_execution_mismatch`.

The guard never changes `AnalysisIntent`, `NativeExecutionPlan`, or planner
output. The planner, executor, reducer, interpreter, binder, transition layer,
API, stores, and semantic-shadow implementation are unchanged.

### Execution to state

Actual analytical result paths attach their final normalized
`executed_intent` to the internal `TurnProcessResult`. This includes native
scalar/series execution, full-balance execution, non-temporal grouping, and
the existing unified standalone path. Clarification and pre-database
no-result paths do not claim an executed intent.

`BalanceChatService.execute_turn` then performs:

```text
execute
  -> store.commit
  -> executed vs committed assertion
  -> store.get (reload)
  -> committed vs reloaded assertion
  -> memory persistence / response cache / normal response
```

The extra reload is deliberate: it verifies the value returned by `commit`
against the state that the store subsequently serves. No store contract or
implementation was changed.

## Canonical intent and execution projections

Raw equality is invalid because the existing planner legally decomposes one
intent into scalar tasks. PR6 instead projects both artifacts into the same
read-only, operation-aware set of dimensions.

| Dimension | AnalysisIntent source | NativeExecutionPlan source | Legitimate normalization/decomposition | Mismatch |
|---|---|---|---|---|
| operation | overall operation plus the expected scalar operation | plan operation plus each scalar task operation | compare/calculate/period-compare use aggregate tasks; group/rank/temporal aggregate use show tasks | overall or task-local operation differs from the legal shape |
| operand | operand ID | task operand ID and scalar operand ID | period comparison repeats one operand with a period index | source set, reference ID, or scalar/task identity differs |
| metric | operand metric | scalar task operand metric | none | any source metric differs |
| period | operand-local periods, otherwise global periods | scalar task periods | period comparison splits the ordered two periods across indexed tasks | bounds, ordering, ownership, or source count differs |
| GEO | role, entity type, entity ID for `geo_object`/`geo_group` | same fields in scalar tasks | entity ordering is canonicalized | identity, type, or role differs |
| business entity | role, entity type, entity ID for all non-GEO entities | same fields in scalar tasks | entity ordering is canonicalized; display labels are not semantic identity | identity, type, or role differs |
| entity roles/direction | all role bindings; explicit source/destination bindings | same fields in scalar tasks | ordering only is ignored | role change or source/destination inversion |
| unit | canonical operand unit | scalar operand unit | existing contract canonicalization remains authoritative | canonical unit differs |
| grain | temporal grouping/aggregation grain or ranking grain | task series grain | `total`/no series is represented as no task series grain | series grain differs |
| grouping | typed grouping dimension, canonical group ID, aggregate | typed task series metadata | the native planner supports temporal grouping; scalar task has no grouping object | inferred temporal grouping differs |
| aggregation | logical operand aggregate, bucket aggregate, series reduction | scalar aggregate plus task bucket/reduce fields | temporal aggregate uses the metric default per bucket and the requested aggregate as series reduction; group/rank use their typed bucket aggregate | any of the three levels differs |
| comparison | explicit or existing implicit baseline/target semantics | plan comparison metadata | period comparison task IDs are mapped to ordered period indices | baseline, target, direction, or percent base differs |
| formula | typed formula | plan formula | operand references remain exact | operator or referenced operands differ |
| ranking | typed ranking | plan ranking | scalar source is a series with ranking grain/bucket | direction, grain, bucket, limit, or return dimension differs |

Generated `source_intent_id`, scalar task `intent_id`, and `task_id` values are
not compared as literal strings. Period-comparison task IDs are first resolved
through their references, so renaming them consistently is equal while changing
the referenced period is not. Entity IDs, operand IDs, roles, and typed formula
or comparison references are not ignored.

## Supported, mismatch, and not comparable policy

The comparison returns one of:

- `SUPPORTED_AND_EQUAL`: execution may proceed;
- `SUPPORTED_AND_MISMATCH`: fail closed;
- `NOT_COMPARABLE`: fail closed if this assertion boundary is invoked.

All current `NativeMultiOperandPlanner` production shapes are comparable.
`MULTI_STEP` and non-temporal grouping do not have a native typed execution
plan. The latter follows the pre-existing `_execute_grouping_mutation` route
before `_execute_mutation`; it receives the state assertion but not an invented
native-plan comparison. PR6 does not redesign that path.

## Exact state invariant

For a result path carrying `executed_intent`, exact Pydantic equality remains
authoritative:

```text
executed intent == committed last_attempted_scope.intent
success: executed intent == committed active_dialog_scope.intent
```

After reload, PR6 compares exact semantic state using an explicit allow-list:

- contract version, session ID, revision, and metadata version;
- active, last-attempted, and last-successful intent plus their turn IDs;
- entity memory;
- result references;
- recent-turn journal;
- resolved conversation window (the mutation/semantic journal);
- pending clarification.

Only infrastructure timestamps are ignored: context `created_at`/`updated_at`
and scope `updated_at`. Request/trace IDs are not persisted in these compared
fields. Referential state IDs such as turn, result, entity, and intent IDs are
kept exact because changing them can break state references.

`EXECUTED_COMMITTED_MISMATCH` diagnostics contain the exact differing intent
field only. `COMMITTED_RELOADED_MISMATCH` uses field names and count/SHA-256
fingerprints for journal collections, avoiding full state or SQL logging.

## Failure lifecycle

| Failure | Execution performed | State committed | Revision | Response cached | Reservation |
|---|---:|---:|---|---:|---|
| intent/plan mismatch | no | no | unchanged | no | released by existing `finally` if reserved |
| executor failure | attempted | no | unchanged | no | released by existing `finally` |
| commit failure | yes | no successful commit | store-defined transactional result | no | released by existing `finally` |
| executed/committed mismatch | yes | once | incremented once | no | released by existing `finally` |
| committed/reloaded mismatch | yes | once | incremented once | no | released by existing `finally` |

A post-commit mismatch cannot roll back a commit through this boundary. It is
not returned as a normal analytical response, is not cached, does not trigger a
second commit, and releases the turn reservation. Existing request-ID cache
lookup still happens before reservation and processing; a normal repeated
request remains idempotent.

## Evidence matrix

| Dimension | Positive case | Hostile mismatch detected | False positive |
|---|---|---|---:|
| operation | simple SHOW and legal task-local decomposition | overall and scalar task operations | 0 |
| metric | SHOW and multi-operand comparison | scalar source metric | 0 |
| period | Period PATCH effective intent and period comparison | scalar period bounds | 0 |
| GEO | GEO PATCH effective intent | canonical GEO identity | 0 |
| business entity | Business Entity PATCH effective intent | canonical balance identity | 0 |
| source/destination | directed source to destination | source/destination inversion | 0 |
| unit | canonical volume unit | scalar unit | 0 |
| aggregation | temporal aggregate and period grouping | series reduction and grouping bucket | 0 |
| comparison | valid multi-task compare and period compare | baseline/target inversion | 0 |
| formula/ranking | calculation and ranking plans | formula operator and ranking direction | 0 |

Eleven valid projection cases (ten operation families plus regenerated IDs)
produced zero false mismatch. Each hostile test changes one semantic dimension;
there is no single catch-all corrupted plan test.

State hostile tests separately cover corrupted committed period, corrupted
committed GEO, stale reload revision, changed reloaded business operand, and a
truncated reloaded conversation journal. A period-only PATCH also proves that
unrelated GEO/business operands survive execution, commit, and reload exactly.

## Tests and differential

| Verification | Result |
|---|---:|
| Full baseline pytest | 567 passed, 1 warning |
| Full PR6 pytest | 603 passed, 1 warning |
| Period PATCH | 37 passed |
| GEO PATCH | 106 passed |
| Business Entity PATCH | 79 passed |
| Transitions | 103 passed |
| Reducer | 8 passed |
| Execution/commit plus PR6 consistency | 45 passed |
| PostgreSQL store | 3 passed |
| Golden pytest | 14 passed |
| Semantic shadow | 66 passed |
| Golden validate-only | 8/8 |
| P0 strict dry-run | 84/84 |
| PR3 strict dry-run | 20/20 |
| PR4 strict dry-run | 50/50 |

The exact collection audit is `567 -> 603`: 36 nodes added in
`tests/test_runtime_consistency.py`, 0 removed. Every one of the 567 baseline
nodes remains green. Therefore the code-tested production differential has 0
semantic mismatches. Golden and acceptance commands above are validation and
dry-run coverage only; no live DB-backed differential is claimed.

## Performance

The comparison is deterministic, in-process, and performs no I/O, LLM call,
embedding, or database query. A representative two-operand comparison measured
10,000 assertions in 938.278 ms, or 93.828 microseconds per call in `ai_env`.
The state boundary adds one intentional `store.get` after commit; database
latency for that reload was not benchmarked in this local, non-live run.

## Modified files

- `src/balance_chat/runtime_consistency.py`
- `src/balance_chat/processor.py`
- `src/balance_chat/service.py`
- `tests/test_runtime_consistency.py`
- `docs/runtime_consistency_pr6.md`

## Known limitations

- The guard validates the typed native plan. Full-balance, balance-section, and
  directed-flow routes still use their existing specialized physical execution
  and deterministic row filtering after that plan check; PR6 does not invent a
  second projection of SQL or result rows.
- Non-temporal grouping and unified standalone execution do not expose a
  `NativeExecutionPlan`; only the execution/state invariant applies there.
- State detection after commit is diagnostic/fail-closed at the response
  boundary, not transactional rollback.
- Execution result to final prose (`AnswerClaim`) remains out of scope.
- Guard Spike G1-G6 and `SemanticTransitionProposal` remain shadow/research
  artifacts and are not imported or activated.

## Mandatory answers

1. **Does PR6 change query semantics? NO.**
2. **Does PR6 enable semantic repair? NO.**
3. **Does PR6 modify planner output? NO.**
4. **Can Intent to Execution divergence now be detected? YES.** Covered:
   operation, operand/source shape, metric, period, GEO, business entity,
   roles/direction, unit, grain, grouping, aggregation, comparison, formula,
   and ranking for typed native plans.
5. **Can a period mismatch pass silently? NO** on covered typed-plan paths.
6. **Can a GEO/business entity mismatch pass silently? NO** on covered
   typed-plan paths.
7. **Can source/destination inversion pass silently? NO** where direction is
   represented in the typed plan.
8. **Does valid multi-task decomposition cause false mismatch? NO.**
9. **Is exact state equality still authoritative where previously used? YES.**
10. **Can committed intent differ semantically from executed intent without
    detection? NO** for analytical paths carrying `executed_intent`.
11. **Can reloaded semantic state differ from committed state without
    detection? NO** for the documented semantic allow-list.
12. **Can generated IDs/timestamps alone cause semantic mismatch? NO** for
    non-semantic plan IDs and infrastructure timestamps; referential state IDs
    intentionally remain exact.
13. **Can consistency detection mutate or repair intent/plan? NO.**
14. **Can a consistency failure double-commit or increment twice? NO.**
15. **Are request-ID idempotency and reservation semantics preserved? YES.**
16. **Were new LLM calls introduced? NO.**
17. **Was AnswerClaim implemented? NO.**
18. **Were Guard Spike G1-G6 productionized? NO.**
19. **Full pytest: 603 passed, 1 pre-existing warning.**
20. **Test nodes: 36 added, 0 removed.**
21. **Production differential semantic mismatches: 0** in the code-tested
    baseline suite; no live DB differential is claimed.
22. **Ready for Claude review: YES.** The baseline SHA, feature branch, focused
    and full results, exact node delta, limitations, and failure lifecycle are
    recorded above.
