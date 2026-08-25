from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ComparisonSpec,
    ContextMutation,
    FieldMutation,
    FormulaSpec,
    GroupingSpec,
    OperandEntityRef,
    IntentPatch,
    MutationAction,
    Operation,
    PeriodRef,
    RankingSpec,
    TransitionOutcome,
)
from balance_chat.execution import NativeExecutionResult
from balance_chat.planning import NativeMultiOperandPlanner
from balance_chat.processor import PipelineV2TurnProcessor
from balance_chat.execution_adapter import ReducerExecutionAdapter
from balance_chat.runtime_consistency import (
    CommittedReloadedConsistencyError,
    ConsistencyStatus,
    ExecutedCommittedConsistencyError,
    IntentExecutionConsistencyError,
    assert_committed_reloaded_consistent,
    assert_executed_committed_consistent,
    assert_intent_execution_consistent,
    compare_intent_execution_semantics,
)
from balance_chat.service import BalanceChatService, TurnProcessResult, TurnProcessingError
from balance_chat.store import InMemoryContextStore


def _period(month: int) -> PeriodRef:
    return PeriodRef(
        date_from=f"2025-{month:02d}-01",
        date_to=f"2025-{month + 1:02d}-01",
    )


def _entity(role: str, entity_type: str, entity_id: str) -> OperandEntityRef:
    return OperandEntityRef(
        role=role,
        entity=CanonicalEntityRef(
            entity_id=entity_id,
            entity_type=entity_type,
            display_name=entity_id,
        ),
    )


def _operand(
    operand_id: str = "supply",
    *,
    metric: str = "distribution",
    aggregate_type: str = "sum",
    entities: list[OperandEntityRef] | None = None,
) -> AnalysisOperand:
    return AnalysisOperand(
        operand_id=operand_id,
        metric=metric,
        aggregate_type=aggregate_type,
        entities=entities or [],
    )


def _simple_intent(*, entities: list[OperandEntityRef] | None = None) -> AnalysisIntent:
    return AnalysisIntent(
        operation=Operation.SHOW,
        operands=[_operand(entities=entities)],
        periods=[_period(5)],
        grain="total",
    )


def _positive_intents() -> list[object]:
    return [
        pytest.param(_simple_intent(), id="simple-show"),
        pytest.param(
            _simple_intent(
                entities=[_entity("destination", "geo_object", "geo:moscow")]
            ),
            id="geo-patch-effective-intent",
        ),
        pytest.param(
            _simple_intent(
                entities=[_entity("balance", "balance", "balance:42")]
            ),
            id="business-entity-patch-effective-intent",
        ),
        pytest.param(
            AnalysisIntent(
                operation=Operation.SHOW,
                operands=[
                    _operand(
                        entities=[
                            _entity("source", "organization", "org:source"),
                            _entity(
                                "destination", "organization", "org:destination"
                            ),
                        ]
                    )
                ],
                periods=[_period(5)],
            ),
            id="directed-source-destination",
        ),
        pytest.param(
            AnalysisIntent(
                operation=Operation.AGGREGATE,
                operands=[_operand(aggregate_type="avg")],
                periods=[PeriodRef(date_from="2025-01-01", date_to="2026-01-01")],
                grain="month",
            ),
            id="series-aggregation",
        ),
        pytest.param(
            AnalysisIntent(
                operation=Operation.COMPARE,
                operands=[_operand("supply"), _operand("needs", metric="own_needs")],
                periods=[_period(5)],
                comparison=ComparisonSpec(
                    baseline_operand_id="supply", target_operand_id="needs"
                ),
            ),
            id="valid-multi-task-comparison",
        ),
        pytest.param(
            AnalysisIntent(
                operation=Operation.COMPARE_PERIODS,
                operands=[_operand()],
                periods=[_period(4), _period(5)],
            ),
            id="period-patch-effective-intent",
        ),
        pytest.param(
            AnalysisIntent(
                operation=Operation.GROUP,
                operands=[_operand(aggregate_type="avg")],
                periods=[PeriodRef(date_from="2025-01-01", date_to="2026-01-01")],
                grouping=[GroupingSpec(dimension="period", aggregate_type="sum")],
                grain="month",
            ),
            id="period-grouping",
        ),
        pytest.param(
            AnalysisIntent(
                operation=Operation.CALCULATE,
                operands=[_operand("needs", metric="own_needs"), _operand("supply")],
                periods=[_period(5)],
                formula=FormulaSpec(
                    operator="percent_of",
                    numerator_operand_id="needs",
                    denominator_operand_id="supply",
                ),
            ),
            id="calculation",
        ),
        pytest.param(
            AnalysisIntent(
                operation=Operation.RANK,
                operands=[_operand()],
                periods=[PeriodRef(date_from="2025-01-01", date_to="2026-01-01")],
                ranking=RankingSpec(
                    direction="max", grain="month", bucket_aggregate="sum"
                ),
            ),
            id="ranking",
        ),
    ]


