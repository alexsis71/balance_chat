# PR4 independent adversarial review — deterministic Business Entity PATCH

Reviewer: independent senior/staff engineer (read-only on production code)
Branch: `feat/business-entity-patch-pr4`, tip `ca0bdc3`
Baseline: `9fd1a47` (PR3.5 merge), differential reference `a1f3383` (ancestor of `9fd1a47`)
Interpreter: `C:\Users\alexs\miniforge3\envs\ai_env\python.exe` (Python 3.14.2)
Metadata: real production bundle `2026.08.1` (54 balances, 210 geo objects) from `C:\#work\sl\pipeline`

## 1. Verdict

**APPROVE**

## 2. Executive summary

PR4 adds exactly one new semantic PATCH category (Business Entity SET) as a new
module inside the already-approved `transitions/` package. All three required
properties are proven.

- **A. Semantic correctness — proven.** Traced end-to-end against the real
  metadata bundle. A business PATCH changes only the `balance`-role entity and,
  when a direction-bound article is present, rebinds that article to the article
  **owned by the new balance**. GEO, period, metric, aggregate, operation and
  grain survive byte-identically.
- **B. Safety — proven.** 1,164 real-bundle phrases swept in both directions:
  324 qualified-business phrases produced **0** GEO patches; 840 bare-geo phrases
  produced **0** business patches. An 81-phrase hand-built adversarial corpus
  produced **0** false positives. Every mixed/multi-field/ambiguous turn falls
  through to the existing contextual path with no partial mutation.
- **C. Architectural integrity — proven.** `processor.py` gained +16/−0 lines of
  pure wiring: an import, one services-struct construction, one evidence
  passthrough, and one entry in the centralized summary-skip set. No ТГ regex, no
  alias table, no business-vs-GEO logic, no mutation construction returned to the
  processor.

The top-priority concern — *state looks correct while execution stays bound to
the old business entity's article* — **does not occur**. Traced with real IDs:
active state Moscow → Vladimir on Moscow's article `ART:2010000039929`; after
`"Только из ГП ТГ Ухта"` the planner input, the committed intent and the reloaded
intent all carry Ukhta's article `ART:2010000039929 → ART:2010000040926`, and
`execute_balance_day` is invoked with `balance_id=2010000040932` (Ukhta). When the
new business has **no** article for the current geo, the whole candidate fails
closed to the LLM path rather than silently keeping the old article (verified on
17 real geo/balance combinations).

Regression: full pytest **501 passed / 0 failed / 1 pre-existing warning** vs
baseline **422 passed / 0 failed**. Test-node-ID diff: **0 removed, 79 added**.
Differential vs baseline over an 81-row corpus: 67 rows byte-identical, 14 rows
differ, and **every** delta is exactly `LLM_FALLBACK → deterministic_business_entity_patch`.

No blockers. No high findings. Four low findings, all non-blocking; the most
useful one is a test-hygiene issue where pinned pytest node IDs hide two flipped
expectations from exactly the node-ID audit this review series relies on.

## 3. Blockers

None.

## 4. High findings

None.

## 5. Medium/Low findings

### Finding 1 — Pinned pytest node IDs conceal two flipped expectations from the node-ID audit

```
Finding: tests/transitions/test_classifier.py pins the pytest node IDs of two
         pre-existing parametrized cases while inverting their expected value,
         so the test-node-ID diff reports a purely additive delta (0 removed /
         79 added) even though two established expectations changed.
Severity: LOW
Evidence: tests/transitions/test_classifier.py:160-171
          pytest.param("А по ГП ТГ Москва?", "deterministic_business_entity_patch",
                       True, id="А по ГП ТГ Москва?-None-True")
          pytest.param("А для Газпром трансгаз Москва?",
                       "deterministic_business_entity_patch", True,
                       id="А для Газпром трансгаз Москва?-None-True")
          Baseline 9fd1a47 had these as plain tuples expecting None.
          Introduced by commit dc643de "test: preserve PR3.5 classifier node identities".
Why it matters: the node ID still encodes "-None-", asserting the opposite of what
          the test now checks. Every review in this series uses the node-ID diff as
          the primary evidence that nothing was silently weakened or removed. A
          pinned ID makes a genuine expectation change invisible to that exact
          control. The behavior change itself is correct and intended (it is the
          PR4 feature); only the audit signal is degraded.
Reproduction:
          git show 9fd1a47:tests/transitions/test_classifier.py | grep "ГП ТГ Москва"
            -> ("А по ГП ТГ Москва?", None, True)
          git show ca0bdc3:tests/transitions/test_classifier.py | grep -A4 "ГП ТГ Москва"
            -> expects deterministic_business_entity_patch, id pinned to "-None-True"
Minimal correction: drop the id= overrides and accept the node-ID churn, then list
          the two intentional expectation changes in the node-ID audit table of
          docs/business_entity_patch_pr4.md §15.
```

### Finding 2 — Two shape guards are unreachable; the contract, not the module, enforces single-entity roles

```
Finding: _replace_distribution_viewpoint's `len(balances) > 1` and
         `len(articles) > 1` branches cannot be reached, because AnalysisOperand
         already rejects duplicate entity roles at contract-validation time.
Severity: LOW
Evidence: src/balance_chat/transitions/business_entity.py:118
            if len(balances) > 1 or len(destinations) != 1 or len(articles) > 1:
          Contract validator rejects construction:
            AnalysisOperand(entities=[bal_ent(MOSCOW), bal_ent(NIZHNY)])
            -> ValidationError: "operand entity roles must be unique"
Why it matters: only a documentation/《what actually protects me》 concern. The
          "never replace the wrong one of several balances" property is real, but
          it is guaranteed one layer lower than the module suggests. Worth knowing
          if the operand contract is ever relaxed to allow repeated roles — the
          module guard would then become live and load-bearing.
Reproduction: constructing a two-balance or two-destination operand raises
          pydantic ValidationError before the detector is ever consulted.
Minimal correction: none required. Optionally note in the module docstring that
          role uniqueness is a contract invariant.
```

