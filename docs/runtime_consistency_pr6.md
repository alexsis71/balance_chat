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

## PR6a required-fixes audit: STOP (Decision D)

PR6a started from `a200e657d050a5ce15ed2048333a65761250ac87` on
`fix/runtime-consistency-pr6a`. The audit reached both explicit stop
conditions in the PR6a brief:

- H-1 cannot be completed without introducing a typed execution artifact and
  planning seam for non-period grouping.
- M-1 cannot be durably contained without a new infrastructure-level session
  integrity status, persistence migration, store contract, and atomic
  commit/containment lifecycle.

No production workaround was applied. In particular PR6a does not manufacture
a fake `NativeExecutionPlan`, reuse turn reservations as quarantine, soft-delete
the session, put infrastructure health in semantic context/metadata, or add an
in-memory circuit breaker. The verified PR6 comparator is unchanged.

This is decision **D — both findings require broader architecture work**. The
branch is suitable for review of the audit and proposed follow-up boundary, but
H-1 and M-1 are not represented as resolved.

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

## PR6 modified files

- `src/balance_chat/runtime_consistency.py`
- `src/balance_chat/processor.py`
- `src/balance_chat/service.py`
- `tests/test_runtime_consistency.py`
- `docs/runtime_consistency_pr6.md`

PR6a modifies only this document. It makes no production or test-code change
because both requested fixes reached their explicit architectural stop
conditions.

## PR6a pre-execution grouping coverage matrix

`GroupingSpec.dimension` has exactly eight values in the current contract. The
matrix below uses the actual dispatch, planner, and row-adaptation code rather
than treating the contract enum as proof of execution support.

| Grouping dimension | NativeExecutionPlan available? | Comparator supports it? | Check invoked before execution? | Actual execution path |
|---|---:|---:|---:|---|
| period | yes | yes | yes | `_execute_grouping_mutation` forwards to `_execute_mutation`; native period-group plan and executor |
| geo | no | no (`NOT_COMPARABLE` if called) | no | cached result rows or `runtime.execute_raw`, then `member_facts_from_rows` and canonical aggregation |
| geo_group | no | no (`NOT_COMPARABLE` if called) | no | cached result rows or `runtime.execute_raw`, then row adaptation and canonical aggregation |
| balance | no | no (`NOT_COMPARABLE` if called) | no | cached result rows or `runtime.execute_raw`, then row adaptation and canonical aggregation |
| article | no | no (`NOT_COMPARABLE` if called) | no | cached result rows or `runtime.execute_raw`, then row adaptation and canonical aggregation |
| source | no | no (`NOT_COMPARABLE` if called) | no | cached/raw rows are obtained first; current row adapter then rejects the unsupported dimension |
| destination | no | no (`NOT_COMPARABLE` if called) | no | cached/raw rows are obtained first; current row adapter then rejects the unsupported dimension |
| route | no | no (`NOT_COMPARABLE` if called) | no | cached/raw rows are obtained first; current row adapter then rejects the unsupported dimension |

Before PR6a, 1/8 grouping dimensions had the pre-execution assertion. After
the STOP audit it remains 1/8. The other seven do receive the PR6
executed/committed/reloaded checks if a `TurnProcessResult` is produced, but do
not have Intent-to-Execution protection before raw analytical work.

### Mandatory H-1 table

| Path / dimension | Before PR6a | After PR6a | Assertion before execution? | Notes |
|---|---|---|---:|---|
| GROUP period | native typed plan | unchanged | yes | existing PR6 coverage is retained |
| GROUP geo | raw/cached rows | unchanged, not covered | no | no typed pre-execution artifact |
| GROUP geo_group | raw/cached rows | unchanged, not covered | no | no typed pre-execution artifact |
| GROUP balance | raw/cached rows | unchanged, not covered | no | no typed pre-execution artifact |
| GROUP article | raw/cached rows | unchanged, not covered | no | no typed pre-execution artifact |
| GROUP source | raw/cached rows, post-fetch rejection | unchanged, not covered | no | contract value is not current row-adapter support |
| GROUP destination | raw/cached rows, post-fetch rejection | unchanged, not covered | no | contract value is not current row-adapter support |
| GROUP route | raw/cached rows, post-fetch rejection | unchanged, not covered | no | contract value is not current row-adapter support |

A production-seam hostile test cannot corrupt a non-period grouping execution
artifact after normal construction because no such typed artifact is
constructed. Adding a `GroupingExecutionSpec` and making both cached-result and
raw-runtime branches consume it is planner/execution architecture work, not
comparator wiring. PR6a therefore does not add self-referential unit tests that
would imply coverage which production does not have.

## PR6a post-commit containment audit

