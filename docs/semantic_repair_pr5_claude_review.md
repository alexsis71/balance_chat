# PR5 Independent Review — Semantic Repair Shadow Infrastructure

Reviewer: independent staff-level review (eighth in the PR1–PR5 series).
Branch: `feat/semantic-repair-shadow-pr5`, tip `840471d`.
Baseline: `f124040` (post-PR4a), re-verified at **501 passed / 1 warning**.
Scope: architecture and isolation only. Model quality is assessed separately in
`docs/semantic_repair_unsafe_analysis.md`.
PR1–PR4a conclusions are treated as established baseline and were not re-reviewed.

---

## 1. Architecture verdict

**APPROVE WITH REQUIRED FIXES.**

The core isolation claim is real and I was unable to falsify it. No shadow
proposal can reach `ContextMutation`, `AnalysisIntent`, `ExecutionAdapter`, the
planner, the executor, query generation, the persisted store, the session
revision, or the user-visible response. I verified this by exhaustive symbol
tracing, by an adversarial fault-injection probe, and by a byte-level production
differential in which a deliberately hostile shadow corrupts the state snapshot
it is handed and still changes nothing.

There are **no BLOCKERs**. The qualifier is not about correctness. It is about
two things that are real and currently undocumented:

1. the shadow call is synchronous and sits **before** `return response`, so it
   delays the client's HTTP response and holds the cross-request store
   reservation for the entire model latency (7–11 s for Qwen3.8-27B); and
2. the branch's own documentation reports a **null result** (endpoint
   unavailable, 0 valid proposals) while the decision-relevant evaluation
   evidence — 26 valid proposals, a 53.85 % unsafe rate, and the one clean
   `swap_direction` island — exists only as uncommitted working-tree data.

Both are fixable without redesign. Neither undermines the isolation guarantee.

## 2. Executive summary

What I verified independently, with evidence:

| Claim | Verdict | How verified |
|---|---|---|
| Shadow cannot affect execution/state/response | **Confirmed** | symbol trace + hostile-shadow differential |
| Deterministic Period/GEO/Business PATCH never shadowed | **Confirmed** | `shadow.py:23-29`, `179-188`; 3/3 controls skipped; 0 backend calls |
| Feature flag default OFF, zero cost when OFF | **Confirmed** | `bootstrap.py:178-186`; no state copy, no model call |
| Contract is bounded, not a query DSL | **Confirmed** | `contracts.py` — 4 actions, 4 mutation kinds, closed enums |
| Validator deterministic and model-independent | **Confirmed** | `validator.py`, pure function of the payload |
| No canonical IDs reachable from the model | **Confirmed** | contract shape + regex + empirical leakage probe |
| No analytical tools exposed | **Confirmed** | `backend.py:60-90` — chat + JSON schema only |
| Bounded context ≤4 turns / ≤4 results | **Confirmed** | `context.py`; probe shows 4/4 under 8-turn pressure |
| Full regression green | **Confirmed** | 535 passed / 1 warning; 0 baseline tests removed or altered |
| Production differential clean | **Confirmed** | byte-identical, shadow OFF and ON |

What the PR does not currently state, and should:

- the shadow blocks the client response and holds the turn reservation;
- the committed evaluation artifact and the documentation describe a failed run,
  not the run this review is based on.

## 3. Blockers

**None.**

I actively tried to construct one. Specifically I tried to find any path by
which a `SemanticTransitionProposal` becomes a `ContextMutation`, any write from
shadow code into the store, any mutation of `processed.mutation`, any way for a
shadow `CLARIFY` to reach the user, and any failure mode that propagates into
production. All attempts failed. Details in sections 6–8.

## 4. High findings

**None.**

## 5. Medium/Low findings

### Finding 1 — Shadow latency is on the client's critical path and holds the turn reservation

