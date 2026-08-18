# PR3 Independent Adversarial Review — Deterministic GEO PATCH

Branch `feat/geo-patch-pr3`, tip `8981914` ("fix: preserve bare GEO names across business aliases").
Baseline `04d548d`. Reviewed read-only; no production code was modified.

Diff scope verified independently (`git diff --name-status 04d548d..8981914`):
`acceptance/geo_patch_pr3_transition.feature` (A), `docs/geo_patch_pr3.md` (A),
`src/balance_chat/processor.py` (M), `tests/test_geo_patch.py` (A). 4 files, **+1276 / -1**.
The single deleted line is the old one-line body of `_should_summarize`, so every pre-existing
helper reused by PR3 (`_typed_entity`, `_unique_direction_article`, `_tagged_geo_objects`,
`_matched_geo_objects`, `_business_entity_token_spans`, `_normalize_text`,
`_metadata_numeric_id`) is byte-for-byte unmodified. No file under `src/` other than
`processor.py` is touched, so planner, executor, reducer, binder, interpreter, prompts, API,
stores and contracts are unchanged by construction.

## Review environment note (material — read before the findings)

The deterministic GEO path depends on `pipeline_v2.nlp_ru.normalize_query_lemmas`, which requires
`mawo-pymorphy3` (a declared production dependency, `pipeline/requirements-mcp.txt:3`). That
package was **not installed in any interpreter on this machine**. Without it,
`lemmatize_text` (`pipeline/pipeline_v2/nlp_ru.py:67-73`) returns the input unchanged when
`_mawo_analyzer()` is `None`, so `normalize_query_lemmas` degrades to casefold/punctuation
normalization only.

In that degraded state the GEO detector rejects almost every real phrase — including the
headline `А по Самарской области?` — because `самарской области` never normalizes toward
`самарская обл`. I installed `mawo-pymorphy3==1.0.4` into the review virtualenv,
confirmed `nlp_status()["mawo_analyzer_available"] == True`, and confirmed lemmatization now
matches production semantics (`самарской области` → `самарский область`,
`москве` → `москва`). **Every probe result below is from the post-install,
production-representative configuration.** The degraded-mode behaviour is recorded as a
MEDIUM finding (M2) because the failure is silent.

## 1. Verdict

**APPROVE.**

No blocker and no high-severity finding. Every invariant the brief marks CRITICAL was proven
empirically against the real production metadata bundle (`2026.08.1`, 210 geo objects,
54 balances), not merely against the PR's own synthetic fixtures. Four MEDIUM and three LOW
findings are recorded; none of them is a correctness defect in the patch mechanism, and all
concern test/deployment/documentation robustness rather than semantics.

I actively tried to construct the failure modes the brief warned about — stale article binding,
lost period, lost GEO, business misclassification, partial capture of a mixed turn, residual LLM
call, executed ≠ committed, entity-list corruption, a third PATCH category — and could not
produce any of them.

## 2. Executive summary

PR3 adds a second deterministic PATCH category following the PR2b pattern exactly: one frozen
dataclass (`GeoPatchCandidate`), one module-level helper (`_whole_geo_mention_matches`), and two
methods (`_detect_geo_followup`, `_deterministic_geo_patch`). No router, classifier, transition
engine, or generic patch framework was introduced, and no processor decomposition occurred.

The mechanism is a genuine patch, not a disguised replacement: the produced `ContextMutation` has
`replace_intent is None` and sets exactly one `IntentPatch` field (`operands=SET`). Because the
new operand is built with `operand.model_copy(update={"entities": ...}, deep=True)` over a
copy-then-swap-one-index entity list, every non-GEO operand field and the entity role *order*
survive by construction — verified empirically field-by-field.

The riskiest area the brief flagged — article/evidence rebinding — is handled correctly and
conservatively. For the directed-flow `[balance, destination, article]` shape the article is
**actively re-resolved** for the new geography via `_unique_direction_article`, which requires an
exact normalized name match and uniqueness; if no such article exists the detector returns `None`
rather than committing a stale binding. The `export`-with-article combination the brief called
out as a potential BLOCKER is unreachable: the `operand.metric != "distribution"` guard fires
before the article assignment, and I confirmed empirically that it returns `None`.

The second flagged area — the business-entity collision reopened by `8981914` — is real in the
narrow sense that 11 bare city names in the production bundle are simultaneously `ГП ТГ <city>`
balance aliases and `geo_object` records, and PR3 now resolves them as GEO. However this is
**consistent with the system's own pre-existing, system-wide policy**: `_tagged_business_balances`
only binds a business when the phrase carries a literal `ТГ` token
(`_business_entity_token_spans`, `processor.py:4883-4895`), so bare `Москва` is not treated as a
business anywhere else in the processor either. All ten qualified business forms
(`ГП ТГ Москва`, `Газпром трансгаз Москва`, `ТГ Москва`, `ТГ Ухта`,
`ГП ТГ Нижний Новгород`, `ООО Газпром трансгаз Самара`, …) are correctly rejected, as are all
inflected qualified variants. Invariant I8 as specified passes.

Regression numbers reproduce exactly (383 / 282 / 101 / 135), with 0 tests removed.

## 3. Blockers

**None.**

## 4. High findings

**None.**

## 5. Medium/Low findings

### M1 — The entire GEO test suite is hermetic against a synthetic registry and a hand-written lemma map

**Severity:** MEDIUM
**Evidence:** `tests/test_geo_patch.py:33-48` (`_LEMMA_MAP`, `_normalize_lemmas`),
`tests/test_geo_patch.py:51-64` (six `SimpleNamespace` geo records),
`tests/test_geo_patch.py:114-154` (`_Registry` with a hardcoded 8-entry `balance()` allow-list).
All 101 tests use this fixture; none touches `pipeline/data/metadata/manifest.json`.
**Why it matters:** the suite cannot detect a bundle or lemmatizer regression. Concretely: while
the morphological analyzer was missing from my environment, `А по Самарской области?` resolved to
`None` against the real bundle — the feature was entirely inert — yet all 101 tests still passed,
because the fixture's `_LEMMA_MAP` hardcodes `"самарской" → "самарская"` and the fixture geo is
named `"Самарская область"` rather than the bundle's actual `"самарская обл"`. The fixture also
diverges from reality in a way that hides the collision surface: `_Registry.balance` accepts
exactly one bare alias (`"москва"`), whereas the real bundle has 11+.
The repo already has the pattern for a real-bundle test (`tests/test_binding.py:29-36`,
`tests/test_result_memory.py:121-127` build a real `MetadataRegistry`).
**Reproduction:** uninstall `mawo-pymorphy3`; run `pytest tests/test_geo_patch.py` → 101 passed;
then run the detector against the real registry → `А по Самарской области?` returns `None`.
**Minimal correction:** add one integration test that loads the real
`MetadataRegistry` and asserts `_detect_geo_followup("А по Самарской области?", ...)` resolves to
`geo:fdf2b36b839e08399ef3`, mirroring `tests/test_binding.py:29-36`.

