# PR3.5 — Deterministic transition layer extraction: independent review

Reviewed commit: `a1f3383` ("docs: clarify transition layer size metrics")
Baseline: `8981914` ("fix: preserve bare GEO names across business aliases", PR3 tip)
Diff scope: `git diff 8981914..a1f3383`
Reviewer interpreter: `C:\Users\alexs\miniforge3\envs\ai_env\python.exe`

## 1. Verdict

**APPROVE**

## 2. Executive summary

PR3.5 claims to be a pure refactor with zero functional change: the already-approved
period and GEO deterministic PATCH recognition logic is lifted out of `processor.py`
into a new `src/balance_chat/transitions/` package. I attacked the claim from both
directions the brief demands — (a) find a request that behaves differently, and
(b) find a second production path implementing period/GEO semantics outside the new
package — and could establish neither.

The strongest evidence is a differential harness that drives the **real**
`PipelineV2TurnProcessor.process()` over 36 corpus phrases plus a 4-turn stateful
sequence, in two separate correctly-nested checkouts of the baseline and the review
commit, capturing interpretation mode, the full serialized `ContextMutation`, the full
serialized planned `AnalysisIntent`, the emitted execution queries, the response
envelope, LLM/interpreter call counts, summarizer call counts, and commit/reload
identity. After normalizing the two randomly generated UUID fields (`turn_id`,
`intent_id` — proven nondeterministic by re-running the baseline against itself and
observing the identical 46/40 diff signature), the two outputs are **byte-identical,
MD5 `68c05ec8a5350f3b80ffa59c02dff09d`**.

A second, tree-adaptive adversarial harness replays the recovered PR2b 55-phrase
false-positive corpus and the full GEO adversarial phrase set extracted from
`tests/test_geo_patch.py` against both trees through whichever production recognition
API each tree exposes: **301 shared phrase-evaluations, 0 behavioural mismatches.**
The period corpus reproduces PR2b's documented 13-accept / 42-reject split exactly.

On the architectural side the extraction is genuine, not cosmetic. `processor.py` has
zero residual copies of the moved tables, regexes, candidate dataclasses, or detector
functions. The only lines PR3.5 *adds* to `processor.py` are two import blocks, the
`detect_deterministic_transition` wiring, and two renamed call sites — I verified this
by listing every added line in the file's diff. The entire `IntentPatch`/`FieldMutation`
production surface of the codebase (exactly two constructions, `periods=SET` and
`operands=SET`) now lives inside `transitions/`. The package imports only
`..contracts` and its own submodules, has no lazy or runtime imports, and importing it
does **not** pull `balance_chat.processor` into `sys.modules` — verified empirically.

Full regression is green and strictly additive: 383 → 422 passed, 0 failed, 1
pre-existing warning, and a node-ID level diff confirms **zero** baseline tests were
removed or renamed.

I raise **no blockers and no high findings**. I record one LOW correctness nit (a
defensive `callable()` guard silently weakened to `is not None` during the move, which
I proved diverges only under a synthetic malformed registry and is unreachable in
production), and three LOW/MEDIUM observations about test hermeticity and layering.

A note on process, since it cost real time: the worktree I was given was checked out at
`8a5bbe6` ("Document current context chat readiness"), an unrelated ref that is neither
the branch tip nor the baseline. This is the third round in this series where the
worktree started on the wrong ref. I detached onto `a1f3383` before doing any
substantive work. Additionally, running pytest inside the worktree yields 26 spurious
failures purely because the worktree sits three directory levels too deep for
`Path(__file__).resolve().parents[2] / "pipeline"` to resolve; all test runs in this
report were performed in correctly-nested throwaway checkouts with a junction to the
real `C:\#work\sl\pipeline`, and the baseline reproduced its documented 383/1 exactly,
which validates the rig.

## 3. Blockers

None.

## 4. High findings

None.

## 5. Medium/Low findings

### Finding 1 — Defensive `callable()` guard weakened to `is not None` during the move

```
Finding: In the extracted GEO detector, the two guards that protected the balance
         lookup were changed from `callable(balance_lookup)` to
         `services.lookup_balance is not None`. A registry exposing a non-callable
         `balance` attribute now raises TypeError instead of falling through safely.
Severity: LOW
Evidence: src/balance_chat/transitions/geo.py:107-112 and :142-148
          (`qualified_business and services.lookup_balance is not None
            and services.lookup_balance(mention) is not None`
           and `or services.lookup_balance is None`)
          Baseline equivalents used `callable(balance_lookup)`:
          8981914:src/balance_chat/processor.py:1607-1618 and :1657-1662
          Injection site: src/balance_chat/processor.py:632
          (`lookup_balance=getattr(self.registry, "balance", None)`)
Why it matters: `getattr(..., None)` cannot distinguish "absent" from "present but not
          callable". The original author wrote `callable()` deliberately; the
          refactor's stated contract is zero semantic change, and this is the one
          place in the diff where that is not a strict identity.
Reproduction: With a registry subclass whose `balance` is the string "not-callable",
          message "А по ГП ТГ Москва?" against an active SHOW/distribution intent:
            baseline  -> None (safe fall-through to the contextual path)
            PR3.5     -> TypeError: 'str' object is not callable
          (I ran exactly this in both trees; "А по Самарской области?" still returns
          deterministic_geo_patch in both, so the divergence is confined to the
          qualified-business branch.)
Minimal correction: restore the original predicate —
          `callable(services.lookup_balance)` in both guards, or type
          `GeoTransitionServices.lookup_balance` as a required callable and pass a
          no-op default at the injection site.
```

**Reachability assessment — why this is LOW and not HIGH.** The production registry's
`balance` is a bound method (`pipeline/metadata_bundle/registry.py:636`,
`def balance(self, value: int | str) -> BalanceRecord | None`), so it is always
callable. Every test double in the repo supplies `balance=lambda ...`. `getattr` on a
well-formed registry therefore yields a callable or `None`, never a non-callable
value. No reachable production input distinguishes the two predicates, which is why the
differential and adversarial harnesses are byte-identical. This is a latent robustness
regression, not an observable behaviour change, and it does not gate the merge.

### Finding 2 — New transition unit tests use hand-rolled lemmatizers, not the real morphology path