The current service detects a mismatch after `store.commit`, raises it, avoids
the successful response cache, and releases the reservation. It does not leave
a durable health marker. An independent three-call probe using a fresh request
ID and the current revision for every call produced:

```text
errors          = [ExecutedCommittedConsistencyError] x 3
revisions       = [1, 2, 3]
processor calls = 3
commit calls    = 3
cache writes    = 0
release calls   = 3
reservations    = {}
```

This reproduces M-1: within-call containment works, while cross-turn durable
containment does not exist.

### Existing mechanism audit

| Candidate | InMemory | SQLite | PostgreSQL | Why it is not session integrity state |
|---|---|---|---|---|
| semantic context payload | persisted only with the conversation contract | persisted as `payload_json` | persisted as `payload` JSONB | integrity health is infrastructure state; adding it changes `ContextContractV2` and semantic schemas |
| `MetadataVersionRef` | typed bundle identity | typed bundle identity | typed bundle identity plus columns | not a dictionary and semantically identifies metadata compatibility, not session health |
| turn reservation | process-local map | durable table until release | durable table with ten-minute expiry | represents one in-flight turn; existing `finally` releases it and PostgreSQL expiry makes it temporary |
| request-result cache | process-local map | durable table | durable table | stores completed successful responses by request ID; it is neither session-wide nor a mutation gate |
| soft delete | removes state | hard delete | `deleted_at` only | deletion is unavailable consistently across stores, hides the session, and has no integrity/recovery semantics |
| service lock | process-local | process-local | process-local | cannot coordinate restarts or multiple workers |

No appropriate mechanism already exists. Reusing any row above would either be
in-memory-only, backend-asymmetric, temporary, or would conflate deletion,
idempotency, metadata compatibility, or user semantics with infrastructure
integrity.

### Mandatory M-1 table: current behavior after STOP

| Request | Revision before | Executed? | Committed? | Revision after | Result |
|---|---:|---:|---:|---:|---|
| first fault | 0 | yes | yes, once | 1 | post-commit consistency error; no cache write |
| retry/current rev | 1 | yes | yes, once | 2 | same error; no durable quarantine |
| next/current rev | 2 | yes | yes, once | 3 | same error; cycle can continue |

A retry with the original stale revision remains blocked by
`RevisionConflict`, but a request using the reloaded current revision is not.
The same failed request ID has no cache entry and is likewise able to re-enter
processing once paired with the current revision. A new request ID behaves the
same way.

## Minimal durable session-integrity design for a separate PR

The smallest production-safe design is infrastructure-level state, separate
from `ContextContractV2`:

1. Add a typed store-level `SessionIntegrityStatus` and distinct errors:
   `PostCommitConsistencyError` for the detecting turn and
   `SessionIntegrityError` for an already quarantined session.
2. Persist one integrity record per session, preferably in a dedicated
   `context_session_integrity_v2` table (with a SQLite equivalent and a
   reference InMemory implementation). Minimum fields are `session_id`,
   `status`, `detected_revision`, `fault_code`, bounded diagnostic hash,
   `detected_at`, and explicit recovery audit fields.
3. Make reservation/mutation admission check `status` atomically with the
   revision. PostgreSQL must check under the session row lock; SQLite must check
   inside `BEGIN IMMEDIATE`; the InMemory reference uses its existing lock.
4. Move commit verification into a store transaction boundary capable of
   persisting the bad revision and quarantine marker atomically, or rolling
   back the bad revision and persisting quarantine atomically. A separate
   best-effort `mark_fault()` after commit is insufficient: if it fails, the
   session again appears healthy.
5. Re-read and verify the persisted payload before that transaction completes,
   so committed/reloaded inconsistency is covered by the same atomic decision.
6. Check integrity before returning a request-ID cache hit. Otherwise an old
   cached analytical success can bypass quarantine. Do not cache generic
   integrity failures.
7. Reject quarantined sessions before processor invocation. Execution, commit,
   cache writes, and revision movement must remain zero for every blocked turn.

This requires a migration, store contract changes, service admission ordering,
and cross-store tests. Those are exactly the changes PR6a forbids introducing
silently.

### Durability and fault-marker ordering

The marker must be in the authoritative database for SQLite/PostgreSQL, so it
survives process restart and is visible to every worker. The InMemory version is
test/reference behavior only and must not be described as production durable.
The commit and marker must share one transaction; otherwise
`commit succeeded -> marker failed -> session looks healthy` remains possible.

### Recovery

No recovery is implemented in PR6a. The separate design should default to
manual/operator recovery after inspecting the failed revision and validating
state. Clearing quarantine must be an explicit audited store operation. There
is no automatic repair, rollback, silent reset, or session deletion. The
current system cannot roll back the already committed bad revision.

### HTTP behavior

