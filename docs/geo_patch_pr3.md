# PR3: deterministic GEO PATCH

## Supported GEO forms

PR3 recognizes only a complete, short replacement follow-up with an active
analytical scope:

- `А по Самарской области?`
- `А по Москве?`
- `А по Татарстану?`
- `А по Ростовской?`
- `А для Германии?`
- the same forms without the initial `А`

The payload after `по` or `для` must resolve as one complete canonical GEO
mention. Exact canonical labels and metadata aliases are preferred; the
existing lemma normalizer and the existing bounded GEO matcher provide
morphology and their existing deterministic fuzzy tolerance. If the complete
payload does not match one record, recognition returns `None`.

The active intent must have one operand, operation `SHOW` or `AGGREGATE`, and
metric `distribution` or `export`. The operand must already contain exactly one
`destination` of type `geo_object`. Comparison, formula, ranking, grouping,
multiple operands, source roles, and route roles are excluded.

For a directed `distribution` carrying `[balance, destination, article]`, PR3
also requires one unambiguous existing metadata article for the new GEO under
the same balance. It replaces destination and article in their existing list
positions. A scalar operand without an article replaces only destination.
`balance` snapshots and `balance_section` intents are not independent GEO
dimensions and fall back. An article-bearing `export` also falls back.

## Unsupported and mixed forms

The conservative grammar rejects full, mixed, plural, explanatory, grouping,
ranking, and comparison turns. Examples include:

- `А по Москве за апрель?`
- `Сравни с Самарской областью`
- `А Москва и Самара?`
- `Покажи максимум по Москве`
- `Почему в Москве меньше?`
- `Покажи по всем областям`
- `Покажи экспорт в Германию`

Period recognition remains first. For the critical overlap
`А по Москве за апрель?`, both deterministic detectors return `None`; the
existing contextual interpreter is invoked once.

## Canonical GEO resolution and business collision protection

The resolver path is:

```text
whole follow-up payload
→ existing lemma normalization
→ existing `_tagged_geo_objects`
→ exactly one metadata GEO record
→ whole-payload verification
→ canonical GEO ID/display name
```

Before GEO lookup, the entire payload is checked with the existing metadata
balance lookup. This gives a known business entity priority over a geographic
substring. Tests reject all of:

- `А для ГП ТГ Москва?`
- `А по Газпром трансгаз Москва?`
- `А для ТГ Ухта?`
- `А по ГП ТГ Нижний Новгород?`

No business-entity PATCH was added.

The current ready bundle (`2026.08.1`) resolves the real phrase
`А по Самарской области?` to
`geo:fdf2b36b839e08399ef3` / `самарская обл`. It does not contain a GEO record
for Poland, although article metadata contains Poland-related articles. Thus
`А по Польше?` safely falls back with the current production bundle. The
country-transition capability is covered using canonical Germany and Poland
records in an isolated contract fixture; PR3 does not invent a missing
production GEO or add a private alias catalog.

## PATCH representation

`IntentPatch` has no top-level `geo` field. PR3 therefore uses its existing
list-field representation without changing contracts:

```text
ContextMutation(
    replace_intent=None,
    patch=IntentPatch(
        operands=FieldMutation(
            action=SET,
            value=[active_operand_with_new_canonical_destination],
        )
    ),
)
```

Conceptually this is the production category `GEO=SET`; physically it is
`operands=SET`. The producer deep-copies the single operand, retains every
non-GEO field and entity position, and changes only destination plus the
required canonical direction article. Reducer semantics are unchanged.

## Control flow and validation

```text
active context
→ deterministic period detector (first)
→ deterministic GEO detector
→ ContextMutation(operands=SET)
→ ExecutionAdapter / reduce_intent
→ existing evidence gate with the canonical GEO record
→ existing planner
→ existing executor
→ original mutation in TurnProcessResult
→ existing store commit/reload
```

The existing evidence gate verifies that the newly resolved GEO is present in
the effective intent. The native scalar test captures the real rendered
execution query and verifies that it contains Samara and no longer contains
Rostov. The production-order directed-flow test verifies that execution uses
the rebound Samara article rather than the previous Rostov article.

PR2a synchronization is unchanged: unchanged normalization preserves the
original patch; changed normalization still produces a normalized replacement
and empty patch through the existing synchronization boundary.