```
Finding: tests/transitions/test_classifier.py and test_geo.py inject a
         GeoTransitionServices built from a hand-written dict-based stemmer, so the new
         unit tests never exercise the real pymorphy3-backed `_normalize_lemmas` or the
         real metadata registry resolution.
Severity: MEDIUM
Evidence: tests/transitions/test_classifier.py:39-52 (`_FORMS` dict + `_normalize`),
          :66-89 (`_services()` with `lookup_balance=lambda ...`,
          `resolve_direction_article=lambda _balance, _target, _metric: None`)
Why it matters: the 31-case equivalence matrix at test_classifier.py:116-149 reads like
          a strong behavioural guarantee, but a GEO-morphology regression in the real
          lemmatizer or resolver would not fail it. The matrix pins the classifier's
          control flow, not the system's GEO recognition.
Reproduction: n/a — design observation, visible on reading the fixtures.
Minimal correction: none required for this PR. If the transition layer grows, consider
          one integration-level case per module that binds the real registry.
```

**Mitigation.** This is materially softened by the fact that PR3.5 did **not** hollow
out the pre-existing end-to-end tests. `tests/test_period_patch.py` and
`tests/test_geo_patch.py` retain exactly the same number of real
`PipelineV2TurnProcessor.process()` invocations and the same number of test functions
as at baseline (5/10 and 10/21 respectively, verified by counting in both trees), and
their diffs are pure import/call-site retargeting. The real-path coverage that existed
at PR3 is fully intact; the new tests are additive. This mirrors the test-hermeticity
findings already recorded and accepted in the PR3 round.

### Finding 3 — `transitions/period.py` doubles as a general-purpose date-parsing utility module

```
Finding: `explicit_single_day_period`, `find_month_number`, and
         `named_comparison_periods` are not period-PATCH logic. They are shared
         helpers consumed by unrelated deterministic detectors in processor.py, and
         they now live inside the transition package.
Severity: LOW
Evidence: src/balance_chat/transitions/period.py:54-93, :139-143, :146-186
          Consumers outside the transition layer:
            src/balance_chat/processor.py:1803 (_deterministic_full_balance_mutation)
            src/balance_chat/processor.py:1852 (_deterministic_balance_section_mutation)
            src/balance_chat/processor.py:1914 (_deterministic_directed_flow_mutation)
            src/balance_chat/processor.py:4440 (_deterministic_period_mutation)
            src/balance_chat/processor.py:4459 (_deterministic_period_mutation)
Why it matters: `transitions.period` is now imported by code that has nothing to do
          with transitions, which blurs the boundary the PR is trying to draw. It also
          means a future change to the transition layer's dependencies has five
          non-transition call sites to consider.
Reproduction: n/a — layering observation.
Minimal correction: a later PR could split these into `balance_chat/periods.py` (or
          similar) that both `transitions.period` and `processor.py` import.
```

I verified all five call sites are the *same* call sites as at baseline, merely
renamed — baseline had `_explicit_single_day_period` at lines 1933/1982/2044,
`_named_comparison_periods` at 4766, and an inline
`next((number for pattern, number in _MONTHS if re.search(pattern, text)), None)` at
4785, now replaced by `find_month_number(text)`. Since `_MONTHS` and the new
`_MONTH_PATTERNS` are the identical 12-tuple, that substitution is an exact identity.
No third copy exists anywhere.

### Finding 4 — Pre-existing near-duplicate month tables travelled along with the move (informational)

`_MONTH_PATTERNS` (word-boundary-anchored stems, used by the followup detector and
`find_month_number`) and `_RUSSIAN_MONTHS` (unanchored stems, used by
`explicit_single_day_period`) are now adjacent in `transitions/period.py:21-41`. Per the
review brief this duplication predates PR3.5 and was merely relocated; I confirmed both
tables existed in `8981914:src/balance_chat/processor.py` (at lines 4625 and 3520
respectively). **Not a PR3.5 finding.** Recorded only because co-locating them makes the
redundancy newly obvious and a future cleanup cheap.

## 6. Behavioural equivalence

### Method

Two correctly-nested throwaway checkouts were created via
`git archive <sha> | tar -x -C <dir>`, placed as siblings of a directory junction to
the real `C:\#work\sl\pipeline` so that `Path(__file__).resolve().parents[2] / "pipeline"`
resolves. No ref was ever checked out inside the review worktree. The baseline tree
reproduced its documented **383 passed, 1 warning** exactly, validating the rig.

An identical harness file was copied into both trees. It imports the shared fixtures
from `tests/test_geo_patch.py` (`_processor`, `_intent`, `_state`, `_entity`,
`_Registry`, `_Runtime`, `_CountingInterpreter`, `_CapturingPlanner`) and drives the
real `PipelineV2TurnProcessor.process(...)` with `execute_db=True`. Per turn it records:
`outcome`, `diagnostics.interpretation.mode`, the full `ContextMutation` JSON, the full
`response` envelope, `interpreter.calls` (LLM proxy), `runtime.summary_calls`
(summarizer proxy), the planner invocation count, the full planned `AnalysisIntent`
JSON, and the emitted execution query strings. Exceptions are captured as behaviour
rather than aborting.

### Result

Baseline output and PR3.5 output differ **only** in the values of `turn_id` and
`intent_id`. Re-running the baseline against itself produces the identical diff
signature (46 `intent_id`, 40 `turn_id`), proving these are freshly generated UUIDs
(`processor.py:214`, `turn_id = str(uuid4())`) rather than a behavioural difference.
After UUID normalization:

```
diff n_base.json n_pr35.json  -> exit 0
md5  68c05ec8a5350f3b80ffa59c02dff09d  n_base.json
md5  68c05ec8a5350f3b80ffa59c02dff09d  n_pr35.json
```

### Equivalence matrix

All rows below are byte-identical pre/post. LLM = interpreter invocations,
SUM = summarizer invocations, PLAN = planner invocations.