### Finding 3 — Article rebinding is only covered in-repo against a synthetic registry

```
Finding: the PR's own tests exercise the direction-article rebinding solely
         through a hand-built SimpleNamespace registry; the single real-bundle
         test checks alias resolution only, not rebinding.
Severity: LOW
Evidence: tests/test_business_entity_patch.py:123-163 (class _Registry, synthetic)
          tests/test_business_entity_patch.py:349 test_direction_bound_article_is_
            rebound_before_balance_day_execution  -> uses _Registry
          tests/test_business_entity_patch.py:510 test_ready_metadata_aliases_and_
            known_short_nizhny_gap -> real bundle, alias resolution only
Why it matters: the highest-risk behavior in PR4 (stale article) is validated only
          against a fixture the PR author also wrote. A metadata-shaped surprise
          (duplicate geo records, article naming drift) would not be caught.
Reproduction: n/a — coverage observation.
Minimal correction: add one real-bundle test asserting
          Moscow/Vladimir(ART:2010000039929) + "Только из ГП ТГ Ухта"
          -> ART:2010000040926. I verified this independently and it passes
          (see §12); the PR simply does not pin it.
```

### Finding 4 — Documented follow-up grammar is narrower than the implemented regex; `_lookup_geo`'s first branch is dead

```
Finding: (a) docs §3 lists only the "А ..."-prefixed frames, but _FOLLOWUP also
         accepts bare "Для X", "По X", "Теперь для X". (b) _lookup_geo's first
         resolution attempt (by entity_id) never succeeds against the real bundle.
Severity: LOW
Evidence: src/balance_chat/transitions/business_entity.py:36-38
            r"(?:(?:а\s+)?(?:теперь\s+)?(?:для|по)|только\s+из)\s+(?P<business>.+)"
            -- the leading "а" and "теперь" are both optional.
          docs/business_entity_patch_pr4.md:41-46 lists only "А для / А по /
            А теперь для / Только из".
          src/balance_chat/transitions/business_entity.py:67
            for value in (reference.entity.entity_id, reference.entity.display_name)
            -- measured over all 210 geo objects: registry.geo(geo_id) resolved 0/210;
               registry.geo(display_name) resolved 210/210.
Why it matters: both are benign. The extra frames are legitimate and behave
          correctly ("Для ГП ТГ Ухта" and "По ГП ТГ Ухта" both patch correctly and
          were in my corpus). The dead entity_id branch costs one failed lookup and
          is harmless because the display_name fallback is exact.
Reproduction: see §20 corpus rows "Для ГП ТГ Ухта" / "По ГП ТГ Ухта"; see §12
          round-trip table.
Minimal correction: extend the docs' frame list, or drop the entity_id branch.
          Neither affects behavior.
```

### Observation (not a finding) — pre-existing duplicate geo records limit GEO-patch reach

The bundle contains 20 stems with two geo records each (`'владимирская'`
geo:96d5… vs `'владимирская обл'` geo:10ef…; `'тверская'` geo:9f0c… vs
`'тверская обл'` geo:beae…). `_tagged_geo_objects` resolves the short record while
distribution articles are named after the long one, so `"А по Тверской области?"`
fails closed on a state that carries an article. This is **PR3.5 behavior,
unchanged by PR4** — verified byte-identical on the baseline tree
(`git diff 9fd1a47..ca0bdc3 -- src/balance_chat/transitions/geo.py` is empty, and
the differential in §21 shows these rows identical). Reported only so it is not
mistaken for a PR4 regression.

## 6. Actual supported subset (documented vs actual)

| Aspect | Documented (docs/business_entity_patch_pr4.md) | Actual (from code + execution) | Match |
|---|---|---|---|
| Operations | `SHOW`/`AGGREGATE` distribution; `SHOW` full balance | `SHOW`+distribution ✓, `AGGREGATE`+distribution ✓, `SHOW`+balance ✓, `AGGREGATE`+balance ✗ | ✅ exact |
| Metrics | `distribution`, `balance` | `distribution`, `balance` only; `export` rejected | ✅ |
| Replaceable role | `balance` only | `balance` only (replaced, or inserted at index 0 if absent) | ✅ |
| Article | rebound for the new balance, mandatory | rebound via `resolve_direction_article`; `None` ⇒ whole candidate `None` | ✅ |
| Destination | never touched | never touched (confirmed over 324 sweep phrases) | ✅ |
| Frames | `А для` / `А по` / `А теперь для` / `Только из` | also bare `Для` / `По` / `Теперь для` | ⚠️ docs narrower (Finding 4) |
| Qualifiers | `ТГ` / `ГП ТГ` / `Газпром трансгаз` / `ООО Газпром трансгаз` | identical, prefix-anchored, `len(tokens) > 1` | ✅ |
| Aliases | via existing metadata registry, no new table | `services.lookup_balance` → `registry.balance`; no alias dict anywhere in the diff | ✅ |
| `Только в` (destination form) | not claimed | not supported — verified falls back | ✅ boundary, not a defect |
| Same-balance no-op | no-match | returns `None` → LLM fallback | ✅ |
| Physical representation | `operands=SET`, no new top-level field | `IntentPatch(operands=FieldMutation(SET, ...))` only | ✅ |
| `ТГ Нижний Новгород` gap | documented limitation §17 | confirmed unresolved; falls back, never becomes GEO | ✅ |
| Live DB | "NOT RUN" | confirmed not run | ✅ honest |
| Test counts | 34 / 103 / 79 / 216, 422→501, 0 removed | reproduced exactly | ✅ |

