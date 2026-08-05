from __future__ import annotations

from decimal import Decimal

import pytest

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    FormulaSpec,
    OperandEntityRef,
    Operation,
    PeriodRef,
    RankingSpec,
)
from balance_chat.execution import (
    NativeExecutionError,
    NativeExecutor,
    PipelineScalarTaskRunner,
    ScalarFact,
    _render_scalar_query,
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


def test_executor_composes_n_way_comparison_set_from_one_call_per_operand() -> None:
    intent = AnalysisIntent(
        operation=Operation.COMPARE,
        operands=[
            AnalysisOperand(
                operand_id=operand_id,
                metric="distribution",
                entities=[OperandEntityRef(
                    role="destination",
                    entity=CanonicalEntityRef(
                        entity_id=operand_id,
                        entity_type="geo_object",
                        display_name=operand_id,
                    ),
                )],
            )
            for operand_id in (
                "kazan_spring", "kazan_summer",
                "yaroslavl_spring", "yaroslavl_summer",
            )
        ],
        periods=[PeriodRef(date_from="2025-03-01", date_to="2025-09-01")],
    )
    runner = FakeRunner(
        {"task_1": "100", "task_2": "80", "task_3": "120", "task_4": "90"}
    )

    result = NativeExecutor(runner, _extract).execute(
        NativeMultiOperandPlanner().plan(intent),
        original_query="сравни Казань и Ярославль весной и летом",
        execute_db=True,
    )

    assert runner.calls == ["task_1", "task_2", "task_3", "task_4"]
    assert [item.value for item in result.comparison_set.members] == [
        Decimal("100"), Decimal("80"), Decimal("120"), Decimal("90")
    ]
    assert result.comparison_set.members[2].percent_change_from_baseline == Decimal("20")


def test_executor_deduplicates_identical_scalar_sources_within_one_dag() -> None:
    period = PeriodRef(date_from="2025-05-01", date_to="2025-06-01")
    intent = AnalysisIntent(
        operation=Operation.COMPARE,
        operands=[
            AnalysisOperand(operand_id="left", metric="distribution"),
            AnalysisOperand(operand_id="right", metric="distribution"),
        ],
        periods=[period],
    )
    runner = FakeRunner({"task_1": "100"})

    result = NativeExecutor(runner, _extract).execute(
        NativeMultiOperandPlanner().plan(intent),
        original_query="сравни один и тот же показатель",
        execute_db=True,
    )

    assert runner.calls == ["task_1"]
    assert result.source_execution_count == 1
    assert [item.value for item in result.comparison_set.members] == [
        Decimal("100"), Decimal("100")
    ]


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


def test_max_scalar_query_requests_exact_extremum_date() -> None:
    intent = AnalysisIntent(
        operation=Operation.AGGREGATE,
        operands=[AnalysisOperand(
            operand_id="route",
            metric="distribution",
            aggregate_type="max",
            entities=[
                OperandEntityRef(
                    role="balance",
                    entity=CanonicalEntityRef(
                        entity_id="10",
                        entity_type="balance",
                        display_name="ГП ТГ Н.Новгород суточный баланс",
                    ),
                ),
                OperandEntityRef(
                    role="article",
                    entity=CanonicalEntityRef(
                        entity_id="11",
                        entity_type="article",
                        display_name="ТГ Москва",
                    ),
                ),
            ],
        )],
        periods=[PeriodRef(date_from="2025-04-01", date_to="2025-07-01")],
    )
    task = NativeMultiOperandPlanner().plan(intent).tasks[0]

    query = _render_scalar_query(task)

    assert query.startswith("Когда был достигнут максимум распределения газа")
    assert "ГП ТГ Н.Новгород суточный баланс" in query
    assert "ТГ Москва" in query


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


def test_executor_computes_percent_without_repeating_scalar_calls() -> None:
    intent = AnalysisIntent(
        operation=Operation.CALCULATE,
        operands=[
            AnalysisOperand(operand_id="needs", metric="own_needs"),
            AnalysisOperand(operand_id="distribution", metric="distribution"),
        ],
        periods=[PeriodRef(date_from="2025-05-01", date_to="2025-06-01")],
        formula=FormulaSpec(
            operator="percent_of",
            numerator_operand_id="needs",
            denominator_operand_id="distribution",
        ),
    )
    runner = FakeRunner({"task_1": "25", "task_2": "100"})

    result = NativeExecutor(runner, _extract).execute(
        NativeMultiOperandPlanner().plan(intent),
        original_query="процент собственных нужд от распределения",
        execute_db=True,
    )

    assert runner.calls == ["task_1", "task_2"]
    assert result.derived.value == Decimal("25")
    assert result.derived.unit == "%"


def test_executor_returns_no_data_for_zero_formula_denominator() -> None:
    intent = AnalysisIntent(
        operation=Operation.CALCULATE,
        operands=[
            AnalysisOperand(operand_id="numerator", metric="incoming"),
            AnalysisOperand(operand_id="denominator", metric="distribution"),
        ],
        periods=[PeriodRef(date_from="2025-05-01", date_to="2025-06-01")],
        formula=FormulaSpec(
            operator="percent_of",
            numerator_operand_id="numerator",
            denominator_operand_id="denominator",
        ),
    )
    runner = FakeRunner({"task_1": "25", "task_2": "0"})

    result = NativeExecutor(runner, _extract).execute(
        NativeMultiOperandPlanner().plan(intent),
        original_query="процент",
        execute_db=True,
    )

    assert result.status == "no_data"
    assert result.derived is None


def test_executor_selects_earliest_month_on_equal_rank_value() -> None:
    intent = AnalysisIntent(
        operation=Operation.RANK,
        operands=[AnalysisOperand(operand_id="incoming", metric="incoming")],
        periods=[PeriodRef(date_from="2025-01-01", date_to="2026-01-01")],
        ranking=RankingSpec(direction="max", grain="month", bucket_aggregate="sum"),
    )
    runner = FakeRunner({"rank_source": "0"})

    def series(task, _envelope):
        return [
            ScalarFact(
                task_id=task.task_id,
                value=Decimal("10"),
                unit="тыс. м3",
                label="февраль",
                periods=[{"date_from": "2025-02-01", "date_to": "2025-03-01"}],
            ),
            ScalarFact(
                task_id=task.task_id,
                value=Decimal("10"),
                unit="тыс. м3",
                label="январь",
                periods=[{"date_from": "2025-01-01", "date_to": "2025-02-01"}],
            ),
        ]

    result = NativeExecutor(runner, _extract, series).execute(
        NativeMultiOperandPlanner().plan(intent),
        original_query="в каком месяце максимум",
        execute_db=True,
    )

    assert result.ranking.source_row_count == 2
    assert result.ranking.selected[0].label == "январь"
    assert result.task_results[0].fact.label == "январь"


def test_rank_query_explicitly_requests_month_buckets() -> None:
    intent = AnalysisIntent(
        operation=Operation.RANK,
        operands=[AnalysisOperand(operand_id="incoming", metric="incoming")],
        periods=[PeriodRef(date_from="2025-01-01", date_to="2026-01-01")],
        ranking=RankingSpec(direction="max", grain="month", bucket_aggregate="sum"),
    )
    query = _render_scalar_query(NativeMultiOperandPlanner().plan(intent).tasks[0])

    assert query.startswith("Покажи поступление газа")
    assert query.endswith("по месяцам")
