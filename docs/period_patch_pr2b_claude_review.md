# PR2b independent adversarial review — deterministic period PATCH

Reviewer: independent senior/staff engineer (read-only review)
Branch: `feat/period-patch-pr2b`, tip `a6709ae`
Baseline: `e11dc25` (PR2a merge)
Diff scope: `git diff e11dc25..a6709ae` — `docs/period_patch_pr2b.md`, `src/balance_chat/processor.py`, `tests/test_period_patch.py`

## 1. Verdict

**APPROVE WITH REQUIRED FIXES**

One HIGH finding must be fixed before this reaches production traffic: the "zero LLM calls" invariant holds only on the native-scalar execution branch. For balance-snapshot, balance-section and directed-flow shaped active intents — which is most of the P0 Golden catalog — a deterministic period PATCH still makes one LLM summarizer call.

The core PATCH semantics are correct and genuinely conservative. The period detector did not produce a single false positive across 55 probes, non-period fields are preserved, year inheritance is correct and clock-independent, and executed == committed == reloaded holds. The defect is in the *LLM-bypass* claim, not in the mutation semantics.

## 2. Executive summary

PR2b adds the first real production PATCH producer. What it does well:

- It is genuinely patch-only and genuinely `periods=SET`. Repo-wide audit confirms exactly one production non-empty patch producer (`processor.py:4541-4545`), and nothing else.
- The recognizer is extremely conservative. It anchors on `re.fullmatch` over a normalized token stream, so any additional token anywhere in the utterance causes it to fall through to the existing contextual path. 55 adversarial probes produced **zero** false positives.
- Year inheritance takes the year from the active intent's period, never from `date.today()`. I verified this on a machine whose system date is 2026-08-17; no probe result referenced 2026.
- The PR2a collapse contract is respected: `previous_effective_intent` passed into `_synchronize_effective_intent` is genuinely the post-materialization/pre-normalization intent.
- Regression is clean: baseline 248 passed, PR2b 277 passed, **no test removed**, 29 added.

What must be fixed:

- The summarization skip was added at only **one** of the two reachable summarizer call sites. `_execute_mutation` (line 2441) is guarded; `_execute_full_balance_mutation` (line 2649) is not. A period PATCH on a balance/section/directed-flow active intent therefore reaches an LLM. PR2b's own Golden test misses this only because its fixture emits entities in a non-canonical order.

Secondary: the documentation states the zero-LLM property unconditionally, which is not true as implemented.

## 3. Blockers

None.

I considered whether the HIGH finding below is a blocker. It is not, because: the period replacement itself is still correct, the analytical facts/rows are unaffected, no semantics are lost, and the turn is still strictly cheaper than the baseline (baseline spent 1 interpreter call + 1 summarizer call; PR2b spends 0 + 1). It is a falsified invariant and a determinism/cost regression against the stated contract, not a correctness or data-integrity failure.

## 4. High findings

```
Finding: The "0 LLM calls" invariant is violated for balance-snapshot, balance-section
         and directed-flow shaped active intents. The deterministic_period_patch guard
         was added to only one of the two reachable LLM summarizer call sites.
Severity: HIGH
Evidence:
  - Guard added by PR2b:  src/balance_chat/processor.py:2441-2446, in _execute_mutation
        if (
            interpretation_mode != "deterministic_period_patch"
            and outcome == TransitionOutcome.SUCCESS
            and execute_db
            and callable(summarize)
        ):
  - Unguarded call site:  src/balance_chat/processor.py:2648-2649, in
        _execute_full_balance_mutation
        summarize = getattr(self.runtime, "summarize_envelope", None)
        if outcome == TransitionOutcome.SUCCESS and execute_db and callable(summarize):
    ^ no interpretation_mode check.
  - Reachability branch:  src/balance_chat/processor.py:2394-2410, in _execute_mutation
        if (_is_full_balance_show(intent) or _is_balance_section_show(intent)
            or _is_directed_flow_show(intent)):
            return self._execute_full_balance_mutation(...)
    This branch is taken BEFORE the guarded summarizer at 2441 is ever reached.
  - summarize_envelope is genuinely LLM-backed:
        src/balance_chat/compat/pipeline_runtime.py:132-140
        -> pipeline_api._apply_configured_summary(...)  ("configured presentation model")
  - The three shape predicates all accept intents the detector also accepts:
        _is_full_balance_show      processor.py:3712-3728  (SHOW, 1 operand, metric "balance")
        _is_balance_section_show   processor.py:3455-3468  (SHOW, 1 operand, metric "balance_section")
        _is_directed_flow_show     processor.py:3559-3574  (SHOW, 1 operand, metric in
                                                            {"incoming","distribution"},
                                                            roles [balance, source|destination, article])
    The detector (processor.py:4472-4483) only requires operation in {SHOW, AGGREGATE},
    one operand, one global period, no operand-local periods. It never inspects metric,
    entity roles, or grouping, so all three shapes pass.
  - Production really produces these shapes in that exact role order:
        _deterministic_directed_flow_mutation, processor.py:1948-1961, builds
        entities=[_typed_entity("balance",...), directional, _typed_entity("article",...)]
        i.e. exactly [balance, source|destination, article].
    The Golden catalog agrees: golden/golden_queries.toml GQ-004 declares entities in
    role order balance, source, article; GQ-001 is metric "balance" / result_shape
    "balance_snapshot".

Reproduction (executed against the real processor, not reasoned about):
  Active intent = full-balance SHOW (GQ-001 shape), follow-up "А за апрель?",
  execute_db=True, runtime counting summarize_envelope calls:

    active intent is full-balance SHOW : True
    detector accepts 'А за апрель?'    : True
    interpretation_mode      : deterministic_period_patch
    mutation.replace_intent  : None
    patch.periods            : [PeriodRef(2025-04-01 .. 2025-05-01)]
    interpreter calls        : 0
    LLM summarize_envelope   : 1     <<< expected 0
    response summary         : {'text': 'LLM SUMMARY', 'generated_by': 'llm'}

  Sweep across shapes, all end-to-end through PipelineV2TurnProcessor.process():

    ACTIVE SHAPE                                       mode                        interp  summarizer
    GQ-001 balance snapshot                            deterministic_period_patch  0       1  <<< LLM
    GQ-004/005 directed flow [balance,source,article]  deterministic_period_patch  0       1  <<< LLM
    distribution [balance,destination,article]         deterministic_period_patch  0       1  <<< LLM
    PR2b test order [balance,article,destination]      deterministic_period_patch  0       0

Why it matters:
  1. It falsifies a headline invariant of this PR ("LLM calls for deterministic period
     follow-up: 0") for what is likely the majority of real P0 traffic. GQ-001 (balance
     snapshot), GQ-002/007/008 (section snapshot) and GQ-004/005/006 (directed flow) are
     seven of the eight approved Golden queries; all establish active scopes of exactly
     the shapes that leak.
  2. A turn advertised and logged as "deterministic" produces a user-visible summary
     generated by an LLM, so the response is not reproducible turn-to-turn. The
     diagnostics say mode=deterministic_period_patch and source=deterministic while the
     rendered summary came from a model.
  3. It carries the latency and cost the PR claims to have removed.
  4. Most importantly for review confidence: PR2b's own Golden test asserts
     summary_calls == 0 and passes, so CI actively signals that the invariant holds when
     it does not. The test only escapes the bug because its fixture
     (tests/test_period_patch.py:_GoldenRuntime.execute_raw) yields entities in role
     order [balance, article, destination], which fails _is_directed_flow_show's exact
     role-list comparison. Production's own producer emits [balance, destination,
     article], which matches. The test's green status depends on incidental entity
     ordering in a mock.

Minimal correction:
  Apply the same guard at the second call site. In _execute_full_balance_mutation,
  processor.py:2649:

      if (
          interpretation_mode != "deterministic_period_patch"
          and outcome == TransitionOutcome.SUCCESS
          and execute_db
          and callable(summarize)
      ):

  interpretation_mode is already a parameter of that method (declared at
  processor.py:2558), so no signature change is needed.

  Better, to stop this recurring for PR3 and beyond: hoist the decision into one helper,
  e.g. _should_summarize(interpretation_mode, outcome, execute_db, summarize), or carry
  a boolean `deterministic` flag on the dispatch so new execution branches inherit the
  policy by default instead of having to remember it.

  Then add a regression test whose active intent is a full-balance SHOW (and one
  directed-flow SHOW in canonical [balance, source, article] order) asserting
  summary_calls == 0.
```