The PR's documentation is unusually accurate. The only mismatch is the narrower
documented frame list (Finding 4).

## 7. Core transition verification

Real bundle, real IDs. Active: `SHOW` / `distribution` / balance `ГП ТГ Москва`
(`BAL:2010000039953`) → destination `владимирская обл` (`geo:10efdfe15c829613d4ab`),
article `ART:2010000039929`, period `2025-05-01..2025-06-01`.

Turn: `"Только из ГП ТГ Ухта"`.

```
MODE       : deterministic_business_entity_patch
OUTCOME    : success
EXEC LAYER : unified_directed_flow
ACTIVE     : bal=BAL:2010000039953 dest=geo:10efdfe15c829613d4ab art=ART:2010000039929 period=2025-05-01..2025-06-01
EFFECTIVE  : bal=BAL:2010000040932 dest=geo:10efdfe15c829613d4ab art=ART:2010000040926 period=2025-05-01..2025-06-01
PLANNER    : bal=BAL:2010000040932 dest=geo:10efdfe15c829613d4ab art=ART:2010000040926 period=2025-05-01..2025-06-01
execute_balance_day(balance_id=2010000040932, day='2025-05-01', request_id='stale:balance')
filtered row -> 'Владимирская обл.'
interpreter calls = 0 ; summarizer calls = 0
effective == planner == committed == reloaded : True
revision 1 -> 2
```

Operation, metric, aggregate, grain, period and destination unchanged; only the
`balance` role and the direction-bound `article` changed. The scalar variant (no
article) renders the query string
`"...по балансу ГП ТГ Ухта суточный баланс в Владимирская обл за период с 2025-05-01 по 2025-06-01"` —
execution genuinely names the new business.

## 8. Sequential state verification (forward and reverse order)

Every turn: `effective == planner input == committed == reloaded`, revision +1
exactly once, 0 summarizer calls.

Forward (period → geo → business → period), start Moscow/Vladimir/May:

| Turn | Message | Mode | Rev | Agreement |
|---|---|---|---|---|
| T2 | `А за апрель?` | `deterministic_period_patch` | 1→2 | ✅ |
| T3 | `А по Тверской области?` | LLM fallback (pre-existing, §5 observation) | — | — |
| T4 | `Только из ГП ТГ Ухта` | `deterministic_business_entity_patch` | 2→3 | ✅ |
| T5 | `А за май?` | `deterministic_period_patch` | 3→4 | ✅ |

T4 preserved the April period and the Vladimir destination; T5 preserved the Ukhta
balance and its rebound article. Both required directions hold.

All six permutations of {business, geo, period} were run through
`InMemoryContextStore` with commit + reload after each turn:

```
business,geo,period   geo,business,period   period,geo,business
geo,period,business   business,period,geo   period,business,geo
```

**All 6 converge to an identical `AnalysisIntent`** — `bal=BAL:2010000040932`,
`dest=geo:10efdfe15c829613d4ab`, `art=ART:2010000040926`, `period=2025-04-01..2025-05-01`.
The dimensions are order-independent. (The geo leg is a no-op in each permutation
for the pre-existing duplicate-geo reason; the business↔period independence is
fully exercised, and business↔geo independence is separately confirmed in §9 where
the destination never moves across 324 business patches.)

## 9. Business-vs-GEO audit (both directions)

**Forward — exhaustive sweep over the real bundle.** All 54 balances × 6 qualified
frames (`А для ГП ТГ X?`, `А для ТГ X?`, `А для Газпром трансгаз X?`,
`А по ГП ТГ X?`, `Только из ГП ТГ X`, `А для ООО Газпром трансгаз X?`) = **324 phrases**:

```
business patch                : 108
safe fallback (LLM)           : 216
errors                        :   0
GEO PATCH (must be zero)      :   0   <-- PASS
wrong balance bound           :   0   <-- PASS
destination geo moved         :   0   <-- PASS
```

Every one of the 11 collision cities resolves as a business entity across all
qualified forms, with the destination untouched:

| City | `ГП ТГ` | `Газпром трансгаз` | `ТГ` |
|---|---|---|---|
| Москва | BAL:2010000039953 | BAL:2010000039953 | BAL:2010000039953 |
| Самара | BAL:2010000040189 | BAL:2010000040189 | BAL:2010000040189 |
| Уфа | BAL:2010000040822 | BAL:2010000040822 | BAL:2010000040822 |
| Волгоград | BAL:2010000039042 | BAL:2010000039042 | BAL:2010000039042 |
| Екатеринбург | BAL:2010000039241 | BAL:2010000039241 | BAL:2010000039241 |
| Казань | BAL:2010000039329 | BAL:2010000039329 | BAL:2010000039329 |
| Томск | BAL:2010000040765 | BAL:2010000040765 | BAL:2010000040765 |
| Саратов | BAL:2010000040307 | BAL:2010000040307 | BAL:2010000040307 |
| Ставрополь | BAL:2010000040419 | BAL:2010000040419 | BAL:2010000040419 |
| Краснодар | BAL:2010000039425 | BAL:2010000039425 | BAL:2010000039425 |
| Нижний Новгород | BAL:2010000040110 | BAL:2010000040110 | **fallback** (documented alias gap) |

`ТГ Нижний Новгород` is unresolvable in this bundle and falls through to the LLM —
crucially it does **not** become `GEO Нижний Новгород`, which was the stated
BLOCKER condition.

