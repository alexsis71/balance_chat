from __future__ import annotations

from decimal import Decimal
from datetime import date
import json
from typing import Any, Callable, Mapping, Protocol

from pydantic import Field

from .contracts import ContractModel, FormulaSpec, Operation
from .domain import metric_definition
from .planning import ExecutionTask, NativeExecutionPlan


class NativeExecutionError(RuntimeError):
    pass


class ScalarFact(ContractModel):
    task_id: str
    value: Decimal
    unit: str
    label: str
    periods: list[dict[str, str]] = Field(default_factory=list)
    extremum_at: date | None = None
    dimension: dict[str, Any] | None = None
    source_row_count: int = Field(default=1, ge=1)
    provenance: list[dict[str, Any]] = Field(default_factory=list)


class TaskExecutionResult(ContractModel):
    task_id: str
    status: str
    fact: ScalarFact | None = None
    series: list[ScalarFact] = Field(default_factory=list)
    envelope: dict[str, Any]


class ComparisonResult(ContractModel):
    baseline_task_id: str
    target_task_id: str
    baseline_value: Decimal
    target_value: Decimal
    delta: Decimal
    percent_change: Decimal | None
    unit: str


class ComparisonMember(ContractModel):
    task_id: str
    operand_id: str
    label: str
    value: Decimal
    delta_from_baseline: Decimal
    percent_change_from_baseline: Decimal | None
    unit: str


class ComparisonSetResult(ContractModel):
    baseline_task_id: str
    members: list[ComparisonMember] = Field(min_length=2)


class DerivedResult(ContractModel):
    operator: str
    numerator_task_id: str
    denominator_task_id: str
    numerator_value: Decimal
    denominator_value: Decimal
    value: Decimal
    unit: str


class RankingResult(ContractModel):
    direction: str
    grain: str
    selected: list[ScalarFact]
    source_row_count: int = Field(ge=1)


class NativeExecutionResult(ContractModel):
    operation: Operation
    status: str
    task_results: list[TaskExecutionResult]
    comparison: ComparisonResult | None = None
    comparison_set: ComparisonSetResult | None = None
    derived: DerivedResult | None = None
    ranking: RankingResult | None = None
    source_execution_count: int = Field(default=0, ge=0)


class ScalarTaskRunner(Protocol):
    def run(
        self,
        task: ExecutionTask,
        *,
        original_query: str,
        execute_db: bool,
        request_id: str | None,
    ) -> Mapping[str, Any]: ...


FactExtractor = Callable[[ExecutionTask, Mapping[str, Any]], ScalarFact | None]
SeriesExtractor = Callable[[ExecutionTask, Mapping[str, Any]], list[ScalarFact]]


class PipelineScalarTaskRunner:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def run(
        self,
        task: ExecutionTask,
        *,
        original_query: str,
        execute_db: bool,
        request_id: str | None,
    ) -> Mapping[str, Any]:
        return self.runtime.execute(
            _render_scalar_query(task),
            task.scalar_intent,
            execute_db=execute_db,
            request_id=f"{request_id}:{task.task_id}" if request_id else task.task_id,
            apply_summary=False,
        )


def _render_scalar_query(task: ExecutionTask) -> str:
    """Render one resolver-safe query from one canonical scalar intent."""
    intent = task.scalar_intent
    operand = intent.operands[0]
    definition = metric_definition(operand.metric)
    metric_label = definition.public_label
    aggregate_metric = definition.genitive_label
    aggregate_prefixes = {
        "max": ["Когда был достигнут максимум", aggregate_metric],
        "min": ["Когда был достигнут минимум", aggregate_metric],
        "avg": ["Покажи среднее значение", aggregate_metric],
        "sum": ["Покажи суммарное значение", aggregate_metric],
        "first": ["Покажи первое значение", aggregate_metric],
        "last": ["Покажи последнее значение", aggregate_metric],
    }
    parts = (
        ["Покажи", metric_label]
        if task.series_grain
        else aggregate_prefixes.get(operand.aggregate_type, ["Покажи", metric_label])
    )
    by_role = {item.role: item.entity.display_name for item in operand.entities}
    if by_role.get("balance"):
        parts.extend(["по балансу", by_role["balance"]])
    if by_role.get("article"):
        parts.extend(["по статье", by_role["article"]])
    if by_role.get("source"):
        parts.extend(["из", by_role["source"]])
    if by_role.get("destination"):
        parts.extend(["в", by_role["destination"]])
    if by_role.get("route"):
        parts.extend(["по маршруту", by_role["route"]])
    periods = operand.periods or intent.periods
    if periods:
        period = periods[0]
        parts.extend(
            [
                "за период с",
                period.date_from.isoformat(),
                "по",
                period.date_to.isoformat(),
            ]
        )
    if task.series_grain:
        grain_label = {
            "day": "по дням",
            "month": "по месяцам",
            "quarter": "по кварталам",
            "year": "по годам",
        }.get(task.series_grain)
        if grain_label:
            parts.append(grain_label)
    return " ".join(parts)


