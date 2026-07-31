from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol

from pydantic import Field

from .contracts import ContractModel, Operation
from .planning import ExecutionTask, NativeExecutionPlan


class NativeExecutionError(RuntimeError):
    pass


class ScalarFact(ContractModel):
    task_id: str
    value: Decimal
    unit: str
    label: str
    provenance: list[dict[str, Any]] = Field(default_factory=list)


class TaskExecutionResult(ContractModel):
    task_id: str
    status: str
    fact: ScalarFact | None = None
    envelope: dict[str, Any]


class ComparisonResult(ContractModel):
    baseline_task_id: str
    target_task_id: str
    baseline_value: Decimal
    target_value: Decimal
    delta: Decimal
    percent_change: Decimal | None
    unit: str


class NativeExecutionResult(ContractModel):
    operation: Operation
    status: str
    task_results: list[TaskExecutionResult]
    comparison: ComparisonResult | None = None


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
            original_query,
            task.scalar_intent,
            execute_db=execute_db,
            request_id=f"{request_id}:{task.task_id}" if request_id else task.task_id,
            apply_summary=False,
        )


class NativeExecutor:
    """Execute every planned scalar exactly once, then compose deterministic facts."""

    def __init__(self, runner: ScalarTaskRunner, fact_extractor: FactExtractor) -> None:
        self.runner = runner
        self.fact_extractor = fact_extractor

    def execute(
        self,
        plan: NativeExecutionPlan,
        *,
        original_query: str,
        execute_db: bool,
        request_id: str | None = None,
    ) -> NativeExecutionResult:
        results: list[TaskExecutionResult] = []
        for task in plan.tasks:
            envelope = dict(
                self.runner.run(
                    task,
                    original_query=original_query,
                    execute_db=execute_db,
                    request_id=request_id,
                )
            )
            status = str(envelope.get("status") or "error")
            fact = (
                self.fact_extractor(task, envelope)
                if status in {"ok", "partial"}
                else None
            )
            results.append(
                TaskExecutionResult(
                    task_id=task.task_id,
                    status=status,
                    fact=fact,
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
            )
        if any(result.fact is None for result in results):
            raise NativeExecutionError("successful scalar task lacks deterministic fact")
        comparison = (
            self._compare(plan, results)
            if plan.operation in {Operation.COMPARE, Operation.COMPARE_PERIODS}
            else None
        )
        return NativeExecutionResult(
            operation=plan.operation,
            status="ok",
            task_results=results,
            comparison=comparison,
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