@pytest.mark.parametrize("intent", _positive_intents())
def test_valid_planner_decomposition_matches_intent(intent: AnalysisIntent) -> None:
    plan = NativeMultiOperandPlanner().plan(intent)

    comparison = compare_intent_execution_semantics(intent, plan)

    assert comparison.status == ConsistencyStatus.SUPPORTED_AND_EQUAL
    assert comparison.mismatches == ()
    assert_intent_execution_consistent(intent, plan)


def test_generated_plan_and_scalar_intent_ids_do_not_create_false_mismatch() -> None:
    intent = AnalysisIntent(
        operation=Operation.COMPARE_PERIODS,
        operands=[_operand()],
        periods=[_period(4), _period(5)],
    )
    plan = NativeMultiOperandPlanner().plan(intent)
    tasks = []
    task_id_map = {}
    for index, task in enumerate(plan.tasks):
        new_task_id = f"generated-{index}"
        task_id_map[task.task_id] = new_task_id
        scalar = task.scalar_intent.model_copy(
            update={"intent_id": f"generated-intent-{index}"}, deep=True
        )
        tasks.append(
            task.model_copy(
                update={"task_id": new_task_id, "scalar_intent": scalar}, deep=True
            )
        )
    comparison = plan.comparison.model_copy(
        update={
            "baseline_operand_id": task_id_map[plan.comparison.baseline_operand_id],
            "target_operand_id": task_id_map[plan.comparison.target_operand_id],
        },
        deep=True,
    )
    regenerated = plan.model_copy(
        update={
            "source_intent_id": "generated-source-intent",
            "tasks": tasks,
            "comparison": comparison,
        },
        deep=True,
    )

    assert_intent_execution_consistent(intent, regenerated)


def test_not_comparable_projection_fails_closed() -> None:
    intent = AnalysisIntent(
        operation=Operation.GROUP,
        operands=[_operand()],
        periods=[_period(5)],
        grouping=[GroupingSpec(dimension="geo")],
    )
    source_plan = NativeMultiOperandPlanner().plan(_simple_intent())
    plan = source_plan.model_copy(update={"operation": Operation.GROUP}, deep=True)

    comparison = compare_intent_execution_semantics(intent, plan)

    assert comparison.status == ConsistencyStatus.NOT_COMPARABLE
    with pytest.raises(IntentExecutionConsistencyError) as captured:
        assert_intent_execution_consistent(intent, plan)
    assert captured.value.dimensions == ("projection_support",)


def _replace_task(plan, index, **updates):
    tasks = list(plan.tasks)
    tasks[index] = tasks[index].model_copy(update=updates, deep=True)
    return plan.model_copy(update={"tasks": tasks}, deep=True)


def _replace_scalar_operand(plan, index=0, **updates):
    task = plan.tasks[index]
    scalar = task.scalar_intent
    operand = scalar.operands[0].model_copy(update=updates, deep=True)
    scalar = scalar.model_copy(update={"operands": [operand]}, deep=True)
    return _replace_task(plan, index, scalar_intent=scalar)


def _replace_scalar_period(plan, period: PeriodRef, index=0):
    task = plan.tasks[index]
    scalar = task.scalar_intent.model_copy(update={"periods": [period]}, deep=True)
    return _replace_task(plan, index, scalar_intent=scalar)