```
Finding:  _run_semantic_shadow is invoked synchronously at service.py:274-280,
          before `return response` at service.py:378. The in-process session
          RLock is correctly released first (the `with` at col 12 closes at
          line 272; lines 273-280 are dedented to col 12). BUT the store-level
          turn reservation taken at service.py:191-194 is only released in the
          `finally:` at service.py:405-418, which runs after the return path.
          The shadow call therefore (a) delays the HTTP response to the current
          client by the full model latency, and (b) extends the window in which
          a concurrent request for the same session is rejected with
          TurnInProgress -> HTTP 409 (api.py:113-116).
Severity: MEDIUM
Evidence: service.py:191-194 (reserve), 195 (lock open, col 12), 272 (last
          statement in lock), 273-280 (shadow, col 12 = lock released),
          378 (return), 405-418 (finally: release). store.py:56-65 and
          store.py:158-180 and store.py:423-458 show reserve() raises
          TurnInProgress when a reservation row exists. Measured shadow latency
          for Qwen3.8-27B: avg 7985 ms, p50 8972 ms, p95 10658 ms.
          Aggravating factor: the SQLite reservation table (store.py:333-335)
          has no expiry column and no TTL reclamation, unlike the Postgres
          table (store.py:428-431, `expires_at <= now()`). A process kill
          during the shadow window therefore wedges the session permanently on
          SQLite. PR5 widens that crash window by ~8-11 s per eligible turn.
Why it matters: this is dormant today (flag OFF) but it is the single thing
          that makes enabling the flag in a live environment costly. A hard-tail
          turn would go from its normal latency to normal + ~9 s, and the
          session would be 409-locked for that whole period. The logged
          `elapsed_ms` metric (service.py:328, fed by production_elapsed_ms
          computed at line 273) deliberately excludes shadow latency, so
          monitoring would not show the regression.
Minimal correction: no code change is strictly required for a dormant flag.
          Before the flag is ever enabled outside an offline harness, either
          (a) move the shadow invocation after `return response` via a
          fire-and-forget worker/thread pool, or (b) move it outside the
          `try/finally` so the reservation is released first. Document the
          tradeoff either way (see Finding 2).
```

### Finding 2 — Branch documentation and committed artifact describe a failed run, not the real evaluation

```
Finding:  docs/semantic_repair_pr5.md sections 12, 13, 15, 17 and mandatory
          answers 16-21 and 23 report an endpoint-unavailable run: 0 valid
          proposals, 26 unavailable, `reachable=false`, unsafe rate "not
          measurable", latency 2038 ms (endpoint-failure latency). The
          committed artifact at 840471d matches that (served_models: [],
          reachable: false, valid_proposals: 0). The real evaluation evidence —
          Qwen3.8-27B, reachable=true, 26/26 valid, 14 unsafe, avg 7985 ms — is
          uncommitted working-tree data, and the second model's artifact
          (semantic_shadow_results_qwen36_35b_a3b.json) is entirely untracked.
Severity: MEDIUM
Evidence: `git show 840471d:artifacts/semantic_shadow_results.json` ->
          valid_proposals 0, unavailable 26, reachable false, avg_latency
          2038.19. Working tree same path -> valid_proposals 26, unavailable 0,
          reachable true, served_models ["Qwen/Qwen3.8-27B"], avg 7985.08.
Why it matters: as committed, the branch tells a reader there is no model data,
          when in fact there is decision-grade model data showing a 53.85 %
          unsafe rate and one clean high-precision mutation kind. A reviewer or
          a future engineer reading only the branch would draw the right PR6
          conclusion ("do not activate") for a reason that is no longer true,
          and would have no record of the swap_direction island. The evidence is
          also at risk of being lost since it is untracked.
Minimal correction: commit both result artifacts and update sections 12/13/15/17
          and answers 16-21 to the observed numbers. The section 17
          recommendation ("do not proceed to active repair") stands unchanged.
```

### Finding 3 — Second model's identity is not established by its own artifact

```
Finding:  semantic_shadow_results_qwen36_35b_a3b.json reports
          endpoint_health.configured_model = "ai-balances-language" and
          served_models = ["ai-balances-planner"]. Neither string identifies
          Qwen3.6-35B-A3B, and the configured and served names disagree with
          each other. The filename is the only assertion of model identity.
Severity: LOW
Evidence: the artifact's endpoint_health and served_models fields. Contrast the
          Qwen3.8 artifact, where both fields agree and self-identify as
          "Qwen/Qwen3.8-27B".
Why it matters: docs/semantic_repair_pr5.md section 12 explicitly and correctly
          states that served model identity "is intentionally not inferred from
          the configured alias". By the PR's own standard, the Qwen3.6 results
          cannot be attributed to a named model. All Qwen3.6 conclusions in the
          companion analysis are therefore reported with that caveat.
Minimal correction: re-run with the served model name resolvable, or record the
          deployment mapping alongside the artifact.
```

### Finding 4 — Shadow eligibility is a deny-list and fails open

