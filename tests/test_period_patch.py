from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ContextContractV2,
    ContextMutation,
    IntentPatch,
    MutationAction,
    OperandEntityRef,
    Operation,
    PeriodRef,
    TransitionOutcome,
)
from balance_chat.execution import NativeExecutionResult, ScalarFact, TaskExecutionResult
from balance_chat.processor import (
    PipelineV2TurnProcessor,
    _detect_period_followup,
    _deterministic_period_patch,
)
from balance_chat.planning import NativeMultiOperandPlanner
from balance_chat.reducer import apply_context_transition, reduce_intent
from balance_chat.store import InMemoryContextStore


def _intent(
    *,
    operation: Operation = Operation.SHOW,
    metric: str = "distribution",
    month: int = 5,
    year: int = 2025,
    entities: list[OperandEntityRef] | None = None,
) -> AnalysisIntent:
    next_month = month % 12 + 1
    next_year = year + 1 if month == 12 else year
    return AnalysisIntent(
        operation=operation,
        operands=[
            AnalysisOperand(
                operand_id="metric",
                metric=metric,
                aggregate_type="sum",
                entities=entities or [],
            )
        ],
        periods=[
            PeriodRef(
                date_from=f"{year:04d}-{month:02d}-01",
                date_to=f"{next_year:04d}-{next_month:02d}-01",
            )
        ],
        grain="total",
    )


def _state(intent: AnalysisIntent | None = None) -> ContextContractV2:
    state = ContextContractV2(session_id="session")
    if intent is None:
        return state
    return apply_context_transition(
        state,
        ContextMutation(
            turn_id="turn-1",
            user_message="initial",
            replace_intent=intent,
        ),
        TransitionOutcome.SUCCESS,
    )


@pytest.mark.parametrize(
    ("message", "date_from", "date_to"),
    [
        ("А за апрель?", "2025-04-01", "2025-05-01"),
        ("А за март?", "2025-03-01", "2025-04-01"),
        ("А за январь 2025?", "2025-01-01", "2025-02-01"),
        ("А за апрель 2024?", "2024-04-01", "2024-05-01"),
        ("За февраль?", "2025-02-01", "2025-03-01"),
        ("Покажи за июнь", "2025-06-01", "2025-07-01"),
        ("А за декабрь?", "2025-12-01", "2026-01-01"),
        ("А за 1 апреля 2025?", "2025-04-01", "2025-04-02"),
    ],
)
def test_period_followup_resolves_canonical_period(
    message: str,
    date_from: str,
    date_to: str,
) -> None:
    candidate = _detect_period_followup(message, _intent())

    assert candidate is not None
    assert candidate.periods == (
        PeriodRef(date_from=date_from, date_to=date_to),
    )


@pytest.mark.parametrize(
    ("operation", "metric", "entity_type"),
    [
        (Operation.SHOW, "distribution", "geo_object"),
        (Operation.SHOW, "export", "balance"),
        (Operation.AGGREGATE, "storage_injection", "article"),
        (Operation.AGGREGATE, "incoming", "route"),
    ],
)
def test_period_patch_preserves_every_non_period_field(
    operation: Operation,
    metric: str,
    entity_type: str,
) -> None:
    role = {
        "geo_object": "destination",
        "balance": "balance",
        "article": "article",
        "route": "route",
    }[entity_type]
    active = _intent(
        operation=operation,
        metric=metric,
        entities=[
            OperandEntityRef(
                role=role,
                entity=CanonicalEntityRef(
                    entity_id=f"{entity_type}:1",
                    entity_type=entity_type,
                    display_name=f"Context {entity_type}",
                ),
            )
        ],
    )
    state = _state(active)

    mutation = _deterministic_period_patch(state, "А за апрель?", "turn-2")

    assert mutation is not None
    assert mutation.replace_intent is None
    assert mutation.patch.periods.action == MutationAction.SET
    assert mutation.patch.periods.value == [
        PeriodRef(date_from="2025-04-01", date_to="2025-05-01")
    ]
    assert mutation.patch.model_copy(update={"periods": None}) == IntentPatch()
    effective = reduce_intent(state, mutation)
    serialized_mutation = ContextMutation.model_validate(
        mutation.model_dump(mode="json")
    )
    assert reduce_intent(state, serialized_mutation) == effective
    assert effective.model_copy(update={"periods": active.periods}, deep=True) == active
    assert effective.periods == [
        PeriodRef(date_from="2025-04-01", date_to="2025-05-01")
    ]


@pytest.mark.parametrize(
    "message",
    [
        "Сравни с апрелем",
        "А по Москве за апрель?",
        "Покажи максимум за апрель",
        "Почему в апреле меньше?",
        "А апрель и май?",
        "За какой апрель?",
        "Покажи апрель по месяцам",
        "А за прошлый месяц?",
    ],
)
def test_ambiguous_or_mixed_followup_is_not_intercepted(message: str) -> None:
    assert _detect_period_followup(message, _intent()) is None


@pytest.mark.parametrize(
    "operation",
    [
        Operation.COMPARE,
        Operation.COMPARE_PERIODS,
        Operation.CALCULATE,
        Operation.RANK,
        Operation.GROUP,
        Operation.MULTI_STEP,
    ],
)
def test_complex_active_operation_is_not_intercepted(operation: Operation) -> None:
    active = _intent()
    active = active.model_copy(update={"operation": operation}, deep=True)

    assert _detect_period_followup("А за апрель?", active) is None


