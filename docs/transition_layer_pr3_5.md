# PR3.5: deterministic transition layer

## 1. Purpose

PR3.5 extracts the already-proven deterministic Period PATCH and GEO PATCH
recognition/production semantics from `processor.py`. It does not add a new
transition, request form, route, LLM call, or user-facing capability.

After the refactor, `processor.py` knows that one deterministic transition was
selected and receives its typed mutation, mode, evidence, and logging fields.
It does not know how Period or GEO transitions are recognized.

## 2. Baseline

The actual integration branch had not yet absorbed PR3, so the feature branch
was created directly from the reviewed PR3 HEAD without merging into the
integration branch:

```text
branch:   refactor/transition-layer-pr3_5
baseline: 89819145efe26f60dd3a9d2b61297f9f1c13be97
status:   clean
pytest:   383 passed, 1 warning
```

The warning is the pre-existing Starlette/httpx deprecation warning.

## 3. Pre-refactor call paths

### Period PATCH

```text
PipelineV2TurnProcessor._process_contextual
→ _deterministic_period_patch
→ _detect_period_followup
→ month/day parsing and active-year inheritance
→ ContextMutation(periods=SET)
→ interpretation_mode="deterministic_period_patch"
→ _dispatch_mutation
→ ExecutionAdapter.effective_intent
→ existing normalization/synchronization
→ evidence gate
→ planner/executor
→ existing summary policy
→ TurnProcessResult(original or synchronized mutation)
→ BalanceChatService.process_turn
→ store.commit
```

### GEO PATCH

```text
PipelineV2TurnProcessor._process_contextual
→ _deterministic_geo_patch
→ _detect_geo_followup
→ existing _tagged_geo_objects resolver
→ qualified business collision protection
→ canonical destination replacement
→ optional unique direction-article rebinding
→ ContextMutation(operands=SET)
→ interpretation_mode="deterministic_geo_patch"
→ the same _dispatch_mutation / execution / commit path
```

Recognition, candidate parsing, mutation construction, and mode selection were
transition responsibilities. Materialization, normalization, validation,
execution, summarization, persistence, and revision handling were shared
execution responsibilities and remain outside the new package.

## 4. New module structure

```text
src/balance_chat/transitions/
├── __init__.py       public transition seam
├── types.py          TransitionKind and TransitionDecision
├── classifier.py     period-first PATCH/NO_MATCH selection
├── period.py         period recognition, parsing, inheritance, mutation
└── geo.py            GEO recognition, canonical orchestration, mutation
```

No transition module imports `processor.py`; therefore the dependency remains:

```text
processor → transitions → contracts / injected metadata services
```

There is no engine, registry framework, DSL, graph, plugin mechanism, or
generic semantic router.

## 5. TransitionDecision contract

`TransitionKind` has only:

```text
PATCH
NO_MATCH
```

`TransitionDecision` contains:

- the original `ContextMutation` for a match;
- the unchanged production interpretation mode;
- canonical GEO evidence when applicable;
- the existing structured-log event name and fields.

It intentionally does not introduce `NEW`, `REINTERPRET`, or `CLARIFY` routes.
`NO_MATCH` means that processor continues through the exact pre-PR3.5 path.

## 6. Period flow

`transitions/period.py` owns:

- the unchanged month/day forms;
- the unchanged active-state shape checks;
- explicit-year parsing and active-year inheritance;
- mixed-turn rejection through the same full-match grammar;
- construction of the same patch-only `periods=SET` mutation;
- `deterministic_period_patch` mode production.

The explicit-day parser and shared month/season parsing helpers moved with the
period-specific semantics. Existing non-PATCH comparison code calls these
helpers but does not duplicate the month patterns in processor.

## 7. GEO flow

`transitions/geo.py` owns:

- the unchanged short `по` / `для` grammar;
- active operation, metric, operand, and entity-shape checks;
- whole-mention and morphology verification;
- qualified `ТГ` / `Газпром трансгаз` business collision protection;
- canonical destination replacement;
- optional unique direction-article rebinding;
- construction of the same patch-only `operands=SET` mutation;
- `deterministic_geo_patch` mode production.