| Class | Phrase | Mode | LLM | SUM | PLAN | Outcome |
|---|---|---|---:|---:|---:|---|
| simple period | `А за апрель?` | deterministic_period_patch | 0 | 0 | 1 | patch |
| explicit-year period | `А за январь 2025?` | deterministic_period_patch | 0 | 0 | 1 | patch |
| period, no particle | `За февраль?` | deterministic_period_patch | 0 | 0 | 1 | patch |
| period w/ verb | `Покажи за июнь` | deterministic_period_patch | 0 | 0 | 1 | patch |
| period year-rollover | `А за декабрь?` | deterministic_period_patch | 0 | 0 | 1 | patch |
| explicit-day period | `А за 15 апреля 2025?` | deterministic_period_patch | 0 | 0 | 1 | patch |
| period (seq re-entry) | `А за май?` | deterministic_period_patch | 0 | 0 | 1 | patch |
| relative period | `А за прошлый месяц?` | — | 1 | 0 | 0 | falls through |
| interrogative period | `За какой апрель?` | — | 1 | 0 | 0 | falls through |
| simple GEO | `А по Самарской области?` | deterministic_geo_patch | 0 | 0 | 1 | patch |
| simple GEO (city) | `А по Москве?` | deterministic_geo_patch | 0 | 0 | 1 | patch |
| simple GEO (republic) | `А по Татарстану?` | deterministic_geo_patch | 0 | 0 | 1 | patch |
| GEO via `для` | `А для Татарстана?` | deterministic_geo_patch | 0 | 0 | 1 | patch |
| bare GEO name | `А по Ростовской?` | deterministic_geo_patch | 0 | 0 | 1 | patch |
| country GEO (export) | `А по Польше?` | deterministic_geo_patch | 0 | 0 | 1 | patch |
| country GEO (export) | `А для Германии?` | deterministic_geo_patch | 0 | 0 | 1 | patch |
| directed-flow GEO (article rebind) | `А по Самарской области?` | deterministic_geo_patch | 0 | 0 | 1 | patch |
| mixed GEO+period | `А по Москве за апрель?` | — | 1 | 0 | 0 | falls through |
| mixed GEO+period | `А по Самаре в мае?` | — | 1 | 0 | 0 | falls through |
| compare-like | `Сравни с Самарской областью` | — | 1 | 0 | 0 | falls through |
| grouping-like | `Разбей по областям` | — | 1 | 0 | 0 | falls through |
| grouping-like | `Покажи по всем областям` | — | 1 | 0 | 0 | falls through |
| ranking-like | `Покажи максимум по Москве` | — | 1 | 0 | 0 | falls through |
| causal-like | `Почему в Москве меньше?` | — | 1 | 0 | 0 | falls through |
| unknown location | `А по неизвестной области?` | — | 1 | 0 | 0 | falls through |
| unknown location | `А по Европе?` | — | 1 | 0 | 0 | falls through |
| multi-GEO | `А Москва и Самара?` | — | 1 | 0 | 0 | falls through |
| route-like | `Из Москвы в Самару` | — | 1 | 0 | 0 | falls through |
| business collision | `А по ГП ТГ Москва?` | — | 1 | 0 | 0 | falls through |
| business collision | `А для ГП ТГ Москва?` | — | 1 | 0 | 0 | falls through |
| business collision | `А по Газпром трансгаз Москва?` | — | 1 | 0 | 0 | falls through |
| business collision | `А для Газпром трансгаз Москва?` | — | 1 | 0 | 0 | falls through |
| business collision | `А для ТГ Ухта?` | — | 1 | 0 | 0 | falls through |
| business collision | `А по ГП ТГ Нижний Новгород?` | — | 1 | 0 | 0 | falls through |
| no active state | `А за апрель?` | standalone | 0 | 1 | 0 | standalone |
| no active state | `А по Самарской области?` | standalone | 0 | 1 | 0 | standalone |

Every deterministic PATCH row: **0 LLM calls, 0 summarizer calls**. Every fall-through
row reaches the contextual interpreter exactly once, exactly as at baseline (the fixture
interpreter raises on invocation, which is itself recorded and compared).

### Sequential scenario (I4)

Driven through the real `InMemoryContextStore` using `store.create` / `store.commit` /
`store.get`, so commit and reload are genuine, not simulated.

| Turn | Message | Mode | Periods after | Destination after | LLM | exec==commit | commit==reload | rev |
|---|---|---|---|---|---:|---|---|---:|
| Q1 | seed: Rostov / May 2025 | — | 2025-05-01 → 2025-06-01 | geo:rostov | — | — | — | 1 |
| Q2 | `А за апрель?` | deterministic_period_patch | 2025-04-01 → 2025-05-01 | geo:rostov | 0 | True | True | 2 |
| Q3 | `А по Самарской области?` | deterministic_geo_patch | 2025-04-01 → 2025-05-01 | geo:samara | 0 | True | True | 3 |
| Q4 | `А за май?` | deterministic_period_patch | 2025-05-01 → 2025-06-01 | geo:samara | 0 | True | True | 4 |

Emitted execution queries confirm the effect reached the database layer:

```
Q2: ... в Ростовская область за период с 2025-04-01 по 2025-05-01
Q3: ... в Самарская область за период с 2025-04-01 по 2025-05-01
Q4: ... в Самарская область за период с 2025-05-01 по 2025-06-01
```

GEO survives a period patch, period survives a GEO patch, year inheritance holds, and
the revision advances monotonically. Byte-identical to baseline.

### Adversarial regression

The PR2b 55-phrase false-positive corpus was recovered verbatim from
`docs/period_patch_pr2b_claude_review.md` §8 (the anticipated
`docs/geo_patch_pr3_claude_review.md` does not exist in this repo; the GEO corpus was
instead extracted programmatically from the Cyrillic string literals in
`tests/test_geo_patch.py`, yielding 123 phrases at baseline and 125 at PR3.5). A
tree-adaptive harness dispatches to `detect_deterministic_transition` on PR3.5 and to
`_deterministic_period_patch` / `_deterministic_geo_patch` on the baseline, normalizing
both to `(mode, mutation_json)`. Each corpus was swept against two active-intent
contexts (SHOW/distribution and export).

```
shared phrase-evaluations compared : 301
behavioural mismatches             : 0
```

Period-55 outcome on PR3.5: **13 ACCEPT / 42 reject**, and the accept set matches
PR2b's documented accept set exactly (set equality asserted programmatically; zero
unexpected accepts, zero missing accepts). The GEO sweep accepts 11 of 125 phrases in
both contexts — all of them legitimate bare-GEO or bare-period follow-ups; every
business-entity, mixed, compare, grouping, ranking, route, and multi-GEO probe is
rejected.

The only corpus asymmetry is that PR3.5's test file contains two additional phrases
(`А по ГП ТГ Москва?`, `А для Газпром трансгаз Москва?`), introduced by commit
`5ca79b1`. Both evaluate to no-match in both intent contexts, i.e. business-vs-GEO
protection holds on them.

*Scope note:* the brief lists `5ca79b1` as "part of baseline", but `git log 8981914..a1f3383`
shows it is a descendant of the baseline and therefore inside the review range. It is a
2-line, test-only commit adding those two parametrize entries. It adds no production
code and does not affect the zero-functional-change assessment.