### M2 — GEO PATCH silently no-ops when the morphological analyzer is unavailable

**Severity:** MEDIUM
**Evidence:** `pipeline/pipeline_v2/nlp_ru.py:67-73` (`lemmatize_text` returns raw text when
`_mawo_analyzer()` is `None`); `_mawo_analyzer` swallows all exceptions
(`nlp_ru.py:280-289`). `src/balance_chat/processor.py:96-102` stores the resulting callable as
`self._normalize_lemmas`. The detector's only guard is `self._normalize_lemmas is None`
(`processor.py:1596`) — which is `False` in the degraded case, because the function exists and
merely stops lemmatizing.
**Why it matters:** the degradation is silent and total for this feature. On a host without
`mawo-pymorphy3`, PR2b's period PATCH keeps working (it uses `_normalize_text`, not lemmas) while
PR3's GEO PATCH never fires, so a demo would show `А за апрель?` working deterministically and
`А по Самарской области?` falling through to the LLM. This is a *safe* degradation (fail-closed to
the pre-PR3 path), which is why it is not HIGH.
**Reproduction:** in an environment without `mawo-pymorphy3`,
`processor._detect_geo_followup("А по Самарской области?", <active scalar distribution intent>)`
→ `None`, while the same call with the package installed → `geo:fdf2b36b839e08399ef3`.
**Minimal correction:** log a startup warning when `natasha_available()`/
`nlp_status()["mawo_analyzer_available"]` is `False`, and/or add the dependency to
`balance_chat`'s own `pyproject.toml` rather than relying on the sibling pipeline's
`requirements-mcp.txt`.

### M3 — Bare city names that are also `ГП ТГ` balance aliases resolve as GEO

**Severity:** MEDIUM (documented product-semantics decision, not a defect)
**Evidence:** `src/balance_chat/processor.py:1610-1620` — the balance-collision guard runs only
when `qualified_business` is true (`"тг" in mention_tokens` or the phrase starts with
`газпром трансгаз` / `ооо газпром трансгаз`). Against the real bundle I enumerated **11**
unqualified balance aliases that are simultaneously `geo_object` records and now resolve as GEO:
`москва, самара, уфа, волгоград, екатеринбург, казань, томск, саратов, ставрополь, краснодар,
нижний новгород` (each an alias of the corresponding `ГП ТГ <city> суточный баланс`).
**Why it is not HIGH:** the behaviour is consistent with the processor's existing system-wide
policy. `_business_entity_token_spans` (`processor.py:4883-4895`) reserves a business span only
after a literal `тг` token, so `_tagged_business_balances("москва")` returns `[]` while
`_tagged_geo_objects("москва")` returns the geo — verified empirically. A bare city name is not
treated as a business anywhere else in the processor either, so before PR3 the same phrase would
have reached the LLM with a `GEO` tag and no `BUSINESS_ENTITY` tag. The `8981914` fix therefore
*aligns* the GEO detector with the surrounding policy; the original unconditional
`balance_lookup(mention)` check was the anomaly, because it used a bare-name lookup form that the
rest of the system only applies inside an already-`ТГ`-qualified span. Additionally, on the
directed-flow shape the article-rebinding requirement rejects most of these anyway
(`А по Саратову?`, `А по Ухте?`, `А по Самаре?` → `None` against balance `ГП ТГ Москва`), and
where it does fire (`А по Москве?`) it rebinds to the genuinely correct article
`2010000039793 'Москва'`.
**Minimal correction:** none required for correctness. Recommend documenting the rule
("a bare toponym is a geography; a business requires the `ТГ`/`Газпром трансгаз` qualifier") in
`docs/geo_patch_pr3.md` as an explicit product decision, and emitting the existing
`deterministic_geo_patch_recognized` log line with the colliding balance id when one exists, so
the ambiguity is observable in production telemetry.

### M4 — "20/20", "84/84" and "8/8" are coverage gates, not behavioural evidence

**Severity:** MEDIUM (documentation/claim accuracy)
**Evidence:** `src/balance_chat/acceptance_runner.py:468-500`, `_dry_run_scenario` constructs
every `CheckResult` with `passed=True` and actual value `"not executed"`. My run of the PR3
catalog reports `execute_db: false`, `executed: 0`, `health: {"status": "not_checked"}`.
The Golden 8/8 is `--validate-only`, i.e. canonical metadata binding checks with no query
execution.
**Why it matters:** `docs/geo_patch_pr3.md:150-167` places the "20/20 automated checks" claim
immediately under a table asserting per-turn LLM counts, execution layers and revisions. A reader
can easily take the 20/20 as confirmation of that table. It is not — it confirms only that the
20 expectation lines parse and map to automated checks. The real behavioural evidence for the
four-turn sequence is `test_period_then_geo_then_period_persists_executed_intent`.
**Minimal correction:** label the three numbers as "dry-run coverage" in the docs table.

### L1 — GEO coverage is unpredictable because whole-mention matching requires equal token arity

**Severity:** LOW
**Evidence:** `src/balance_chat/processor.py` `_whole_geo_mention_matches` — the
`if len(label_tokens) != len(mention_tokens): continue` filter. The `region_types` map normalizes
`область`/`област` → `обл`, so `по <X>ской области` becomes a 2-token mention and only matches a
geo that happens to carry a 2-token alias.
**Observed:** `по Воронежской области` → resolves (geo `воронежская` has alias
`воронежская обл`); `по Тульской области` → `None` (geo `тульская` has no 2-token alias);
`по Рязанской области` → `None`; `по Белгородской области` → `None`;
`по Калужской области` → `None`.
**Why it matters:** these are safe false negatives (fallback to the contextual interpreter), so
there is no correctness risk — but coverage varies region-by-region on bundle alias data alone,
which is a demo hazard: an operator picking an arbitrary oblast has a substantial chance of the
deterministic path silently not engaging.
**Minimal correction:** none required. Optionally enrich bundle aliases, or allow the mention's
trailing `обл` token to be dropped when the label is a single token.