```
Finding:  _eligible() (shadow.py:179-188) excludes shadowing only for the three
          names in DETERMINISTIC_PATCH_MODES. Any other mode, including an
          unrecognised one, is eligible. Separately, _run_semantic_shadow
          (service.py:430-435) sets interpretation_mode to None when
          processed.diagnostics has no dict "interpretation" entry, and
          str(None or "") == "" is not in the deny set, so missing diagnostics
          also results in shadowing.
Severity: LOW
Evidence: shadow.py:23-29, 179-188; service.py:430-435.
Why it matters: if a future PR adds a fourth deterministic transition category,
          it will silently begin being shadowed unless someone remembers to add
          the string here. Consequence is cost and latency (Finding 1), never
          correctness, because the shadow has no authority. It does, however,
          quietly weaken the A5 guarantee over time.
Minimal correction: invert to an allow-list of modes that are known hard-tail,
          or assert that the mode is a member of a known enum and skip on
          unknown values.
```

### Finding 5 — `except Exception` does not contain `BaseException` on the critical path

```
Finding:  _run_semantic_shadow wraps its body in `except Exception`
          (service.py:445). KeyboardInterrupt and SystemExit are BaseException
          subclasses and escape. Because the shadow call sits before
          `return response`, an escape converts an already-committed,
          already-cached successful turn into a client-visible error.
Severity: LOW
Evidence: my adversarial probe — of exception / returns_none / returns_garbage /
          recursion / memoryerror / systemexit / keyboard, all were contained
          except systemexit and keyboard, which escaped.
Why it matters: bounded. The commit at service.py:215-223 and the idempotent
          response cache write at service.py:270-272 both happen before the
          shadow runs, so canonical state is already correct and a client retry
          with the same request_id returns the correct cached response. The
          realistic trigger is SIGINT during shutdown landing inside the ~9 s
          shadow window. This finding disappears entirely if Finding 1 is fixed
          by moving the call off the response path.
Minimal correction: `except BaseException` with a re-raise for
          KeyboardInterrupt/SystemExit after logging, or fix Finding 1.
```

### Finding 6 — Evaluation harness reports two hardcoded constants as measurements

```
Finding:  evaluation.py:305-307 emits "tool_call_count": 0, "repeated_tool_calls":
          0, "tool_errors": 0 as literals, never measured. Also
          evaluation.py:260-262 computes
          eligible_hard_tail = runs - (runs - eligible_turns), which is just
          eligible_turns written obscurely.
Severity: LOW
Evidence: evaluation.py:253-308.
Why it matters: docs/semantic_repair_pr5.md section 12 presents "Tool calls | 0"
          in the results table as an observation. It is vacuously true — the
          backend has no tool surface at all (verified) — but reporting a
          constant as a metric is the kind of thing that later gets trusted.
Minimal correction: state "not applicable — no tool surface exists" rather than
          emitting a zero, and simplify the coverage denominator.
```

### Finding 7 — Prompt file is not declared as package data

