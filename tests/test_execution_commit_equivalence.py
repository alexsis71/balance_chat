from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    ContextMutation,
    FieldMutation,
    GroupingSpec,
    IntentPatch,
    MutationAction,
    Operation,
    PeriodRef,
    TransitionOutcome,
)
from balance_chat.execution import NativeExecutionResult, ScalarFact, TaskExecutionResult
from balance_chat.execution_adapter import ReducerExecutionAdapter
from balance_chat.planning import NativeMultiOperandPlanner
from balance_chat.processor import PipelineV2TurnProcessor
from balance_chat.service import TurnProcessingError
from balance_chat.store import InMemoryContextStore


def _intent() -> AnalysisIntent:
    return AnalysisIntent(
        operation=Operation.SHOW,
        operands=[AnalysisOperand(operand_id="supply", metric="distribution")],
        periods=[PeriodRef(date_from="2025-06-01", date_to="2025-07-01")],
        grain="total",
    )


class _CapturingAdapter(ReducerExecutionAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def effective_intent(self, state, mutation):
        self.calls += 1
        return super().effective_intent(state, mutation)


class _CapturingPlanner:
    def __init__(self) -> None:
        self.intent = None
        self.output_plan = None

    def plan(self, intent):
        self.intent = intent
        self.output_plan = NativeMultiOperandPlanner().plan(intent)
        return self.output_plan


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
                        label="Поставки",
                    ),
                    envelope={"status": "ok"},
                )
            ],
        )


def _processor(*, adapter=None, planner=None, executor=None):
    return PipelineV2TurnProcessor(
        runtime=SimpleNamespace(),
        registry=SimpleNamespace(),
        interpreter=object(),
        compiler=object(),
        executor=executor or _SuccessfulExecutor(),
        planner=planner,
        execution_adapter=adapter,
    )


def test_replace_only_dispatch_preserves_intent_and_original_mutation() -> None:
    state = InMemoryContextStore().create("session-1")
    intent = _intent()
    mutation = ContextMutation(
        turn_id="turn-1",
        user_message="june",
        replace_intent=intent,
    )
    adapter = _CapturingAdapter()
    planner = _CapturingPlanner()
    processor = _processor(adapter=adapter, planner=planner)

    processed = processor._dispatch_mutation(
        state,
        mutation,
        normalized_message="june",
        execute_db=False,
        request_id="request-1",
        started=0,
        interpretation_mode="deterministic",
    )

    assert adapter.calls == 1
    assert planner.intent == intent
    assert processed.response["operation"] == Operation.SHOW.value
    assert processed.mutation == mutation


def test_patch_only_execution_and_commit_use_the_same_effective_intent() -> None:
    store = InMemoryContextStore()
    store.create("session-1")
    store.commit(
        "session-1",
        0,
        ContextMutation(
            turn_id="turn-1",
            user_message="june",
            replace_intent=_intent(),
        ),
        TransitionOutcome.SUCCESS,
    )
    state = store.get("session-1")
    periods = [PeriodRef(date_from="2025-07-01", date_to="2025-08-01")]
    mutation = ContextMutation(
        turn_id="turn-2",
        user_message="july",
        patch=IntentPatch(
            periods=FieldMutation(action=MutationAction.SET, value=periods)
        ),
    )
    adapter = _CapturingAdapter()
    planner = _CapturingPlanner()
    executor = _SuccessfulExecutor()
    processor = _processor(adapter=adapter, planner=planner, executor=executor)
    effective_before_execution = adapter.effective_intent(state, mutation)
    adapter.calls = 0

    processed = processor._dispatch_mutation(
        state,
        mutation,
        normalized_message="july",
        execute_db=False,
        request_id="request-2",
        started=0,
        interpretation_mode="mutation",
    )
    committed = store.commit(
        "session-1",
        state.revision,
        processed.mutation,
        processed.outcome,
        result=processed.result_reference,
    )
    reloaded = store.get("session-1")

    assert adapter.calls == 1
    assert planner.intent == effective_before_execution
    assert executor.plan == planner.output_plan
    assert processed.mutation == mutation
    assert processed.mutation.replace_intent is None
    assert committed.active_dialog_scope.intent == effective_before_execution
    assert reloaded.active_dialog_scope.intent == effective_before_execution


def test_patch_only_grouping_routes_from_the_effective_intent() -> None:
    store = InMemoryContextStore()
    store.create("session-1")
    state = store.commit(
        "session-1",
        0,
        ContextMutation(
            turn_id="turn-1",
            user_message="june",
            replace_intent=_intent(),
        ),
        TransitionOutcome.SUCCESS,
    )
    mutation = ContextMutation(
        turn_id="turn-2",
        user_message="group",
        patch=IntentPatch(
            operation=FieldMutation(action=MutationAction.SET, value=Operation.GROUP),
            grouping=FieldMutation(
                action=MutationAction.SET,
                value=[GroupingSpec(dimension="geo_group")],
            ),
        ),
    )
    processor = _processor()
    calls = []
    processor._execute_grouping_mutation = (
        lambda _state, original, **kwargs: calls.append(
            (original, kwargs["effective_intent"])
        )
    )
    processor._execute_mutation = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("grouping mutation reached scalar execution")
    )

    processor._dispatch_mutation(
        state,
        mutation,
        normalized_message="group",
        execute_db=False,
        request_id="request-2",
        started=0,
        interpretation_mode="mutation",
    )

    assert calls[0][0] == mutation
    assert calls[0][1].operation == Operation.GROUP
    assert calls[0][1].grouping == [GroupingSpec(dimension="geo_group")]


def test_normalization_fails_closed_if_patch_would_change_committed_intent() -> None:
    store = InMemoryContextStore()
    store.create("session-1")
    state = store.commit(
        "session-1",
        0,
        ContextMutation(
            turn_id="turn-1",
            user_message="june",
            replace_intent=_intent(),
        ),
        TransitionOutcome.SUCCESS,
    )
    mutation = ContextMutation(
        turn_id="turn-2",
        user_message="july",
        patch=IntentPatch(
            periods=FieldMutation(
                action=MutationAction.SET,
                value=[PeriodRef(date_from="2025-07-01", date_to="2025-08-01")],
            )
        ),
    )
    normalized = _intent().model_copy(
        update={
            "periods": [PeriodRef(date_from="2025-08-01", date_to="2025-09-01")]
        },
        deep=True,
    )

    with pytest.raises(
        TurnProcessingError,
        match="executed effective intent would differ from committed intent",
    ) as exc_info:
        _processor()._synchronize_effective_intent(state, mutation, normalized)
    assert exc_info.value.code == "context_reduction_failed"
