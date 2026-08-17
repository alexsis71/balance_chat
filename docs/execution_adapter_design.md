# ExecutionAdapter PR1 design review

## Scope and baseline

PR1 is an infrastructure-only change on `feat/execution-adapter-pr1`, based on
`pre-stateful-analytics-baseline` (`790f3d702d37b6890e213054edcfd88d5c11eda3`).
It must decouple execution from the full-replacement representation without
adding a PATCH producer or changing user-visible behavior.

Baseline test result before PR1 production changes: `228 passed, 1 warning`.

## Current execution call graph

The production request path is:

1. `src/balance_chat/api.py::create_app.execute_turn()` handles
   `POST /api/v2/chat` and calls `BalanceChatService.execute_turn()`.
2. `src/balance_chat/service.py::BalanceChatService.execute_turn()` loads and
   revision-checks `ContextContractV2`, then calls
   `PipelineV2TurnProcessor.process()`.
3. `src/balance_chat/processor.py::PipelineV2TurnProcessor.process()` selects
   contextual or standalone/deterministic routing. Producers are the
   deterministic helpers and `InterpretationMutationCompiler.compile()`; they
   currently return full `ContextMutation(replace_intent=...)` values.
4. The interpreted path calls `_repair_incomplete_evidence()`. It validates the
   intent by directly reading `mutation.replace_intent` (current lines 819 and
   898) and logs the repaired operation through the same representation
   (current line 921).
5. `process()` and `_process_contextual()` choose grouping versus scalar
   execution by directly reading `mutation.replace_intent.grouping` and
   `.operation` (current lines 489 and 773).
6. `_execute_grouping_mutation()` directly assigns
   `intent = mutation.replace_intent` (current line 941), validates evidence,
   performs canonical grouping, and returns `TurnProcessResult`.
7. `_execute_mutation()` directly assigns `intent = mutation.replace_intent`
   (current line 2175), performs existing normalization and evidence
   validation, calls `NativeMultiOperandPlanner.plan(intent)`, validates the
   plan, and calls `NativeExecutor.execute(plan, ...)`. Full-balance routing is
   selected from that same intent.
8. `_reverse_no_data()` reads `mutation.replace_intent.operation` to build its
   result envelope (current line 1317). Role-separated producer post-processing
   reads the replacement at current line 1912, and its pre-execution logging
   reads it at current lines 1985 and 1988.
9. `src/balance_chat/service.py::BalanceChatService.execute_turn()` receives the
   `TurnProcessResult`, then calls `store.commit(..., processed.mutation, ...)`.
10. Each store implementation in `src/balance_chat/store.py` calls
    `reducer.apply_context_transition()`. That function calls
    `reduce_intent(state, mutation)` again and persists the resulting active,
    attempted, and successful scopes plus the mutation-derived turn frame.

Current coupling therefore exists before evidence validation, at the
grouping/scalar decision, inside both execution paths, and in the no-data
response path.

## Proposed call graph

```text
producer
  -> ContextMutation
  -> PipelineV2TurnProcessor._dispatch_mutation()
  -> ExecutionAdapter.effective_intent(state, mutation)
  -> effective AnalysisIntent
  -> evidence validation / evidence repair
  -> grouping or scalar routing
  -> existing normalization
  -> existing planner
  -> existing executor
  -> TurnProcessResult(original mutation representation, preserving existing
     normalization synchronization)
  -> BalanceChatService
  -> store.commit
  -> reduce_intent
```

`ReducerExecutionAdapter` delegates mutation semantics to `reduce_intent()`.
The adapter adds only execution-boundary fail-closed validation for an empty
mutation (`replace_intent is None` and every patch field is unset), because the
reducer intentionally treats that mutation as reuse of the active intent.

Every executable mutation is materialized through one processor helper. If an
evidence repair returns a different mutation, that new mutation is materialized
once and replaces the rejected materialization.