## 7. Architectural extraction audit

**Verdict: CLEAN BOUNDARY.**

Target shape, as realized:

```
processor.py:624  detect_deterministic_transition(...)
                    -> transitions/classifier.py:9
                         -> transitions/period.py:189  detect_period_transition
                         -> transitions/geo.py:166     detect_geo_transition
                    -> TransitionDecision (kind, mutation, interpretation_mode,
                                           evidence_geos, diagnostic_event,
                                           diagnostic_fields)
processor.py:656  _dispatch_mutation(...)  [pre-existing execution path, untouched]
```

**Residual ownership in `processor.py`: none for period/GEO PATCH.** A grep for every
moved symbol returns nothing:

```
_RUSSIAN_MONTHS | ^_MONTHS | _SEASONS | _MONTH_PATTERNS |
def _explicit_single_day_period | def _named_comparison_periods |
def _detect_period_followup | def _detect_geo_followup |
def _deterministic_period_patch | def _deterministic_geo_patch |
_whole_geo_mention_matches | PeriodPatchCandidate | GeoPatchCandidate
   -> 0 matches in src/balance_chat/processor.py
```

**The only lines PR3.5 adds to `processor.py`** (complete list, taken from the file's
own diff) are: the `from .transitions import (...)` block, the
`from .transitions.period import (...)` block, the `detect_deterministic_transition`
call with its `GeoTransitionServices` bundle, the `TransitionKind.PATCH` branch, and
the two renamed call sites `named_comparison_periods(scope.intent, text)` and
`find_month_number(text)`. No new logic, no new branches beyond the one that replaces
the two it removes.

**Detector census.** `processor.py` went from 14 to 10 functions matching
`def _deterministic|def _detect` — exactly the four removed are
`_detect_period_followup`, `_deterministic_period_patch`, `_detect_geo_followup`,
`_deterministic_geo_patch`. Nothing else was disturbed.

**On `GeoTransitionServices` being constructed inline in `processor.py:629-642`** (the
brief asks for an explicit judgement): I assess this as **legitimate dependency
injection, not residual ownership of recognition semantics**. The five injected members
are `self._normalize_lemmas` (runtime-bound morphology), `self._tagged_geo_objects`
(registry-bound resolution), `getattr(self.registry, "balance", None)` (registry
lookup), a lambda closing over `self.registry` for `_unique_direction_article`, and the
free function `_typed_entity`. Every one of them requires runtime or registry state that
by construction lives on the processor; none of them encodes *when* a GEO follow-up is
recognized, *which* mention frames qualify, how morphological similarity is thresholded,
or how the business-vs-GEO collision is arbitrated. All of that — the `по|для` frame,
the `тг`/`газпром трансгаз` qualification test, the 0.84/0.76 SequenceMatcher
thresholds, the destination/role gating, the article rebinding rule — sits in
`transitions/geo.py`. The processor knows *that* a deterministic transition may exist
and *how to supply resolvers*; it no longer knows *what makes one*. That is the correct
side of the line.

Two structural facts corroborate the boundary is real rather than nominal:

1. The entire `IntentPatch` + `FieldMutation` production surface of `src/` is now two
   constructions, both inside `transitions/` (see §11). `processor.py` constructs no
   field-level patch at all any more.
2. `import balance_chat.transitions` does **not** load `balance_chat.processor` —
   verified by inspecting `sys.modules` after a bare import. The layer is genuinely
   independently importable and testable.

## 8. Period call graph

Request: `"А за апрель?"` with active scope Rostov / May 2025.

```
service/API boundary
  balance_chat/service.py:52          TurnProcessor.process (protocol)
  processor.py:206                    PipelineV2TurnProcessor.process
  processor.py:214                      turn_id = str(uuid4())
  processor.py:219                      if state.active_dialog_scope is not None:
  processor.py:220                        -> _process_contextual(...)          [RETURNS]

transition layer
  processor.py:612                    _process_contextual
  processor.py:624                      detect_deterministic_transition(
                                          message, state, turn_id,
                                          clarification_provided=False,
                                          geo_services=GeoTransitionServices(...))
  transitions/classifier.py:9           detect_deterministic_transition
  transitions/classifier.py:18            clarification/pending guard -> no_match
  transitions/classifier.py:20            detect_period_transition(state, message, turn_id)
  transitions/period.py:189               detect_period_transition
  transitions/period.py:194                 scope = state.active_dialog_scope
  transitions/period.py:197                 detect_period_followup(message, scope.intent)
  transitions/period.py:96                    detect_period_followup
  transitions/period.py:100-106                 operation/operand/period arity gate
  transitions/period.py:107                     re.fullmatch(r"(?:а\s+)?(?:покажи\s+)?за\s+(?P<period>.+)")
  transitions/period.py:114                     for month_pattern, month in _MONTH_PATTERNS
  transitions/period.py:122                       named_month fullmatch
  transitions/period.py:128-132                   year inheritance from active period
  transitions/period.py:133-135                   PeriodRef(2025-04-01 .. 2025-05-01)
  transitions/period.py:200-210             ContextMutation(patch=IntentPatch(
                                              periods=FieldMutation(SET, [PeriodRef])))
  transitions/period.py:211-217             TransitionDecision(PATCH,
                                              interpretation_mode="deterministic_period_patch",
                                              diagnostic_event="deterministic_period_patch_recognized",
                                              diagnostic_fields={"mutation_mode": "period_patch"})

existing execution path (untouched by PR3.5)
  processor.py:644                      if transition.kind == TransitionKind.PATCH
  processor.py:649-655                    log_event(deterministic_period_patch_recognized)
  processor.py:656                        _dispatch_mutation(..., evidence_geos=())
  processor.py:130                      _dispatch_mutation
  processor.py:148                        _materialize_mutation -> reducer
  reducer.py                              reduce_intent / normalization (PR2a semantics)
  processor.py:1113                       gate.validate_bound_intent(explicit_geos=())
  gating.py:86                            validate_bound_intent
  processor.py (execute path)             ExecutionAdapter -> planner -> executor
  processor.py:2469/2677/2791             _should_summarize("deterministic_period_patch") -> False
  store.py:100                            InMemoryContextStore.commit
  store.py:117                            apply_context_transition
```

## 9. GEO call graph

Request: `"А по Самарской области?"` with active scope Rostov / April 2025.