### L2 — Pre-existing dead code adjacent to the new producer

**Severity:** LOW (pre-existing, not introduced by PR3)
**Evidence:** `src/balance_chat/processor.py:1293` `_deterministic_geo_mutation` is called only at
`processor.py:438`, inside `process()`. `process()` returns to `_process_contextual` at
`processor.py:212-221` whenever `state.active_dialog_scope is not None`, and
`_deterministic_geo_mutation` returns `None` when `scope is None`. The call is therefore
unreachable.
**Why it matters:** it is a full-replacement (`replace_intent`) GEO producer sitting three lines
from the new patch producer with a nearly identical name. It is correctly excluded from the PATCH
audit (it uses `replace_intent`, not `IntentPatch`), and `docs/geo_patch_pr3.md:218-220` already
acknowledges it, but it is a live confusion hazard for the next reader.
**Minimal correction:** out of scope for PR3; worth a follow-up removal.

### L3 — Redundant (but harmless and defensible) guards in the detector

**Severity:** LOW (informational)
**Evidence:** `_detect_geo_followup` guards on `comparison is not None`, `formula is not None`,
`ranking is not None` and `grouping`. Per `AnalysisIntent.validate_shape`
(`contracts.py:153-203`), `comparison` implies `COMPARE`, `formula` implies `CALCULATE`,
`ranking` implies `RANK`, and `grouping` implies `GROUP` — all four already excluded by
`operation not in {SHOW, AGGREGATE}`. The guards are therefore redundant.
**Assessment:** correct defence-in-depth, not a finding to fix. Recorded because the brief asked
whether `comparison is not None` catches a case the operand-count check misses: it does not, but
neither does it mask anything (`len(operands) != 1` independently excludes `COMPARE` and
`CALCULATE`, and the operation check independently excludes `COMPARE_PERIODS`, `RANK`, `GROUP`,
`MULTI_STEP`, all of which I confirmed empirically return `None`).

## 6. Invariant verification

| Invariant | PASS / FAIL / UNCERTAIN | Evidence |
|---|---|---|
| Production PATCH categories exactly periods+GEO | **PASS** | Repo-wide grep for `IntentPatch(` / `FieldMutation(` / `MutationAction.` yields 5 production sites; only `processor.py:1704 operands=SET` and `processor.py:4751 periods=SET` are non-empty producers. See §11. |
| GEO PATCH is patch-only | **PASS** | Real-registry E2E: `replace_intent is None`, patch set fields `['operands']` only. `tests/test_geo_patch.py:381-383` asserts the same. |
| GEO changes actual planner/execution | **PASS** | Real-registry E2E: planner input destination `geo:d09b…` → `geo:fdf2…`; the *rendered execution query string* changed from `…в ростовская за период…` to `…в Самарская обл за период…`. Also `tests/test_geo_patch.py:659-661`. |
| Non-GEO semantics preserved | **PASS** | Field-by-field probe over all six `AnalysisOperand` fields: `operand_id`, `metric`, `aggregate_type`, `periods`, `unit` all identical; only `entities` differs. Intent-level `operation`, `periods`, `grain`, `comparison`, `formula`, `ranking`, `grouping` untouched (reducer applies only the `operands` field). |
| Period survives GEO PATCH | **PASS** | E2E Q2→Q3: April 2025 preserved across the Samara patch (`2025-04-01..2025-05-01` at revision 3). |
| GEO survives later period PATCH | **PASS** | E2E Q3→Q4: Samara preserved across the May patch (revision 4, dest `geo:fdf2…`). Also 5-step round-trip probe: no period drift over Samara→Moscow→Rostov→Samara→Rostov. |
| Deterministic GEO uses 0 LLM | **PASS** | E2E: interpreter 0, summarizer 0 on both reachable execution shapes — native scalar (`_execute_mutation`) and full-balance-family/`unified_directed_flow` (`_execute_full_balance_mutation`). All three `summarize_envelope` sites gated by `_should_summarize`; no fourth site; no literal mode-string comparison anywhere. |
| Business entity collision protected | **PASS** (with M3) | All 10 qualified forms rejected, plus 12 inflected/edge qualified variants (`ТГ Москве`, `ГП ТГ Москве`, `Газпром трансгаз Москве`, `ООО Газпром трансгаз Москве`, `трансгаз Москва`, …) — all `None`. Residual bare-name ambiguity documented as M3. |
| Mixed turns not intercepted | **PASS** | 68 mixed/compare/ranking/grouping/causal/route/new-query/nonsense/geo-group/anaphora phrases → 0 false positives. `А по Москве за апрель?`: period regex requires `за` at position 0 (after optional `а`/`покажи`) so it declines; GEO detector declines on whole-mention arity. |
| executed == committed == reloaded | **PASS** | E2E asserts all three equalities at every one of Q2/Q3/Q4. Structurally enforced by `_synchronize_effective_intent`'s `_materialize_mutation(...) != normalized` → `TurnProcessingError` (`processor.py:189-196`). |
| PR2a normalization preserved | **PASS** | `_execute_mutation:2450-2480` passes `previous_effective_intent=intent` where `intent` is the freshly materialized post-PATCH pre-normalization intent, then re-threads the result into the second synchronization. Collapse to `replace_intent + IntentPatch()` on change; original patch retained on no-op. No double application (empty patch over `replace_intent` base). |
| Period PATCH unaffected | **PASS** | `tests/test_period_patch.py` 34 tests green; combined suite 135; E2E Q2 and Q4 both `deterministic_period_patch` with 0 LLM. |
| Single-turn Golden behavior unaffected | **PASS** | Golden catalog validates 8/8 (GQ-001…GQ-008); full suite 383 passed with 0 removed tests; PR3 code is gated behind `state.active_dialog_scope is not None` so a first turn cannot reach it. |

## 7. Production GEO call graph

Traced for `А по Самарской области?` with the active scope already period-patched to April 2025.
All line numbers are at tip `8981914`.