**Reverse — exhaustive sweep.** All 210 geo objects × 4 bare-geo frames
(`А по X?`, `А для X?`, `По X`, `А теперь по X?`) = **840 phrases**:

```
geo patch                       : 576
safe fallback (LLM)             : 264
BUSINESS PATCH (must be zero)   :   0   <-- PASS
```

All 11 collision cities in bare form produce `deterministic_geo_patch` with the
balance left unchanged at `BAL:2010000040932`.

**Handoff mechanism.** `geo.py` is byte-identical to baseline
(`git diff 9fd1a47..ca0bdc3 -- src/balance_chat/transitions/geo.py` is empty). Its
pre-existing gate at `geo.py:102-112` declines whenever the mention is
qualified-business **and** `lookup_balance` resolves it. The business gate is a
strict subset of the geo gate:

```
business (business_entity.py:46-56): tokens[0]=="тг" | ["гп","тг"] | ["газпром","трансгаз"] | ["ооо","газпром","трансгаз"]
geo      (geo.py:102-106)          : "тг" in tokens | ["газпром","трансгаз"] | ["ооо","газпром","трансгаз"]
```

Every business-qualified form contains `тг` or the `газпром трансгаз` prefix, so
geo's gate always fires first and defers. The two modules use *different*
normalizers (geo uses the pymorphy lemmatizer, business uses a regex casefold), so
I probed 23 inflected/punctuated/cased variants specifically for a hole:
`ТГ Ухты`, `ТГ Москвы`, `ТГ-Москва`, `ТГ.Москва`, `ГП-ТГ Москва`, `А ДЛЯ ТГ МОСКВА?`,
`А для Москва ТГ?`, `А для области ТГ Москва?`, … — **0 became a GEO patch**.
Inflected forms fall back safely; punctuation variants patch correctly.

The fuzzy `SequenceMatcher` path in `_whole_geo_mention_matches` is unreachable for
qualified mentions because the qualified-business short-circuit precedes it, and it
additionally requires exact token-count equality, which a `ТГ`-prefixed mention
cannot satisfy against a bare geo label.

## 10. Role-selection audit

Selection is derived from the active shape, never from language inference.

| Shape | Selection rule | Verified |
|---|---|---|
| `SHOW` + `metric="balance"` + exactly 1 entity with `role=="balance"` and `entity_type=="balance"` | replace that sole entity | ✅ |
| `metric="distribution"` | replace the sole `balance`; insert at index 0 if none | ✅ |
| any shape | `destination` never touched | ✅ (0/324 moved) |
| article present | requires a pre-existing `balance` **and** `aggregate_type=="sum"` | ✅ |

`business_entity.py:100` restricts roles to `{balance, destination, article}` and
`business_entity.py:120` requires `len(balances)+len(destinations)+len(articles) == len(entities)`,
so no unaccounted entity can ride along. Empirically:

| Active shape | Result |
|---|---|
| balance + destination + article (`sum`) | `deterministic_business_entity_patch` |
| zero destinations (balance only, distribution) | LLM fallback |
| article **without** balance | LLM fallback |
| balance + destination + article, `aggregate_type="avg"` | LLM fallback |
| balance + destination + article, `aggregate_type="max"` | LLM fallback |
| `route` role present | LLM fallback |
| destination is a balance, not a geo_object | LLM fallback |
| two operands | LLM fallback |

This is not "replace the first business entity found" and not "replace all". Note
per Finding 2 that duplicate-role shapes are additionally impossible at the
contract layer.

## 11. Entity-list preservation audit

For every successful business PATCH the operand is deep-copied
(`business_entity.py:129`) and only the targeted indices are overwritten. Measured
across the 324-phrase sweep and the core traces:

- destination entity id: unchanged in 108/108 successful patches;
- entity ordering: preserved (replacement occupies the original index; the insert
  path yields `[balance, destination]`, index 0);
- `operand_id`, `metric`, `aggregate_type`: unchanged;
- `periods`, `grain`, `operation`: unchanged (patch touches `operands` only);
- `replace_intent` is `None` — the mutation is patch-only, so nothing outside
  `operands` can be disturbed.

The full-balance path replaces the entity list wholesale with a single new
`balance` entity, which is correct because that shape is gated to exactly one
entity (`business_entity.py:78`).

## 12. Stale evidence/article audit — top priority

This is the section the requesting engineer cared about most. It is clean, and I
verified it against the **real** bundle rather than a mock.

**Why it is structurally safe.** The rebinding calls
`services.resolve_direction_article(business, geo, "distribution")`, wired in
`processor.py:647-654` to `_unique_direction_article(registry, balance=<new business>, ...)`.
That helper iterates `registry.articles_for_balance(balance.balance_id)`
(`processor.py:3533`) — the candidate set is **scoped to the new balance's own
articles**, so an old-business article is not merely unlikely, it is not in the
search space. There is no closure over the old business and no cache.

Confirmed the returned article is genuinely owned by the balance passed in:

| Balance passed | Returned article (target = `владимирская обл`) | Owned by that balance |
|---|---|---|
| ГП ТГ Москва (2010000039953) | ART:2010000039929 | ✅ |
| ГП ТГ Ухта (2010000040932) | ART:2010000040926 | ✅ |
| ГП ТГ Н.Новгород (2010000040110) | ART:2010000040027 | ✅ |

**End-to-end trace with real IDs** (full output in §7): old article
`ART:2010000039929` (Moscow's) → new article `ART:2010000040926` (Ukhta's), present
in the **planner input**, the executor path, the committed intent and the reloaded
intent. `execute_balance_day` receives `balance_id=2010000040932`, and
`_filter_directed_flow_envelope` filters the returned rows by the **new** article,
yielding the Ukhta row. The old article ID appears nowhere downstream.