## 5. Medium/Low findings

```
Finding: docs/period_patch_pr2b.md states the zero-LLM property unconditionally.
Severity: MEDIUM (documentation correctness; it is the artifact reviewers trust)
Evidence: docs/period_patch_pr2b.md:77-81
    "Successful deterministic period replacement performs zero LLM calls:
     - the contextual interpreter is not invoked;
     - optional LLM result summarization is not requested for deterministic_period_patch."
  The second bullet is false for the _execute_full_balance_mutation branch.
  Line 118 likewise claims the Golden Transition "makes zero LLM calls" — true only for
  that fixture's non-canonical entity ordering.
Why it matters: the doc is the basis on which the invariant was signed off; it currently
  overstates a safety property.
Minimal correction: after fixing the HIGH finding the text becomes true. If the fix is
  deferred, scope the claim to the native-scalar execution branch explicitly.
```

```
Finding: The month stem regexes accept grammatically malformed period text.
Severity: LOW
Evidence: _MONTHS, processor.py:4451-4457, uses open stems such as r"\bянвар\w*",
  r"\bмарт\w*", r"\bавгуст\w*". Combined with the detector's fullmatch these accept:
      "За марта"        -> ACCEPT 2025-03-01 .. 2025-04-01
      "За январе"       -> ACCEPT 2025-01-01 .. 2025-02-01
      "За августе 2025" -> ACCEPT 2025-08-01 .. 2025-09-01
Why it matters: low. Every one of these resolves to the month the user obviously meant,
  so the failure mode is over-acceptance of clumsy phrasing, not a semantic error. Worth
  recording only because it shows the stems are looser than the documented surface forms.
  Note May is already tightened (r"\bма(?:й|я|е|ю|ем)\b"), which is why "За майские"
  correctly falls through. _MONTHS is pre-existing and unmodified by PR2b.
Minimal correction: none required for PR2b.
```

```
Finding: Two similarly named period producers now exist, one of which is pre-existing
         dead code, and the dead one is the template PR3 would naturally copy.
Severity: LOW (pre-existing; forward-looking risk for PR3)
Evidence: process() delegates to _process_contextual at processor.py:212-221 whenever
  state.active_dialog_scope is not None. Everything after that line therefore runs only
  when the scope is None. But at processor.py:435 process() calls
  _deterministic_period_mutation(state, ...), and that function returns None immediately
  when scope is None (processor.py:4550-4552). It is unreachable. The same applies to
  self._deterministic_geo_mutation at processor.py:438 and _deterministic_grouping_query
  at processor.py:407.
Why it matters: not a PR2b defect — process() is untouched by this diff, so this is
  baseline behaviour. It matters for PR3: the existing "deterministic geo" hook is dead,
  so a GEO PATCH must be wired into _process_contextual the way PR2b wired the period
  PATCH, not into the existing process() slot.
Minimal correction: out of scope here; worth a separate cleanup PR.
```

