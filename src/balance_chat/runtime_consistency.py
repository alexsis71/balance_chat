from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .contracts import AnalysisIntent, AnalysisOperand, ContextContractV2, Operation
from .domain import metric_definition
from .planning import NativeExecutionPlan


class ConsistencyStatus(StrEnum):
    SUPPORTED_AND_EQUAL = "supported_and_equal"
    SUPPORTED_AND_MISMATCH = "supported_and_mismatch"
    NOT_COMPARABLE = "not_comparable"


@dataclass(frozen=True)
class SemanticMismatch:
    dimension: str
    expected: Any
    actual: Any


@dataclass(frozen=True)
class IntentSemanticProjection:
    dimensions: tuple[tuple[str, Any], ...]

    def values(self) -> dict[str, Any]:
        return dict(self.dimensions)


@dataclass(frozen=True)
class ConsistencyComparison:
    status: ConsistencyStatus
    mismatches: tuple[SemanticMismatch, ...] = ()
    reason: str | None = None


class RuntimeConsistencyError(RuntimeError):
    code = "runtime_consistency_error"
    event_code = "RUNTIME_CONSISTENCY_ERROR"


class IntentExecutionConsistencyError(RuntimeConsistencyError):
    code = "intent_execution_mismatch"
    event_code = "INTENT_EXECUTION_MISMATCH"

    def __init__(
        self,
        intent: AnalysisIntent,
        comparison: ConsistencyComparison,
    ) -> None:
        self.intent_id = intent.intent_id
        self.comparison = comparison
        self.dimensions = tuple(item.dimension for item in comparison.mismatches)
        if not self.dimensions:
            self.dimensions = ("projection_support",)
        detail = ", ".join(self.dimensions)
        super().__init__(f"intent and execution plan differ: {detail}")


class ExecutedCommittedConsistencyError(RuntimeConsistencyError):
    code = "executed_committed_mismatch"
    event_code = "EXECUTED_COMMITTED_MISMATCH"

    def __init__(self, dimension: str, expected: Any, actual: Any) -> None:
        self.dimension = dimension
        self.expected = expected
        self.actual = actual
        super().__init__(f"executed and committed intent differ: {dimension}")


class CommittedReloadedConsistencyError(RuntimeConsistencyError):
    code = "committed_reloaded_mismatch"
    event_code = "COMMITTED_RELOADED_MISMATCH"

    def __init__(self, dimension: str, expected: Any, actual: Any) -> None:
        self.dimension = dimension
        self.expected = expected
        self.actual = actual
        super().__init__(f"committed and reloaded state differ: {dimension}")


class _NotComparable(ValueError):
    pass


@dataclass(frozen=True)
class _Source:
    key: str
    operation: str
    operand: AnalysisOperand
    periods: tuple[tuple[str, str], ...]
    series_grain: str | None
    bucket_aggregate: str | None
    series_reduce: str | None


_DIMENSION_ORDER = (
    "operation",
    "operand",
    "metric",
    "period",
    "geo",
    "business_entity",
    "entity_role",
    "direction",
    "unit",
    "grain",
    "grouping",
    "aggregation",
    "comparison",
    "formula",
    "ranking",
)


def _periods(periods) -> tuple[tuple[str, str], ...]:
    return tuple(
        (item.date_from.isoformat(), item.date_to.isoformat()) for item in periods
    )


def _source_key(operand_id: str, period_index: int | None = None) -> str:
    return operand_id if period_index is None else f"{operand_id}@period:{period_index}"


def _task_operation(intent: AnalysisIntent) -> Operation:
    if intent.operation in {
        Operation.COMPARE,
        Operation.COMPARE_PERIODS,
        Operation.CALCULATE,
    }:
        return Operation.AGGREGATE
    if intent.operation in {Operation.GROUP, Operation.RANK}:
        return Operation.SHOW
    if (
        intent.operation == Operation.AGGREGATE
        and intent.grain in {"day", "month", "quarter", "year"}
    ):
        return Operation.SHOW
    return intent.operation