| # | Node | file:line | Input | Output |
|---|---|---|---|---|
| 1 | `PipelineV2TurnProcessor.process` | `processor.py:199` | `state` (active scope: SHOW/distribution/Rostov/April), `message` | delegates because `state.active_dialog_scope is not None` (`:212`) |
| 2 | `_process_contextual` | `processor.py:605` | same | detector chain |
| 3 | `_deterministic_period_patch` (**first**) | `processor.py:617-621` → `:4733` | message | `None` — `re.fullmatch(r"(?:а\s+)?(?:покажи\s+)?за\s+…")` fails |
| 4 | `_deterministic_geo_patch` (**second**) | `processor.py:640-644` → `:1688` | `state`, message, `turn_id` | `(ContextMutation, GeoPatchCandidate)` |
| 5 | `_detect_geo_followup` | `processor.py:1582` | message, `scope.intent` | shape guards → regex → mention `самарской области` |
| 6 | business guard | `processor.py:1610-1620` | mention tokens | `qualified_business=False` → guard skipped |
| 7 | `_tagged_geo_objects` (pre-existing, reused) | `processor.py:1502` | mention | exactly 1 match: `geo:fdf2b36b839e08399ef3 / самарская обл` |
| 8 | `_whole_geo_mention_matches` (new) | `processor.py:4666` | mention, geo, lemma fn | `True` (`['самарский','обл']` vs `['самарский','обл']`, both ratios 1.0 ≥ 0.76) |
| 9 | entity swap | `processor.py:1621-1650` | `operand.entities` | deep-copied list, destination replaced in place via `_typed_entity`; article rebound only if `metric=="distribution"` and one article + one balance |
| 10 | `ContextMutation(patch=IntentPatch(operands=FieldMutation(SET, [operand])))` | `processor.py:1700-1710` | candidate | patch-only mutation, `replace_intent=None` |
| 11 | `log_event deterministic_geo_patch_recognized` | `processor.py:647-654` | `geo_id` | structured log |
| 12 | `_dispatch_mutation(..., interpretation_mode="deterministic_geo_patch", memory_chunks=[], evidence_geos=[geo])` | `processor.py:655-665` → `:123` | mutation | → `_execute_mutation` (no grouping) |
| 13 | `_materialize_mutation` → `reduce_intent` | `processor.py:141` → `reducer.py:148` | state + mutation | base = `active_dialog_scope.intent.model_dump()`; `_apply_field` SET → `payload["operands"] = deepcopy(value)`; `AnalysisIntent.model_validate` |
| 14 | `_synchronize_effective_intent` ×2 (PR2a) | `processor.py:2453`, `:2474` | previous = materialized post-PATCH intent | no-op here → original patch preserved |
| 15 | `gate.validate_bound_intent(intent, explicit_geos=[geo])` | `processor.py:2495` → `gating.py:86` | intent + evidence geo | `explicit_geo_not_bound:` check (`gating.py:149-153`) confirms the new GEO is actually bound in the effective intent |
| 16 | `self.planner.plan(intent)` | `processor.py:2516` | effective intent (Samara/April) | `NativeExecutionPlan` |
| 17 | `gate.validate_plan(plan)` | `processor.py:2522` | plan | task period/operand checks |
| 18 | executor → rendered query | `processor.py:2551+` | plan | `Покажи суммарное значение распределения газа в Самарская обл за период с 2025-04-01 по 2025-05-01` |
| 19 | `_should_summarize("deterministic_geo_patch")` | `processor.py:2599` → `:4618` | mode | `False` → **summarizer not called** |
| 20 | store commit / reload | `store.py` via `apply_context_transition` (`reducer.py:140`) | mutation + SUCCESS | revision 2 → 3; `active_dialog_scope.intent == executed` |

**Detector ordering conclusion:** period is textually and behaviourally first; GEO second; the rest
of the pre-existing chain follows. Neither can win by accident on `А по Москве за апрель?` —
the period regex is anchored (`fullmatch` requiring `за` after only optional `а `/`покажи `) and
the GEO regex, while it does match with `geo="москве за апрель"`, is then rejected by
`_whole_geo_mention_matches` because the 3-token mention has no equal-arity label. Confirmed
empirically: both return `None`, and `tests/test_geo_patch.py:817-829` asserts the contextual
interpreter is invoked exactly once.

## 8. GEO representation/entity-list audit

GEO lives in `operand.entities` as `OperandEntityRef(role="destination",
entity=CanonicalEntityRef(entity_type="geo_object", …))` (`contracts.py:79-82`).

`AnalysisOperand` (`contracts.py:84-102`) has exactly six fields: `operand_id, metric,
aggregate_type, entities, periods, unit`. Empirical field-by-field check on an `AGGREGATE`
operand with `aggregate_type="max"`, a custom `operand_id`, operand-level `periods`, and roles
`[subject, destination]`:

| field | preserved |
|---|---|
| `operand_id` | yes |
| `metric` | yes |
| `aggregate_type` | yes |
| `entities` | **changed by design** (destination swapped in place) |
| `periods` | yes |
| `unit` | yes |

Role order `['subject','destination']` before → `['subject','destination']` after.