```
processor.py:206                    process
processor.py:220                      -> _process_contextual                    [RETURNS]
processor.py:624                    detect_deterministic_transition
processor.py:629-642                  GeoTransitionServices(
                                        normalize_lemmas=self._normalize_lemmas,
                                        resolve_geo_objects=self._tagged_geo_objects,
                                        lookup_balance=getattr(self.registry,"balance",None),
                                        resolve_direction_article=lambda b,t,m:
                                          _unique_direction_article(self.registry,...),
                                        make_entity=_typed_entity)
transitions/classifier.py:20        detect_period_transition -> None  (period tried FIRST)
transitions/classifier.py:23        detect_geo_transition(state, message, turn_id, services)
transitions/geo.py:166                detect_geo_transition
transitions/geo.py:175                  detect_geo_followup(message, scope.intent, services)
transitions/geo.py:76                     detect_geo_followup
transitions/geo.py:82-91                    operation/operand/grouping/comparison/
                                            formula/ranking gate + lemmatizer presence
transitions/geo.py:93-94                    operand.metric in {distribution, export}
transitions/geo.py:95-99                    re.fullmatch(r"(?:а\s+)?(?:по|для)\s+(?P<geo>.+)")
transitions/geo.py:101-106                  qualified-business test
                                            ("тг" / "газпром трансгаз" / "ооо газпром трансгаз")
transitions/geo.py:107-112                  services.lookup_balance(mention) -> bail if business
transitions/geo.py:113                      services.resolve_geo_objects(mention)
transitions/geo.py:115-118                  _whole_geo_mention_matches (0.84 / 0.76 thresholds)
transitions/geo.py:120-127                  exactly one destination, no source/route roles
transitions/geo.py:131-135                  services.make_entity("destination","geo_object",geo)
transitions/geo.py:136-161                  optional article rebinding via
                                            services.lookup_balance + resolve_direction_article
transitions/geo.py:162-163                  GeoPatchCandidate(geo, (updated_operand,))
transitions/geo.py:178-188              ContextMutation(patch=IntentPatch(
                                          operands=FieldMutation(SET, [operand])))
transitions/geo.py:189-198              TransitionDecision(PATCH,
                                          interpretation_mode="deterministic_geo_patch",
                                          evidence_geos=(candidate.geo,),
                                          diagnostic_event="deterministic_geo_patch_recognized",
                                          diagnostic_fields={"mutation_mode":"geo_patch",
                                                             "geo_id": ...})

processor.py:656                    _dispatch_mutation(..., evidence_geos=(geo,))
processor.py:1113                     gate.validate_bound_intent(explicit_geos=(geo,))
   ... identical execution/commit/reload path as §8 ...
```

**`evidence_geos` list-vs-tuple check (independently verified, as requested).** The
baseline passed `evidence_geos=[candidate.geo]`; PR3.5 passes `(candidate.geo,)`. The
value is consumed at exactly two points downstream —
`processor.py:1116` / `2368` (`explicit_geos=evidence_geos`) and `processor.py:1143` /
`2418` (`explicit_geo_count=len(evidence_geos)`) — and inside
`gating.py:86-103` it is used only in a truthiness test
(`if not operand.entities and (explicit_businesses or explicit_geos)`) and via `len()`.
Both are sequence-agnostic. Separately, the **period** path is the more interesting case:
baseline omitted `evidence_geos` entirely, taking `_dispatch_mutation`'s default; PR3.5
passes `transition.evidence_geos`, which for a period decision is `TransitionDecision`'s
default `()`. `_dispatch_mutation`'s own default is also `()` (`processor.py:143`), so the
two are the same object shape and the same empty value. No divergence — and the harness
confirms it empirically for both paths.

## 10. Duplicate routing audit

**No duplicate production routing exists.** Three independent lines of evidence:

1. **Mutual exclusivity by construction.** `process()` returns unconditionally into
   `_process_contextual` when `state.active_dialog_scope is not None`
   (`processor.py:219-227`). The legacy `_deterministic_period_mutation` /
   `_deterministic_geo_mutation` pair at `processor.py:442-445` is therefore reachable
   **only when there is no active scope**, while the transition layer is reachable
   **only when there is one**. The two can never both run for the same turn. This guard
   is byte-identical to the baseline (`8981914:processor.py:212-221`).

2. **Different semantic category.** Those legacy detectors produce
   `replace_intent` mutations under interpretation modes `deterministic_period` and
   `deterministic_geo` (`processor.py:443`, `:446`) — not `IntentPatch`, and not the
   `*_patch` modes. `_deterministic_geo_mutation` (`processor.py:1294-1330`) builds a
   whole replacement intent; `_deterministic_period_mutation` (`processor.py:4434`)
   builds `COMPARE_PERIODS` replacements. Neither is a PATCH producer. Both predate
   PR3.5 and are untouched by it.

3. **Single producer per mode.** Repo-wide, the strings `deterministic_period_patch`
   and `deterministic_geo_patch` appear in production code at only four sites:
   `transitions/period.py:214,215` and `transitions/geo.py:192,194` (the producers), plus
   `processor.py:4429-4430`, which is the `_should_summarize` membership set — a consumer,
   not a producer.

**Detector overlap.** I swept the entire recovered corpus (all GEO phrases plus the
period probes) through `detect_period_transition` and `detect_geo_transition`
independently and counted phrases where **both** return non-`None`: **0**. The two
trigger frames (`за <period>` vs `по|для <geo>`) are disjoint under `re.fullmatch`, so
the period-before-geo ordering — which I confirmed is preserved from the baseline's
order in `_process_contextual` — is not even load-bearing on any known input. No hidden
priority rule was introduced.

## 11. PATCH producer audit

Exhaustive `grep -rn "FieldMutation(" src/`:

| Site | Field | Action |
|---|---|---|
| `src/balance_chat/contracts.py:308` | (class definition) | n/a |
| `src/balance_chat/transitions/geo.py:183` | `operands` | `SET` |
| `src/balance_chat/transitions/period.py:205` | `periods` | `SET` |

Exhaustive `grep -rn "IntentPatch(" src/`:

| Site | Content |
|---|---|
| `src/balance_chat/contracts.py:328` | class definition |
| `src/balance_chat/processor.py:192` | `IntentPatch()` — empty, inside `_synchronize_effective_intent` (PR2a normalization-collapse semantics; untouched by this diff) |
| `src/balance_chat/transitions/geo.py:182` | `operands=SET` |
| `src/balance_chat/transitions/period.py:204` | `periods=SET` |