def _assert_dimension(intent, hostile_plan, dimension: str) -> None:
    comparison = compare_intent_execution_semantics(intent, hostile_plan)
    assert comparison.status == ConsistencyStatus.SUPPORTED_AND_MISMATCH
    assert dimension in {item.dimension for item in comparison.mismatches}
    with pytest.raises(IntentExecutionConsistencyError) as captured:
        assert_intent_execution_consistent(intent, hostile_plan)
    assert dimension in captured.value.dimensions


def test_hostile_period_mismatch_is_detected() -> None:
    intent = _simple_intent()
    plan = NativeMultiOperandPlanner().plan(intent)
    _assert_dimension(intent, _replace_scalar_period(plan, _period(6)), "period")


def test_hostile_metric_mismatch_is_detected() -> None:
    intent = _simple_intent()
    plan = NativeMultiOperandPlanner().plan(intent)
    _assert_dimension(intent, _replace_scalar_operand(plan, metric="own_needs"), "metric")


def test_hostile_geo_mismatch_is_detected() -> None:
    intent = _simple_intent(
        entities=[_entity("destination", "geo_object", "geo:moscow")]
    )
    plan = NativeMultiOperandPlanner().plan(intent)
    changed = [_entity("destination", "geo_object", "geo:samara")]
    _assert_dimension(intent, _replace_scalar_operand(plan, entities=changed), "geo")


def test_hostile_business_entity_mismatch_is_detected() -> None:
    intent = _simple_intent(entities=[_entity("balance", "balance", "balance:42")])
    plan = NativeMultiOperandPlanner().plan(intent)
    changed = [_entity("balance", "balance", "balance:99")]
    _assert_dimension(
        intent, _replace_scalar_operand(plan, entities=changed), "business_entity"
    )


def test_hostile_source_destination_inversion_is_detected() -> None:
    intent = _simple_intent(
        entities=[
            _entity("source", "organization", "org:source"),
            _entity("destination", "organization", "org:destination"),
        ]
    )
    plan = NativeMultiOperandPlanner().plan(intent)
    inverted = [
        _entity("source", "organization", "org:destination"),
        _entity("destination", "organization", "org:source"),
    ]
    _assert_dimension(intent, _replace_scalar_operand(plan, entities=inverted), "direction")


def test_hostile_overall_operation_mismatch_is_detected() -> None:
    intent = _simple_intent()
    plan = NativeMultiOperandPlanner().plan(intent)
    _assert_dimension(
        intent,
        plan.model_copy(update={"operation": Operation.AGGREGATE}, deep=True),
        "operation",
    )


def test_hostile_task_operation_mismatch_is_detected() -> None:
    intent = _simple_intent()
    plan = NativeMultiOperandPlanner().plan(intent)
    scalar = plan.tasks[0].scalar_intent.model_copy(
        update={"operation": Operation.AGGREGATE}, deep=True
    )
    _assert_dimension(intent, _replace_task(plan, 0, scalar_intent=scalar), "operation")


def test_hostile_unit_mismatch_is_detected() -> None:
    intent = _simple_intent()
    plan = NativeMultiOperandPlanner().plan(intent)
    _assert_dimension(intent, _replace_scalar_operand(plan, unit="м3"), "unit")


def test_hostile_aggregation_mismatch_is_detected() -> None:
    intent = AnalysisIntent(
        operation=Operation.AGGREGATE,
        operands=[_operand(aggregate_type="avg")],
        periods=[PeriodRef(date_from="2025-01-01", date_to="2026-01-01")],
        grain="month",
    )
    plan = NativeMultiOperandPlanner().plan(intent)
    _assert_dimension(
        intent, _replace_task(plan, 0, series_reduce="max"), "aggregation"
    )


