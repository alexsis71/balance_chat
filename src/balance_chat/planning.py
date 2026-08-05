from __future__ import annotations

from pydantic import Field, model_validator

from .contracts import (
    AnalysisIntent,
    AnalysisOperand,
    ComparisonSpec,
    ContractModel,
    FormulaSpec,
    Operation,
    PeriodRef,
    RankingSpec,
)
from .domain import metric_definition


class PlanningError(ValueError):
    pass


class ExecutionTask(ContractModel):
    task_id: str
    operand_id: str
    period_index: int | None = Field(default=None, ge=0)
    series_grain: str | None = None
    bucket_aggregate: str | None = None
    scalar_intent: AnalysisIntent

    @model_validator(mode="after")
    def validate_scalar_shape(self) -> "ExecutionTask":
        if len(self.scalar_intent.operands) != 1:
            raise ValueError("execution task must contain exactly one operand")
        if self.scalar_intent.grouping:
            raise ValueError("scalar task cannot contain grouping")
        if self.scalar_intent.comparison is not None:
            raise ValueError("scalar task cannot contain comparison")
        return self


class NativeExecutionPlan(ContractModel):
    source_intent_id: str
    operation: Operation
    tasks: list[ExecutionTask] = Field(min_length=1)
    comparison: ComparisonSpec | None = None
    formula: FormulaSpec | None = None
    ranking: RankingSpec | None = None


class NativeMultiOperandPlanner:
    """Deterministically decomposes a bound intent; it never calls an LLM."""

    def plan(self, intent: AnalysisIntent) -> NativeExecutionPlan:
        if intent.grouping:
            raise PlanningError("grouped intent requires the grouping planner")
        if intent.operation == Operation.COMPARE:
            return self._entity_or_metric_comparison(intent)
        if intent.operation == Operation.COMPARE_PERIODS:
            return self._period_comparison(intent)
        if intent.operation == Operation.CALCULATE:
            return self._calculation(intent)
        if intent.operation == Operation.RANK:
            return self._ranking(intent)
        if len(intent.operands) != 1:
            raise PlanningError(
                f"{intent.operation.value} requires exactly one operand before execution"
            )
        task = self._task(
            intent.operands[0],
            periods=intent.operands[0].periods or intent.periods,
            operation=intent.operation,
            task_id="task_1",
        )
        return NativeExecutionPlan(
            source_intent_id=intent.intent_id,
            operation=intent.operation,
            tasks=[task],
        )

    def _entity_or_metric_comparison(
        self, intent: AnalysisIntent
    ) -> NativeExecutionPlan:
        if len(intent.operands) < 2:
            raise PlanningError("comparison requires at least two operands")
        comparison = intent.comparison or ComparisonSpec(
            baseline_operand_id=intent.operands[0].operand_id,
            target_operand_id=intent.operands[1].operand_id,
        )
        tasks = [
            self._task(
                operand,
                periods=operand.periods or intent.periods,
                operation=Operation.AGGREGATE,
                task_id=f"task_{index + 1}",
            )
            for index, operand in enumerate(intent.operands)
        ]
        return NativeExecutionPlan(
            source_intent_id=intent.intent_id,
            operation=intent.operation,
            tasks=tasks,
            comparison=comparison,
        )

    def _calculation(self, intent: AnalysisIntent) -> NativeExecutionPlan:
        if intent.formula is None:
            raise PlanningError("calculation requires typed formula semantics")
        referenced = {
            intent.formula.numerator_operand_id,
            intent.formula.denominator_operand_id,
        }
        selected = [item for item in intent.operands if item.operand_id in referenced]
        if len(selected) != 2:
            raise PlanningError("calculation formula operands are incomplete")
        tasks = [
            self._task(
                operand,
                periods=operand.periods or intent.periods,
                operation=Operation.AGGREGATE,
                task_id=f"task_{index + 1}",
            )
            for index, operand in enumerate(selected)
        ]
        return NativeExecutionPlan(
            source_intent_id=intent.intent_id,
            operation=intent.operation,
            tasks=tasks,
            formula=intent.formula,
        )

    def _ranking(self, intent: AnalysisIntent) -> NativeExecutionPlan:
        if intent.ranking is None or len(intent.operands) != 1:
            raise PlanningError("ranking requires one operand and typed ranking semantics")
        definition = metric_definition(intent.operands[0].metric)
        if intent.ranking.bucket_aggregate not in definition.allowed_aggregates:
            raise PlanningError(
                f"bucket aggregate {intent.ranking.bucket_aggregate!r} is invalid "
                f"for metric {intent.operands[0].metric!r}"
            )
        operand = intent.operands[0].model_copy(
            update={"aggregate_type": intent.ranking.bucket_aggregate},
            deep=True,
        )
        task = self._task(
            operand,
            periods=operand.periods or intent.periods,
            operation=Operation.SHOW,
            task_id="rank_source",
            series_grain=intent.ranking.grain,
            bucket_aggregate=intent.ranking.bucket_aggregate,
        )
        return NativeExecutionPlan(
            source_intent_id=intent.intent_id,
            operation=intent.operation,
            tasks=[task],
            ranking=intent.ranking,
        )

    def _period_comparison(self, intent: AnalysisIntent) -> NativeExecutionPlan:
        if len(intent.operands) != 1 or len(intent.periods) != 2:
            raise PlanningError(
                "period comparison requires one operand and two global periods"
            )
        operand = intent.operands[0]
        tasks = [
            self._task(
                operand,
                periods=[period],
                operation=Operation.AGGREGATE,
                task_id=f"period_{index + 1}",
                period_index=index,
            )
            for index, period in enumerate(intent.periods)
        ]
        comparison = ComparisonSpec(
            baseline_operand_id=tasks[0].task_id,
            target_operand_id=tasks[1].task_id,
        )
        return NativeExecutionPlan(
            source_intent_id=intent.intent_id,
            operation=intent.operation,
            tasks=tasks,
            comparison=comparison,
        )

    @staticmethod
    def _task(
        operand: AnalysisOperand,
        *,
        periods: list[PeriodRef],
        operation: Operation,
        task_id: str,
        period_index: int | None = None,
        series_grain: str | None = None,
        bucket_aggregate: str | None = None,
    ) -> ExecutionTask:
        if not periods:
            raise PlanningError(f"operand {operand.operand_id} has no period")
        scalar_operand = operand.model_copy(update={"periods": []}, deep=True)
        scalar_intent = AnalysisIntent(
            operation=operation,
            operands=[scalar_operand],
            periods=periods,
            grain=series_grain,
        )
        return ExecutionTask(
            task_id=task_id,
            operand_id=operand.operand_id,
            period_index=period_index,
            series_grain=series_grain,
            bucket_aggregate=bucket_aggregate,
            scalar_intent=scalar_intent,
        )