It does not implement a second metadata resolver. Processor injects the
existing tagged-GEO resolver, balance lookup, direction-article resolver, and
canonical entity factory through the small `GeoTransitionServices` dependency
bundle. Canonical metadata and binder semantics remain unchanged.

## 8. Processor integration seam

`_process_contextual` now performs one call:

```text
detect_deterministic_transition(...)
```

For `PATCH`, it passes the returned mutation, mode, and evidence to the
existing `_dispatch_mutation`. For `NO_MATCH`, it proceeds to the existing
direction comparison, flow, balance, evidence-gate, and contextual interpreter
paths without reordering them.

The classifier preserves the proven order:

```text
pending/answered clarification → NO_MATCH
period transition
GEO transition
NO_MATCH
```

Independent signal evaluation was not introduced because behavioral
equivalence has priority. In particular, `А по Москве за апрель?` still matches
neither transition.

## 9. Behavioral equivalence

The same existing E2E tests were run before and after extraction. New tests do
not replace them. The representative classifier matrix contains 30 turns; the
sequential E2E and execution-shape tests add the state/execution assertions that
are not meaningful for `NO_MATCH` rows.

| # | Representative turn/state | Pre → post mode | Mutation/effective intent | LLM / execution / final state |
|---:|---|---|---|---|
| 1 | `А за апрель?` | period → period | identical `periods=SET`, April | 0; same layer; same state |
| 2 | `А за январь 2025?` | period → period | identical explicit-year SET | 0; same layer/state |
| 3 | `За февраль?` | period → period | identical inherited-year SET | 0; same layer/state |
| 4 | `Покажи за июнь` | period → period | identical inherited-year SET | 0; same layer/state |
| 5 | `А за декабрь?` | period → period | identical year boundary | 0; same layer/state |
| 6 | `А за 1 апреля 2025?` | period → period | identical one-day range | 0; same layer/state |
| 7 | `А по Самарской области?` | GEO → GEO | identical canonical operands SET | 0; same layer/state |
| 8 | `А по Москве?` | GEO → GEO | bare Москва remains GEO | 0; same layer/state |
| 9 | `А по Татарстану?` | GEO → GEO | identical canonical SET | 0; same layer/state |
| 10 | `А по Ростовской?` | GEO → GEO | identical alias SET | 0; same layer/state |
| 11 | `А для Германии?` | GEO → GEO | identical country SET | 0; same layer/state |
| 12 | `А по Польше?`, canonical fixture | GEO → GEO | identical country SET | 0; same layer/state |
| 13 | Rostov/May → April | period → period | Rostov/April | 0; executed=committed=reloaded |
| 14 | Rostov/April → Samara | GEO → GEO | Samara/April | 0; executed=committed=reloaded |
| 15 | Samara/April → May | period → period | Samara/May | 0; executed=committed=reloaded |
| 16 | `А по Москве за апрель?` | NO_MATCH → NO_MATCH | none | same contextual fallback |
| 17 | `Сравни с Самарской областью` | NO_MATCH → NO_MATCH | none | same instrumented interpreter call |
| 18 | `А по ГП ТГ Москва?` | NO_MATCH → NO_MATCH | none | same fallback/final state |
| 19 | `А для Газпром трансгаз Москва?` | NO_MATCH → NO_MATCH | none | same fallback/final state |
| 20 | `А Москва и Самара?` | NO_MATCH → NO_MATCH | none | same fallback/final state |
| 21 | `Москва или Ростов?` | NO_MATCH → NO_MATCH | none | same fallback/final state |
| 22 | `Покажи максимум по Москве` | NO_MATCH → NO_MATCH | none | same fallback/final state |
| 23 | `Почему в Москве меньше?` | NO_MATCH → NO_MATCH | none | same fallback/final state |
| 24 | `Разбей по областям` | NO_MATCH → NO_MATCH | none | same fallback/final state |
| 25 | `Покажи по всем областям` | NO_MATCH → NO_MATCH | none | same fallback/final state |
| 26 | `Покажи поставки по областям` | NO_MATCH → NO_MATCH | none | same fallback/final state |
| 27 | unknown GEO/group | NO_MATCH → NO_MATCH | none | same fallback/final state |
| 28 | complete new export query | NO_MATCH → NO_MATCH | none | same fallback/final state |
| 29 | period/GEO phrase without active state | NO_MATCH → NO_MATCH | none | same standalone path |
| 30 | clarification present/pending | NO_MATCH → NO_MATCH | none | same clarification path |