```
Finding:  SemanticShadowRunner.__init__ reads the prompt eagerly at construction
          (shadow.py:48-49). pyproject.toml declares no package-data and no
          include-package-data, so a non-editable wheel would omit
          src/balance_chat/prompts/*.md and bootstrap would raise
          FileNotFoundError when the flag is ON. This is caught outside the
          backend try/except in bootstrap.py, which only wraps backend
          construction (bootstrap.py:186-197).
Severity: LOW (pre-existing pattern, not a PR5 regression)
Evidence: pyproject.toml; shadow.py:48-49; bootstrap.py:186-201.
Why it matters: interpretation.py:303 already loads
          prompts/interpretation_system_prompt.md the same way, so the
          deployment evidently runs from source and this is latent, not active.
          PR5 adds a second instance of the pattern.
Minimal correction: add package-data for `prompts/*.md`, or read lazily inside
          the existing failure-isolated path.
```

### Informational — the one shadow operation outside the shadow's own guard

`shadow_state = state.model_copy(deep=True)` (service.py:200-204) is the only
shadow-related statement not covered by `_run_semantic_shadow`'s try/except. It
is a pydantic deep copy of a contract of primitives, dates, and nested models,
and cannot realistically raise. It also runs before `processor.process` and
before any commit, so a hypothetical failure aborts the turn atomically through
the existing `except Exception` at service.py:391 with no partially applied
state. It only executes at all when the flag is ON. No action needed; recorded
because the requesting engineer asked it be checked.

## 6. Production call graph

Verified by reading `service.py` with explicit column offsets, not by eye.

```
BalanceChatService.execute_turn(session_id, expected_revision, message, request_id)
│
├─ 190  try:                                              (col 8)
├─ 191-194  reserve(session_id, expected_revision, trace_id)   -> reserved = True
│           ^ cross-request mutex; released only in finally (405)
│
├─ 195  with self._session_lock(session_id):              (col 12)
│  ├─ 196   state = self.store.get(session_id)            -> already a deep copy
│  ├─ 197   self._validate_metadata(state)
│  ├─ 198-199 revision guard -> RevisionConflict
│  ├─ 200-204 shadow_state = state.model_copy(deep=True) if shadow else None
│  │          ^ PRE-TURN snapshot, taken BEFORE processing
│  ├─ 205-211 processed = self.processor.process(state, ...)   AUTHORITATIVE
│  ├─ 212-214 processed.mutation.assistant_summary = ...
│  ├─ 215-223 committed = self.store.commit(...)              AUTHORITATIVE COMMIT
│  ├─ 224-261 result-memory persistence
│  ├─ 262-269 response = {...}                                RESPONSE BUILT
│  └─ 270-272 save_request_result(session_id, trace_id, response)  IDEMPOTENT CACHE
│                                                          ^ last stmt in lock
│  ── lock released here ──
│
├─ 273  production_elapsed_ms = ...                       (col 12)
├─ 274-280  self._run_semantic_shadow(state=shadow_state, ...)   SHADOW
│           returns None; return value discarded
├─ 281-329  log_event("turn_completed", elapsed_ms=production_elapsed_ms)
├─ 330-377  log_event("conversation_turn_committed", ...)
├─ 378  return response                                   <-- delayed by shadow
│
├─ 379-390  except RevisionConflict: log + raise
├─ 391-404  except Exception: log + raise
└─ 405-418  finally: release(session_id, trace_id)        <-- reservation held
                                                              across the shadow
```

Two facts that matter and that I re-derived rather than trusted:

- The `with` statement is at column 12 and lines 273–280 are also at column 12,
  so the session `RLock` **is** released before the shadow runs. The requesting
  engineer's self-correction on this point is right.
- The store reservation is **not** released before the shadow runs, because
  `finally` executes after `return`. This is Finding 1.

`_run_semantic_shadow` (service.py:420-475) reads
`processed.diagnostics["interpretation"]["mode"]`, deep-copies `shadow_state`
a second time, calls `self.semantic_shadow.run(...)`, serialises the proposal
to a plain dict only when `validation_status == "valid"`, logs one INFO event,
and returns `None`. Its entire body is inside `try/except Exception` with no
re-raise.

### On "after authoritative commit" vs the pre-turn snapshot

The requesting engineer asked whether the documentation conflates *when* the
shadow runs with *what state it receives*. It does not. Section 9 of
`semantic_repair_pr5.md` reads: "The service passes a deep copy of the
**pre-turn** state to shadow **after `store.commit`**." That sentence
disambiguates both axes correctly in one line, and matches the code exactly —
snapshot taken at line 200-204 before `processor.process`, invocation at line
274 after `store.commit`. **No documentation-clarity finding is warranted on
this point.**

## 7. Shadow isolation

`SemanticShadowRunner.run` (`shadow.py:51-80`) receives the state, calls
`_eligible(state, ...)` (read-only) and `build_semantic_shadow_context(...)`.
That builder (`context.py:17-66`) reads fields and constructs a fresh
`SemanticShadowContext` of plain dicts and strings. It stores no reference to
the incoming state, holds no module-level cache, and closes over nothing. The
backend receives only `serialized_context`, a JSON string. Nothing that reaches
the model or the log is the live object.

The whole-repo symbol trace for `semantic_repair`, `SemanticTransitionProposal`,
`SemanticShadowRunner`, `semantic_shadow`, `SemanticMutation`,
`SemanticShadowResult`, `ProposalAction`, and `ProposalValidator` returns 19
files: the 7 package modules, 6 test modules, `service.py`, `bootstrap.py`,
`scripts/run_semantic_shadow_eval.py`, `config.example.json`, and 3 docs.
**`processor.py`, `transitions/*`, `execution.py`, `execution_adapter.py`,
`planning.py`, `reducer.py`, `interpretation.py`, and `store.py` contain zero
references.** There is no conversion function from any proposal type to any
production type anywhere in the repository.

Fault injection through the real runner — timeout, unavailable, HTTP error,
malformed JSON, non-object JSON, schema-rejected payload, canonical-ID payload,
maximum-size payload — left production intact in all eight cases, with no
`proposal` key anywhere in the serialized response.

## 8. State/persistence isolation

The strongest evidence is behavioural rather than structural. My differential
harness runs five turns (success and clarification paths) against a real
`BalanceChatService` + `InMemoryContextStore`, with a shadow whose `run()`
deliberately attacks the snapshot it is given:

```python
state.active_dialog_scope.intent.operands[0].metric = "CORRUPTED"
state.revision = 9999
state.conversation_window.clear()
```

Comparing the full normalized snapshot — status, result, context view, session
view, diagnostics, response keys, committed state, revision after each turn,
and the reloaded state — against the `f124040` baseline:

- baseline vs PR5 **shadow OFF**: byte-identical, zero differences;
- baseline vs PR5 **shadow ON** with the hostile shadow, 5 shadow calls fired:
  identical except my own instrumentation counter (`shadow_calls: 0 -> 5`).

Final revision 6 in all three configurations. The PR's own
`DangerousShadow` tests (`test_service_integration.py:69-103`) make the same
point from inside the suite and additionally assert that
`processor.last_result.mutation` is unchanged after the shadow ran.

Shadow code performs no store writes: `store.commit`, `save_request_result`,
`complete_memory_write`, `reserve`, and `release` are all called from
`execute_turn` only, never from `semantic_repair/*`.

## 9. Deterministic fast-path audit

`DETERMINISTIC_PATCH_MODES` (`shadow.py:23-29`) is
`{deterministic_period_patch, deterministic_geo_patch,
deterministic_business_entity_patch}` and `_eligible` returns `False` for all
three. Evidence at three levels:

- unit: `test_shadow.py:60-79` parametrises all three modes and asserts
  `backend.calls == []`;
- service: `test_service_integration.py:208-225` drives the full
  `execute_turn` path for all three and asserts `backend.calls == 0`;
- corpus: all three deterministic controls in both live runs show
  `status=skipped`, `latency=None`, `deterministic_controls_skipped 3/3`.

**Extra Qwen calls for deterministic PATCH turns: 0.**

No hidden second classifier is introduced. `_eligible` consumes the mode string
the existing classifier already put into `processed.diagnostics`; it never
re-classifies the message. The deny-list direction of this check is Finding 4.

## 10. Contract audit

`contracts.py` defines four closed enums and three models:

| Type | Vocabulary |
|---|---|
| `ProposalAction` | `patch`, `rebuild`, `clarify`, `unsupported` |
| `SemanticMutationKind` | `set_operation`, `swap_direction`, `set_comparison`, `reference_prior_result` |
| `ReferenceKind` | `active_state`, `previous_result`, `last_two_results`, `previous_period`, `previous_entity` |
| `ReferenceSelector` | `first`, `second`, `last`, `same` |

`SemanticMutation.value` is `str \| None` capped at 80 chars and further
restricted by the validator to two closed value sets. `clarification_question`
is capped at 500, `reason_code` at 80, `unresolved_mentions` at 8 × 200. The
endpoint schema sets `additionalProperties: false`, `mutations.maxItems: 3`,
`references.maxItems: 4`, and requires every property.

This is not a query DSL. There is no free-form field, no entity identifier, no
column or table name, no filter expression, no plan, and no graph. `rebuild` is
declared but unconditionally rejected (`validator.py:172-173`).

Note that `clarify` and `unsupported` are **actions**, not mutation kinds. The
brief's suggested vocabulary listed them alongside the mutation kinds; the code
separates the two axes cleanly, which is the better design.

One contract-design defect worth recording, because it directly produced model
errors (see the companion analysis): the `(ReferenceKind, ReferenceSelector)`
product space contains meaningless combinations that the validator accepts.
`last_two_results` with `selector: "last"` is semantically incoherent — "last"
of a reference that already denotes a pair — yet `validator.py:131-135` only
rejects `first`/`second` on non-`last_two_results` kinds. Qwen3.8 emitted
exactly this combination in 13 of its 14 unsafe runs.

## 11. Validator audit

`ProposalValidator.validate` is a pure function of the payload. It calls no
model, reads no clock, touches no I/O, and has no state beyond four integer
bounds set in `__init__`. Confirmed rejections, all present in code and all
covered by tests that exercise the **production** class (not a mock — the test
module imports `ProposalValidator` directly):

| Rule | Code | Tested |
|---|---|---|
| unknown action | `validator.py:71-73` | via schema |
| unknown mutation kind | `:101-102` | yes |
| unknown reference kind / selector | `:123-130` | yes |
| PATCH with no mutations | `:150-151` | yes |
| PATCH carrying a clarification | `:152-153` | — |
| CLARIFY without a question | `:164-166` | yes |
| CLARIFY with mutations | `:167-168` | yes |
| UNSUPPORTED not empty | `:169-171` | — |
| `rebuild` in PR5 | `:172-173` | — |
| too many mutations / references / mentions | `:87-92` | yes |
| duplicate mutation / reference kind | `:113-114`, `:136-137` | yes |
| `swap_direction` combined with anything | `:155-156` | yes |
| prior-result mutation without a bounded ref | `:157-163` | yes |
| invalid operation / comparison value | `:105-110` | — |
| invalid confidence | `:139-146` | yes |
| canonical ID token | `:68-69` | yes (2 cases) |
| output over 8192 chars | `:66-67` | yes |
| schema-invalid payload | `:176-179` | yes |

The A9 fix in `840471d` is real and complete for its stated purpose: the regex
gained `\b[0-9]{10,}\b` to catch bare numeric IDs such as `2010000039953`
alongside the existing prefixed forms, and a matching test case was added. Note
that this filter is defence-in-depth rather than the primary control — the
contract already leaves the model nowhere to place an identifier, since
`value` is enum-restricted for the two kinds that accept it and must be `null`
for the other two. A 9-digit identifier would slip the regex; it would still
have nowhere to go.

**The validator does not claim semantic correctness, and the documentation is
careful about this.** Section 13 of `semantic_repair_pr5.md` states that a zero
unsafe count "is **not measurable**, not 0%", which is exactly the right
distinction. I found no place where "validator accepted" is presented as
"semantically safe". The live data now proves the point empirically: for
Qwen3.8, the validator rejected **0 of 26** outputs while **14 of those 26 were
semantically unsafe**. Structural validation caught 0 % of semantic errors.

## 12. Context/privacy audit

`build_semantic_shadow_context` emits exactly four top-level keys:
`current_active_state`, `recent_semantic_turns`, `recent_addressable_results`,
`current_user_message`. Limits are enforced twice — by the `max_length=4`
pydantic constraint on the model and by the slicing in `context.py:29-31`.

I probed this empirically by committing eight turns with results, each carrying
`entity_id="BAL:2010000039953"` and `entity_id="geo:61-ROSTOV-SECRET"`, then
capturing the exact `messages` payload handed to the backend:

```
roles sent                     : ['system', 'user']
recent_semantic_turns          : 4  (cap 4)
recent_addressable_results     : 4  (cap 4)
BAL:2010000039953              : absent
geo:61-ROSTOV-SECRET           : absent
entity_id / intent_id          : absent
turn_id / session_id / revision: absent
SELECT / password              : absent
display_name "ГП ТГ Томск"     : present (intended)
tool/function surface in schema: none
```

`_intent_summary` (`context.py:69-136`) is an allow-list projection: it names
each field it copies and never spreads a `model_dump()`. Entity projection takes
`role`, `entity_type`, `display_name` and deliberately omits `entity_id`. The
suite's own canary fixture uses `BAL:secret`/`geo:secret` and
`test_contracts.py:42-52` asserts their absence.

One inherent limitation, recorded not as a defect but as a property of the
design: `frame.user_message` and `normalized_message` are passed verbatim, and
the canonical-ID regex applies only to the model's **output**, not to the
inbound context. If a user types an identifier into chat it reaches the model.
That is unavoidable for a system that must read the user's message, and the
proposal still cannot carry it back into anything authoritative.

Credentials stay inside the existing inference profile
(`backend.py:45-57` clones the configured profile and never logs it).

## 13. Feature-flag audit

Default is OFF and the OFF path costs nothing:

- `bootstrap._semantic_shadow` reads the env var named by
  `semantic_repair_shadow.enabled_env` (default `SEMANTIC_REPAIR_SHADOW_ENABLED`)
  and returns `None` immediately unless `_env_flag` matches
  `{"1","true","yes","on"}` case-insensitively (`bootstrap.py:178-186`,
  `:205-206`);
- `service.semantic_shadow is None` then makes `shadow_state` evaluate to
  `None` (service.py:200-204), so there is **no extra deep copy**;
- `_run_semantic_shadow` returns at its first line (service.py:429).

Zero extra state copies, zero model calls, zero behaviour change — confirmed by
both `test_bootstrap.py:6-9` and my baseline-vs-PR5-OFF differential being
byte-identical.

Backend-construction failure with the flag ON is handled: `bootstrap.py:186-197`
catches, logs, sets `backend = None`, and still returns a runner. I traced what
that runner does — `SemanticShadowRunner.run` hits `shadow.py:66-72` and returns
`SemanticShadowResult(eligible=True, invoked=False,
validation_status=UNAVAILABLE, validation_errors=["backend_unavailable"])`.
That is graceful and the diagnostics are honest: it reports "unavailable" rather
than pretending the model abstained. Good.

The one gap in this path is Finding 7: the prompt file is read in `__init__`,
outside that try/except, so a missing prompt file fails bootstrap outright.

## 14. Regression results

All runs with `C:\Users\alexs\miniforge3\envs\ai_env\python.exe`.

| Suite | Result | Doc claim | Match |
|---|---|---|---|
| Full pytest (PR5) | **535 passed, 1 warning** | 535, 1 warning | yes |
| Full pytest (baseline `f124040`) | **501 passed, 1 warning** | 501 | yes |
| `tests/semantic_repair` | **34 passed** | 34 | yes |
| `tests/test_period_patch.py` | **34 passed** | 34 | yes |
| `tests/test_geo_patch.py` | **103 passed** | 103 | yes |
| `tests/transitions` | **103 passed** | 103 | yes |
| `tests/test_golden_queries.py` | **14 passed** | 14 | yes |
| `tests/test_business_entity_patch.py` | **13 passed** | 79 | **no** |
| `tests/test_reducer.py` | **8 passed** | 20 | **no** |
| Golden `--validate-only` | **8/8** | — | yes |
| P0 strict dry-run (`--tag P0`) | **84/84 checks** | 84/84 | yes |
| PR3 transition dry-run | **20/20 checks** | 20/20 | yes |
| PR4 transition dry-run | **50/50 checks** | 50/50 | yes |
| Business scenarios dry-run | 121/121, coverage 100 % | — | — |

The single warning is the pre-existing Starlette/httpx deprecation.

The two mismatches are reporting discrepancies, not regressions — nothing fails
anywhere. I could not reproduce 79 or 20 by any selection I tried
(`-k business_entity` gives 89; `-k "reducer or store or persist"` gives 21;
`test_reducer.py + test_postgres_store.py` gives 11). Recommend correcting those
two rows in `semantic_repair_pr5.md` section 16 or stating the selector used.

## 15. Test-delta audit

Baseline 501 → PR5 535, delta +34.

Collected node IDs were diffed in both directions:

```
baseline nodes : 501
PR5 nodes      : 535
removed        : 0      (comm -23 produced no output)
added          : 34
```

All 34 additions are in the new package: `test_validator.py` 11,
`test_shadow.py` 10, `test_service_integration.py` 6, `test_contracts.py` 4,
`test_bootstrap.py` 2, `test_evaluation.py` 1.

Because PR4a's lesson was that a node-ID diff alone can hide a changed
expectation behind an unchanged parametrized ID, I did **not** stop at node IDs.
A recursive byte-level diff of the whole `tests/` tree between the extracted
`f124040` baseline and the PR5 worktree reports differences only in
`__pycache__/*.pyc` compilation artifacts. **Every pre-existing test `.py` file
is byte-identical.** The same diff over `src/` shows only `bootstrap.py` and
`service.py` differing.

That makes the stale-parametrized-ID class of defect structurally impossible
here: no pre-existing test file changed at all, so no expectation or parameter
value inside one could have changed.

## 16. Production differential

Harness: real `BalanceChatService`, real `InMemoryContextStore`, deterministic
stand-in processor exercising both the success and the clarification path over
five turns. Normalized only genuinely nondeterministic values — timestamps,
UUIDs (`intent_id`, `clarification_id`), `turn_id`, handles, `request_id`,
`session_id`, `resolved_plan_hash`, and `elapsed_ms`.

Compared: status, result, context view, session view, diagnostics, response key
set, committed state after every turn, revision after every turn, reloaded state,
and final revision.

| Comparison | Result |
|---|---|
| `f124040` vs PR5, shadow **OFF** | **identical — 0 differences** |
| `f124040` vs PR5, shadow **ON** (hostile shadow, 5 calls) | identical except my own `shadow_calls` counter |

Final revision 6 in all three runs. **No semantic mismatch in either
configuration.**

Note on why OFF is provably identical rather than merely observed identical: with
`semantic_shadow=None` the only surviving delta against baseline is that
`elapsed_ms` is computed at line 273 instead of inline at line 328 — a
sub-millisecond shift in a value that is nondeterministic anyway.

Not claimed: live DB-backed acceptance differential. The PR does not claim it
either, which is appropriately scoped.

## 17. Active-repair readiness verdict

**NOT READY.** Full reasoning, per-case forensics, and the mutation-kind matrix
that drives this are in `docs/semantic_repair_unsafe_analysis.md`.

Summary for this document: with every validator-accepted PATCH applied, Qwen3.8
would have produced 6 correct repairs and **14 unsafe state transitions** — 70 %
of applied patches wrong. The validator caught none of them. One mutation kind,
`swap_direction`, is clean at 5/5 with zero false positives across 21
non-direction runs, and is the only credible future candidate; its evidence base
of three distinct phrasings is too thin to activate on.

This verdict does not affect the architecture verdict. The shadow subsystem is
doing exactly the job it was built for: it produced the evidence that says
"do not activate", at zero risk to production. That is a successful experiment.

## 18. Final answers

**Q1 — Can a Qwen proposal affect current execution?**
**NO.** Zero references to any `semantic_repair` symbol exist in `processor.py`,
`transitions/*`, `execution.py`, `execution_adapter.py`, `planning.py`, or
`reducer.py`. No conversion function from any proposal type to any production
type exists anywhere in the repository. `_run_semantic_shadow` returns `None`
and its return value is discarded at service.py:274.

**Q2 — Can a proposal affect persisted semantic state?**
**NO.** No `semantic_repair` module calls any store method. The shadow receives a
state that has been deep-copied twice from a store `get()` that itself returns a
deep copy. A shadow that sets `metric = "CORRUPTED"`, `revision = 9999`, and
clears `conversation_window` on its snapshot changed nothing in the committed or
reloaded state.

**Q3 — Can a proposal affect session revision?**
**NO.** The revision is set by `store.commit` at service.py:215-223, which runs
before the shadow is invoked and is never called again in the turn. Differential
final revision was 6 with shadow OFF and 6 with a hostile shadow ON.

**Q4 — Can a proposal change user-visible clarification?**
**NO.** `response` is fully constructed at service.py:262-269 and cached at
:270-272, both before the shadow runs at :274. A shadow `CLARIFY` proposal is
never read back. `test_shadow_clarification_cannot_replace_production_clarification`
asserts the production question survives and `"Shadow question?" not in str(response)`.

**Q5 — Are Period/GEO/Business PATCHes shadowed?**
**NO.** Excluded by `DETERMINISTIC_PATCH_MODES` at `shadow.py:23-29`, verified at
unit, service, and corpus level; 3/3 controls skipped in both live runs.

**Q6 — Extra Qwen calls for deterministic PATCH?**
**0.** Asserted by `backend.calls == 0` in the parametrized service-level test
across all three modes.

**Q7 — Is the feature flag default OFF?**
**YES.** `_env_flag(os.getenv(...))` on an unset variable returns `False` and
`_semantic_shadow` returns `None`, producing zero state copies and zero model
calls.

**Q8 — Can the model provide authoritative canonical IDs?**
**NO.** Structurally the contract has nowhere to put one; additionally the
validator rejects prefixed and 10+ digit numeric ID tokens anywhere in the
serialized payload; and no consumer of a proposal exists to be authoritative for.

**Q9 — Can a proposal reach `ContextMutation` / `AnalysisIntent`?**
**NO.** Traced exhaustively. The only consumers of a `SemanticShadowResult` are
`log_event` in `_run_semantic_shadow` and the offline evaluator.

**Q10 — Did PR5 alter reducer / normalization / `ExecutionAdapter` semantics?**
**NO.** Byte-level diff of `src/` between `f124040` and `840471d` shows only
`bootstrap.py` and `service.py` changed, plus the new `semantic_repair/` package
and prompt.

**Q11 — Did the production differential reveal any semantic mismatch?**
**NO.** Byte-identical with shadow OFF; identical with a hostile shadow ON.

**Q12 — Architecture verdict?**
**APPROVE WITH REQUIRED FIXES.** No blockers, no high findings. Required before
the flag is enabled anywhere real: address Finding 1 (synchronous shadow on the
response path holding the turn reservation) and Finding 2 (commit the real
evaluation artifacts and correct the documented results). Findings 3–7 are
low-severity cleanups.
