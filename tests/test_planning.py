from __future__ import annotations

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    ComparisonSpec,
    Operation,
    PeriodRef,
)
from balance_chat.planning import NativeMultiOperandPlanner


def _period(month: int) -> PeriodRef:
    return PeriodRef(
        date_from=f"2025-{month:02d}-01",
        date_to=f"2025-{month + 1:02d}-01",
    )


def test_metric_comparison_becomes_two_scalar_tasks() -> None:
    intent = AnalysisIntent(
        operation=Operation.COMPARE,
        operands=[
            AnalysisOperand(operand_id="supply", metric="distribution"),
            AnalysisOperand(operand_id="needs", metric="own_needs"),
        ],
        periods=[_period(5)],
        comparison=ComparisonSpec(
            baseline_operand_id="supply",
            target_operand_id="needs",
        ),
    )

    plan = NativeMultiOperandPlanner().plan(intent)

    assert [task.operand_id for task in plan.tasks] == ["supply", "needs"]
    assert all(task.scalar_intent.operation == Operation.AGGREGATE for task in plan.tasks)
    assert all(len(task.scalar_intent.operands) == 1 for task in plan.tasks)
    assert plan.comparison.baseline_operand_id == "supply"


def test_period_comparison_preserves_two_exclusive_end_periods() -> None:
    intent = AnalysisIntent(
        operation=Operation.COMPARE_PERIODS,
        operands=[AnalysisOperand(operand_id="supply", metric="distribution")],
        periods=[_period(4), _period(5)],
    )

    plan = NativeMultiOperandPlanner().plan(intent)

    assert [task.period_index for task in plan.tasks] == [0, 1]
    assert plan.tasks[0].scalar_intent.periods == [_period(4)]
    assert plan.tasks[1].scalar_intent.periods == [_period(5)]
    assert plan.comparison.baseline_operand_id == "period_1"


def test_operand_specific_period_overrides_global_period() -> None:
    intent = AnalysisIntent(
        operation=Operation.COMPARE,
        operands=[
            AnalysisOperand(
                operand_id="spring",
                metric="distribution",
                periods=[PeriodRef(date_from="2025-03-01", date_to="2025-06-01")],
            ),
            AnalysisOperand(
                operand_id="summer",
                metric="distribution",
                periods=[PeriodRef(date_from="2025-06-01", date_to="2025-09-01")],
            ),
        ],
    )

    plan = NativeMultiOperandPlanner().plan(intent)

    assert plan.tasks[0].scalar_intent.periods[0].date_from.isoformat() == "2025-03-01"
    assert plan.tasks[1].scalar_intent.periods[0].date_from.isoformat() == "2025-06-01"