def test_hostile_grouping_mismatch_is_detected() -> None:
    intent = AnalysisIntent(
        operation=Operation.GROUP,
        operands=[_operand()],
        periods=[PeriodRef(date_from="2025-01-01", date_to="2026-01-01")],
        grouping=[GroupingSpec(dimension="period", aggregate_type="sum")],
        grain="month",
    )
    plan = NativeMultiOperandPlanner().plan(intent)
    _assert_dimension(
        intent, _replace_task(plan, 0, bucket_aggregate="avg"), "grouping"
    )


def test_hostile_comparison_direction_mismatch_is_detected() -> None:
    intent = AnalysisIntent(
        operation=Operation.COMPARE,
        operands=[_operand("supply"), _operand("needs", metric="own_needs")],
        periods=[_period(5)],
        comparison=ComparisonSpec(
            baseline_operand_id="supply", target_operand_id="needs"
        ),
    )
    plan = NativeMultiOperandPlanner().plan(intent)
    hostile = plan.model_copy(
        update={
            "comparison": ComparisonSpec(
                baseline_operand_id="needs", target_operand_id="supply"
            )
        },
        deep=True,
    )
    _assert_dimension(intent, hostile, "comparison")


def test_hostile_formula_mismatch_is_detected() -> None:
    intent = AnalysisIntent(
        operation=Operation.CALCULATE,
        operands=[_operand("needs", metric="own_needs"), _operand("supply")],
        periods=[_period(5)],
        formula=FormulaSpec(
            operator="percent_of",
            numerator_operand_id="needs",
            denominator_operand_id="supply",
        ),
    )
    plan = NativeMultiOperandPlanner().plan(intent)
    hostile = plan.model_copy(
        update={
            "formula": plan.formula.model_copy(update={"operator": "ratio"}, deep=True)
        },
        deep=True,
    )
    _assert_dimension(intent, hostile, "formula")


def test_hostile_ranking_mismatch_is_detected() -> None:
    intent = AnalysisIntent(
        operation=Operation.RANK,
        operands=[_operand()],
        periods=[PeriodRef(date_from="2025-01-01", date_to="2026-01-01")],
        ranking=RankingSpec(direction="max", grain="month", bucket_aggregate="sum"),
    )
    plan = NativeMultiOperandPlanner().plan(intent)
    hostile = plan.model_copy(
        update={
            "ranking": plan.ranking.model_copy(update={"direction": "min"}, deep=True)
        },
        deep=True,
    )
    _assert_dimension(intent, hostile, "ranking")


class _HostilePlanner:
    def plan(self, intent):
        plan = NativeMultiOperandPlanner().plan(intent)
        return _replace_scalar_period(plan, _period(6))


class _MustNotExecute:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("inconsistent plan reached execution")


def test_processor_fails_closed_before_analytical_execution() -> None:
    executor = _MustNotExecute()
    processor = PipelineV2TurnProcessor(
        runtime=SimpleNamespace(),
        registry=SimpleNamespace(),
        interpreter=object(),
        compiler=object(),
        planner=_HostilePlanner(),
        executor=executor,
    )
    state = InMemoryContextStore().create("session")
    mutation = ContextMutation(
        turn_id="turn-1", user_message="may", replace_intent=_simple_intent()
    )

    with pytest.raises(TurnProcessingError) as captured:
        processor._dispatch_mutation(
            state,
            mutation,
            normalized_message="may",
            execute_db=False,
            request_id="request-1",
            started=0,
            interpretation_mode="deterministic",
        )

    assert captured.value.code == "intent_execution_mismatch"
    assert isinstance(captured.value.__cause__, IntentExecutionConsistencyError)
    assert executor.calls == 0


def _committed_state(intent: AnalysisIntent, *, session_id: str = "session"):
    store = InMemoryContextStore()
    store.create(session_id)
    state = store.commit(
        session_id,
        0,
        ContextMutation(
            turn_id="turn-1", user_message="may", replace_intent=intent
        ),
        TransitionOutcome.SUCCESS,
    )
    return store, state


def _replace_scope_intent(state, field: str, intent: AnalysisIntent):
    scope = getattr(state, field)
    return state.model_copy(
        update={field: scope.model_copy(update={"intent": intent}, deep=True)},
        deep=True,
    )