**Fail-closed when no rebinding exists.** 17 real geos have a Moscow distribution
article but no Ukhta one. On all sampled cases the result is LLM fallback with
`planner calls = 0` — the turn never reaches execution, and the old article is not
retained:

| Geo | Old article | Result |
|---|---|---|
| белгородская обл | ART:2010000039919 | LLM fallback, 0 planner calls |
| брянская обл | ART:2010000039930 | LLM fallback, 0 planner calls |
| воронежская | ART:2010000039841 | LLM fallback, 0 planner calls |
| воскресенск | ART:2010000039820 | LLM fallback, 0 planner calls |

**Geo re-resolution fidelity.** `_lookup_geo` (`business_entity.py:63-70`)
re-resolves the destination before rebinding. If it ever returned a *different* geo
record than the one in the operand, the article would be rebound against the wrong
geo — silent corruption. Measured over **all 210** geo objects:

```
resolved via entity_id      :   0   (dead branch, Finding 4)
resolved via display_name   : 210
unresolvable (fail closed)  :   0
RESOLVED TO A DIFFERENT GEO :   0   <-- PASS
```

So the article is always rebound against exactly the geo the operand carries.

**Service-hook fail-closed** (commit 1538b29). With each hook disabled the
detector returns `None` rather than proceeding:

| Hook state | Result |
|---|---|
| `lookup_balance=None` | `None` (no patch) |
| `lookup_geo=None`, article-bearing shape | `None` (no patch) |
| `resolve_direction_article` returns `None` | `None` (no patch) |
| `lookup_geo=None`, **no-article** shape | patch proceeds (geo not needed) — correct |

**Verdict: no stale-article path exists.** Execution genuinely switches to the new
entity, and where it cannot, it refuses rather than guessing.

## 13. Production execution call graph

```
PipelineV2TurnProcessor.process()                       processor.py:207
└── _process_contextual()                               processor.py:613   (active scope present)
    └── detect_deterministic_transition(...)            transitions/classifier.py:13
        ├── detect_period_transition                    transitions/period.py    (1st)
        ├── detect_geo_transition                       transitions/geo.py       (2nd)
        └── detect_business_entity_transition           transitions/business_entity.py:189 (3rd)
            └── detect_business_entity_followup         business_entity.py:151
                ├── _FOLLOWUP.fullmatch(_normalize_text)        :166
                ├── _is_qualified_business                      :170
                ├── services.lookup_balance -> registry.balance :172
                └── _replace_full_balance | _replace_distribution_viewpoint
                    └── services.resolve_direction_article
                        -> _unique_direction_article(registry, balance=NEW, ...)  processor.py:3519
    └── _dispatch_mutation(evidence_businesses=..., evidence_geos=...)  processor.py:670
        └── _materialize_mutation / _synchronize_effective_intent       processor.py:149 / :180
            └── _execute_mutation | _execute_full_balance_mutation      processor.py:2319 / :2590
                └── planner.plan(intent) -> executor -> runtime.execute
                                          | runtime.execute_balance_day  processor.py:2639
                    └── _filter_directed_flow_envelope(envelope, NEW article)  processor.py:2682
```

Business-specific reasoning terminates entirely inside
`transitions/business_entity.py`; everything below `_dispatch_mutation` is the
shared, pre-existing path.

## 14. Transition-layer architectural audit

`business_entity.py` (224 lines) does **only**: recognize, extract mention,
validate qualification, resolve through injected services, validate active shape,
build a `ContextMutation`, return a `TransitionDecision`. It does not execute,
plan, commit, reload, summarize, invoke an LLM, or own persistence.

Imports (`business_entity.py:1-19`) are `dataclasses`, `re`, `types`, `typing`,
`..contracts`, `.types` — nothing else. Dependency direction is one-way:

```
processor -> transitions -> contracts        (verified)
contracts/reducer/binding/execution_adapter/planning/execution -> transitions : NONE
```

`grep -rn "transitions" src/balance_chat/{contracts,reducer,binding,execution_adapter,planning,execution}.py`
returns empty. No circular or lazy imports.

**Special-case growth / god-router check.** The classifier gained exactly one
sequential branch and no phrase-specific exceptions:

```python
period = detect_period_transition(...)      # unchanged
if period is not None: return period
geo = detect_geo_transition(...)            # unchanged
if geo is not None: return geo
business = detect_business_entity_transition(state, message, turn_id, business_services)
return business if business is not None else TransitionDecision.no_match()
```

Net classifier delta: +12/−1 lines, all structural. **Zero** bespoke phrase
branches. The boundary held under its first real extension.

## 15. Processor boundary audit

Complete set of lines PR4 adds to `processor.py` (+16/−0, no deletions):

```
+    BusinessEntityTransitionServices,                       (import)
+            business_services=BusinessEntityTransitionServices(
+                lookup_balance=getattr(self.registry, "balance", None),
+                lookup_geo=getattr(self.registry, "geo", None),
+                resolve_direction_article=lambda balance, target, metric: (
+                    _unique_direction_article(self.registry, balance=balance,
+                                              target=target, metric=metric)),
+                make_entity=_typed_entity,
+            ),
+                evidence_businesses=transition.evidence_businesses,
+        "deterministic_business_entity_patch",                (summary-skip set)
```

No ТГ regex, no alias table, no phrase parsing, no business-vs-GEO logic, no
mutation construction. `grep` over the added `src/` lines for
`тг|газпром|трансгаз|ооо` matches **only** the four qualification lines inside
`transitions/business_entity.py` — none in `processor.py`.

