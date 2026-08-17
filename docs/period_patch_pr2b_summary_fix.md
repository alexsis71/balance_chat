# PR2b fix: deterministic period PATCH summary policy

## Root cause

PR2b suppressed LLM result summarization in the native scalar branch of
`_execute_mutation`, but `_execute_full_balance_mutation` owns an independent
`summarize_envelope` call. Full-balance, balance-section, and canonical
directed-flow intents route to that method before reaching the native scalar
summary guard.

The period mutation and execution results remained correct, but those three
reachable shapes still made one LLM summary call despite the documented
zero-LLM invariant.

The fix introduces one narrow policy helper:

```text
deterministic_period_patch → do not summarize
every other interpretation mode → preserve existing behavior
```

The helper is applied uniformly without changing execution routing or result
semantics.

## Summarizer call-site audit

Production has three calls to `runtime.summarize_envelope` in `processor.py`:

1. Native scalar execution in `_execute_mutation` is reachable for ordinary
   `SHOW` and `AGGREGATE` period patches. PR2b already guarded this branch; the
   inline comparison is now replaced by the shared helper.
2. `_execute_full_balance_mutation` is reachable for full-balance snapshots,
   balance sections, and directed flows. This was the residual unguarded call
   and is now governed by the same helper.
3. `_standalone` is not reachable after a period PATCH because a recognized
   patch enters `_dispatch_mutation`. The helper is nevertheless applied here
   so the policy is expressed consistently at every processor summary call.

`compat/pipeline_runtime.py` implements `summarize_envelope` by delegating to
the configured presentation model. It does not add another call site after
dispatch. Full-balance execution already passes `apply_summary=False` to its
source execution, so the processor call above was the only residual leak.

## Test fixture mismatch

The original Golden Transition fixture was translated into entity roles in the
order:

```text
[balance, article, destination]
```

Production deterministic directed-flow intents use:

```text
[balance, destination, article]
```

`_is_directed_flow_show` intentionally retains its existing exact-order
predicate in this fix. The misleading article was removed from the native
scalar Golden fixture, and the new directed-flow fixture explicitly uses and
asserts the production order. That test now enters
`unified_directed_flow`, the same branch used by production.

No entity-order normalization or routing change was made.

## LLM-call counts

All counts below use `execute_db=True` and a runtime that counts
`summarize_envelope` calls.

| Execution shape | Before fix | After fix |
|---|---:|---:|
| Native scalar period PATCH | 0 | 0 |
| Full-balance snapshot period PATCH | 1 | 0 |
| Balance-section period PATCH | 1 | 0 |
| Directed-flow period PATCH | 1 | 0 |
| Normal non-PATCH full-balance execution | 1 | 1 |

The contextual interpreter count remains zero for every successful
deterministic period PATCH. The non-PATCH regression test confirms that the
existing summarizer policy is unchanged for other modes.

## Regression results

PR2b baseline at `2ecf41ffbad9da860974c25752266f960fc697e3`:

```text
277 passed, 1 warning
0 failed
```

PR2b summary fix:

```text
282 passed, 1 warning
0 failed
```

Focused results:

- period PATCH suite: `34 passed`;
- period PATCH plus pipeline wiring, PR2a commit equivalence, and API:
  `140 passed, 1 warning`;
- Golden catalog validation: `8/8`;
- P0 acceptance dry-run: `7` scenarios, `84/84` automated checks.

The warning is the unchanged Starlette `httpx` deprecation warning. Live
DB-backed acceptance was not run because it has external-state and cleanup
side effects; the dry-run validates catalog coverage only.

## Scope confirmation

- The only production behavior change is suppression of LLM result summaries
  for `interpretation_mode="deterministic_period_patch"` on the previously
  leaking full-balance family.
- All other interpretation modes continue to request summaries under the same
  success, `execute_db`, and callable-runtime conditions as before.
- `_is_directed_flow_show` is unchanged.
- Planner, executor, reducer, binder, interpreter, contracts, API, stores, and
  PATCH semantics are unchanged.
- The only production non-empty PATCH category remains `periods=SET`.
- No GEO, entity, operation, grouping, comparison, formula, or ranking PATCH
  producer was introduced.
