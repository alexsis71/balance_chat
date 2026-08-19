# PR4: deterministic Business Entity PATCH

## 1. Purpose

PR4 adds one conservative deterministic transition that replaces the canonical
business viewpoint of an existing analytical state. It does not perform generic
entity reasoning and does not compose multiple field changes.

The new flow is:

```text
processor
→ deterministic transition classifier
→ business_entity transition
→ ContextMutation(operands=SET)
→ existing ExecutionAdapter / normalization / planner / executor / commit
```

## 2. Baseline

```text
integration branch: architecture/stateful-analytics
baseline SHA:       9fd1a47f09596c3560f3334a38764229e7e4ae61
feature branch:     feat/business-entity-patch-pr4
baseline pytest:    422 passed, 1 warning
```

The warning is the pre-existing Starlette/httpx deprecation warning.

## 3. Supported subset

PR4 accepts exactly two unambiguous active shapes:

1. A single `SHOW`/`AGGREGATE` distribution operand with one destination GEO.
   It may contain no business viewpoint yet, one `balance`, or one `balance`
   plus one direction-bound article.
2. A `SHOW` full-balance snapshot whose only entity is one `balance`.

Accepted follow-up frames are deliberately narrow:

```text
А для <qualified business>
А по <qualified business>
А теперь для <qualified business>
Только из <qualified business>
```

The entire mention must resolve through the existing metadata registry. Bare
city names are not business mentions.

## 4. Business entity representation

No top-level field was added. Business identity remains inside
`AnalysisIntent.operands[*].entities`.

| Analytical shape | Physical representation |
|---|---|
| Distribution to GEO | `role=balance`, `entity_type=balance`; this is the source accounting viewpoint |
| Full-balance snapshot | `role=balance`, `entity_type=balance` |
| Destination GEO | unchanged `role=destination`, `entity_type=geo_object` |
| Direction-bound article | unchanged `role=article`, but resolved again for a new balance |

Incoming and business-to-business flow shapes have different viewpoint/role
semantics and are not part of the initial subset.

## 5. Role selection rules

The transition admits a replacement only when the role follows from the active
shape rather than from arbitrary language inference:

- distribution plus exactly one destination GEO selects the `balance`
  viewpoint;
- a full-balance snapshot replaces its sole `balance`;
- an already selected identical balance is a no-match rather than a silent
  re-execution;
- any `source`, route, subject, multiple operand, multiple business, grouping,
  comparison, formula, or ranking shape is a no-match;
- balance section, incoming, and business-to-business directed flow are a
  no-match.

## 6. Transition module

`src/balance_chat/transitions/business_entity.py` owns:

- the small safe follow-up grammar;
- qualified-business validation;
- lookup through the injected production metadata registry;
- safe active-shape and target-role checks;
- business replacement and mandatory article rebinding;
- construction of the patch-only mutation;
- the single `deterministic_business_entity_patch` mode producer.

It receives only small metadata hooks and the existing canonical entity factory.
It does not import processor, binder internals, planner, executor, store, LLM, or
agentic code. The file is 224 physical lines.

## 7. Business-vs-GEO policy

Classifier order remains period, GEO, then business. This preserves the proven
Period/GEO behavior. The existing GEO transition rejects a qualified `ТГ` /
`Газпром трансгаз` mention when the registry resolves it as a balance; the new
business module then handles that same canonical record.

```text
А по Москве?       → deterministic_geo_patch
А для ТГ Москва?  → deterministic_business_entity_patch
```

`ГП ТГ Москва` and `Газпром трансгаз Нижний Новгород` therefore cannot collapse
to their GEO substrings.

## 8. Positive scenarios

Validated scenarios include:

- destination-only distribution plus `Только из ГП ТГ Ухта` adds the Ukhta
  balance viewpoint and preserves Rostov/May;
- aliases `ГП ТГ Ухта`, `ТГ Ухта`, and `Газпром трансгаз Ухта` resolve to one
  canonical metadata ID;
- Rostov/April plus business PATCH preserves both period and GEO;
- Samara/April plus a later Period PATCH preserves the Ukhta business entity;
- full-balance Moscow plus `А для ГП ТГ Нижний Новгород?` switches the actual
  balance-level execution ID while preserving the date;
- a direction-bound distribution article is replaced with the unique article
  owned by the new balance for the existing GEO.

## 9. Negative and ambiguous scenarios

The following deliberately use the existing contextual fallback:

```text
А для ГП ТГ Ухта за апрель?
А из ГП ТГ Ухта в Самарскую область?
Сравни с ГП ТГ Ухта
А для ТГ Ухта и ТГ Москва?
А из Томска в Сургут?
Покажи максимум для ТГ Ухта
Разбей по трансгазам
А теперь наоборот
```

No partial mutation is produced. No active state, unknown metadata, the same
already-active balance, missing article rebinding, multiple entities, and all
unsupported active shapes also return no-match.