`evidence_businesses` was already a `_dispatch_mutation` parameter before PR4
(`processor.py:143`, used by the standalone path at 279/340/357/375). PR4 reuses
it rather than adding a channel. `registry.geo` is likewise pre-existing
(`binding.py:173`, `compat/envelope_translation.py:155`).

## 16. PATCH producer audit

Every non-empty `IntentPatch` construction in production:

| # | File:line | Semantic category | Physical field | Action |
|---|---|---|---|---|
| 1 | `transitions/period.py:204` | Period SET | `periods` | `SET` |
| 2 | `transitions/geo.py:182` | GEO SET | `operands` | `SET` |
| 3 | `transitions/business_entity.py:205` | Business Entity SET | `operands` | `SET` |
| — | `processor.py:193` | *(empty)* normalization reset | — | — |

`FieldMutation(` appears exactly 3 times in production, all `MutationAction.SET`.
No `ADD`/`REMOVE`/`REFERENCE`/`CLEAR` producers. No route, grouping, comparison,
operation, direction or ranking patches exist.

**Important distinction:** there are **3 semantic categories** but only **2
distinct physical `IntentPatch` fields** in use (`periods` and `operands`), because
GEO SET and Business Entity SET both express themselves through `operands=SET`.
A field-count audit would report "2" — that must not be misread as "only 2
categories".

## 17. Interpretation-mode audit

Exactly one production producer of `deterministic_business_entity_patch`:
`transitions/business_entity.py:215`. The only other occurrences in `src/` are the
diagnostic event name (`business_entity.py:217`) and the summary-skip set entry
(`processor.py:4446`). Mirrors period (`period.py:214-215`) and geo
(`geo.py:192-194`) exactly. No duplicate detector anywhere in the repo.

Diagnostics emitted:

```
event  : deterministic_business_entity_patch_recognized
fields : {'mutation_mode': 'business_entity_patch', 'balance_id': '2010000040932'}
evidence_businesses : (<balance record 2010000040932>,)
evidence_geos       : ()
patch fields set    : ['operands']
replace_intent      : None
```

## 18. LLM-call audit

`_should_summarize` (`processor.py:4442`) is the single centralized gate, consulted
at all three summary call sites — `_execute_mutation` (`:2484`),
`_execute_full_balance_mutation` (`:2692`), `_standalone` (`:2806`). PR4 adds
`deterministic_business_entity_patch` to its exclusion set.

Measured by running code, not reading it:

| Path exercised | Layer | Interpreter calls | Summarizer calls |
|---|---|---|---|
| distribution + article | `unified_directed_flow` (`_execute_full_balance_mutation`) | **0** | **0** |
| distribution, no article | scalar (`_execute_mutation`) | **0** | **0** |
| full balance | `unified_balance_level` (`_execute_full_balance_mutation`) | **0** | **0** |
| 6 permutation sequences | mixed | **0** | **0** |
| 108 successful sweep patches | mixed | **0** | **0** |

The interpreter was wired to raise on invocation, so any call would abort the run.
`_standalone` is unreachable for this mode (it is only entered when there is no
active scope, where the business detector returns `None` immediately); the gate
addition there is consistency, not a live path.

## 19. Persistence/revision audit

Every deterministic turn advances the revision exactly once, with no double-commit
and no skipped revision. Across the forward 5-turn sequence and all 6 permutations,
run through `InMemoryContextStore` with an explicit `store.get()` reload each turn:

```
revision 1 -> 2 -> 3 -> 4        (forward, one increment per applied turn)
effective == planner input == committed == reloaded : True at every step
```

`processed.mutation.replace_intent is None` and
`processed.mutation.patch.operands.action == SET` for business turns, so
persistence stores a patch, not a snapshot — matching period/GEO. A turn that
falls back or yields `no_data` correctly does **not** advance the committed intent.

## 20. Adversarial attack

I built an 81-phrase corpus (the mandated 12 phrases plus 69 more) across
12 categories, run through the **real detector** via `PipelineV2TurnProcessor.process()`
on three active shapes (distribution+article, distribution without article, full
balance).

```
TOTAL = 81
business-patch FALSE POSITIVES = 0
```

Category results (all negatives fell through to the contextual path):

| Category | Count | False positives |
|---|---|---|
| Canonical aliases (`ГП ТГ` / `ТГ` / `Газпром трансгаз` / `ООО …`) | 9 | 0 |
| Business/GEO collision cities, qualified | 17 | 0 |
| Bare GEO (must stay GEO) | 6 | 0 |
| Mixed business + period | 5 | 0 |
| Mixed business + GEO | 3 | 0 |
| Multiple business entities | 5 | 0 |
| Comparison / ranking / grouping / explanation | 10 | 0 |
| Unqualified bare names | 5 | 0 |
| Unknown / nonexistent business | 3 | 0 |
| Full new query / non-followup | 5 | 0 |
| Prepositional edge cases | 4 | 0 |
| Full-balance shape cases | 6 | 0 |
| Directed-flow ambiguity (separate shape battery, §10) | 8 shapes | 0 |

All mandated phrases behaved correctly:

| Phrase | Result |
|---|---|
| `А по Москве?` | `deterministic_geo_patch` ✅ |
| `А для ТГ Москва?` | `deterministic_business_entity_patch` (BAL:2010000039953) ✅ |
| `А по ГП ТГ Москва?` | business patch ✅ |
| `А для Газпром трансгаз Москва?` | business patch ✅ |
| `А для ТГ Ухта за апрель?` | fallback ✅ |
| `А из ГП ТГ Ухта в Самарскую область?` | fallback ✅ |
| `Сравни с ТГ Ухта` | fallback ✅ |
| `Сравни ТГ Ухта и ТГ Москва` | fallback ✅ |
| `Кто больше — ТГ Ухта или ТГ Москва?` | fallback ✅ |
| `Разбей по трансгазам` | fallback ✅ |
| `Покажи по всем трансгазам` | fallback ✅ |
| `Почему у ТГ Ухта меньше?` | fallback ✅ |