Current PR6 consistency exceptions are not handled by the chat API's explicit
exception mapping and therefore surface as framework HTTP 500 failures. PR6a
does not change that mapping. A later containment PR should map both the
detecting fault and quarantined-session rejection to an infrastructure/session
integrity response, recommended HTTP 503 with a stable non-semantic error code,
not clarification or the normal user 422 path.

## Corrected known limitations

- Intent-to-Execution protection covers 1/8 grouping dimensions (`period`),
  not all grouping execution.
- `geo`, `geo_group`, `balance`, and `article` grouping execute through raw or
  cached rows without a comparable typed pre-execution artifact.
- `source`, `destination`, and `route` grouping can reach raw/cached row
  acquisition before the current adapter rejects the dimension.
- Post-commit detection prevents a successful response for that turn but does
  not quarantine the session. Correctly revisioned later turns may commit and
  fail repeatedly.
- The guard validates the typed native plan. Specialized full-balance physical
  execution and deterministic row filtering are not separately projected.
- Unified standalone execution has no `NativeExecutionPlan`; only the state
  invariant applies.
- Execution result to final prose (`AnswerClaim`) remains out of scope.
- Guard Spike G1-G6 and `SemanticTransitionProposal` remain shadow/research
  artifacts and are not imported or activated.

## PR6a mandatory answers

### H-1

1. **How many grouping dimensions exist? 8.**
2. **How many were pre-execution checked before PR6a? 1/8 (`period`).**
3. **How many are checked after PR6a? 1/8.** The STOP condition prevents a
   false wiring-only fix.
4. **Which remain uncovered?** `geo`, `geo_group`, `balance`, `article`,
   `source`, `destination`, and `route`.
5. **Why?** They construct no typed pre-execution artifact; execution uses
   cached/raw rows and post-execution row adaptation.
6. **Did fixing coverage require planner redesign? YES.** Therefore it was not
   implemented in PR6a.
7. **Did comparator semantics change? NO.**
8. **Can GEO grouping divergence reach execution? YES.** It is uncovered.
9. **Can business grouping divergence reach execution? YES** for current
   balance/article grouping paths; they are uncovered.
10. **Can source/destination grouping divergence reach execution? YES.** Rows
    may be acquired before the unsupported-dimension error; there is no
    pre-execution assertion.

### M-1

1. **What durable mechanism represents session integrity failure? None.**
2. **Did an appropriate mechanism already exist? NO.**
3. **Was `ContextContractV2.metadata` used? NO.** It is typed metadata-bundle
   identity and is architecturally unsuitable.
4. **Where is the fault stored? Nowhere; implementation stopped.** The proposed
   separate design uses infrastructure persistence, not semantic state.
5. **Is a marker durable across restart? NO marker exists.**
6. **Is it safe across multiple workers? NO durable containment exists.**
7. **Is any implemented containment in-memory-only? NO new containment was
   implemented.** Existing process locks are not represented as a fix.
8. **After the first mismatch, can a current-revision request execute? YES.**
9. **Can it commit? YES.**
10. **Can revision advance again? YES.** The probe showed `0 -> 1 -> 2 -> 3`.
11. **Can the same request ID re-execute? YES** when there is no cached success
    and the caller supplies the current revision.
12. **Can a new request ID re-execute? YES.**
13. **Can a consistency failure appear as success? NO for the detecting turn,**
    but the session is not durably blocked afterward.
14. **What is the recovery mechanism? None.** A separate design proposes
    explicit audited operator recovery.
15. **Is recovery automatic? NO.**
16. **Can the system roll back the already committed bad revision? NO.**
17. **Is that explicit? YES.**

### General

1. **Planner semantics changed? NO.**
2. **Executor semantics changed? NO.**
3. **Reducer semantics changed? NO.**
4. **Binder/interpreter semantics changed? NO.**
5. **New LLM calls? NO.**
6. **Semantic repair enabled? NO.**
7. **AnswerClaim implemented? NO.**
8. **Guard Spike G1-G6 productionized? NO.**
9. **Full pytest result: 603 passed, 1 pre-existing warning.**
10. **Test nodes added/removed: 0/0 relative to PR6; 603 collected.**
11. **Golden/P0/PR3/PR4: 8/8 validated, 84/84, 20/20, and 50/50.** The
    acceptance results are strict structural dry-runs (`DB execution: false`),
    not live service execution.
12. **Production differential:** PR6 evidence remains 0 semantic mismatches in
    the code-tested baseline suite; no live DB differential is claimed.
13. **Are H-1 and M-1 both resolved? NO. Decision D / STOP.**
14. **Ready for targeted Claude re-review? YES** for the STOP audit, corrected
    claims, and proposed architecture; **NO** as a completed H-1/M-1 fix.
