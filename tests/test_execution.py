from __future__ import annotations

from decimal import Decimal

import pytest

from balance_chat.contracts import AnalysisIntent, AnalysisOperand, Operation, PeriodRef
from balance_chat.execution import (
    NativeExecutionError,
    NativeExecutor,
    PipelineScalarTaskRunner,
    ScalarFact,
)
from balance_chat.planning import NativeMultiOperandPlanner


class FakeRunner:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.calls: list[str] = []

    def run(self, task, **_):
        self.calls.append(task.task_id)
        return {
            "status": "ok",
            "rows": [{"value": self.values[task.task_id]}],
        }


def _plan():
    return NativeMultiOperandPlanner().plan(
        AnalysisIntent(
            operation=Operation.COMPARE,
            operands=[
                AnalysisOperand(operand_id="supply", metric="distribution"),
                AnalysisOperand(operand_id="needs", metric="own_needs"),
            ],
            periods=[PeriodRef(date_from="2025-05-01", date_to="2025-06-01")],
        )
    )


def _extract(task, envelope):
    return ScalarFact(
        task_id=task.task_id,
        value=Decimal(envelope["rows"][0]["value"]),
        unit="тыс. м3",
        label=task.operand_id,
    )


def test_executor_runs_each_operand_once_and_composes_delta() -> None:
    runner = FakeRunner({"task_1": "100", "task_2": "25"})
    result = NativeExecutor(runner, _extract).execute(
        _plan(),
        original_query="Сравни поставки и собственные нужды",
        execute_db=True,
    )

    assert runner.calls == ["task_1", "task_2"]
    assert result.comparison.delta == Decimal("-75")
    assert result.comparison.percent_change == Decimal("-75")


def test_executor_rejects_success_without_explicit_fact() -> None:
    runner = FakeRunner({"task_1": "100", "task_2": "25"})
    with pytest.raises(NativeExecutionError, match="lacks deterministic fact"):
        NativeExecutor(runner, lambda *_: None).execute(
            _plan(),
            original_query="compare",
            execute_db=True,
        )


def test_executor_does_not_hide_no_data() -> None:
    class NoDataRunner(FakeRunner):
        def run(self, task, **_):
            return {"status": "no_data" if task.task_id == "task_2" else "ok"}

    result = NativeExecutor(NoDataRunner({}), lambda *_: None).execute(
        _plan(),
        original_query="compare",
        execute_db=True,
    )
    assert result.status == "no_data"
    assert result.comparison is None


def test_pipeline_runner_renders_each_scalar_without_sibling_operand() -> None:
    from balance_chat.contracts import CanonicalEntityRef, OperandEntityRef

    class Runtime:
        def __init__(self) -> None:
            self.queries = []

        def execute(self, query, *_args, **_kwargs):
            self.queries.append(query)
            return {"status": "no_data"}

    intent = AnalysisIntent(
        operation=Operation.COMPARE,
        operands=[
            AnalysisOperand(
                operand_id="kazan",
                metric="distribution",
                entities=[
                    OperandEntityRef(
                        role="article",
                        entity=CanonicalEntityRef(
                            entity_id="1",
                            entity_type="article",
                            display_name="Казань",
                        ),
                    )
                ],
            ),
            AnalysisOperand(
                operand_id="yaroslavl",
                metric="distribution",
                entities=[
                    OperandEntityRef(
                        role="article",
                        entity=CanonicalEntityRef(
                            entity_id="2",
                            entity_type="article",
                            display_name="Ярославль",
                        ),
                    )
                ],
            ),
        ],
        periods=[PeriodRef(date_from="2025-05-01", date_to="2025-06-01")],
    )
    plan = NativeMultiOperandPlanner().plan(intent)
    runtime = Runtime()
    runner = PipelineScalarTaskRunner(runtime)

    runner.run(
        plan.tasks[1],
        original_query="Сравни Казань и Ярославль",
        execute_db=False,
        request_id="request",
    )

    assert "Ярославль" in runtime.queries[0]
    assert "Казань" not in runtime.queries[0]
    assert "2025-05-01" in runtime.queries[0]