Plus the two exhaustive real-bundle sweeps (§9): 324 + 840 = 1,164 phrases,
0 misclassifications in either direction, and 23 normalizer-divergence probes,
0 misclassifications. **Grand total ≈ 1,268 phrases, 0 false positives.**

An existing PR4 corpus does exist in-repo
(`tests/transitions/test_business_entity.py:276-331`, 55 parametrized adversarial
phrases); mine was built independently and overlaps only partially.

## 21. Differential PR3.5→PR4 audit

The 81-row corpus was executed on a clean extraction of baseline `9fd1a47`
(via `git archive` into a sibling directory at the correct depth with a junction to
the real `pipeline/`), then on the PR4 tip, and the structured results diffed.

```
corpus size         : 81
byte-identical rows : 67
differing rows      : 14
```

**Every one of the 14 deltas is exactly `LLM_FALLBACK → deterministic_business_entity_patch`:**

```
delta kinds (PR3.5 mode -> PR4 mode):
   LLM_FALLBACK -> deterministic_business_entity_patch   x14
```

The 14: 9 Ukhta alias/frame variants on the distribution+article shape, 2 Nizhny
Novgorod variants, 1 Ukhta on the no-article shape, and 2 on the full-balance shape.
There is **no** other category of change. Specifically byte-identical across the
boundary:

- `А по Москве?` → `deterministic_geo_patch` on both, same geo id, same LLM counts;
- all period phrases → `deterministic_period_patch` on both;
- all 40+ negative phrases → `LLM_FALLBACK` on both;
- `А по Нижнему Новгороду?`, `А по Самаре?`, `А по Тверской области?` → identical
  fallback on both, confirming the §5 observation is pre-existing, not a regression.

Independently, `git diff 9fd1a47..ca0bdc3` touches neither `geo.py` nor `period.py`
(both empty diffs), so recognition precedence for the existing two categories is
byte-unchanged; the classifier only appends a third attempt after both decline.

## 22. Full regression

All runs on `C:\Users\alexs\miniforge3\envs\ai_env\python.exe`.

| Suite | Baseline `9fd1a47` | PR4 `ca0bdc3` |
|---|---|---|
| Full pytest | **422 passed, 0 failed, 1 warning** | **501 passed, 0 failed, 1 warning** |
| `tests/test_period_patch.py` | 34 | **34** |
| `tests/test_geo_patch.py` | 103 | **103** |
| `tests/transitions/` | 37 | **103** |
| `tests/test_business_entity_patch.py` | — | **13** |
| `tests/transitions/test_business_entity.py` | — | **66** |
| Period + GEO + Business combined | — | **216** |

The single warning is the pre-existing Starlette/httpx deprecation, identical on
both trees. Skipped: 0 on both.

Acceptance dry-runs (strict coverage), all with **0 gaps**:

```
acceptance/business_entity_patch_pr4_transition.feature  DRY_RUN  T1 5/5  T2 6/6  T3 6/6  T4 6/6
acceptance/geo_patch_pr3_transition.feature              DRY_RUN  T1 5/5  T2 5/5  T3 5/5  T4 5/5
acceptance/context_chat_business_scenarios.feature       DRY_RUN  0 gaps
```

`scripts/run_golden.py` requires a live backend JSONL log and could not be run
here; the pytest-level `tests/test_golden_queries.py` passed inside the full suite.

## 23. Test-delta audit

Collected node IDs compared set-wise between the two trees:

```
baseline nodes : 422
PR4 nodes      : 501
removed        :   0        <-- nothing silently dropped
added          :  79
```

Added tests by file:

```
tests/transitions/test_business_entity.py   66
tests/test_business_entity_patch.py         13
```

The delta is purely additive **by node ID**. However, two pre-existing
`test_representative_turn_equivalence_matrix` cases changed their expected value
while keeping pinned IDs — see Finding 1. That is the one place where the node-ID
diff understates the change.

**Test quality — both levels present:**

- *Unit / transition level*: `tests/transitions/test_business_entity.py` (8
  functions, 66 cases) — alias canonicalization, article rebinding, rebinding
  fail-closed, full-balance replacement, 55 adversarial phrases, unsafe active
  shapes, no-active-state and same-entity no-match.
- *End-to-end*: `tests/test_business_entity_patch.py` (7 functions, 13 cases) —
  real `PipelineV2TurnProcessor.process()` → planner capture → executor capture →
  `InMemoryContextStore` commit → reload, plus 4 permutation sequences and an
  LLM-call counter.

These are genuine end-to-end tests, not detector-in/mode-out stubs. The gap noted
in Finding 3 is that the E2E layer uses a synthetic registry.

## 24. Live DB status

**LIVE DB ACCEPTANCE = NOT PROVEN — and the PR does not claim otherwise.**

`docs/business_entity_patch_pr4.md:269-270` states plainly: *"Live DB-backed
acceptance: NOT RUN. Dry-run results validate catalog coverage only and are not
presented as live semantic evidence."* I independently confirmed the acceptance
runs available here are `--dry-run` only (status `DRY_RUN`, no HTTP column
populated), and that `scripts/run_golden.py` refuses to run without a live backend
log. No dry-run has been misrepresented as live execution. This is not a blocker
because the PR's own definition of done did not require it.

## 25. Scope creep