def test_executed_committed_period_corruption_is_detected() -> None:
    intent = _simple_intent()
    _, committed = _committed_state(intent)
    corrupted = intent.model_copy(update={"periods": [_period(6)]}, deep=True)
    committed = _replace_scope_intent(committed, "last_attempted_scope", corrupted)

    with pytest.raises(ExecutedCommittedConsistencyError) as captured:
        assert_executed_committed_consistent(
            intent, committed, outcome=TransitionOutcome.SUCCESS
        )

    assert captured.value.dimension == "last_attempted_intent.periods"


def test_executed_committed_geo_corruption_is_detected() -> None:
    intent = _simple_intent(
        entities=[_entity("destination", "geo_object", "geo:moscow")]
    )
    _, committed = _committed_state(intent)
    corrupted = intent.model_copy(
        update={
            "operands": [
                intent.operands[0].model_copy(
                    update={
                        "entities": [
                            _entity("destination", "geo_object", "geo:samara")
                        ]
                    },
                    deep=True,
                )
            ]
        },
        deep=True,
    )
    committed = _replace_scope_intent(committed, "last_attempted_scope", corrupted)

    with pytest.raises(ExecutedCommittedConsistencyError):
        assert_executed_committed_consistent(
            intent, committed, outcome=TransitionOutcome.SUCCESS
        )


def test_committed_reloaded_stale_revision_is_detected() -> None:
    intent = _simple_intent()
    store, committed = _committed_state(intent)
    stale = store.create("other").model_copy(update={"session_id": "session"}, deep=True)

    with pytest.raises(CommittedReloadedConsistencyError) as captured:
        assert_committed_reloaded_consistent(committed, stale)

    assert captured.value.dimension == "revision"


def test_committed_reloaded_business_operand_corruption_is_detected() -> None:
    intent = _simple_intent(entities=[_entity("balance", "balance", "balance:42")])
    _, committed = _committed_state(intent)
    corrupted = intent.model_copy(
        update={
            "operands": [
                intent.operands[0].model_copy(
                    update={
                        "entities": [_entity("balance", "balance", "balance:99")]
                    },
                    deep=True,
                )
            ]
        },
        deep=True,
    )
    reloaded = _replace_scope_intent(committed, "active_dialog_scope", corrupted)

    with pytest.raises(CommittedReloadedConsistencyError) as captured:
        assert_committed_reloaded_consistent(committed, reloaded)

    assert captured.value.dimension == "active_intent.operands"


def test_state_consistency_ignores_only_infrastructure_timestamps() -> None:
    intent = _simple_intent()
    _, committed = _committed_state(intent)
    reloaded = committed.model_copy(
        update={"updated_at": committed.updated_at + timedelta(seconds=1)}, deep=True
    )
    reloaded.active_dialog_scope.updated_at += timedelta(seconds=2)
    reloaded.last_attempted_scope.updated_at += timedelta(seconds=3)

    assert_executed_committed_consistent(
        intent, committed, outcome=TransitionOutcome.SUCCESS
    )
    assert_committed_reloaded_consistent(committed, reloaded)


class _ExecutedIntentProcessor:
    def __init__(self, intent: AnalysisIntent) -> None:
        self.intent = intent
        self.calls = 0

    def process(self, _state, *, message, **_kwargs):
        self.calls += 1
        return TurnProcessResult(
            mutation=ContextMutation(
                turn_id=f"turn-{self.calls}",
                user_message=message,
                replace_intent=self.intent,
            ),
            outcome=TransitionOutcome.SUCCESS,
            executed_intent=self.intent,
            response={"status": "ok"},
        )