**Exactly two production PATCH categories: `periods=SET` and `GEO operands=SET`.** No
entity, grouping, operation, comparison, ranking, or route PATCH exists anywhere. Both
producers live in the transition layer; `processor.py` produces no field-level patch at
all.

## 12. Interpretation-mode audit

`interpretation_mode` values reaching `_dispatch_mutation` from `_process_contextual`
are unchanged. PR3.5 replaces two hard-coded literals with
`transition.interpretation_mode`, whose only two possible non-`None` values are the same
two literals, now defined at `transitions/period.py:214` and `transitions/geo.py:192`.

Diagnostics are likewise preserved rather than reshaped. Baseline logged
`"deterministic_period_patch_recognized"` with `mutation_mode="period_patch"`, and
`"deterministic_geo_patch_recognized"` with `mutation_mode="geo_patch"` plus
`geo_id=str(candidate.geo.geo_id)`. PR3.5 carries exactly those event names and field
dicts on the `TransitionDecision` (`period.py:215-216`, `geo.py:194-197`) and expands
them at `processor.py:652-654` via
`transition.diagnostic_event or "deterministic_transition_recognized"` and
`**dict(transition.diagnostic_fields)`. The `or` fallback is unreachable for the two
current producers, both of which always set the event; it is a defensive default, not a
new mode.

No new interpretation mode is introduced. `TransitionKind` has exactly two members,
`PATCH` and `NO_MATCH` (`types.py:11-13`); there is no NEW/REINTERPRET decision, no
confidence score, no clarification workflow.

## 13. LLM-call audit

**Verified by execution, not by reading the helper.** The fixture interpreter
(`tests/test_geo_patch.py:_CountingInterpreter`) increments a counter and raises on
invocation, and the fixture runtime counts `summarize_envelope` calls. Across the
harness:

- Every deterministic period PATCH: `llm_calls = 0`, `summary_calls = 0` (7 phrases).
- Every deterministic GEO PATCH: `llm_calls = 0`, `summary_calls = 0` (8 phrases,
  including the directed-flow article-rebinding variant).
- Every turn of the 4-turn sequential scenario: `llm_calls = 0`, `summary_calls = 0`.
- Fall-through phrases: `llm_calls = 1`, matching baseline exactly.
- No-active-state phrases: `summary_calls = 1` under mode `standalone`, matching baseline.

Structurally, `_should_summarize` (`processor.py:4427-4431`) and its exactly three call
sites (`processor.py:2469`, `:2677`, `:2791`) are **untouched** by this diff —
`_should_summarize` appears in the processor diff only as a hunk-header context line,
never as a `+`/`-` line. The suppression set still contains both
`deterministic_period_patch` and `deterministic_geo_patch`.

The transition package contains no LLM surface at all: a grep for
`qwen|agentic|summar|llm|openai|anthropic` over `src/balance_chat/transitions/` returns
only incidental substring hits inside `re.fullmatch`.

## 14. Dependency/cycle audit

Complete import inventory of `src/balance_chat/transitions/`:

```
types.py       : dataclasses, enum, types, typing, ..contracts
period.py      : dataclasses, datetime, re, types, ..contracts, .types
geo.py         : dataclasses, difflib, re, types, typing, ..contracts, .types
classifier.py  : ..contracts, .geo, .period, .types
__init__.py    : .classifier, .geo, .types
```

- **Direction:** `processor -> transitions` only. Zero references to `processor` from
  anywhere in the package.
- **Cycles:** none. The internal graph is a DAG rooted at `classifier`, with `types` as
  a leaf.
- **No import hacks:** every import is module-level; there are no function-local,
  deferred, or `TYPE_CHECKING`-guarded imports used to conceal a cycle.
- **Independent testability — verified empirically:** importing `balance_chat.transitions`
  in a fresh interpreter leaves `balance_chat.processor` absent from `sys.modules`. The
  layer is importable and exercisable without constructing a `PipelineV2TurnProcessor`,
  which the three new test modules in `tests/transitions/` demonstrate in practice.
- **Unused imports:** an AST-based sweep of `processor.py` and all five transition
  modules reports **none**. PR3.5 correctly dropped `dataclass`, `FieldMutation`, and
  `MutationAction` from `processor.py` when the code that used them left.

`git diff 8981914..a1f3383` touches **no** other source file: `contracts.py`,
`execution_adapter.py`, `reducer.py`, `gating.py`, `interpretation.py`, `planning.py`,
and `service.py` are all byte-identical to baseline (empty diffs, verified individually).

## 15. Test results

All runs on `C:\Users\alexs\miniforge3\envs\ai_env\python.exe`, in correctly-nested
throwaway checkouts.

| Suite | Baseline `8981914` | PR3.5 `a1f3383` | Delta |
|---|---|---|---|
| Full `pytest tests/` | **383 passed**, 0 failed, 0 skipped, 1 warning | **422 passed**, 0 failed, 0 skipped, 1 warning | +39 passed |
| `tests/test_period_patch.py` | 34 passed | 34 passed | 0 |
| `tests/test_geo_patch.py` | 101 passed | 103 passed | +2 |
| period + GEO combined | 135 passed | 137 passed | +2 |
| `tests/test_golden_queries.py` | 14 passed | 14 passed | 0 |
| `tests/transitions/` | n/a (did not exist) | 37 passed | +37 |
| Acceptance dry-run (`scripts/run_acceptance.py --dry-run`) | 12 scenarios, 0 failed, checks **121/121**, coverage **100.00%** | 12 scenarios, 0 failed, checks **121/121**, coverage **100.00%** | identical |

The single warning is pre-existing and unrelated
(`StarletteDeprecationWarning` from `fastapi/testclient.py`, present at baseline).

**Environment caveat.** Running `pytest` directly inside the supplied review worktree
produces 26 failures in `test_binding.py`, `test_golden_queries.py`,
`test_legacy_projection.py`, and `test_result_memory.py`. These are **not** code
regressions: those modules resolve metadata via
`Path(__file__).resolve().parents[2] / "pipeline"` (e.g. `tests/test_binding.py:30`), and
the worktree sits three levels deeper than the real checkout, so the path resolves to
the non-existent `C:\#work\sl\balance_chat\.claude\worktrees\pipeline\data\metadata\manifest.json`.
The nested rig eliminates all 26, and the baseline reproduces its documented 383/1 there
exactly.

## 16. Test-delta audit