## 10. Mutation semantics

Business Entity SET uses the existing mutation contracts:

```text
ContextMutation
└── IntentPatch
    └── operands = FieldMutation(SET, updated operands)
```

GEO and Business Entity are distinct semantic PATCH categories but share the
physical `IntentPatch.operands` field. PR4 copies the operand and changes only
the target business component. Metric, aggregate, unit, global/operand periods,
GEO destination, operation, grain, and compatible non-target roles survive.

The existing PR2a rules remain unchanged:

- no normalization change preserves the original patch-only mutation;
- a normalization change synchronizes to normalized `replace_intent` plus an
  empty patch.

## 11. Execution propagation

Processor passes the transition's original mutation through the existing
materialization boundary. Tests capture all downstream layers:

```text
effective AnalysisIntent
== planner input
→ rendered scalar query contains the new canonical business
→ runtime execution receives the new business
```

For full balance, `execute` receives the new canonical balance intent. For a
direction-bound distribution, `execute_balance_day` receives the new balance ID
and the response is filtered using the newly resolved article. Missing or
ambiguous rebinding fails closed before execution.

## 12. Persistence

Both required sequences and four permutations were executed through
`InMemoryContextStore`:

```text
Period → GEO → Business
Period → Business → GEO
GEO → Business → Period
Business → GEO → Period
```

Every turn advances exactly one revision. For every deterministic transition:

```text
effective == executed == committed == reloaded
```

The final state consistently contains Ukhta, Samara, and the requested month.

## 13. LLM policy

The centralized summary policy gained one value:

```text
deterministic_period_patch          → no summary
deterministic_geo_patch             → no summary
deterministic_business_entity_patch → no summary
all other modes                     → unchanged
```

Counter-based execution tests cover native scalar, full balance, and directed
article execution. Successful Business Entity PATCH invokes neither the
contextual interpreter nor the summarizer: **0 LLM calls**.

The new transition module contains no LLM, Qwen, prompt, chat, invoke, or
agentic dependency.

## 14. PATCH audit

Production non-empty PATCH producers after PR4 are exactly:

| Semantic category | Physical field | Producer |
|---|---|---|
| Period SET | `periods=SET` | `transitions/period.py` |
| GEO SET | `operands=SET` | `transitions/geo.py` |
| Business Entity SET | `operands=SET` | `transitions/business_entity.py` |

There is no fourth category. `processor.py` contains only the existing empty
`IntentPatch()` normalization reset.

Mode audit finds one production producer each for:

- `deterministic_period_patch`;
- `deterministic_geo_patch`;
- `deterministic_business_entity_patch`.

## 15. Test results

```text
baseline full pytest:              422 passed, 1 warning
PR4 full pytest:                   501 passed, 1 warning
Period PATCH:                       34 passed
GEO PATCH:                         103 passed
Business PATCH focused:             79 passed
Period + GEO + Business combined:  216 passed
Golden catalog:                       8/8
P0 strict dry-run:                   84/84
PR3 transition strict dry-run:       20/20
PR4 transition strict dry-run:       50/50
```

The `+79` test delta is additive: 66 transition-level cases and 13
processor/execution/persistence cases. Existing expected results were not
removed or weakened.

Node-ID audit:

```text
baseline nodes: 422
PR4 nodes:      501
removed:          0
added:           79
```

Live DB-backed acceptance: **NOT RUN**. Dry-run results validate catalog
coverage only and are not presented as live semantic evidence.

## 16. Adversarial results

```text
existing GEO adversarial corpus:      55/55 safe, 0 false positives
new business adversarial corpus:      50/50 safe, 0 false positives
```

The business corpus covers GEO collisions, period/GEO mixtures, comparisons,
ranking, grouping, multiple TGs, ambiguous directed flow, standalone requests,
unknown entities, routes, references, and explanation turns. Safe false
negatives remain acceptable.

## 17. Known limitations

- Incoming and business-to-business directed flows are deliberately unsupported.
- Balance-section replacement is unsupported because its article must be
  mapped safely across balances.
- No destination-business replacement is attempted.
- No multi-field or multi-entity composition is attempted.
- The ready metadata bundle currently does not resolve `ТГ Нижний Новгород`.
  It does resolve `ГП ТГ Нижний Новгород`, `Газпром трансгаз Нижний Новгород`,
  `ТГ Н Новгород`, and `ГП ТГ Н.Новгород`. The missing short alias safely falls
  back and is not interpreted as GEO. PR4 does not change metadata.

## 18. Deferred semantic-repair cases

These families belong to future Qwen Semantic Repair rather than a larger PR4
grammar:

- business plus period or GEO in one turn;
- comparisons, ranking, grouping, explanations, and references;
- multiple businesses;
- arbitrary incoming/source/destination role inference;
- business-to-business direction changes;
- reverse/route semantics;
- unknown or missing metadata aliases.

No Qwen or Agentic PoC code is imported or reused by PR4.