class _LifecycleStore(InMemoryContextStore):
    def __init__(self, *, corrupt_commit=False, stale_reload=False) -> None:
        super().__init__()
        self.corrupt_commit = corrupt_commit
        self.stale_reload = stale_reload
        self.commit_calls = 0
        self.release_calls = 0
        self.cache_writes = 0
        self._committed = False
        self._stale_state = None

    def get(self, session_id):
        if self.stale_reload and self._committed:
            return self._stale_state.model_copy(deep=True)
        return super().get(session_id)

    def commit(self, *args, **kwargs):
        self.commit_calls += 1
        self._stale_state = InMemoryContextStore.get(self, args[0])
        committed = super().commit(*args, **kwargs)
        self._committed = True
        if not self.corrupt_commit:
            return committed
        intent = committed.last_attempted_scope.intent.model_copy(
            update={"periods": [_period(6)]}, deep=True
        )
        return _replace_scope_intent(
            committed, "last_attempted_scope", intent
        )

    def release(self, *args, **kwargs):
        self.release_calls += 1
        return super().release(*args, **kwargs)

    def save_request_result(self, *args, **kwargs):
        self.cache_writes += 1
        return super().save_request_result(*args, **kwargs)


def test_post_commit_mismatch_does_not_double_commit_cache_or_leak_reservation() -> None:
    intent = _simple_intent()
    store = _LifecycleStore(corrupt_commit=True)
    state = store.create("session")
    service = BalanceChatService(store, _ExecutedIntentProcessor(intent))

    with pytest.raises(ExecutedCommittedConsistencyError):
        service.execute_turn(
            state.session_id,
            expected_revision=0,
            message="may",
            request_id="request-1",
        )

    assert InMemoryContextStore.get(store, "session").revision == 1
    assert store.commit_calls == 1
    assert store.cache_writes == 0
    assert store.release_calls == 1
    assert store._reservations == {}


def test_stale_reload_fails_after_one_commit_without_caching() -> None:
    intent = _simple_intent()
    store = _LifecycleStore(stale_reload=True)
    state = store.create("session")
    service = BalanceChatService(store, _ExecutedIntentProcessor(intent))

    with pytest.raises(CommittedReloadedConsistencyError):
        service.execute_turn(
            state.session_id,
            expected_revision=0,
            message="may",
            request_id="request-1",
        )

    assert InMemoryContextStore.get(store, "session").revision == 1
    assert store.commit_calls == 1
    assert store.cache_writes == 0
    assert store.release_calls == 1


def test_normal_runtime_check_preserves_request_id_cache_behavior() -> None:
    intent = _simple_intent()
    store = _LifecycleStore()
    state = store.create("session")
    processor = _ExecutedIntentProcessor(intent)
    service = BalanceChatService(store, processor)

    first = service.execute_turn(
        state.session_id,
        expected_revision=0,
        message="may",
        request_id="request-1",
    )
    replay = service.execute_turn(
        state.session_id,
        expected_revision=0,
        message="may",
        request_id="request-1",
    )

    assert replay == first
    assert processor.calls == 1
    assert store.commit_calls == 1
    assert store.cache_writes == 1
    assert store.release_calls == 1


class _PatchProcessor:
    def __init__(self, state, mutation) -> None:
        self.mutation = mutation
        self.executed_intent = ReducerExecutionAdapter().effective_intent(
            state, mutation
        )

    def process(self, *_args, **_kwargs):
        return TurnProcessResult(
            mutation=self.mutation,
            outcome=TransitionOutcome.SUCCESS,
            executed_intent=self.executed_intent,
            response={"status": "ok"},
        )


def test_period_patch_preserves_unrelated_dimensions_through_commit_and_reload() -> None:
    initial = _simple_intent(
        entities=[
            _entity("balance", "balance", "balance:42"),
            _entity("destination", "geo_object", "geo:moscow"),
        ]
    )
    store, state = _committed_state(initial)
    mutation = ContextMutation(
        turn_id="turn-2",
        user_message="june",
        patch=IntentPatch(
            periods=FieldMutation(
                action=MutationAction.SET,
                value=[_period(6)],
            )
        ),
    )
    processor = _PatchProcessor(state, mutation)
    service = BalanceChatService(store, processor)

    service.execute_turn(
        state.session_id,
        expected_revision=state.revision,
        message="june",
        request_id="request-patch",
    )
    reloaded = store.get(state.session_id)

    assert reloaded.active_dialog_scope.intent == processor.executed_intent
    assert reloaded.active_dialog_scope.intent.periods == [_period(6)]
    assert reloaded.active_dialog_scope.intent.operands == initial.operands