I collected every test node ID from both trees (`pytest --collect-only`) and diffed the
sorted sets.

```
baseline node IDs : 383
PR3.5 node IDs    : 422
removed (in baseline, absent in PR3.5) : 0
added                                   : 39
```

**Zero removals, zero renames.** All 383 baseline node IDs — including the ones whose
bodies were retargeted to the new imports — survive under identical names. The count
growth is therefore genuinely additive and cannot have been inflated by replacing old
coverage.

Breakdown of the 39 additions:

| File | Added | Nature |
|---|---:|---|
| `tests/transitions/test_classifier.py` | 31 | new: 30-row equivalence matrix + clarification guard |
| `tests/transitions/test_geo.py` | 3 | new: GEO decision contract, services isolation |
| `tests/transitions/test_period.py` | 3 | new: patch contract, serialization stability, day parser |
| `tests/test_geo_patch.py` | 2 | from `5ca79b1`: two extra business-collision parametrize entries |

**No hollowing-out.** `tests/test_period_patch.py` and `tests/test_geo_patch.py` retain
exactly the baseline's count of real `PipelineV2TurnProcessor.process()` invocations
(5 and 10) and test functions (10 and 21). Their diffs consist solely of import
retargeting and call-site renames (`_detect_period_followup` → `detect_period_followup`,
`processor._deterministic_geo_patch(...)` → `detect_geo_transition(...)`, plus a local
`_services()` builder). `tests/test_pipeline_wiring.py` changes only the import source of
`explicit_single_day_period`. The end-to-end assertions — planner intent, execution query
text, `interpreter.calls == 0`, `runtime.summary_calls == 0` — are unchanged.

## 17. Processor responsibility/LOC comparison

| Metric | Before `8981914` | After `a1f3383` | Delta |
|---|---:|---:|---:|
| `processor.py` physical lines | 5031 | 4665 | −366 |
| `processor.py` non-blank LOC | 4773 | 4431 | −342 |
| `transitions/` package physical | 0 | 478 | +478 |
| Detector functions in `processor.py` (`def _deterministic` / `def _detect`) | 14 | 10 | −4 |

The doc's own metrics (`docs/transition_layer_pr3_5.md:275-291`) are accurate; I
recomputed all of them independently and they match to the line, including the claim
that `period.py` is 197 non-blank / 217 physical.

**Responsibility delta, which matters more than LOC.** `processor.py` shed these
semantic responsibilities entirely:

1. Russian month-name parsing tables and stem matching (`_MONTHS`, `_RUSSIAN_MONTHS`).
2. Season parsing tables (`_SEASONS`).
3. Period follow-up frame recognition (`за <period>` fullmatch).
4. Year inheritance from the active period.
5. Period `ContextMutation` / `IntentPatch` construction.
6. GEO follow-up frame recognition (`по|для <geo>` fullmatch).
7. GEO morphological whole-mention matching and its similarity thresholds.
8. Business-vs-GEO collision arbitration.
9. Destination-role gating and entity substitution for GEO patches.
10. Direction-article rebinding for distribution flows.
11. GEO `ContextMutation` / `IntentPatch` construction.

It retains, appropriately: turn orchestration, the active-scope routing decision,
resolver/registry ownership (which it injects), dispatch, gating, execution,
summarization policy, and the other nine pre-existing deterministic detectors that PR3.5
did not claim to touch. This is a real reduction in what the file *knows*, not just in
how many lines it occupies — a −366-line move accompanied by a boundary that survives
adversarial probing.

`processor.py` remains a 4665-line file with ten detectors, so the accumulation concern
raised in the PR2b review still stands as a longer-term matter. PR3.5 moves it in the
right direction and establishes the pattern for continuing.

## 18. Scope creep audit

| Check | Result |
|---|---|
| New Business Entity / Grouping / Comparison / Ranking / Route PATCH | **None** — §11 shows exactly two `FieldMutation` sites |
| New NEW / PATCH / REINTERPRET semantics | **None** — `TransitionKind` is `{PATCH, NO_MATCH}` only (`types.py:11-13`) |
| Independent NEW-vs-REINTERPRET classifier | **None** — `classifier.py` is 24 lines: a clarification guard and two ordered detector calls |
| New LLM behaviour | **None** — 0 LLM surface in `transitions/`; `_should_summarize` and its 3 call sites untouched |
| Qwen / `agentic_poc` / tool schemas / `SemanticTransitionProposal` / LLM repair | **None in production.** The only occurrence anywhere in the diff is `docs/transition_layer_pr3_5.md:326`, the line "Qwen Semantic Repair → later PR." — a deferred-work note, not an integration |
| `TransitionDecision` scope | **Minimal** — 6 fields (`kind`, `mutation`, `interpretation_mode`, `evidence_geos`, `diagnostic_event`, `diagnostic_fields`) plus a `no_match()` constructor. No routing graph, execution plan, semantic plan, agent plan, or clarification workflow |
| Transition layer owning execution/persistence | **No** — grep for `commit|save|load|store|revision|execute|planner|executor|summar|reload|ExecutionAdapter` over `transitions/` returns nothing |
| `ContextMutation` / `IntentPatch` / `FieldMutation` / `MutationAction` / `AnalysisIntent` / `ExecutionAdapter` redesign | **None** — `contracts.py` and `execution_adapter.py` diffs are empty |
| Reducer / normalization (PR2a) semantics | **Unchanged** — `_synchronize_effective_intent` (`processor.py:179`) and its four call sites are untouched; `reducer.py` diff is empty |

## 19. Deferred-work boundary

`docs/transition_layer_pr3_5.md` explicitly defers Qwen Semantic Repair and the broader
semantic-transition work to later PRs. I consider the seam PR3.5 leaves to be the right
one: `detect_deterministic_transition` is a single, narrow entrypoint returning a
`TransitionDecision` whose `NO_MATCH` branch already routes to the untouched contextual
path. A future semantic layer can be slotted between "deterministic no-match" and "call
the interpreter" without disturbing either side, and without any of the deterministic
recognition logic needing to move again.

Two things a follow-up PR should pick up, neither blocking:

- Restore the `callable()` predicate in `transitions/geo.py` (Finding 1).
- Consider relocating the three general date helpers out of `transitions/period.py`
  into a neutral module, since five non-transition call sites depend on them
  (Finding 3), and collapse the `_MONTH_PATTERNS` / `_RUSSIAN_MONTHS` duplication
  (Finding 4) while they are adjacent.

## 20. Final answers

