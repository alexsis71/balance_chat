# ExecutionAdapter PR1 report

## Baseline and branches

- Baseline commit: `790f3d702d37b6890e213054edcfd88d5c11eda3`
- Annotated tag: `pre-stateful-analytics-baseline`
- Architecture branch: `architecture/stateful-analytics`
- Feature branch: `feat/execution-adapter-pr1`

Both branch ancestry checks pass: the architecture branch points at the
baseline, and the feature branch descends from the architecture branch.

## Modified files

### Production

- `src/balance_chat/execution_adapter.py`
- `src/balance_chat/processor.py`
- `src/balance_chat/bootstrap.py`

### Tests

- `tests/test_execution_adapter.py`
- `tests/test_execution_commit_equivalence.py`
- `tests/test_pipeline_wiring.py`

### Documentation

- `docs/execution_adapter_design.md`
- `docs/execution_adapter_pr1.md`

## What changed

- Added the `ExecutionAdapter` protocol and thin `ReducerExecutionAdapter`.
- Delegated all mutation semantics to the existing `reduce_intent()`.
- Added fail-closed rejection for an empty executable mutation so an active
  intent cannot be silently re-executed.
- Injected the adapter into `PipelineV2TurnProcessor`; `bootstrap.py` constructs
  the production instance explicitly. Constructor defaulting is retained for
  existing unit fixtures.
- Added one materialization helper and one dispatcher. Evidence validation,
  grouping/scalar routing, no-data response construction, normalization,
  planning, execution, and behavior-affecting logging now consume an explicit
  effective `AnalysisIntent`.
- Preserved the original `ContextMutation` in `TurnProcessResult` whenever no
  existing normalization changes the effective intent. Existing normalization
  synchronization remains unchanged for current replace-only producers.
- Added a fail-closed post-normalization check: execution does not proceed if a
  mutation would reduce to a committed intent different from the normalized
  intent about to be executed.

## What did NOT change

- **routing:** contextual, deterministic, and standalone route selection is
  unchanged.
- **interpreter:** invocation, prompts, decisions, retry, and error mapping are
  unchanged.
- **binder:** `InterpretationMutationCompiler` and canonical binding semantics
  are unchanged.
- **reducer:** `reduce_intent()` and `apply_context_transition()` are unchanged.
- **planner:** no planner code or semantics changed.
- **executor:** no executor code or semantics changed.
- **API:** endpoints, request/response contracts, and error mapping are
  unchanged.
- **stores:** InMemory, SQLite, and PostgreSQL stores are unchanged.
- **PATCH producers:** none were added; all production producers remain full
  replacements.

## Test results

### Before PR1

```text
conda run -n ai_env python -m pytest -q
228 passed, 1 warning
```

There were no baseline failures.

### After PR1

```text
conda run -n ai_env python -m pytest -q
238 passed, 1 warning
```

The ten added tests account for the increase from 228 to 238; there are no new
failures.

```text
conda run -n ai_env python -m pytest tests/test_execution_adapter.py tests/test_execution_commit_equivalence.py tests/test_reducer.py tests/test_interpretation.py tests/test_pipeline_wiring.py tests/test_api.py tests/test_golden_queries.py tests/test_acceptance_runner.py -q
146 passed, 1 warning
```

Relevant suites include reducer, interpretation, processor wiring, API, Golden
Queries, and acceptance-runner tests. A live acceptance run against an external
deployed API/database was not performed because it is outside the isolated test
environment; the in-repository acceptance-runner suite is included.

The added tests cover:

- replacement-only materialization;
- replacement plus patch reducer precedence;
- missing base and reducer error propagation;
- fail-closed empty mutation;
- state and mutation immutability;
- replace-only planner input and `TurnProcessResult.mutation` preservation;
- patch-only scalar and grouping dispatch;
- patch-only execution, store commit, and reload equivalence;
- fail-closed normalization when a retained patch would make commit diverge.

## Behavioral equivalence

`OBSERVABLE BEHAVIOR CHANGE = NO`.

The interpreter, routing and binding suites remain green. Planner and executor
files are unchanged. API and Golden Query suites remain green. Existing
normalization ordering, result envelopes, SQL-producing execution plans,
persisted state, and replace-only mutation journaling expectations are
unchanged. The full regression suite has no new failures.

## Direct `replace_intent` dependency audit

Remaining production direct reads are:

| Location | Classification | Reason it remains |
| --- | --- | --- |
| `execution_adapter.py` | OTHER | The materialization boundary checks only whether the executable mutation is empty before delegating to the reducer. |
| `reducer.py` | OTHER | Canonical mutation semantics; this is the required single source of truth. |
| `processor.py::_enforce_role_separated_entities()` | PRODUCER | This deterministic producer post-processes its own full replacement before dispatch. It is not an execution dependency. |

There are no remaining **EXECUTION DEPENDENCY** reads in processor dispatch,
evidence validation, grouping, scalar planning/execution, no-data handling, or
operation-dependent logging. Other `replace_intent=...` occurrences are current
full-replacement producers or existing normalization synchronization. Test
reads are classified as **TEST**.

## Mandatory verification answers

### Question 1

Is the execution path fully decoupled from `mutation.replace_intent`?

```text
YES
```

The only processor direct read is in a full-replacement producer
post-processing helper, before the execution boundary.

### Question 2

Did observable behavior change?

```text
NO
```

### Question 3

Was any production `IntentPatch` or `FieldMutation` producer added?

```text
NO
```

### Question 4

Can the execution pipeline accept a patch-only `ContextMutation` without
planner or executor changes?

```text
YES
```

The reducer-backed adapter materializes a patch-only mutation over the active
scope, after which the unchanged planner and executor receive the effective
intent/plan. Scalar and grouping capability tests cover this. No production
producer emits such a mutation in PR1. If an existing execution-time
normalization could make the committed intent diverge, the boundary fails
closed instead of executing inconsistent state.

### Question 5

Is `executed effective intent == committed effective intent` confirmed?

```text
YES
```

The test starts from persisted `state0`, materializes a patch-only mutation,
executes it through the unchanged planner/executor, commits the original
mutation, reloads the store, and compares both committed and reloaded active
intents with the effective intent used before execution. A separate negative
test verifies fail-closed normalization on potential divergence.