## LLM policy

There is one `deterministic_geo_patch` producer. It executes before the
unconditional contextual interpreter. The existing `_should_summarize` helper
now contains the two explicit no-summary modes:

```text
deterministic_period_patch → false
deterministic_geo_patch    → false
all other existing modes  → unchanged
```

All three processor summarizer sites use this helper. A successful GEO PATCH
therefore invokes neither the interpreter nor the summarizer. Tests cover the
native scalar branch and the reachable production-order directed-flow branch.
Full-balance snapshot and balance-section turns are not recognized as GEO
PATCHes, so they cannot leak a GEO PATCH summary call.

## Sequential period → GEO → period transition

The test and the dedicated acceptance catalog execute this transition:

| Turn | Interpretation mode | LLM calls | Period | GEO | Operation | Execution layer | Revision |
|---|---|---:|---|---|---|---|---:|
| Q1 `Покажи ... Ростовскую область за май 2025` | `standalone` | 0 in dry-run fixture | May 2025 | Rostov | `show` | `unified_strict` | 1 |
| Q2 `А за апрель?` | `deterministic_period_patch` | 0 | April 2025 | Rostov | `show` | `native_deterministic` | 2 |
| Q3 `А по Самарской области?` | `deterministic_geo_patch` | 0 | April 2025 | Samara | `show` | `native_deterministic` | 3 |
| Q4 `А за май?` | `deterministic_period_patch` | 0 | May 2025 | Samara | `show` | `native_deterministic` | 4 |

At Q3, the materialized intent used by the planner equals the reduced intent,
the committed active intent, and the reloaded active intent. Q4 proves that the
subsequent period PATCH inherits Samara rather than restoring Rostov.

The catalog is `acceptance/geo_patch_pr3_transition.feature`. Its strict
dry-run covers one four-turn scenario with `20/20` automated checks and no
coverage gaps.

## False-positive audit

The focused suite contains 55 parameterized adversarial phrases plus separate
business, mixed-turn, unsupported-operation, unsupported-shape, missing-GEO,
and fallback tests. Categories include comparison, ranking, grouping,
explanation, GEO plus period, multiple GEOs, business names containing GEOs,
routes, complete new queries, ambiguous references, GEO groups, and unknown
locations. No adversarial phrase produced a partial GEO PATCH.

## Test results

Baseline after PR2b (`197fbf2c150dd62ca46262178fe8d14f6fcce4c5`):

```text
282 passed, 1 warning
0 failed
```

PR3:

```text
full pytest:                  383 passed, 1 warning
focused GEO PATCH:           101 passed
period + GEO PATCH:          135 passed
Golden catalog validation:  8/8
existing P0 dry-run:         7 scenarios, 84/84 checks
PR3 transition dry-run:      1 scenario, 20/20 checks
false-positive attack set:   55 phrases, 0 partial PATCHes
```

The warning is the unchanged Starlette `httpx` deprecation warning. The delta
is `+101 passed` and no new failures. Live DB-backed acceptance was not
executed because it has external state and cleanup side effects.

## Production PATCH audit

Repository-wide production occurrences classify as follows:

1. `contracts.py`: existing `FieldMutation`, `IntentPatch`, and SET contract.
2. `reducer.py`: existing generic SET application.
3. `processor.py`: existing PR2a normalization reset to empty `IntentPatch()`.
4. `processor.py`: PR3 `operands=SET` producer (`GEO=SET`).
5. `processor.py`: PR2b `periods=SET` producer.

Production non-empty PATCH categories are exactly:

1. `periods=SET`
2. `GEO=SET` represented by `operands=SET`

The older `_deterministic_geo_mutation` full-replacement compatibility method
was not modified or removed. Its call is after the no-active-scope branch while
the method requires an active scope, so it is not the PR3 production producer.

## Scope confirmation and known limitations

Modified production code is limited to `processor.py`. The acceptance catalog,
focused tests, and this document are the only other files. Planner, executor,
reducer, binder, interpreter, prompts, API, stores, and contracts are unchanged.
No generic classifier/router and no business, route, grouping, comparison,
ranking, or operation PATCH was added.

Known limitations are deliberate safe fallbacks: multiple operands, complex
operations, source/route semantics, full-balance and balance-section shapes,
non-unique article rebinding, missing canonical GEO records, and GEO groups.