```
Finding: Test gap — no coverage for the execution branches that actually leak the LLM,
         nor for the two year-boundary cases the docs call out.
Severity: LOW (as a test-quality finding; the underlying leak is the HIGH above)
Evidence: tests/test_period_patch.py never constructs an active intent with
  metric "balance", metric "balance_section", or canonical directed-flow role order, so
  _execute_full_balance_mutation is never exercised by a period PATCH.
  docs/period_patch_pr2b.md:47-49 documents "January 2025 + А за декабрь? -> December
  2025", but no test asserts it; the suite only covers December from a May-2025 active
  state (tests/test_period_patch.py:86).
Minimal correction: add the shape tests described in the HIGH fix, plus two
  parametrised year-boundary cases.
```

## 6. Invariant verification

| Invariant | PASS / FAIL / UNCERTAIN | Evidence |
|---|---|---|
| Production PATCH is periods=SET only | **PASS** | Repo-wide audit of `src/`: `IntentPatch(` only at `contracts.py:328` (class), `processor.py:185` (PR2a empty-reset), `processor.py:4541` (the producer). `FieldMutation(` only at `contracts.py:308` (class) and `processor.py:4542`. `MutationAction.SET` in production only at `processor.py:4543`; all other `MutationAction.*` hits are the contract validator (`contracts.py:314-323`) and the reducer's application semantics (`reducer.py:64-83`). No GEO/entity/operation/grouping/compare producer exists. |
| Pure period follow-up uses 0 LLM calls | **FAIL** | Holds on the native-scalar branch (`_execute_mutation`, guard at `processor.py:2441-2446`) — verified `interpreter.calls == 0`, `summary_calls == 0`. Fails on `_execute_full_balance_mutation` (`processor.py:2648-2649`, unguarded), reached via `processor.py:2394-2398`. Measured `summary_calls == 1` for balance-snapshot, section and directed-flow active shapes. See §4. |
| Non-period fields preserved | **PASS** | The patch carries only `periods` (`processor.py:4541-4545`); everything else flows from the active intent through `reduce_intent`. Asserted structurally in `tests/test_period_patch.py:147` (`mutation.patch.model_copy(update={"periods": None}) == IntentPatch()`) and `:153` (`effective.model_copy(update={"periods": active.periods}) == active`) across four operation/metric/entity-type combinations. Independently reconfirmed end-to-end at `:372-374` on the committed intent. |
| Year inheritance correct | **PASS** | `processor.py:4507-4511` takes `active_intent.periods[0].date_from.year`. Measured: May 2025 + "А за апрель?" -> 2025-04-01..2025-05-01; Jun 2023 + "А за апрель?" -> 2023-04-01..2023-05-01. Machine system date was 2026-08-17 and no probe produced a 2026 period, so the result is provably independent of `date.today()`. Boundaries are half-open and month rollover is handled at `processor.py:4513-4517` (Dec -> `date(year+1,1,1)`). |
| Explicit year overrides inherited year | **PASS** | `processor.py:4507-4509` prefers `named_month.group(1)`. Measured: May 2025 + "А за апрель 2024?" -> 2024-04-01..2024-05-01; "А за апрель 2025 года?" -> 2025-04-01..2025-05-01. Also covered by `tests/test_period_patch.py:82-83`. |
| Ambiguous/mixed turns not intercepted | **PASS** | 55-phrase audit, zero false positives (§8). The `re.fullmatch` anchor at `processor.py:4485-4488` over `_normalize_text` output is what enforces this: any surviving extra token rejects the whole utterance. Operation subset enforced at `processor.py:4473-4479`. |
| No-active-state safe | **PASS** | `_deterministic_period_patch` returns `None` when `state.active_dialog_scope is None` (`processor.py:4527-4530`), so `_process_contextual` is never even entered for that case — `process()` only routes there when a scope exists (`processor.py:212`). Asserted by `tests/test_period_patch.py:194-197`. No fabricated or default intent is constructed. |
| Executed == committed == reloaded | **PASS** | `tests/test_period_patch.py:366-374` captures the real planner input via `_CapturingPlanner` and asserts `executed_intent.periods == [April 2025]`, `committed == reloaded`, `committed == reduce_intent(may_state, followup.mutation)`, and `committed == executed_intent`. I re-ran this test: passes. Revision advances 1 -> 2 (`:375`). |
| PR2a normalization semantics preserved | **PASS** | The period-patch path enters `_execute_mutation` like every other mutation and hits both `_synchronize_effective_intent` calls unchanged (`processor.py:2296-2301`, `2317-2322`). Critically for the PR2a follow-up concern: `previous_effective_intent=intent` where `intent = effective_intent` (`processor.py:2293`), i.e. genuinely the post-materialization, pre-normalization intent — materialized by `_dispatch_mutation` at `processor.py:139-141` via `_materialize_mutation` -> `execution_adapter.effective_intent`. The second call correctly chains from `normalized_comparison`. No bypass. |
| Existing single-turn behavior unchanged | **PASS** | 248 -> 277 passed, zero failures, zero removed tests. The detector is gated behind `state.active_dialog_scope is not None` (via `_process_contextual`) plus `clarification is None and state.pending_clarification is None` (`processor.py:617-621`), so no standalone turn can reach it. Golden 14/14, acceptance-runner 10/10. |

## 7. Production call graph

Scenario: session already holds an active scope from
"Покажи распределение газа в Ростовскую область за май 2025"; the user sends "А за апрель?".