`git diff --name-only 9fd1a47..ca0bdc3` — 10 files, 5 of them tests/docs/acceptance:

```
acceptance/business_entity_patch_pr4_transition.feature
docs/business_entity_patch_pr4.md
src/balance_chat/processor.py                       (+16/-0, wiring only)
src/balance_chat/transitions/__init__.py            (+2, exports)
src/balance_chat/transitions/business_entity.py     (+224, new module)
src/balance_chat/transitions/classifier.py          (+12/-1, one branch)
src/balance_chat/transitions/types.py               (+1, evidence_businesses field)
tests/test_business_entity_patch.py                 (new)
tests/transitions/test_business_entity.py           (new)
tests/transitions/test_classifier.py                (+56/-8)
```

Unchanged (empty diffs, verified): `execution_adapter.py`, `reducer.py`,
`contracts.py`, `transitions/geo.py`, `transitions/period.py`. Also untouched:
planner, executor, normalization, store, API, prompts, gating, binding.

No `agentic_poc`, no Qwen tool loop, no `SemanticTransitionProposal`, no
`query_metric` / `query_grouped`, no compare-tool integration anywhere in the diff.
(`grep` hits for "qwen" in `src/` are pre-existing `interpretation_source="qwen"`
labels and a comment, none added by PR4; the docs mention Qwen only to state it is
absent.)

---

## Final answers

### Q1 — Exactly one new semantic PATCH category added?
**YES** — Business Entity SET, produced solely at `transitions/business_entity.py:205/215`.

### Q2 — How many production semantic PATCH categories now exist?
**3** — Period SET (`periods=SET`), GEO SET (`operands=SET`), Business Entity SET (`operands=SET`). Note this is 3 *semantic* categories over only 2 distinct *physical* `IntentPatch` fields.

### Q3 — Is business entity PATCH implemented inside the transition layer?
**YES** — entirely within `src/balance_chat/transitions/business_entity.py`.

### Q4 — Did business-specific semantic logic return to processor.py?
**NO** — +16/−0 lines, all wiring; zero business literals added to `processor.py`.

### Q5 — Can "ГП ТГ Москва" become GEO Moscow?
**NO** — 324-phrase exhaustive real-bundle sweep plus 23 inflection probes yielded 0 GEO patches for any qualified business mention.

### Q6 — Can "А по Москве?" become Business Entity PATCH on ГП ТГ Москва?
**NO** — 840-phrase reverse sweep yielded 0 business patches; `А по Москве?` is byte-identical to baseline (`deterministic_geo_patch`).

### Q7 — Can "А для ГП ТГ Ухта за апрель?" be partially handled by business PATCH?
**NO** — falls through to the contextual path; no mutation produced.

### Q8 — Can "А из ГП ТГ Ухта в Самарскую область?" be partially handled?
**NO** — falls through; no mutation produced.

### Q9 — Can an ambiguous directed-flow state arbitrarily pick source/destination?
**NO** — any `source` or `route` role fails the `{balance, destination, article}` role-subset check and returns `None`. Verified across 8 shape variants.

### Q10 — Do period and GEO survive a Business Entity PATCH?
**YES** — verified in the 5-turn sequence and all 6 permutations; destination unmoved in 108/108 successful patches.

### Q11 — Does business entity survive a subsequent period/GEO PATCH?
**YES** — verified (T5 `А за май?` preserved Ukhta and its rebound article).

### Q12 — Does planner/execution genuinely use the new canonical business entity?
**YES** — planner input carries `BAL:2010000040932` / `ART:2010000040926`; `execute_balance_day(balance_id=2010000040932)`; scalar query text names `ГП ТГ Ухта суточный баланс`.

### Q13 — LLM calls for a successful Business Entity PATCH?
**0** — interpreter 0, summarizer 0, measured across all three execution layers and 108 successful patches.

### Q14 — Is effective==executed==committed==reloaded confirmed?
**YES** — asserted at every turn of the forward sequence and all 6 permutations.

### Q15 — Did PR2a normalization semantics change?
**NO** — business PATCH flows through the same `_synchronize_effective_intent`; `processor.py:180-205` untouched; no second normalization implementation.

### Q16 — Did ExecutionAdapter semantics change?
**NO** — `git diff` for `execution_adapter.py`, `reducer.py`, `contracts.py` is empty.

### Q17 — Any period PATCH regressions?
**NO** — `transitions/period.py` byte-unchanged; 34/34 pass; all period corpus rows byte-identical in the differential.

### Q18 — Any GEO PATCH regressions?
**NO** — `transitions/geo.py` byte-unchanged; 103/103 pass; all GEO corpus rows byte-identical in the differential.

### Q19 — Any hidden production PATCH categories beyond Period/GEO/Business?
**NO** — exactly 3 non-empty `IntentPatch(` constructions, all `SET`, plus one empty normalization reset.

### Q20 — Any Qwen/agentic integration?
**NO** — nothing added by the diff.

### Q21 — False-positive count in your adversarial corpus?
**0** — 81-phrase corpus: 0. Extended: 324 qualified-business + 840 bare-geo + 23 normalizer probes ≈ 1,268 phrases total, **0 false positives**.

### Q22 — Live-DB-backed Business Entity PATCH acceptance?
**NO — NOT RUN.** Dry-runs only (0 gaps). The PR states this honestly and does not present dry-run as live evidence.

### Q23 — Is the transition layer still a CLEAN BOUNDARY after its first real extension?
**YES** — one new module, one sequential classifier branch, zero phrase-specific exceptions, one-way dependencies, no reverse imports.

### Q24 — Ready to merge?
**YES.** Finding 1 (pinned node IDs) is worth fixing as test hygiene but does not
block; Findings 2–4 are informational.