def _execution_aggregation(
    intent: AnalysisIntent, operand: AnalysisOperand
) -> tuple[str, str | None, str | None]:
    if intent.operation == Operation.GROUP:
        if len(intent.grouping) != 1 or intent.grouping[0].dimension != "period":
            raise _NotComparable("only temporal grouping has a typed native plan")
        aggregate = intent.grouping[0].aggregate_type
        return aggregate, aggregate, None
    if intent.operation == Operation.RANK:
        if intent.ranking is None:
            raise _NotComparable("ranking metadata is missing")
        aggregate = intent.ranking.bucket_aggregate
        return aggregate, aggregate, None
    if (
        intent.operation == Operation.AGGREGATE
        and intent.grain in {"day", "month", "quarter", "year"}
    ):
        bucket = metric_definition(operand.metric).default_aggregate
        return bucket, bucket, operand.aggregate_type
    return operand.aggregate_type, None, None


def _intent_sources(intent: AnalysisIntent) -> tuple[_Source, ...]:
    if intent.operation == Operation.MULTI_STEP:
        raise _NotComparable("multi-step has no native execution plan")
    operation = _task_operation(intent).value
    sources: list[_Source] = []
    if intent.operation == Operation.COMPARE_PERIODS:
        if len(intent.operands) != 1:
            raise _NotComparable("period comparison operand shape is unsupported")
        operand = intent.operands[0]
        scalar_aggregate, bucket, reduction = _execution_aggregation(intent, operand)
        scalar_operand = operand.model_copy(
            update={"aggregate_type": scalar_aggregate}, deep=True
        )
        for index, period in enumerate(intent.periods):
            sources.append(
                _Source(
                    key=_source_key(operand.operand_id, index),
                    operation=operation,
                    operand=scalar_operand,
                    periods=_periods([period]),
                    series_grain=None,
                    bucket_aggregate=bucket,
                    series_reduce=reduction,
                )
            )
        return tuple(sources)

    for operand in intent.operands:
        scalar_aggregate, bucket, reduction = _execution_aggregation(intent, operand)
        scalar_operand = operand.model_copy(
            update={"aggregate_type": scalar_aggregate}, deep=True
        )
        grain = (
            intent.ranking.grain
            if intent.operation == Operation.RANK and intent.ranking is not None
            else intent.grain
            if intent.operation in {Operation.GROUP, Operation.AGGREGATE}
            and intent.grain in {"day", "month", "quarter", "year"}
            else None
        )
        sources.append(
            _Source(
                key=_source_key(operand.operand_id),
                operation=operation,
                operand=scalar_operand,
                periods=_periods(operand.periods or intent.periods),
                series_grain=grain,
                bucket_aggregate=bucket,
                series_reduce=reduction,
            )
        )
    return tuple(sources)


def _plan_sources(plan: NativeExecutionPlan) -> tuple[_Source, ...]:
    return tuple(
        _Source(
            key=_source_key(task.operand_id, task.period_index),
            operation=task.scalar_intent.operation.value,
            operand=task.scalar_intent.operands[0],
            periods=_periods(task.scalar_intent.periods),
            series_grain=task.series_grain,
            bucket_aggregate=task.bucket_aggregate,
            series_reduce=task.series_reduce,
        )
        for task in plan.tasks
    )


def _sorted(items) -> tuple:
    return tuple(sorted(items, key=repr))


def _entities(sources: tuple[_Source, ...], *, geo: bool | None = None) -> tuple:
    values = []
    for source in sources:
        for reference in source.operand.entities:
            is_geo = reference.entity.entity_type in {"geo_object", "geo_group"}
            if geo is not None and is_geo != geo:
                continue
            values.append(
                (
                    source.key,
                    reference.role,
                    reference.entity.entity_type,
                    reference.entity.entity_id,
                )
            )
    return _sorted(values)