| # | Location | Function | Input | Output |
|---|---|---|---|---|
| 1 | `api.py` | FastAPI turn endpoint | HTTP body `{message, execute_db, ...}` | delegates to service |
| 2 | `service.py` | turn service | loads `ContextContractV2` from store | calls `processor.process(...)` |
| 3 | `processor.py:199` | `PipelineV2TurnProcessor.process` | `state`, `message="А за апрель?"` | `turn_id = uuid4()`; `clarification is None` so no validation |
| 4 | `processor.py:212-221` | same | `state.active_dialog_scope is not None` | **returns** `self._process_contextual(...)` — the whole non-contextual detector chain at 222-603 is skipped |
| 5 | `processor.py:617-621` | `_process_contextual` | guard `clarification is None and state.pending_clarification is None` | calls `_deterministic_period_patch(state, message, turn_id)` |
| 6 | `processor.py:4526-4534` | `_deterministic_period_patch` | `scope = state.active_dialog_scope` (not None) | calls `_detect_period_followup(message, scope.intent)` |
| 7 | `processor.py:4472-4483` | `_detect_period_followup` | active intent preconditions: `operation in {SHOW, AGGREGATE}`, 1 operand, 1 global period, no operand-local periods | passes |
| 8 | `processor.py:4484-4488` | same | `_normalize_text("А за апрель?")` -> `"а за апрель"`; `re.fullmatch(r"(?:а\s+)?(?:покажи\s+)?за\s+(?P<period>.+)")` | `period_text = "апрель"` |
| 9 | `processor.py:4489-4519` | same | iterate `_MONTHS`; `\bапрел\w*` fullmatches; no explicit year | `year = 2025` (inherited from active period), `PeriodPatchCandidate(periods=(PeriodRef(2025-04-01, 2025-05-01),))` |
| 10 | `processor.py:4535-4546` | `_deterministic_period_patch` | candidate | `ContextMutation(turn_id, user_message, normalized_message=message, replace_intent=None, patch=IntentPatch(periods=FieldMutation(SET, [April 2025])))` |
| 11 | `processor.py:622-629` | `_process_contextual` | mutation not None | logs `deterministic_period_patch_recognized` |
| 12 | `processor.py:630-639` | same | | **returns** `_dispatch_mutation(..., interpretation_mode="deterministic_period_patch", memory_chunks=[])` — **this is the LLM bypass point**: `self.interpreter.interpret(...)` at `processor.py:761` is never reached, and neither is the result-memory retrieval at `752-759` or the gate `route` at `735` |
| 13 | `processor.py:139-141` | `_dispatch_mutation` | `effective_intent is None` | `intent = _materialize_mutation(state, mutation)` |
| 14 | `processor.py:110-116` | `_materialize_mutation` | | `execution_adapter.effective_intent(state, mutation)` -> April-2025 intent, all other fields inherited |
| 15 | `processor.py:142-156` | `_dispatch_mutation` | `intent.grouping` falsy and operation != GROUP | falls through to `_execute_mutation` |
| 16 | `processor.py:2293-2302` | `_execute_mutation` | `previous_effective_intent = intent` (post-materialization, pre-normalization) | `_normalize_same_scope_period_comparison_intent` no-op here -> `_synchronize_effective_intent` returns the mutation **unchanged**, patch preserved (PR2a no-op branch) |
| 17 | `processor.py:2313-2322` | same | `_normalize_series_reduction_intent` | no-op -> patch still preserved |
| 18 | `processor.py:2338-2342` | same | `gate.validate_bound_intent(intent, explicit_businesses=(), explicit_geos=())` | passes (no new entities were mentioned; bindings are inherited) |
| 19 | `processor.py:2359` | same | `planner.plan(intent)` | plan over April 2025 — verified equal to the committed intent by `tests/test_period_patch.py:366-371` |
| 20 | `processor.py:2394-2398` | same | **branch point** | if `_is_full_balance_show / _is_balance_section_show / _is_directed_flow_show` -> `_execute_full_balance_mutation` (**LLM leak, §4**); otherwise continue |
| 21 | `processor.py:2412-2417` | same | `executor.execute(plan, ...)` | native scalar result |
| 22 | `processor.py:2441-2446` | same | guard `interpretation_mode != "deterministic_period_patch"` | **summarizer skipped** — this is the intended 0-LLM behaviour |
| 23 | `processor.py:2501-2515` | same | `decision is None` | diagnostics `{"mode": "deterministic_period_patch", "source": "deterministic", "confidence": 1.0}` |
| 24 | — | `TurnProcessResult` | | returned to service |
| 25 | `store.py` | `store.commit(session, revision, mutation, outcome, result)` | patch-only mutation | revision N -> N+1 |
| 26 | `store.py` | `store.get(session)` | | reloaded active intent carries April 2025, all other fields unchanged |

LLM bypass summary: the single `return` at step 12 is what removes the interpreter call. It is structurally sound — `_dispatch_mutation` returns a `TurnProcessResult` directly, so nothing downstream in `_process_contextual` can execute. The only surviving LLM reachability on this turn is the summarizer at step 20/§4. The third summarizer in the file (`processor.py:2757`, inside `_standalone`) is not reachable from a period-patch turn, since the patch path never calls `_standalone`.

## 8. False-positive audit

Method: every phrase below was executed through `_detect_period_followup(phrase, active_intent)` against a representative active intent (SHOW / distribution / Ростовская область / May 2025, one operand, one global period). Results are actual program output, not manual regex reading.