The existing planner-capture tests confirm the same canonical IDs and periods.
The scalar execution query switches Rostov to Samara exactly as before. The
production-order directed-flow test selects the same rebound Samara article and
execution layer. Response shape is covered by the unchanged full suite.

## 10. Production PATCH audit

Production non-empty PATCH producers remain exactly:

1. `transitions/period.py`: `periods=SET`.
2. `transitions/geo.py`: conceptual `GEO=SET`, represented by `operands=SET`.

Other occurrences are the existing contract, reducer SET implementation, and
processor normalization reset to an empty `IntentPatch()`. No third production
PATCH category exists.

## 11. Interpretation-mode audit

Mode production is singular:

- `deterministic_period_patch`: one producer in `transitions/period.py`;
- `deterministic_geo_patch`: one producer in `transitions/geo.py`.

Processor's `_should_summarize` remains the existing consumer of both strings.
There are no duplicate old recognizers or mutation builders in processor.

## 12. LLM-call verification

The summary policy is unchanged:

```text
deterministic_period_patch → no summary
deterministic_geo_patch    → no summary
all other modes            → previous behavior
```

Existing counter-based tests prove:

- Period PATCH: zero interpreter and summary calls for native scalar,
  balance snapshot, balance section, and directed-flow execution shapes.
- GEO PATCH: zero interpreter and summary calls for its supported native scalar
  and production-order directed-flow shapes.
- Mixed/compare fallback still invokes the contextual interpreter where it did
  before.

The transition package has no LLM, prompt, agent, or tool-loop imports.

## 13. Tests

```text
baseline full pytest:       383 passed, 1 warning
post-refactor full pytest:  422 passed, 1 warning
Period PATCH suite:          34 passed
GEO PATCH suite:            103 passed
Period + GEO suites:        137 passed
transition-layer unit tests: 37 passed
Golden catalog:               8/8
P0 dry-run:                  84/84
PR3 transition dry-run:      20/20
adversarial GEO phrases:     55/55 rejected safely, 0 false positives
```

The `+39` test delta consists of 37 transition-layer tests and two exact
business-collision characterizations. No expected result was loosened. Live
DB-backed acceptance was not executed because correctness does not depend on it
and the available acceptance flow has external state/cleanup effects.

## 14. LOC and responsibility comparison

```text
                         physical lines   nonblank LOC
processor.py before:              5031           4773
processor.py after:               4665           4431
reduction:                          366            342

classifier.py:                       24             21
period.py:                          217            197
geo.py:                             198            181
types.py:                            29             22
__init__.py:                         10              9
```

`period.py` is 197 nonblank LOC and 217 physical lines. The latter is slightly
above the non-binding 200-line guideline because the extracted production
grammar and parsing remain explicit; no behavior was compressed or split into
an otherwise unnecessary sixth module to optimize the count.

Before, processor owned nine transition responsibilities: period recognition,
period parsing/inheritance, period mutation, GEO grammar/shape checks, GEO
resolution orchestration, business collision protection, entity/article
rebinding, GEO mutation, and two-mode selection.

After, processor owns one coordination responsibility: consume the typed
transition decision and pass it to existing dispatch. It owns zero Period/GEO
recognition or mutation-production responsibilities.

## 15. Known limitations

All PR3 limitations remain unchanged. Complex or mixed turns, multiple
operands, grouping, comparison, ranking, source/route semantics, ambiguous
article rebinding, and missing GEO metadata fail to the existing processor
path. The ready production bundle still lacks a canonical Poland GEO, so
`А по Польше?` falls back there; the canonical country behavior remains covered
by a fixture without inventing metadata.

Unexpected infrastructure exceptions are not masked by a new catch-all because
that would change pre-refactor error behavior. Semantic uncertainty continues
to return `NO_MATCH`.

## 16. Explicitly deferred work

- Business Entity PATCH → PR4.
- Qwen Semantic Repair → later PR.
- Grouping PATCH → later.
- Comparison PATCH → later.
- Full `NEW/PATCH/REINTERPRET` classifier → later, only if justified.

No agentic code or future transition was imported or implemented in PR3.5.