**Entity order (PR2b's load-bearing concern):** on the real production directed-flow shape the
roles were `['balance','destination','article']` before and after, with ids
`['BAL:2010000039953', '<new geo>', 'ART:<rebound>']`. This matters because
`_is_directed_flow_show` (`processor.py:3737-3741`) compares the role list *positionally*
(`roles in (["balance","source","article"], ["balance","destination","article"])`). The producer
never appends or reorders — it builds
`entities = [item.model_copy(deep=True) for item in operand.entities]` and then assigns
`entities[destination_index]` / `entities[article_indexes[0]]`, so arity, order and every
untouched role (`balance`, `subject`) survive. Confirmed end-to-end: the patched turn routed to
`execution.layer == "unified_directed_flow"`.

**Reducer semantics for `operands=SET`.** `_apply_field` (`reducer.py:70-72`) treats SET
uniformly: `payload[field] = deepcopy(mutation.value)` — wholesale replacement, identical to how
`periods=SET` is applied. `operands` is in `_LIST_FIELDS` (`reducer.py:28`) but that only affects
ADD/REMOVE/CLEAR, which PR3 does not use. There is no list-merge semantic that could partially
splice the new operand into the old list, so the patch's intent (replace the single operand) and
the reducer's behaviour agree exactly. `AnalysisIntent.model_validate(payload)`
(`reducer.py:101`) re-validates the result.

**Non-GEO roles are never dropped:** verified with a `subject`/`organization` role
(preserved byte-identical, `tests/test_geo_patch.py:418-431` covers the same) and with
`balance` on the directed-flow shape.

## 9. Business-vs-GEO audit

**Canonical resolution chain:** raw phrase → `_normalize_text` → anchored regex → mention →
(conditional) `registry.balance` guard → `_tagged_geo_objects` (pre-existing, reused — **no second
GEO resolver was introduced**) → `_whole_geo_mention_matches` → canonical geo record →
`_typed_entity`. **No LLM appears anywhere in this path** — confirmed by grep and by the E2E
interpreter call count of 0.

**Fuzzy matching:** `SequenceMatcher` with thresholds 0.84 (single token) / 0.76 (multi-token),
applied token-wise with `all(...)` and an equal-arity precondition. `_tagged_geo_objects`
additionally drops any span where the top two candidates are within 0.03
(`processor.py:1539-1540`), so near-ties are rejected rather than arbitrated. I could not make an
unknown location produce a wrong canonical GEO: `Мордор`, `Атлантида`, `Нарния`, `Зимбабве`,
`неизвестная область`, `ерунда`, `Ямал`, `РФ`, `Россия`, `Украина`, `Белоруссия`, `Казахстан`,
`Башкортостан`, `Польша` all → `None`.

**Qualified business forms — all rejected (22/22):**

| phrase | `registry.balance(mention)` | result |
|---|---|---|
| `А по ГП ТГ Москва?` | hit | None |
| `А по Газпром трансгаз Москва?` | hit | None |
| `А по ТГ Москва?` | hit | None |
| `А по ГП ТГ Нижний Новгород?` | hit | None |
| `А по Газпром трансгаз Нижний Новгород?` | hit | None |
| `А по ТГ Ухта?` | hit | None |
| `А для ТГ Самара?` | hit | None |
| `А по ООО Газпром трансгаз Самара?` | hit | None |
| `А по ТГ Екатеринбург?` | hit | None |
| `А по ГП ТГ Волгоград?` | hit | None |
| `А по ТГ Москве?` (inflected) | **None** | None (saved by arity check) |
| `А по ТГ Самаре?` | None | None |
| `А по ТГ Ухте?` | None | None |
| `А по Газпром трансгаз Москве?` | None | None |
| `А по ООО Газпром трансгаз Москве?` | None | None |
| `А по трансгаз Москва?` | hit (guard not armed) | None (saved by arity check) |
| `А по трансгазу Москва?` | None | None |
| `А по ГП ТГ Москве?` | None | None |
| `А по ТГ Нижнему Новгороду?` | None | None |
| `А по газпром трансгаз самара?` | hit | None |
| `А по тг?` | None | None |
| `А по ТГ Беларусь?` | hit | None |

Note the defence-in-depth: six of these are rejected by `_whole_geo_mention_matches`' equal-arity
requirement even when the balance guard does *not* arm (because `registry.balance` is lemma-blind
and misses inflected aliases). The guard alone is not load-bearing.

**Bare-alias collision surface (M3).** Enumerated exhaustively from
`pipeline/data/metadata/catalog/balances.jsonl`: 11 bare aliases are simultaneously balance
aliases and geo objects and now resolve as GEO — `москва, самара, уфа, волгоград, екатеринбург,
казань, томск, саратов, ставрополь, краснодар, нижний новгород`. Four further bare aliases
(`ухта, сургут, югорск, беларусь`) do **not** resolve as GEO because the bundle has no matching
geo record — the detector returns `None`.

Decisive mitigating evidence that this is not a misclassification:

| phrase | `_tagged_business_balances` | `_tagged_geo_objects` |
|---|---|---|
| `москва` | `[]` | `['москва']` |
| `по москве` | `[]` | `['москва']` |
| `самара` | `[]` | `['самара']` |
| `тг москва` | `['ГП ТГ Москва суточный баланс']` | `[]` |
| `гп тг москва` | `['ГП ТГ Москва суточный баланс']` | `[]` |

A bare toponym is not a business anywhere in this processor. PR3 post-`8981914` matches that
policy; pre-`8981914` it did not.

**Article rebinding as a second guard.** For balance `ГП ТГ Москва суточный баланс` (121 articles,
73 in section `Распределение`), only **19 of 210** geo objects have a uniquely-named distribution
article and can therefore rebind. On the directed-flow shape, `А по Саратову?`, `А по Ухте?`,
`А по Самаре?` and `А по Самарской области?` all return `None`; `А по Москве?` fires and rebinds
to article `2010000039793 'Москва'` — the correct Moscow-city distribution line under that balance.

## 10. False-positive attack

**115 phrases**, all executed through the real `_detect_geo_followup` against the real
`MetadataRegistry` (bundle `2026.08.1`) with the production lemmatizer active — not reasoned about
by eye, and independent of the 55 phrases the PR author claims. Active shape for the main run:
single-operand `SHOW` / `distribution` with one `geo_object` destination (`ростовская`), May 2025.

| # | Category | n | True positive | Safe false negative | **False positive** |
|---|---|---:|---:|---:|---:|
| A | Simple GEO replacement | 8 | 7 | 1 (`Башкортостану`) | 0 |
| B | Business collision, qualified | 10 | — | 10 (correct rejects) | **0** |
| C | Business collision, bare alias | 15 | 11 (see M3) | 4 | **0**\* |
| D | Multiple GEO mentioned | 7 | — | 7 | **0** |
| E | GEO + period combined | 6 | — | 6 | **0** |
| F | GEO + compare | 5 | — | 5 | **0** |
| G | GEO + ranking | 4 | — | 4 | **0** |
| H | GEO + grouping / plural | 7 | — | 7 | **0** |
| I | GEO + causal/explanatory | 5 | — | 5 | **0** |
| J | Route / business names w/ cities | 5 | — | 5 | **0** |
| K | Full new unrelated query | 6 | — | 6 | **0** |
| L | Unknown / nonsense location | 7 | — | 7 | **0** |
| M | GEO group / expansion | 3 | — | 3 | **0** |
| N | Anaphora / vague reference | 5 | — | 5 | **0** |
| — | Inflected/edge business forms | 22 | 1 (`Санкт-Петербургу`) | 21 | **0** |
| | **Total** | **115** | **19** | **96** | **0** |

\* Category C is classified as true-positive-by-policy per M3, not as a false positive; see §9 for
the reasoning and the mitigating evidence. If a reviewer disagrees with the policy, these 11 are
the only phrases in the entire 115 that would change classification.

**Per-phrase results (abridged to the decisive rows; all `→ None` unless shown):**

*A — simple replacement.* `А по Самарской области?` → `geo:fdf2b36b839e08399ef3 самарская обл`;
`А по Ростовской области?` → `geo:d09b4af4532c60823f22 ростовская`;
`По Самарской области` → `самарская обл`; `А по Московской области?` → `московская область`;
`А для Самарской области?` → `самарская обл`; `А по Татарстану?` → `татарстан`;
`А по Германии?` → `германия`; `А по Башкортостану?` → None (safe FN, no bundle record).

*B — qualified business.* `А по ГП ТГ Москва?`, `А по Газпром трансгаз Москва?`,
`А по ТГ Москва?`, `А по ГП ТГ Нижний Новгород?`, `А по Газпром трансгаз Нижний Новгород?`,
`А по ТГ Ухта?`, `А для ТГ Самара?`, `А по ООО Газпром трансгаз Самара?`,
`А по ТГ Екатеринбург?`, `А по ГП ТГ Волгоград?` → all None. **I8 satisfied.**

*C — bare aliases.* GEO: `А по Москве?`→москва, `А по Самаре?`→самара, `А по Уфе?`→уфа,
`А по Волгограду?`→волгоград, `А по Екатеринбургу?`→екатеринбург, `А по Казани?`→казань,
`А по Томску?`→томск, `А по Саратову?`→саратов, `А по Ставрополю?`→ставрополь,
`А по Краснодару?`→краснодар, `А по Нижнему Новгороду?`→нижний новгород.
None: `А по Ухте?`, `А по Сургуту?`, `А по Югорску?`, `А по Беларуси?`.

*D.* `А Москва и Самара?`, `Москва или Ростов?`, `Сравни Москву и Самару`,
`А по Москве и Самарской области?`, `По Самарской области и по Ростовской области`,
`Между Москвой и Самарой`, `Германия или Польша?` → all None. **No first/last/highest-score pick.**

*E.* `А по Москве за апрель?`, `А по Самарской области за май?`, `А по Самарской области в мае?`,
`Для Германии в августе`, `А по Ростовской области за 2025 год?`, `По Татарстану за квартал`
→ all None. **No partial capture.**

*F.* `Сравни с Самарской областью`, `Сравни по Самарской области с Ростовской`,
`Москва больше Самары?`, `Кто больше - Москва или Самара?`, `А по Самарской области больше?`
→ all None.

*G.* `Покажи максимум по Москве`, `Покажи минимум по Самарской области`,
`Топ по Самарской области`, `Рейтинг регионов включая Москву` → all None.

*H.* `Разбей по областям`, `Покажи по всем областям`, `По регионам`, `По регионам России`,
`Сгруппируй по областям`, `По всем регионам`, `По областям` → all None. **I10 satisfied** —
plural/grouping forms are never converted to a singular GEO SET.

*I.* `Почему в Москве меньше?`, `Почему по Москве меньше?`, `Объясни снижение в Самарской области`,
`Что случилось в Ростовской области?`, `Есть аномалия по Самарской области?` → all None.

*J.* `По маршруту Москва - Самара`, `А по газопроводу Москва Ростов`, `Через Москву в Самару`,
`Из Москвы в Самару`, `Маршрут Германия Польша` → all None.

*K.* `Покажи экспорт в Германию`, `Покажи распределение в Самарскую область`,
`Сколько газа в Самарскую область?`, `Новый запрос по Ростовской области`,
`Дай данные по Татарстану за май`, `Покажи баланс ГП ТГ Москва` → all None.

*L.* `А по Мордору?`, `А по неизвестной области?`, `А по Атлантиде?`, `А по Зимбабве?`,
`А по Нарнии?`, `А по ерунде?`, `А по Польше?` → all None. **No fuzzy over-match.**

*M.* `По Европе`, `Для Дальнего зарубежья`, `По СНГ` → all None (geo *groups* are not geo objects).

*N.* `А там по Москве?`, `А что по ней в Самаре?`, `По этому региону Москва`,
`А по соседней с Москвой области?`, `Для этой Германии?` → all None.

**Additional shape-level negative probes:** `COMPARE` with 2 operands → None;
`COMPARE_PERIODS` / `CALCULATE` / `RANK` / `GROUP` → None; `metric="storage_injection"` → None;
`metric="incoming"` → None; `metric="balance"` / `"balance_section"` → None; destination of type
`balance` → None; no destination → None; no active scope → None; `export` + article → None.

## 11. PATCH usage audit

Repo-wide grep for `IntentPatch(`, `FieldMutation(`, `MutationAction.` across `src/`:

| # | file:line | Occurrence | Classification |
|---|---|---|---|
| 1 | `contracts.py:308-326` | `FieldMutation` model + action validation | Contract definition — not a producer |
| 2 | `contracts.py:328` | `IntentPatch` model | Contract definition |
| 3 | `execution_adapter.py:30` | `field_mutation.action != MutationAction.KEEP` | PR1 read-only inspection |
| 4 | `reducer.py:62-85` | Generic `_apply_field` KEEP/CLEAR/REFERENCE/SET/ADD/REMOVE | Generic application — not a producer |
| 5 | `processor.py:185` | `IntentPatch()` inside `_synchronize_effective_intent` | PR2a collapse — **empty** patch, by definition not a category |
| 6 | `processor.py:1704-1707` | `IntentPatch(operands=FieldMutation(SET, [operand]))` | **PR3 — GEO=SET** |
| 7 | `processor.py:4751-4754` | `IntentPatch(periods=FieldMutation(SET, periods))` | **PR2b — periods=SET** |

**Exactly two non-empty production PATCH categories exist: `periods=SET` and `operands=SET`
(GEO).** No `ADD`, `REMOVE`, `CLEAR` or `REFERENCE` producer exists in production. No hidden
entity/operation/grouping/comparison/route/business PATCH was added.

Other deterministic producers in `processor.py` (`_deterministic_geo_mutation`,
`_deterministic_reverse_mutation`, `_deterministic_extremum_comparison`,
`_deterministic_grouping_query`, `_deterministic_period_mutation`,
`_deterministic_balance_section_mutation`, `_deterministic_direction_comparison_mutation`,
`_deterministic_analyzed_flow_mutation`) all use `replace_intent`, i.e. full replacement, and are
therefore correctly outside the PATCH taxonomy. All are pre-existing and unmodified.

## 12. Test adequacy

`tests/test_geo_patch.py`, 829 lines, 101 tests. Coverage against the brief's checklist:

| Required coverage | Present | Reference |
|---|---|---|
| period → GEO | yes | `test_period_then_geo_then_period_persists_executed_intent:694` |
| GEO → period | yes | same test, Q4 |
| repeated GEO | yes | `test_repeated_geo_replacement_preserves_period:766` |
| country GEO | yes | `test_country_export_replacement_preserves_august:797` |
| region GEO | yes | `test_detector_resolves_canonical_geo_and_morphology:354` |
| business-entity collision | partial | `test_business_entity_has_priority_over_geo:443` — qualified forms only; no bare-alias case (see M1) |
| mixed GEO+period | yes | `test_period_geo_overlap_reaches_contextual_interpreter:817` |
| multi-GEO | yes | `test_false_positive_attack…:498-502` |
| compare fallback | yes | `test_unsupported_active_shape_falls_back:539`, `test_contextual_fallback_still_invokes_interpreter:618` |
| grouping fallback | yes | `:543`, `:525-526` |
| no active state | yes | `test_geo_patch_requires_active_context:610` |
| **actual planner/execution GEO** | **yes** | `test_geo_patch_changes_planner_and_scalar_execution_geo_without_llm:643` |
| 0 LLM | yes | `:657-658`, `:691`, `:762-763`, `:792-793`, `:814` |
| persistence/reload | yes | `:731`, `:749-752` |
| entity role order | yes | `test_geo_patch_preserves_production_entity_order_and_rebinds_article:392` |
| non-GEO field preservation | yes | `test_geo_patch_is_patch_only_and_changes_only_operands:370`, `:418` |
| PR2a normalization interaction | partial | covered transitively via `test_period_then_geo_then_period…`; no dedicated collapse test |

**Critical test-adequacy check — SATISFIED.** The brief requires at least one test proving
planner/execution input GEO equals the new canonical GEO rather than only asserting persisted
state. `test_geo_patch_changes_planner_and_scalar_execution_geo_without_llm`
(`tests/test_geo_patch.py:643-661`) asserts both
`planner.intents[-1].operands[0].entities[-1].entity.entity_id == SAMARA.geo_id` **and** that the
rendered execution query string contains `"Самарская область"` and does **not** contain
`"Ростовская область"`. `test_directed_flow_geo_patch_rebinds_execution_article_without_llm`
(`:664-691`) does the same for the article on the directed-flow path. The call graph (§7)
independently corroborates this structurally: `_execute_mutation` passes the *same* `intent`
object to `self.planner.plan(intent)` that `_synchronize_effective_intent` has just proven equal
to the committed intent. Neither the test nor the call graph is ambiguous, so no finding here.

**Gap (M1):** every one of the 101 tests runs against a synthetic 6-record registry and a
hand-written lemma map. The suite would pass unchanged if the real bundle produced zero matches —
which is exactly what I observed before installing the lemmatizer.

**Not weakened:** `--collect-only` ID diff shows 0 removed / 101 added, and no existing test file
appears in the diff.

## 13. Regression results

Re-run independently with `pytest 9.1.1 / pydantic 2.13.4 / CPython 3.13.13`, from trees extracted
via `git archive` into a scratch location at a directory depth that satisfies the
`Path(__file__).resolve().parents[2] / "pipeline"` assumption (NTFS junction to the real
`C:\#work\sl\pipeline`) — **not** from inside the nested worktree, avoiding the ~26-28 spurious
failures a prior review hit.

| Run | Result |
|---|---|
| Baseline `04d548d`, full suite | **282 passed, 1 warning, 0 failed** |
| PR3 `8981914`, full suite | **383 passed, 1 warning, 0 failed** |
| `tests/test_geo_patch.py` | **101 passed** |
| `test_geo_patch.py` + `test_period_patch.py` | **135 passed** |

All four claimed numbers reproduce exactly. 282 + 101 = 383. Test-ID diff: **0 removed, 101
added**, all in `tests/test_geo_patch.py`. 0 skipped. The single warning is the pre-existing
Starlette/httpx deprecation, identical on both commits. Growth was not achieved by deleting or
weakening prior tests, and could not have been — the diff touches no existing test file.

## 14. Golden/P0 verification

| Claim | Verified | Nature of the evidence |
|---|---|---|
| Golden catalog 8/8 | YES — `validated 8 Golden Queries: GQ-001..GQ-008` | `--validate-only`: canonical metadata binding checks; **no query execution** |
| P0 dry-run 84/84 | YES — `check_count 84, check_passed 84, check_failed 0`, 7 scenarios, 47 actions, `coverage_rate 100.0` | `execute_db: false`, `executed: 0`, `health: not_checked` |
| PR3 transition 20/20 | YES — 1 scenario, 4 turns, 5/5 checks each, 0 gaps, run with `--strict-coverage` | dry-run only |

**All three are explicitly dry-run / validation-only, not live-DB evidence** (see M4).
`AcceptanceRunner._dry_run_scenario` (`acceptance_runner.py:468-500`) marks every check
`passed=True` with actual value `"not executed"`, so a dry-run proves expectation lines parse and
map to automated checks — a coverage gate, not a behavioural result.

## 15. Live DB status

**Live DB-backed acceptance: NOT RUN, therefore NOT PROVEN.**

A clean separation is warranted:

- **STATE and ROUTING correctness — verified by me, independently, against the real metadata
  bundle.** Detection, canonical GEO resolution, patch construction, reducer application,
  PR2a normalization, evidence gating, planner input, the rendered execution query string,
  commit, reload, revision sequencing, and LLM call counts are all confirmed. Notably, the
  rendered execution query text itself changed from `…в ростовская…` to `…в Самарская обл…`,
  so this is stronger than state-equality evidence.
- **LIVE DB execution correctness — NOT verified and not claimable.** No PostgreSQL-backed run
  was performed. Whether the resulting SQL returns correct rows for Samara/April, whether the
  rebound article id resolves to the intended fact rows, and whether volumes are numerically
  correct are all unproven here.

The correct characterization of PR3 is **"state/routing correctness verified; live DB acceptance
NOT PROVEN"** — never "full production acceptance PASS". Not being able to safely run live
acceptance in this review environment is a limitation of the review, not a PR3 finding.

## 16. Scope creep

**None found.** PR3 is a strictly additive second detector, mirroring PR2b's structure.

Added production symbols (complete list, from `git diff | grep '^+\(class\|def\|@dataclass\)'`):

1. `@dataclass(frozen=True, slots=True) class GeoPatchCandidate` — mirrors the existing
   `PeriodPatchCandidate`.
2. `def _whole_geo_mention_matches(...)` — module-level helper.
3. `PipelineV2TurnProcessor._detect_geo_followup` — mirrors `_detect_period_followup`.
4. `PipelineV2TurnProcessor._deterministic_geo_patch` — mirrors `_deterministic_period_patch`.

Explicitly absent: no `PatchRouter`, `TurnClassifier`, `TransitionEngine`, `PatchRegistry` or
`DetectorRegistry` (grep confirms zero matches); no generic patch framework; no processor
decomposition; no business-entity, grouping, comparison, ranking, route or operation PATCH.

**Summary policy centralization confirmed.** `_should_summarize` remains the single helper
(`processor.py:4618`). Exactly three `summarize_envelope` call sites exist — `_execute_mutation`
(`:2597`, native scalar), `_execute_full_balance_mutation` (`:2805`, full-balance family incl.
directed flow), `_standalone` (`:2919`) — and all three gate on it. There is no fourth site, and
a grep for `interpretation_mode == "` / `!= "` returns **zero** literal comparisons anywhere in
`src/`, so the PR2b-era leak pattern has not been reintroduced. I confirmed 0 summarizer calls
empirically on both execution shapes a GEO PATCH can actually reach.

**API compatibility:** `src/balance_chat/api.py` and `service.py` are untouched;
`TurnProcessResult` is unchanged; the response shape is unchanged. The only externally visible
additions are a new `interpretation_mode` value (`deterministic_geo_patch`) in diagnostics and a
new structured log event (`deterministic_geo_patch_recognized`), both consistent with how PR2b
surfaced `deterministic_period_patch`. Clients need no knowledge of the internal mutation type.

## 17. Demo readiness

**Ready, with two operational preconditions.**

The exact demo path — new query → period PATCH → GEO PATCH → period PATCH — was executed
end-to-end against the real metadata registry and real planner, and behaved correctly at every
turn (0 LLM calls on turns 2-4, correct period/GEO on all four, revisions 1→2→3→4,
executed == committed == reloaded throughout, and the execution query string genuinely switching
geography).

Preconditions before demoing:

1. **Verify `mawo-pymorphy3` is installed on the demo host** (M2). Without it the GEO PATCH
   silently never fires while the period PATCH continues to work — a confusing half-broken demo.
   Check `pipeline_v2.nlp_ru.nlp_status()["mawo_analyzer_available"] is True`.
2. **Rehearse the exact GEO phrases** (L1). Coverage depends on bundle alias arity:
   `Самарская`, `Ростовская`, `Московская`, `Воронежская`, `Липецкая` and `Татарстан` work;
   `Тульская`, `Рязанская`, `Белгородская`, `Калужская` fall back to the LLM. Falling back is
   safe and correct, but it does not demonstrate the feature.

Live-DB numeric correctness remains unproven (§15); demo on a dataset already validated by the
Golden/P0 live runs.

## Final Q&A

### Q1 — Does the sequence Rostov/May → April → Samara → May produce the correct state after every turn?
**YES.** Verified end-to-end against the real metadata registry and real planner:
rev1 Rostov/May → rev2 Rostov/April (`deterministic_period_patch`) → rev3 Samara/April
(`deterministic_geo_patch`) → rev4 Samara/May (`deterministic_period_patch`). State evolves along
independent dimensions with no reassembly and no partial loss.

### Q2 — LLM call count for deterministic GEO PATCH?
**0.** Interpreter 0 and summarizer 0, confirmed on both reachable execution shapes (native scalar
and full-balance-family/`unified_directed_flow`), and 0 across a 5-step repeated GEO round-trip.

### Q3 — Does period survive GEO PATCH?
**YES.** April 2025 preserved through the Samara patch; also verified over 5 consecutive GEO
replacements with zero period drift.

### Q4 — Does GEO survive a subsequent period PATCH?
**YES.** Samara preserved through the Q4 May period patch, from committed-and-reloaded state.

### Q5 — Can "ГП ТГ Москва" be misclassified as GEO Moscow?
**NO.** Rejected, along with `Газпром трансгаз Москва`, `ТГ Москва`, `ТГ Ухта`,
`ГП ТГ Нижний Новгород`, `ООО Газпром трансгаз Самара` and 16 further qualified/inflected
variants — 22/22 rejected. (Bare unqualified `Москва` does resolve as GEO; see M3 for why that is
the system's consistent policy rather than a misclassification.)

### Q6 — Can "А по Москве за апрель?" be partially captured by a deterministic PATCH?
**NO.** Both detectors return `None` — the period regex is anchored and declines; the GEO
detector's whole-mention arity check declines. The turn reaches the contextual interpreter
exactly once. All 6 GEO+period phrases and all 68 mixed-turn phrases behaved identically.

### Q7 — Does the actual planner/execution GEO change, not just stored state?
**YES.** Planner input destination changed `geo:d09b4af…` → `geo:fdf2b36…`, and the rendered
execution query string changed from
`Покажи суммарное значение распределения газа в ростовская за период с 2025-04-01 по 2025-05-01`
to `…в Самарская обл за период с 2025-04-01 по 2025-05-01`. On the directed-flow shape the
article also rebound (`ART:2010000039808` → `ART:2010000039841`) with the balance unchanged.

### Q8 — Is executed == committed == reloaded confirmed for GEO PATCH?
**YES.** Asserted at every turn of the real-registry E2E, and structurally enforced by
`_synchronize_effective_intent` raising `TurnProcessingError("executed effective intent would
differ from committed intent")` (`processor.py:189-196`) if they ever diverge.

### Q9 — Are there exactly two production PATCH categories (periods=SET, GEO=SET)?
**YES.** `processor.py:4751` `periods=SET` and `processor.py:1704` `operands=SET`. The only other
production `IntentPatch(` is the deliberately empty one in PR2a's collapse
(`processor.py:185`). No ADD/REMOVE/CLEAR/REFERENCE producer exists.

### Q10 — Is there an observable regression vs PR2b?
**NO.** 282 → 383 passed, 0 failed, 0 removed tests, identical single pre-existing warning.
Period PATCH behaviour unchanged; Golden 8/8 and P0 84/84 unchanged.

### Q11 — Is live DB-backed acceptance confirmed?
**NO — NOT RUN.** State and routing correctness are verified; live DB execution correctness is
not claimable from this review.

### Q12 — Is this ready for a controlled user demo of new-query → period PATCH → GEO PATCH → period PATCH?
**YES**, subject to the two operational preconditions in §17: confirm `mawo-pymorphy3` is present
on the demo host, and rehearse the specific GEO phrases (coverage varies by bundle alias arity).
The exact four-turn flow was executed end-to-end and behaved correctly at every turn.
