# PR2b: deterministic period replacement PATCH

## Supported follow-ups

PR2b recognizes a deliberately small class of pure period replacements:

- `А за апрель?`
- `А за апрель 2024?`
- `За февраль?`
- `Покажи за июнь`
- equivalent Russian month names in grammatical forms already covered by the
  existing month patterns
- an explicit named calendar day with an explicit year, for example
  `А за 1 апреля 2025?`

Recognition requires an active `SHOW` or `AGGREGATE` intent with exactly one
operand, exactly one global period, and no operand-local periods. A pending
clarification also prevents interception.

## Unsupported and ambiguous follow-ups

The deterministic handler returns `None` for mixed or ambiguous requests,
including:

- `Сравни с апрелем`
- `А по Москве за апрель?`
- `Покажи максимум за апрель`
- `Почему в апреле меньше?`
- `А апрель и май?`
- `За какой апрель?`
- `Покажи апрель по месяцам`

These turns continue through the existing contextual path. Active operations
`COMPARE`, `COMPARE_PERIODS`, `CALCULATE`, `RANK`, `GROUP`, and `MULTI_STEP`
are intentionally excluded.

Relative forms such as `за прошлый месяц` and `за предыдущий месяц` are out of
scope. No existing parser provides the required active-state-relative contract,
so PR2b does not add an ad-hoc interpretation.

## Year inheritance

A month without an explicit year inherits the year of the active global
period. It never uses the machine date or the current turn date. Thus May 2025
followed by `А за апрель?` resolves to April 2025.

An explicit year overrides the inherited year. January 2025 followed by
`А за декабрь?` resolves to December 2025 under the same-year rule; PR2b does
not infer December 2024.

Months use the existing half-open canonical representation. April 2025 is
`[2025-04-01, 2025-05-01)`. The supported explicit-day form also reuses the
existing deterministic day parser and produces a one-day half-open period.

## PATCH construction

The producer creates a patch-only mutation:

```text
ContextMutation(
    replace_intent=None,
    patch=IntentPatch(
        periods=FieldMutation(
            action=MutationAction.SET,
            value=resolved_periods,
        )
    ),
)
```

Standard turn metadata is retained. No empty or KEEP-only mutation is emitted.
All operation, metric, operand, entity, GEO, route, grouping, formula, ranking,
and comparison fields come from the active intent through `reduce_intent()`.

## LLM call policy

Successful deterministic period replacement performs zero LLM calls:

- the contextual interpreter is not invoked;
- optional LLM result summarization is not requested for
  `deterministic_period_patch`.

The baseline active-context path invoked the contextual interpreter once for
`А за апрель?`. PR2b counter-based integration coverage confirms zero
interpreter calls and zero summary calls for the same successful follow-up with
database execution enabled. Ambiguous input still invokes the contextual
interpreter once.

## Execution path

```text
deterministic period recognizer
→ ContextMutation(periods=SET)
→ ExecutionAdapter
→ effective AnalysisIntent
→ existing normalization/synchronization
→ existing evidence gate
→ existing planner
→ existing executor
→ commit/reload
```

The PR2a rule remains unchanged. No-op normalization preserves the original
patch; changed normalization would collapse it to a normalized replacement and
empty patch. The PR2b Golden Transition exercises the real no-op branch and
commits the original period patch.

## Golden Transition

```text
Initial:   Покажи распределение газа в Ростовскую область за май 2025
Follow-up: А за апрель?
```

The initial canonical state is produced by the existing standalone pipeline
translation path. The follow-up produces `periods=SET April 2025`, makes zero
LLM calls, and preserves the operation, metric, balance/article bindings, GEO
`Ростовская область`, direction, entities, and other intent fields. The exact
planner input equals the committed and reloaded active intent. Revision advances
from 1 to 2.

## Test results

Baseline `architecture/stateful-analytics` at
`e11dc256036502ba54f36dd98d858531834b5574`:

```text
248 passed, 1 warning
0 failed
```

PR2b full suite:

```text
277 passed, 1 warning
0 failed
```

Focused Golden, acceptance-runner, API, period PATCH, ExecutionAdapter, and
normalization/commit suite:

```text
89 passed, 1 warning
0 failed
```

Additional checks:

- period PATCH suite: `29 passed`;
- Golden catalog validation: `8/8` queries;
- P0 acceptance dry-run: `7` scenarios and `84/84` automated checks covered;
- live DB-backed acceptance was not run because it has external state and
  cleanup side effects; the in-repository runner tests are included above.

The warning is the pre-existing Starlette `httpx` deprecation warning.
Regression delta: `+29 passed`, no new failures.

## Production PATCH usage audit

Production occurrences after PR2b are:

- `contracts.py`: declarations and SET validation;
- `reducer.py`: existing SET application semantics;
- `processor.py`: PR2a's empty `IntentPatch()` normalization reset;
- `processor.py`: the single PR2b producer constructing
  `IntentPatch(periods=FieldMutation(action=SET, ...))`.

There are no production GEO, entity, operation, grouping, comparison, formula,
or ranking PATCH producers.

## Scope and compatibility

Modified production code is limited to `processor.py`. No planner, executor,
reducer, contract, store, API, interpreter, or binder semantics changed. No
required API field was added, and clients receive the existing response shape.
New standalone requests and all non-matching contextual turns retain their
existing routing.