def _direction(sources: tuple[_Source, ...]) -> tuple:
    return _sorted(
        (
            source.key,
            reference.role,
            reference.entity.entity_type,
            reference.entity.entity_id,
        )
        for source in sources
        for reference in source.operand.entities
        if reference.role in {"source", "destination"}
    )


def _model_semantics(value) -> tuple | None:
    if value is None:
        return None
    payload = value.model_dump(mode="json")
    return tuple(sorted(payload.items()))


def _intent_comparison(intent: AnalysisIntent, sources: tuple[_Source, ...]):
    if intent.operation == Operation.COMPARE_PERIODS:
        return (
            sources[0].key,
            sources[1].key,
            "target_minus_baseline",
            "baseline",
        )
    if intent.operation != Operation.COMPARE:
        return None
    comparison = intent.comparison
    if comparison is None:
        comparison = type("ImplicitComparison", (), {
            "baseline_operand_id": intent.operands[0].operand_id,
            "target_operand_id": intent.operands[1].operand_id,
            "delta_direction": "target_minus_baseline",
            "percent_base": "baseline",
        })()
    return (
        comparison.baseline_operand_id,
        comparison.target_operand_id,
        comparison.delta_direction,
        comparison.percent_base,
    )


def _plan_comparison(plan: NativeExecutionPlan, sources: tuple[_Source, ...]):
    if plan.comparison is None:
        return None
    if plan.operation == Operation.COMPARE_PERIODS:
        task_keys = {
            task.task_id: source.key for task, source in zip(plan.tasks, sources)
        }
        baseline = task_keys.get(
            plan.comparison.baseline_operand_id,
            f"unknown:{plan.comparison.baseline_operand_id}",
        )
        target = task_keys.get(
            plan.comparison.target_operand_id,
            f"unknown:{plan.comparison.target_operand_id}",
        )
    else:
        baseline = plan.comparison.baseline_operand_id
        target = plan.comparison.target_operand_id
    return (
        baseline,
        target,
        plan.comparison.delta_direction,
        plan.comparison.percent_base,
    )


def _dimensions(
    *,
    overall_operation: Operation,
    sources: tuple[_Source, ...],
    grouping,
    comparison,
    formula,
    ranking,
) -> IntentSemanticProjection:
    values = {
        "operation": (
            overall_operation.value,
            _sorted((source.key, source.operation) for source in sources),
        ),
        "operand": _sorted(source.key for source in sources),
        "metric": _sorted(
            (source.key, source.operand.metric) for source in sources
        ),
        "period": _sorted((source.key, source.periods) for source in sources),
        "geo": _entities(sources, geo=True),
        "business_entity": _entities(sources, geo=False),
        "entity_role": _entities(sources),
        "direction": _direction(sources),
        "unit": _sorted((source.key, source.operand.unit) for source in sources),
        "grain": _sorted(
            (source.key, source.series_grain) for source in sources
        ),
        "grouping": grouping,
        "aggregation": _sorted(
            (
                source.key,
                source.operand.aggregate_type,
                source.bucket_aggregate,
                source.series_reduce,
            )
            for source in sources
        ),
        "comparison": comparison,
        "formula": _model_semantics(formula),
        "ranking": _model_semantics(ranking),
    }
    return IntentSemanticProjection(
        dimensions=tuple((name, values[name]) for name in _DIMENSION_ORDER)
    )


def project_intent_semantics(intent: AnalysisIntent) -> IntentSemanticProjection:
    sources = _intent_sources(intent)
    grouping = tuple(
        (item.dimension, item.canonical_group_id, item.aggregate_type)
        for item in intent.grouping
    )
    return _dimensions(
        overall_operation=intent.operation,
        sources=sources,
        grouping=grouping,
        comparison=_intent_comparison(intent, sources),
        formula=intent.formula,
        ranking=intent.ranking,
    )