Existing execution-time normalization currently synchronizes the normalized
intent back into the full replacement mutation so commit reduces to the intent
that was executed. PR1 preserves that behavior and re-materializes the
synchronized mutation before planning; if an existing non-empty patch would
make commit reduce to a different intent, execution fails closed with
`ContextReductionError`. This verifies the invariant:

```text
executed effective intent == committed effective intent
```

## Production files to modify

| File | Reason | Expected change | Risk |
| --- | --- | ---: | --- |
| `src/balance_chat/execution_adapter.py` | Add the protocol and thin reducer-backed implementation with fail-closed empty-mutation validation. | ~30 lines | Low: isolated delegation boundary. |
| `src/balance_chat/processor.py` | Inject the adapter, centralize materialization/dispatch, and replace execution reads with an explicit `effective_intent`. | Small, localized edits across existing dispatch and execution methods | Medium: many routes converge in this large file. |
| `src/balance_chat/bootstrap.py` | Construct `ReducerExecutionAdapter` in the composition layer and inject it into the processor. | 2-4 lines | Low: wiring only. |

Test and documentation files are modified separately. No contract, reducer,
planner, executor, service, store, or API production file is planned for change.

## Blast radius

- **Routing:** contextual/standalone and evidence-gate routing are unchanged.
  Only grouping-versus-scalar dispatch reads the materialized intent.
- **Interpretation:** interpreter calls, prompts, modes, retries, and error
  mapping are unchanged.
- **Binding:** compiler and binder semantics are unchanged. Existing producers
  remain full replacements.
- **Grouping:** algorithm, source selection, aggregation, result shape, and
  normalization are unchanged; only the input intent source changes.
- **Scalar execution:** planning and execution algorithms are unchanged; only
  the input intent source changes.
- **Normalization:** existing normalizers and their ordering are unchanged.
  Existing replacement synchronization is preserved for commit equivalence.
- **Commit:** the service still commits `TurnProcessResult.mutation` through the
  selected store.
- **Reducer:** `reduce_intent()` and `apply_context_transition()` are unchanged
  and remain the only source of mutation semantics.
- **Store:** InMemory, SQLite, and PostgreSQL implementations are unchanged.

## Direct `replace_intent` dependency audit before PR1

### Production

| Location | Classification | Explanation |
| --- | --- | --- |
| `reducer.py:90-91` | OTHER | Canonical reducer semantics; intentionally remains. |
| `binding.py:312,509` | PRODUCER | Interpretation compiler creates full replacement mutations. |
| `conversation.py:120` | PRODUCER | Conversation helper creates a full replacement mutation. |
| `processor.py:489,773` | EXECUTION DEPENDENCY | Grouping/scalar routing reads the concrete representation. |
| `processor.py:819,898` | EXECUTION DEPENDENCY | Evidence validation reads the concrete representation. |
| `processor.py:921` | LOGGING | Evidence repair operation log reads the replacement. |
| `processor.py:941` | EXECUTION DEPENDENCY | Grouping execution takes its intent from the replacement. |
| `processor.py:1317` | EXECUTION DEPENDENCY | No-data response operation reads the replacement. |
| `processor.py:1912` | PRODUCER | Role-separated deterministic post-binding edits its full replacement producer output. |
| `processor.py:1985,1988` | LOGGING | Role-separated producer resolution log reads operation and periods. |
| `processor.py:2175` | EXECUTION DEPENDENCY | Scalar execution takes its intent from the replacement. |

Occurrences such as `replace_intent=...` and
`update={"replace_intent": ...}` are producer construction or existing
normalization synchronization, not direct reads. They remain replace-only in
PR1. Test occurrences are classified as **TEST**.

## Stop condition review

The proposed implementation does **not** require changes to `reduce_intent`,
the `AnalysisIntent` contract, planner, executor, stores, or API. The design is
therefore **NOT BLOCKED** and implementation may proceed with the bounded
production-file list above.