def test_period_patch_requires_active_analytical_state() -> None:
    assert _deterministic_period_patch(
        _state(), "А за апрель?", "turn-1"
    ) is None


class _CountingInterpreter:
    def __init__(self) -> None:
        self.calls = 0

    def interpret(self, **_kwargs):
        self.calls += 1
        raise AssertionError("contextual interpreter was invoked")


class _SuccessfulExecutor:
    def __init__(self) -> None:
        self.plan = None

    def execute(self, plan, **_kwargs):
        self.plan = plan
        task = plan.tasks[0]
        return NativeExecutionResult(
            operation=plan.operation,
            status="ok",
            task_results=[
                TaskExecutionResult(
                    task_id=task.task_id,
                    status="ok",
                    fact=ScalarFact(
                        task_id=task.task_id,
                        value=Decimal("12.5"),
                        unit="тыс. м3",
                        label="Распределение",
                    ),
                    envelope={"status": "ok"},
                )
            ],
        )


class _CapturingPlanner:
    def __init__(self) -> None:
        self.intent = None

    def plan(self, intent):
        self.intent = intent
        return NativeMultiOperandPlanner().plan(intent)


class _NoInterpretationPolicy:
    def should_invoke(self, *_args, **_kwargs):
        return False


class _GoldenRuntime:
    def __init__(self) -> None:
        self.calls = 0
        self.summary_calls = 0

    def _import_pipeline_module(self, _name):
        raise ImportError

    def execute_raw(self, _message, **_kwargs):
        self.calls += 1
        return {
            "status": "ok",
            "unit": "тыс. м3",
            "rows": [{"fact_value": "12.5", "unit": "тыс. м3"}],
            "debug": {
                "resolved_plan": {
                    "_intent": {
                        "intent": "show",
                        "metric": "distribution",
                        "date_from": "2025-05-01",
                        "date_to": "2025-06-01",
                    },
                    "expressions": [
                        {
                            "canonical_metric": "distribution",
                            "balance": {"id": "balance:1", "label": "Баланс"},
                            "article": {
                                "id": "article:1",
                                "label": "Распределение",
                            },
                            "geo": [
                                {
                                    "id": "geo:rostov",
                                    "label": "Ростовская область",
                                }
                            ],
                        }
                    ],
                }
            },
        }

    def summarize_envelope(self, envelope, **_kwargs):
        self.summary_calls += 1
        return envelope


def _processor(runtime, interpreter, executor, *, planner=None):
    registry = SimpleNamespace(
        geo_objects=(),
        geo_groups=(),
        routes=(),
        manifest=SimpleNamespace(bundle_version="test"),
    )
    return PipelineV2TurnProcessor(
        runtime=runtime,
        registry=registry,
        interpreter=interpreter,
        compiler=object(),
        executor=executor,
        planner=planner,
        policy=_NoInterpretationPolicy(),
    )


def test_golden_period_transition_uses_patch_without_llm_and_persists() -> None:
    store = InMemoryContextStore()
    initial_state = store.create("session")
    runtime = _GoldenRuntime()
    interpreter = _CountingInterpreter()
    executor = _SuccessfulExecutor()
    planner = _CapturingPlanner()
    processor = _processor(runtime, interpreter, executor, planner=planner)

    initial = processor.process(
        initial_state,
        message="Покажи распределение газа в Ростовскую область за май 2025",
        execute_db=False,
        clarification=None,
        request_id="initial",
    )
    may_state = store.commit(
        "session",
        0,
        initial.mutation,
        initial.outcome,
        result=initial.result_reference,
    )
    may_intent = may_state.active_dialog_scope.intent

    followup = processor.process(
        may_state,
        message="А за апрель?",
        execute_db=True,
        clarification=None,
        request_id="followup",
    )
    executed_intent = planner.intent
    committed = store.commit(
        "session",
        may_state.revision,
        followup.mutation,
        followup.outcome,
        result=followup.result_reference,
    )
    reloaded = store.get("session")

    assert runtime.calls == 1
    assert runtime.summary_calls == 0
    assert interpreter.calls == 0
    assert followup.mutation.replace_intent is None
    assert followup.mutation.patch.periods.action == MutationAction.SET
    assert followup.diagnostics["interpretation"] == {
        "mode": "deterministic_period_patch",
        "source": "deterministic",
        "confidence": 1.0,
    }
    assert executed_intent.periods == [
        PeriodRef(date_from="2025-04-01", date_to="2025-05-01")
    ]
    assert committed.active_dialog_scope.intent == reloaded.active_dialog_scope.intent
    assert committed.active_dialog_scope.intent == reduce_intent(may_state, followup.mutation)
    assert committed.active_dialog_scope.intent == executed_intent
    assert committed.active_dialog_scope.intent.model_copy(
        update={"periods": may_intent.periods}, deep=True
    ) == may_intent
    assert committed.revision == 2


def test_ambiguous_followup_still_uses_contextual_interpreter() -> None:
    interpreter = _CountingInterpreter()
    processor = _processor(_GoldenRuntime(), interpreter, _SuccessfulExecutor())

    with pytest.raises(AssertionError, match="contextual interpreter was invoked"):
        processor.process(
            _state(_intent()),
            message="Сравни с апрелем",
            execute_db=False,
            clarification=None,
            request_id="ambiguous",
        )

    assert interpreter.calls == 1