### Q1 — New user-facing functionality added?
**NO.** The only lines added to `processor.py` are imports, the transition call, and two
renamed call sites. `TransitionKind` admits only `PATCH` and `NO_MATCH`. No new PATCH
category, mode, or LLM behaviour exists.

### Q2 — Production PATCH categories exactly `periods=SET`, `GEO operands=SET`?
**YES.** The only two `FieldMutation` constructions in all of `src/` are
`transitions/period.py:205` (`periods=SET`) and `transitions/geo.py:183`
(`operands=SET`).

### Q3 — Period recognition/mutation no longer implemented in `processor.py`?
**YES.** Zero residual month tables, season tables, follow-up regexes,
`PeriodPatchCandidate`, `_detect_period_followup`, or `_deterministic_period_patch`.
Period PATCH mutation construction occurs only at `transitions/period.py:200-210`.
(`processor.py` still imports three general date helpers from `transitions.period` for
unrelated pre-existing detectors — see Finding 3 — but owns no period-PATCH semantics.)

### Q4 — GEO recognition/mutation no longer implemented in `processor.py`?
**YES.** Zero residual `_detect_geo_followup`, `_deterministic_geo_patch`,
`_whole_geo_mention_matches`, `GeoPatchCandidate`, business-collision logic, or
similarity thresholds. `processor.py` constructs only the `GeoTransitionServices`
resolver bundle, which I assess as legitimate dependency injection (§7).

### Q5 — Duplicate old/new production routing exists?
**NO.** The legacy `_deterministic_period_mutation` / `_deterministic_geo_mutation` pair
is reachable only when `active_dialog_scope is None`, while the transition layer is
reachable only when it is not — mutually exclusive by a guard identical to baseline.
They also produce `replace_intent` under different modes, not PATCHes. Exactly one
producer per deterministic patch mode.

### Q6 — Period PATCH behaviour unchanged?
**YES.** Byte-identical differential output; PR2b's 55-phrase corpus reproduces its
documented 13-accept / 42-reject split exactly.

### Q7 — GEO PATCH behaviour unchanged?
**YES.** Byte-identical differential output across 125 GEO adversarial phrases in two
intent contexts, including the directed-flow article-rebinding path.

### Q8 — Period PATCH LLM calls?
**0.** Measured by execution across 7 period phrases plus 2 sequence turns; interpreter
and summarizer counters both zero.

### Q9 — GEO PATCH LLM calls?
**0.** Measured by execution across 8 GEO phrases plus 1 sequence turn.

### Q10 — Can "А по Москве за апрель?" be partially intercepted?
**NO.** It returns no-match from both detectors and falls through to the contextual
interpreter (`llm_calls = 1`), identically to baseline. The same holds for
"А по Самаре в мае?" and every mixed probe in the 301-evaluation adversarial sweep.

### Q11 — Can "ГП ТГ Москва" become GEO Moscow?
**NO.** All six business-collision variants (`А по ГП ТГ Москва?`,
`А для ГП ТГ Москва?`, `А по Газпром трансгаз Москва?`,
`А для Газпром трансгаз Москва?`, `А для ТГ Ухта?`, `А по ГП ТГ Нижний Новгород?`)
return no-match and fall through, in both trees.

### Q12 — Does Rostov/May → April → Samara → May work?
**YES.** Verified end-to-end with a real store: Rostov/May → Rostov/April →
Samara/April → Samara/May, with the emitted SQL-layer query text confirming each
transition, 0 LLM calls throughout, byte-identical to baseline.

### Q13 — executed == committed == reloaded for period and GEO PATCH?
**YES.** For all three transition turns, the planner's executed `AnalysisIntent` equals
the committed active-scope intent equals the reloaded intent (`store.commit` →
`store.get`), with revisions advancing 2 → 3 → 4.

### Q14 — Reducer/normalization semantics changed?
**NO.** `reducer.py` diff is empty; `_synchronize_effective_intent` and its four call
sites are untouched by this diff.

### Q15 — ExecutionAdapter semantics changed?
**NO.** `git diff 8981914..a1f3383 -- src/balance_chat/execution_adapter.py` is empty,
as is the `contracts.py` diff.

### Q16 — Transition layer contains execution/persistence logic?
**NO.** No planner, executor, store, commit, reload, revision, summarizer, or LLM
reference exists in `transitions/`. The layer detects, resolves via injected services,
builds a `ContextMutation`, and returns a diagnostic mode.

### Q17 — Qwen/agentic integration in PR3.5?
**NO.** The sole occurrence in the entire diff is a deferred-work bullet in the design
doc (`docs/transition_layer_pr3_5.md:326`).

### Q18 — Observable regressions vs PR3 baseline?
**NO.** 383 → 422 passed with zero failures and zero removed tests; all targeted suites,
Golden, and the acceptance dry-run are equal or additive; the differential and
adversarial harnesses find no behavioural difference on any reachable input. The single
divergence I could construct (Finding 1) requires a malformed registry that cannot occur
in production.

### Q19 — Architectural boundary genuinely created, or code just physically moved?
**CLEAN BOUNDARY.** Not merely a file move: `processor.py` has zero residual recognition
logic, the entire PATCH-production surface of the codebase now lives behind the new
interface, the dependency edge is one-way with no cycles or import hacks, and the package
imports without dragging in `processor` — so it is independently testable, which the new
test modules exercise. The one soft spot is that `transitions/period.py` also hosts three
general date utilities consumed by non-transition code (Finding 3), which makes the
boundary slightly porous in the import graph but not in the semantics.

### Q20 — Ready to merge?
**YES.** No blockers, no high findings. Finding 1 is worth a one-line follow-up but is
unreachable in production and does not gate this merge.

---

### Review method note

- Worktree arrived at `8a5bbe6`, an unrelated ref; detached onto `a1f3383` before any
  substantive work. No ref was ever checked out inside the review worktree for
  comparison purposes.
- Baseline comparison used `git archive 8981914 | tar -x` into a throwaway directory
  nested as a sibling of a junction to the real `C:\#work\sl\pipeline`, so that
  `parents[2] / "pipeline"` resolves; the baseline reproduced its documented
  383 passed / 1 warning exactly, validating the rig. All throwaway directories were
  deleted afterwards.
- All test and harness runs used `C:\Users\alexs\miniforge3\envs\ai_env\python.exe`
  directly. No packages were installed; no virtualenv was created.
- Production code was treated as strictly read-only. The only file written in the
  repository is this review document.