class NativeExecutor:
    """Execute every planned scalar exactly once, then compose deterministic facts."""

    def __init__(
        self,
        runner: ScalarTaskRunner,
        fact_extractor: FactExtractor,
        series_extractor: SeriesExtractor | None = None,
    ) -> None:
        self.runner = runner
        self.fact_extractor = fact_extractor
        self.series_extractor = series_extractor

    def execute(
        self,
        plan: NativeExecutionPlan,
        *,
        original_query: str,
        execute_db: bool,
        request_id: str | None = None,
    ) -> NativeExecutionResult:
        results: list[TaskExecutionResult] = []
        envelope_cache: dict[str, dict[str, Any]] = {}
        source_execution_count = 0
        for task in plan.tasks:
            semantic_payload = task.scalar_intent.model_dump(
                mode="json", exclude={"intent_id"}
            )
            for operand in semantic_payload.get("operands", []):
                operand.pop("operand_id", None)
            cache_key = json.dumps(
                semantic_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            envelope = envelope_cache.get(cache_key)
            if envelope is None:
                envelope = dict(
                    self.runner.run(
                        task,
                        original_query=original_query,
                        execute_db=execute_db,
                        request_id=request_id,
                    )
                )
                envelope_cache[cache_key] = envelope
                source_execution_count += 1
            else:
                envelope = dict(envelope)
            status = str(envelope.get("status") or "error")
            series = (
                self.series_extractor(task, envelope)
                if status in {"ok", "partial"} and task.series_grain and self.series_extractor
                else []
            )
            fact = None
            if status in {"ok", "partial"}:
                fact = series[0] if len(series) == 1 else (
                    None if task.series_grain else self.fact_extractor(task, envelope)
                )
            results.append(
                TaskExecutionResult(
                    task_id=task.task_id,
                    status=status,
                    fact=fact,
                    series=series,
                    envelope=envelope,
                )
            )
        if any(result.status not in {"ok", "partial"} for result in results):
            return NativeExecutionResult(
                operation=plan.operation,
                status="no_data"
                if any(result.status == "no_data" for result in results)
                else "error",
                task_results=results,
                source_execution_count=source_execution_count,
            )
        ranking = self._rank(plan, results) if plan.operation == Operation.RANK else None
        if ranking is not None:
            results[0].fact = ranking.selected[0]
        if any(result.fact is None for result in results):
            raise NativeExecutionError("successful scalar task lacks deterministic fact")
        comparison = (
            self._compare(plan, results)
            if plan.operation in {Operation.COMPARE, Operation.COMPARE_PERIODS}
            else None
        )
        comparison_set = (
            self._compare_set(plan, results)
            if plan.operation == Operation.COMPARE
            else None
        )
        derived = self._calculate(plan, results) if plan.operation == Operation.CALCULATE else None
        if plan.operation == Operation.CALCULATE and derived is None:
            return NativeExecutionResult(
                operation=plan.operation,
                status="no_data",
                task_results=results,
                source_execution_count=source_execution_count,
            )
        return NativeExecutionResult(
            operation=plan.operation,
            status="ok",
            task_results=results,
            comparison=comparison,
            comparison_set=comparison_set,
            derived=derived,
            ranking=ranking,
            source_execution_count=source_execution_count,
        )

    @classmethod
    def _compare_set(
        cls,
        plan: NativeExecutionPlan,
        results: list[TaskExecutionResult],
    ) -> ComparisonSetResult:
        if plan.comparison is None:
            raise NativeExecutionError("comparison plan lacks comparison semantics")
        baseline = cls._fact_for_operand(
            plan, results, plan.comparison.baseline_operand_id
        )
        members: list[ComparisonMember] = []
        for task, result in zip(plan.tasks, results):
            fact = result.fact
            if fact is None:
                raise NativeExecutionError("comparison set fact is missing")
            if fact.unit != baseline.unit:
                raise NativeExecutionError("comparison set units differ")
            delta = fact.value - baseline.value
            percent = (
                delta / baseline.value * Decimal("100")
                if baseline.value != 0
                else None
            )
            members.append(
                ComparisonMember(
                    task_id=task.task_id,
                    operand_id=task.operand_id,
                    label=fact.label,
                    value=fact.value,
                    delta_from_baseline=delta,
                    percent_change_from_baseline=percent,
                    unit=fact.unit,
                )
            )
        return ComparisonSetResult(
            baseline_task_id=baseline.task_id,
            members=members,
        )

    @staticmethod
    def _fact_for_operand(
        plan: NativeExecutionPlan,
        results: list[TaskExecutionResult],
        operand_id: str,
    ) -> ScalarFact:
        for task, result in zip(plan.tasks, results):
            if task.operand_id == operand_id and result.fact is not None:
                return result.fact
        raise NativeExecutionError(f"calculation operand {operand_id!r} has no fact")

    @classmethod
    def _calculate(
        cls,
        plan: NativeExecutionPlan,
        results: list[TaskExecutionResult],
    ) -> DerivedResult | None:
        formula: FormulaSpec | None = plan.formula
        if formula is None:
            raise NativeExecutionError("calculation plan lacks formula semantics")
        numerator = cls._fact_for_operand(plan, results, formula.numerator_operand_id)
        denominator = cls._fact_for_operand(plan, results, formula.denominator_operand_id)
        if numerator.unit != denominator.unit:
            raise NativeExecutionError("calculation units differ")
        if formula.operator in {"ratio", "percent_of", "percent_change"} and denominator.value == 0:
            return None
        if formula.operator == "delta":
            value, unit = numerator.value - denominator.value, numerator.unit
        elif formula.operator == "ratio":
            value, unit = numerator.value / denominator.value, "ratio"
        elif formula.operator == "percent_of":
            value, unit = numerator.value / denominator.value * Decimal("100"), "%"
        elif formula.operator == "percent_change":
            value = (numerator.value - denominator.value) / denominator.value * Decimal("100")
            unit = "%"
        else:  # pragma: no cover - FormulaSpec prevents it
            raise NativeExecutionError(f"unsupported formula operator: {formula.operator}")
        return DerivedResult(
            operator=formula.operator,
            numerator_task_id=numerator.task_id,
            denominator_task_id=denominator.task_id,
            numerator_value=numerator.value,
            denominator_value=denominator.value,
            value=value,
            unit=unit,
        )

    @staticmethod
    def _rank(
        plan: NativeExecutionPlan,
        results: list[TaskExecutionResult],
    ) -> RankingResult:
        if plan.ranking is None or len(results) != 1:
            raise NativeExecutionError("ranking plan lacks ranking semantics")
        series = results[0].series
        if not series:
            raise NativeExecutionError("ranking source returned no deterministic series")

        def tie_key(fact: ScalarFact) -> tuple[str, str]:
            period = fact.periods[0].get("date_from", "") if fact.periods else ""
            dimension = str((fact.dimension or {}).get("value") or "")
            return period, dimension

        ordered = sorted(series, key=tie_key)
        ordered = sorted(
            ordered,
            key=lambda fact: fact.value,
            reverse=plan.ranking.direction == "max",
        )
        return RankingResult(
            direction=plan.ranking.direction,
            grain=plan.ranking.grain,
            selected=ordered[: plan.ranking.limit],
            source_row_count=len(series),
        )

    @staticmethod
    def _compare(
        plan: NativeExecutionPlan,
        results: list[TaskExecutionResult],
    ) -> ComparisonResult:
        if plan.comparison is None:
            raise NativeExecutionError("comparison plan lacks comparison semantics")
        by_task = {result.task_id: result for result in results}
        baseline_id = plan.comparison.baseline_operand_id
        target_id = plan.comparison.target_operand_id
        if baseline_id not in by_task:
            baseline_task = next(
                (
                    result
                    for result, task in zip(results, plan.tasks)
                    if task.operand_id == baseline_id
                ),
                None,
            )
        else:
            baseline_task = by_task[baseline_id]
        if target_id not in by_task:
            target_task = next(
                (
                    result
                    for result, task in zip(results, plan.tasks)
                    if task.operand_id == target_id
                ),
                None,
            )
        else:
            target_task = by_task[target_id]
        if baseline_task is None or target_task is None:
            raise NativeExecutionError("comparison task mapping is incomplete")
        baseline = baseline_task.fact
        target = target_task.fact
        if baseline is None or target is None:
            raise NativeExecutionError("comparison fact is missing")
        if baseline.unit != target.unit:
            raise NativeExecutionError("comparison units differ")
        delta = target.value - baseline.value
        percent = (
            (delta / baseline.value * Decimal("100"))
            if baseline.value != 0
            else None
        )
        return ComparisonResult(
            baseline_task_id=baseline.task_id,
            target_task_id=target.task_id,
            baseline_value=baseline.value,
            target_value=target.value,
            delta=delta,
            percent_change=percent,
            unit=baseline.unit,
        )
