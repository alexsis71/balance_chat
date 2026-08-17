# PR2a: PATCH and normalization semantics

## Problem

Execution materializes a `ContextMutation` before normalization. If normalization
then changes that effective intent, retaining the original patch beside a
normalized `replace_intent` would cause the reducer to apply the same patch a
second time during commit. The intent used for execution could therefore differ
from the committed active intent, or the commit could fail closed.

## Chosen semantics

- When `normalized_effective_intent == previous_effective_intent`, preserve the
  original mutation unchanged. A patch-only mutation remains patch-only.
- When normalization changes the effective intent, copy the original mutation,
  preserve its turn metadata, set `replace_intent` to the normalized intent, and
  reset `patch` to an empty `IntentPatch()`.
- Validate the collapsed mutation through the existing execution adapter before
  proceeding. Normalization collapse itself is not error control flow.

This reconciliation remains at the processor/execution boundary. The reducer is
unchanged and remains the single source of truth for mutation semantics.

## Trade-off

Normalization intentionally collapses PATCH provenance for that turn. This
keeps the rule deterministic, avoids a generic intent diff compiler, and
guarantees that execution and commit use the same normalized intent.

## PR2 readiness

The pipeline can accept a future period patch without planner or executor
changes. If normalization leaves the materialized intent unchanged, the patch
is committed as-is. If normalization changes it, commit receives the canonical
normalized replacement with no patch left to reapply.

No production non-empty PATCH producer is introduced by PR2a.

## Verification

Baseline on `architecture/stateful-analytics` at
`5bcc74505bfb233ecf31f6811e665c25bbf38875`:

```text
243 passed, 1 warning
0 failed
```

PR2a focused synchronization and execution/commit tests:

```text
9 passed
```

PR2a adapter, reducer, pipeline wiring, API, and execution/commit regression
tests:

```text
124 passed, 1 warning
0 failed
```

Full suite on PR2a:

```text
248 passed, 1 warning
0 failed
```

The five additional passing tests are the net result of replacing one older
normalization guard test with the six required PR2a scenarios.

Repository-wide constructor audit:

- `src/balance_chat/contracts.py` contains the `IntentPatch` and
  `FieldMutation` class declarations.
- `src/balance_chat/processor.py` constructs only an empty `IntentPatch()` to
  clear an already-consumed patch after normalization.
- Non-empty `IntentPatch` and all `FieldMutation` construction remain confined
  to tests.
- No production PATCH producer exists.

Protected-file audit against the baseline found no changes to `planning.py`,
`execution.py`, `reducer.py`, `contracts.py`, `store.py`, `api.py`,
`interpretation.py`, or `binding.py`.

## Self-review

- The change is limited to synchronization semantics in `processor.py`, focused
  tests, and this document.
- No planner, executor, reducer, contract, store, API, interpreter, binder, or
  routing semantics are changed.
- The original mutation object is returned on an unchanged normalization.
- A changed normalization always produces a normalized replacement plus an
  empty patch, independent of which field normalization changed.
- The collapsed mutation is rematerialized through `ExecutionAdapter`, ensuring
  it commits to the same intent that execution receives.