| # | Phrase | Normalized | Result |
|---|---|---|---|
| 1 | `А за апрель?` | `а за апрель` | ACCEPT `[2025-04-01 .. 2025-05-01)` |
| 2 | `За февраль?` | `за февраль` | ACCEPT `[2025-02-01 .. 2025-03-01)` |
| 3 | `Покажи за июнь` | `покажи за июнь` | ACCEPT `[2025-06-01 .. 2025-07-01)` |
| 4 | `А за апрель 2024?` | `а за апрель 2024` | ACCEPT `[2024-04-01 .. 2024-05-01)` |
| 5 | `А за 1 апреля 2025?` | `а за 1 апреля 2025` | ACCEPT `[2025-04-01 .. 2025-04-02)` |
| 6 | `А по Москве за апрель?` | `а по москве за апрель` | None (falls through) |
| 7 | `А по Самаре в апреле?` | `а по самаре в апреле` | None (falls through) |
| 8 | `За апрель по областям` | `за апрель по областям` | None (falls through) |
| 9 | `А за апрель по Москве?` | `а за апрель по москве` | None (falls through) |
| 10 | `За апрель в Ростовскую область` | `за апрель в ростовскую область` | None (falls through) |
| 11 | `Покажи максимум за апрель` | `покажи максимум за апрель` | None (falls through) |
| 12 | `А за апрель покажи максимум` | `а за апрель покажи максимум` | None (falls through) |
| 13 | `А за апрель кто больше?` | `а за апрель кто больше` | None (falls through) |
| 14 | `За апрель по месяцам` | `за апрель по месяцам` | None (falls through) |
| 15 | `Покажи апрель по месяцам` | `покажи апрель по месяцам` | None (falls through) |
| 16 | `За апрель суммарно по всем регионам` | `за апрель суммарно по всем регионам` | None (falls through) |
| 17 | `Сравни с апрелем` | `сравни с апрелем` | None (falls through) |
| 18 | `Сравни апрель с мартом` | `сравни апрель с мартом` | None (falls through) |
| 19 | `А за апрель и май?` | `а за апрель и май` | None (falls through) |
| 20 | `А апрель и май?` | `а апрель и май` | None (falls through) |
| 21 | `За апрель и март` | `за апрель и март` | None (falls through) |
| 22 | `За апрель против марта` | `за апрель против марта` | None (falls through) |
| 23 | `Почему в апреле меньше?` | `почему в апреле меньше` | None (falls through) |
| 24 | `Почему апрель хуже?` | `почему апрель хуже` | None (falls through) |
| 25 | `А что изменилось в апреле?` | `а что изменилось в апреле` | None (falls through) |
| 26 | `За апрель почему так мало?` | `за апрель почему так мало` | None (falls through) |
| 27 | `За какой апрель?` | `за какой апрель` | None (falls through) |
| 28 | `А за прошлый месяц?` | `а за прошлый месяц` | None (falls through) |
| 29 | `А за предыдущий месяц?` | `а за предыдущий месяц` | None (falls through) |
| 30 | `А за тот же период?` | `а за тот же период` | None (falls through) |
| 31 | `А Москва в апреле?` | `а москва в апреле` | None (falls through) |
| 32 | `Покажи максимум апреля` | `покажи максимум апреля` | None (falls through) |
| 33 | `В апреле?` | `в апреле` | None (falls through) |
| 34 | `Апрель` | `апрель` | None (falls through) |
| 35 | `А в апреле?` | `а в апреле` | None (falls through) |
| 36 | `За апрель-май` | `за апрель май` | None (falls through) |
| 37 | `За апрель 2025 - май 2025` | `за апрель 2025 май 2025` | None (falls through) |
| 38 | `За период с апреля по май` | `за период с апреля по май` | None (falls through) |
| 39 | `За 2 квартал` | `за 2 квартал` | None (falls through) |
| 40 | `За год` | `за год` | None (falls through) |
| 41 | `За 2024 год` | `за 2024 год` | None (falls through) |
| 42 | `А за апрель, пожалуйста` | `а за апрель пожалуйста` | None (falls through) |
| 43 | `А за апрель!!!` | `а за апрель` | ACCEPT `[2025-04-01 .. 2025-05-01)` |
| 44 | `а за апрель` | `а за апрель` | ACCEPT `[2025-04-01 .. 2025-05-01)` |
| 45 | `А ЗА АПРЕЛЬ?` | `а за апрель` | ACCEPT `[2025-04-01 .. 2025-05-01)` |
| 46 | `А за апреля?` | `а за апреля` | ACCEPT `[2025-04-01 .. 2025-05-01)` |
| 47 | `Покажи за апрель месяц` | `покажи за апрель месяц` | None (falls through) |
| 48 | `А за апрель 2025 года?` | `а за апрель 2025 года` | ACCEPT `[2025-04-01 .. 2025-05-01)` |
| 49 | `За марта` | `за марта` | ACCEPT `[2025-03-01 .. 2025-04-01)` |
| 50 | `За майские` | `за майские` | None (falls through) |
| 51 | `За январе` | `за январе` | ACCEPT `[2025-01-01 .. 2025-02-01)` |
| 52 | `За августе 2025` | `за августе 2025` | ACCEPT `[2025-08-01 .. 2025-09-01)` |
| 53 | `Интересно, а за апрель?` | `интересно а за апрель` | None (falls through) |
| 54 | `Хорошо, а за апрель?` | `хорошо а за апрель` | None (falls through) |
| 55 | `Спасибо. А за апрель?` | `спасибо а за апрель` | None (falls through) |

**False positives found: 0.**

Notable observations:

- **The `fullmatch` anchor is the entire safety mechanism, and it works.** Every mixed-intent probe (#6-#22) is rejected purely because a token survives outside the `за <month>` frame. There is no semantic understanding here at all — which in this context is a virtue.
- **The failure mode is false negatives, and they are safe.** #42 (`А за апрель, пожалуйста`), #47 (`Покажи за апрель месяц`), #53-#55 (a discourse particle before the request) are legitimate pure-period follow-ups that fall through to the LLM path. Users pay a latency/cost penalty and get the old behaviour; nothing breaks. This is the correct direction to err.
- **#50 vs #49/#51/#52 is an instructive asymmetry.** May's pattern is tightened to `\bма(?:й|я|е|ю|ем)\b`, so `За майские` correctly falls through, while the open stems for March/January/August accept oblique-case noise (`За марта`, `За январе`, `За августе 2025`). All three still resolve to the intended month, so the impact is nil — but it shows the stems are looser than the documented surface forms.
- **Branch-ordering concern is unfounded.** I specifically checked whether the `_MONTHS` iteration order plus `continue` could let an earlier month's stem swallow a later, more-correct branch. It cannot: the inner match is a `fullmatch` against the entire `period_text`, and no two month stems in `_MONTHS` can both fullmatch the same single-month token (`\bмарт\w*` vs `\bма(?:й|я|е|ю|ем)\b` is the only near-collision, and it is disjoint). The explicit-day branch delegates to the pre-existing `_explicit_single_day_period`, and if that returns `None` the detector returns `None` rather than falling through to a wrong month — conservative and correct.

Is this becoming a second general-purpose NLP parser? Not yet, and I would not flag it as a god-router today. It is ~50 lines, has one trigger frame, reuses the existing `_MONTHS` table and the existing day parser, and contains no domain routing (no GEO, no metric, no operation vocabulary). The thing to watch is that it is the fourteenth deterministic detector in this file and the second one named "period"; the risk is accumulation in `processor.py` (4821 lines), not this function's own complexity.

## 9. PATCH usage audit

Repo-wide over `src/` (production). Test occurrences excluded as out of scope for the invariant.

| Symbol | Location | Classification |
|---|---|---|
| `IntentPatch(` | `contracts.py:328` | class declaration |
| `IntentPatch(` | `processor.py:185` | PR2a collapse: empty-patch reset inside `_synchronize_effective_intent` |
| `IntentPatch(` | **`processor.py:4541`** | **the PR2b producer** |
| `FieldMutation(` | `contracts.py:308` | class declaration |
| `FieldMutation(` | **`processor.py:4542`** | **the PR2b producer** |
| `MutationAction.KEEP/CLEAR` | `contracts.py:314` | validator |
| `MutationAction.SET/ADD/REMOVE/REFERENCE` | `contracts.py:317-323` | validator |
| `MutationAction.SET` | **`processor.py:4543`** | **the PR2b producer** |
| `MutationAction.CLEAR/REFERENCE/SET/ADD/REMOVE` | `reducer.py:64,67,70,77,83` | reducer application semantics (pre-existing, unmodified) |

**Result: exactly one production non-empty patch category — `periods=SET`.** No GEO, entity, operation, grouping, comparison, formula, or ranking patch producer exists in production. Invariant I1 holds.

Mutation provenance: for a typical "А за апрель?" turn neither normalizer fires, so `_synchronize_effective_intent` takes its equality branch (`processor.py:180-181`) and returns the mutation untouched. The mutation that reaches `store.commit` is therefore genuinely still patch-only — `replace_intent is None`, `patch.periods.action == SET` — and is **not** silently collapsed to a full replacement. Asserted at `tests/test_period_patch.py:359-360` on the real processor output.

## 10. Test adequacy

29 new tests in `tests/test_period_patch.py`. Assessment per required area, including whether a wrong implementation could still pass:

| Area | Covered | Could a wrong implementation pass? |
|---|---|---|
| Month inheritance | Yes — `:77-100`, 8 parametrised cases | Weakly. All cases use a May-2025 active state, so an implementation that hardcoded 2025 would pass. The clock-independence property is *not* directly asserted; I verified it separately (system date 2026, no 2026 output). |
| Explicit year | Yes — `:82-83` | No. `А за апрель 2024?` -> 2024 distinguishes override from inheritance. |
| No active state | Yes — `:194-197` | No. Directly asserts `None` with an empty state. |
| Ambiguous compare | Yes — `:159-173`, 8 phrases | No, for the listed phrases. I extended this to 55 phrases with zero false positives. |
| Mixed GEO + period | Yes — `А по Москве за апрель?` at `:164` | Partially. Only one GEO phrasing; I added five more (#7-#10, #31), all correctly rejected. |
| Different metric contexts | Yes — `:103-110` covers distribution/export/storage_injection/incoming | No for field preservation. **But** none of these four combinations produces a full-balance/section/directed-flow shape, which is exactly why the LLM leak escaped. |
| Different business/GEO contexts | Yes — `:104-110` parametrises geo_object/balance/article/route entity types | Partially — see above; entity *order* is never varied, and order is load-bearing for `_is_directed_flow_show`. |
| No-LLM-call assertion | Yes — `:356-358` (`runtime.calls == 1`, `summary_calls == 0`, `interpreter.calls == 0`) | **Yes — and it does.** This is the critical gap. The assertion passes while the invariant is violated in production, because `_GoldenRuntime.execute_raw` yields entity order `[balance, article, destination]` which misses `_is_directed_flow_show`. |
| Contextual fallback still calls interpreter | Yes — `:378-391` | No. `_CountingInterpreter` raises on call and the test asserts the raise plus `calls == 1`. Good negative-control design. |
| Persistence / reload | Yes — `:347-375` | No. Real `InMemoryContextStore`, real commit/reload, revision asserted 1 -> 2. |
| Non-period-field equality | Yes — `:147`, `:153`, `:372-374` | No. The `model_copy(update={"periods": ...}) == active` idiom is a strong whole-object check rather than field-by-field spot checks. |
| Actual production producer end-to-end | Yes — `:314-375` | Partially — it exercises the real `process()`/`_dispatch_mutation`/planner/store chain, but only on the native-scalar branch. |
| PR2a normalization interaction | Partially — `:370` asserts `committed == reduce_intent(...)`, exercising the no-op preserve branch | The *changed-normalization* collapse branch is not exercised from a period-patch turn. Given PR2a covered it directly this is acceptable, but the interaction is untested. |
| Serialization round-trip | Yes — `:149-152` | Good addition; catches patches that survive in memory but not through JSON. |

Overall: the tests are well constructed — real store, real planner capture, negative controls, whole-object equality rather than spot checks. The one material weakness is that the fixture's entity ordering accidentally steers every test away from the execution branch that contains the bug.

## 11. Regression results

Environment note: the repository's tests resolve the metadata bundle as a *sibling* of the repo root (`Path(__file__).resolve().parents[2] / "pipeline"`). Running inside a nested agent worktree breaks that assumption and produced 28 spurious failures. Per the git-hygiene instruction I did not check out anything into the review worktree; instead I extracted both commits with `git archive` into throwaway directories nested at the correct depth, with a junction to the real `C:\#work\sl\pipeline`, and ran pytest there. Dependencies: fastapi, pydantic, psycopg[binary], uvicorn, pytest, httpx, urllib3 in a scratch venv.

| Run | Result |
|---|---|
| Baseline `e11dc25` | **248 passed, 0 failed, 1 warning** |
| PR2b `a6709ae` | **277 passed, 0 failed, 1 warning** |
| Delta | **+29 passed, 0 new failures** |

Both claimed numbers reproduce **exactly**.

- Collected test IDs diffed between the two commits: **0 removed**, **29 added**. Confirmed via `comm -23` on sorted `--collect-only` output — the removal set is empty. Nothing was deleted to obtain a green run, and unlike PR2a there is no superseded-test situation here at all.
- Skipped: 0 in both runs.
- Warnings: 1 in both — the pre-existing Starlette/`httpx` deprecation warning from `fastapi.testclient`. Not introduced by PR2b.
- All 29 new tests live in the single new file `tests/test_period_patch.py`; no existing test file was modified.

Focused suites (PR2b):

| Suite | Result |
|---|---|
| `tests/test_period_patch.py` | 29 passed |
| `tests/test_golden_queries.py` | 14 passed |
| `tests/test_acceptance_runner.py` | 10 passed |
| Golden + acceptance + api + period_patch + execution_adapter + execution_commit_equivalence | **89 passed, 1 warning** |

The "89 passed" focused-suite claim reproduces exactly with that file set.

## 12. Golden / acceptance verification

**Golden catalog — 8/8: CONFIRMED.** `golden/golden_queries.toml` contains exactly 8 `[[queries]]` entries (GQ-001 … GQ-008), all `approval_status = "approved"`, all `priority = "P0 Core"`. `tests/test_golden_queries.py` passes 14/14 against the real metadata bundle at `pipeline/data/metadata/manifest.json` (bundle `sha256:f8d326b0…`), including the per-query canonical metadata contract checks.

**P0 acceptance dry-run — 84/84: CONFIRMED, dry-run only.** Executed `scripts/run_acceptance.py --dry-run --tag P0`:

```
Scenarios                       7
Automated expectation coverage  100.00%
Checks                          84/84
Scenario ids  BC-01, BC-02, BC-03, BC-04, BC-05, BC-14, BC-15
```

Reproduces the claim exactly (7 scenarios, 84 checks, 100% coverage).

**Explicit scope limitation — live DB NOT executed.** `--dry-run` is documented in the tool's own help as "Parse and measure automation coverage without HTTP". It parses the catalog and verifies that every declared expectation has an automated checker; it issues **no HTTP requests, runs no turns, and touches no database**. It therefore proves *coverage*, not *behaviour*. I did not run the DB-backed acceptance suite: it requires a live staging PostgreSQL (`ai-balances-staging-2025@2026-08-05`), a running backend, and has external state plus session-cleanup side effects. Nothing in this section should be read as evidence of live production correctness.

This limitation compounds the §4 HIGH finding: the leak is in the summarization stage, which only engages when `execute_db=True` against a real runtime. A dry-run structurally cannot detect it, and the in-repo test that could have is neutralised by its fixture's entity ordering. So the LLM leak would most plausibly have first surfaced in production.

## 13. Scope creep

**None found.** The diff touches exactly three files:

```
docs/period_patch_pr2b.md     | 177 +++++++++++++++++++
src/balance_chat/processor.py | 116 ++++++++++++-
tests/test_period_patch.py    | 391 ++++++++++++++++++++++++++++++++++++++++++
```

Production changes are confined to `processor.py` and consist solely of: two import additions (`dataclass`; `FieldMutation`, `MutationAction`), the detector block at the top of `_process_contextual`, the summarizer-skip condition, and three new module-level definitions at the end of the file.

Checked and confirmed absent: no GEO PATCH, no business-entity PATCH, no operation PATCH, no grouping PATCH, no generic `TurnClassifier`, no prompt changes (`src/balance_chat/prompts` untouched), no binder/planner/executor/reducer/contracts/store/API changes, no unrelated `processor.py` cleanup or decomposition. `_MONTHS`, `_normalize_text`, and `_explicit_single_day_period` are reused **unmodified** — verified against the diff, which contains no hunks at those locations.

Date-parser reuse is consistent with the existing contract: the month branch produces half-open `[2025-04-01, 2025-05-01)`, and the explicit-day branch delegates to `_explicit_single_day_period` (`processor.py:3369-3411`), which returns `date_to = start + timedelta(days=1)` — the same half-open convention used by `_deterministic_balance_section_mutation` and `_deterministic_full_balance_mutation`. No new date semantics were invented.

One deliberate non-addition worth crediting: relative forms (`за прошлый месяц`) were explicitly left out rather than approximated, and the docs say why. That is the right call.

## 14. PR3 readiness

**PARTIALLY ready.** The architecture is sound and PR2b validates the pattern end-to-end, but three things should be settled before a GEO PATCH lands.

Confirmed working and reusable for PR3:

- The patch-only mutation -> `ExecutionAdapter` -> `_synchronize_effective_intent` -> planner -> commit -> reload chain is proven with a real production producer, and `previous_effective_intent` is correctly sourced.
- Interception at the top of `_process_contextual` is structurally correct and cleanly bypasses the interpreter.
- The `fullmatch`-anchored, conservative-by-construction detector style demonstrably yields zero false positives and should be the template.

Concrete items to resolve first:

1. **Fix the summarizer guard, preferably centrally (§4).** If PR3 adds a `deterministic_geo_patch` mode, it will have to remember the same skip at every execution branch. A GEO patch is *more* likely than a period patch to land on balance/section/directed-flow shapes, so it would inherit the bug immediately and more broadly. Make the policy default-on for deterministic modes rather than per-call-site opt-out.
2. **Do not copy the dead `process():438` `_deterministic_geo_mutation` slot (§5).** That hook is unreachable — everything with an active scope is routed to `_process_contextual` at `processor.py:212`. A GEO PATCH must be wired into `_process_contextual` the way PR2b did.
3. **Vary entity order in test fixtures.** The single most valuable lesson from this review is that `_is_directed_flow_show` compares an exact role *list*, and PR2b's fixture happens to emit a non-matching order — which hid a real defect behind a green assertion. PR3's tests must exercise canonical `[balance, source|destination, article]` ordering, and ideally a shared fixture builder should be used so shape-routing predicates are actually hit.

Additional consideration specific to GEO: unlike `periods`, a GEO change interacts with the evidence gate. The period patch can safely pass `evidence_businesses=()`/`evidence_geos=()` because it mentions no new entities; a GEO patch introduces a new entity and must feed the gate correctly, or `validate_bound_intent` will either wrongly reject or wrongly accept unevidenced routing. That is a genuinely harder problem than PR2b faced and deserves its own design note.

## Final Q&A

### Q1 — Does `May 2025 + "А за апрель?"` → April 2025 actually work in production via patch-only periods=SET?
**YES.** Verified end-to-end through the real `PipelineV2TurnProcessor.process()`, `InMemoryContextStore` commit and reload: `mutation.replace_intent is None`, `patch.periods.action == SET`, value `[2025-04-01 .. 2025-05-01)`, planner input == committed == reloaded intent, revision 1 -> 2.

### Q2 — LLM call count for this follow-up?
**0 on the native-scalar branch; 1 on the balance/section/directed-flow branch.** The interpreter is never called on any period-patch path (verified 0). The LLM *summarizer* is skipped in `_execute_mutation` but not in `_execute_full_balance_mutation` (`processor.py:2649`), where I measured 1 call for balance-snapshot, balance-section and directed-flow shaped active intents. The unconditional "0" claim is **not proven and in fact false** for most of the P0 catalog's shapes.

### Q3 — Are all non-period semantic fields preserved?
**YES.** The patch carries only `periods`; everything else is inherited via `reduce_intent`. Verified structurally (`patch` minus `periods` equals an empty `IntentPatch`), by whole-object equality on the materialized intent, and again on the committed intent after reload.

### Q4 — Can a mixed/ambiguous follow-up be mistakenly captured as a period PATCH?
**NO.** Zero false positives across 55 executed probes spanning mixed GEO, added operations, comparisons, causal questions, ranges, relative periods, word-order variants and embedded phrasings. The `re.fullmatch` anchor makes any surviving extra token fatal to recognition. Errors fall on the safe side (false negatives that route to the existing LLM path).

### Q5 — Is there a production non-empty PATCH other than periods=SET?
**NO.** Repo-wide audit of `src/` found exactly one production non-empty patch producer (`processor.py:4541-4545`, `periods=SET`). All other occurrences are the contract class/validator, the reducer's application semantics, and PR2a's empty-patch collapse reset.

### Q6 — Is executed == committed == reloaded confirmed for a real production period PATCH?
**YES.** `tests/test_period_patch.py:346-374` captures the actual planner input via a capturing planner and asserts equality against the committed and reloaded active intent through a real store round-trip. I re-ran it; it passes.

### Q7 — Is there an observable regression vs PR2a?
**NO** for the test suite and for mutation semantics — 248 -> 277 passed, no failures, no removed tests, no behaviour change for standalone or non-period contextual turns. **However**, the HIGH finding means one *claimed improvement* is not delivered where it matters most: for balance/section/directed-flow follow-ups the turn still costs one LLM call, so it is a shortfall against the stated contract rather than a regression against PR2a behaviour (baseline spent 2 LLM calls on those turns; PR2b spends 1).

### Q8 — Is the pipeline ready for the next step, PR3 — deterministic GEO PATCH?
**PARTIALLY.** Concrete blockers to clear first:
1. Fix the unguarded summarizer at `processor.py:2649`, and preferably centralise the deterministic-mode summarization policy so new execution branches inherit it by default.
2. Wire any GEO PATCH into `_process_contextual`, not the unreachable `process():438` `_deterministic_geo_mutation` slot.
3. Vary entity ordering in test fixtures so shape-routing predicates (`_is_directed_flow_show` compares an exact role list) are genuinely exercised.
4. Design and document how a GEO PATCH feeds `gate.validate_bound_intent` with evidence, since unlike a period change it introduces a new entity.

---

### Review method note

Worktree HEAD initially pointed at `8a5bbe6` (a divergent ref that did not contain the branch), reproducing the hazard flagged in the brief. I detached onto `a6709ae` before any substantive work. Baseline comparison was done via `git archive` into throwaway directories, never by checking out another ref in the review worktree. All quantitative claims in this report were produced by executing code, not by reading it. Final state: `git status` clean, `HEAD == a6709aef65c5bb0e072c89d3ee13ebc6da84e9ec`.