def project_execution_semantics(
    plan: NativeExecutionPlan,
) -> IntentSemanticProjection:
    sources = _plan_sources(plan)
    grouping = (
        (
            "period",
            None,
            plan.tasks[0].bucket_aggregate if len(plan.tasks) == 1 else None,
        ),
    ) if plan.operation == Operation.GROUP else ()
    return _dimensions(
        overall_operation=plan.operation,
        sources=sources,
        grouping=grouping,
        comparison=_plan_comparison(plan, sources),
        formula=plan.formula,
        ranking=plan.ranking,
    )


def compare_intent_execution_semantics(
    intent: AnalysisIntent,
    plan: NativeExecutionPlan,
) -> ConsistencyComparison:
    try:
        expected = project_intent_semantics(intent).values()
        actual = project_execution_semantics(plan).values()
    except _NotComparable as exc:
        return ConsistencyComparison(
            status=ConsistencyStatus.NOT_COMPARABLE,
            reason=str(exc),
        )
    mismatches = tuple(
        SemanticMismatch(
            dimension=dimension,
            expected=expected[dimension],
            actual=actual[dimension],
        )
        for dimension in _DIMENSION_ORDER
        if expected[dimension] != actual[dimension]
    )
    return ConsistencyComparison(
        status=(
            ConsistencyStatus.SUPPORTED_AND_MISMATCH
            if mismatches
            else ConsistencyStatus.SUPPORTED_AND_EQUAL
        ),
        mismatches=mismatches,
    )


def assert_intent_execution_consistent(
    intent: AnalysisIntent,
    plan: NativeExecutionPlan,
) -> None:
    comparison = compare_intent_execution_semantics(intent, plan)
    if comparison.status != ConsistencyStatus.SUPPORTED_AND_EQUAL:
        raise IntentExecutionConsistencyError(intent, comparison)


def _intent_id(value: AnalysisIntent | None) -> str | None:
    return value.intent_id if value is not None else None


def assert_executed_committed_consistent(
    executed_intent: AnalysisIntent,
    committed: ContextContractV2,
    *,
    outcome,
) -> None:
    attempted = (
        committed.last_attempted_scope.intent
        if committed.last_attempted_scope is not None
        else None
    )
    if attempted != executed_intent:
        raise ExecutedCommittedConsistencyError(
            "last_attempted_intent",
            _intent_id(executed_intent),
            _intent_id(attempted),
        )
    if outcome == "success" or getattr(outcome, "value", None) == "success":
        active = (
            committed.active_dialog_scope.intent
            if committed.active_dialog_scope is not None
            else None
        )
        if active != executed_intent:
            raise ExecutedCommittedConsistencyError(
                "active_intent",
                _intent_id(executed_intent),
                _intent_id(active),
            )


def _scope_intent(state: ContextContractV2, field: str) -> AnalysisIntent | None:
    scope = getattr(state, field)
    return scope.intent if scope is not None else None


def assert_committed_reloaded_consistent(
    committed: ContextContractV2,
    reloaded: ContextContractV2,
) -> None:
    scalar_fields = (
        ("session_id", committed.session_id, reloaded.session_id),
        ("revision", committed.revision, reloaded.revision),
    )
    for dimension, expected, actual in scalar_fields:
        if expected != actual:
            raise CommittedReloadedConsistencyError(dimension, expected, actual)
    for field, dimension in (
        ("active_dialog_scope", "active_intent"),
        ("last_attempted_scope", "last_attempted_intent"),
        ("last_successful_scope", "last_successful_intent"),
    ):
        expected = _scope_intent(committed, field)
        actual = _scope_intent(reloaded, field)
        if expected != actual:
            raise CommittedReloadedConsistencyError(
                dimension,
                _intent_id(expected),
                _intent_id(actual),
            )
