from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    ComparisonSpec,
    Operation,
    PeriodRef,
    interpretation_decision_json_schema,
)


def test_period_ref_uses_exclusive_end() -> None:
    period = PeriodRef(date_from="2025-05-01", date_to="2025-06-01")
    assert period.date_from == date(2025, 5, 1)
    assert period.date_to == date(2025, 6, 1)


def test_period_ref_rejects_empty_range() -> None:
    with pytest.raises(ValidationError, match="exclusive-end"):
        PeriodRef(date_from="2025-05-01", date_to="2025-05-01")


def test_comparison_references_existing_operands() -> None:
    with pytest.raises(ValidationError, match="existing operands"):
        AnalysisIntent(
            operation=Operation.COMPARE,
            operands=[
                AnalysisOperand(operand_id="supply", metric="distribution"),
                AnalysisOperand(operand_id="needs", metric="own_needs"),
            ],
            comparison=ComparisonSpec(
                baseline_operand_id="supply",
                target_operand_id="missing",
            ),
        )


def test_compare_supports_two_different_metrics() -> None:
    intent = AnalysisIntent(
        operation=Operation.COMPARE,
        operands=[
            AnalysisOperand(operand_id="supply", metric="distribution"),
            AnalysisOperand(operand_id="needs", metric="own_needs"),
        ],
        comparison=ComparisonSpec(
            baseline_operand_id="supply",
            target_operand_id="needs",
        ),
    )
    assert [operand.metric for operand in intent.operands] == [
        "distribution",
        "own_needs",
    ]


def test_compare_rejects_more_than_two_operands() -> None:
    with pytest.raises(ValidationError, match="exactly two operands"):
        AnalysisIntent(
            operation=Operation.COMPARE,
            operands=[
                AnalysisOperand(operand_id=f"item_{index}", metric="distribution")
                for index in range(3)
            ],
        )


def test_compare_periods_rejects_multiple_operands() -> None:
    with pytest.raises(ValidationError, match="exactly one operand"):
        AnalysisIntent(
            operation=Operation.COMPARE_PERIODS,
            operands=[
                AnalysisOperand(operand_id="first", metric="distribution"),
                AnalysisOperand(operand_id="second", metric="distribution"),
            ],
            periods=[
                PeriodRef(date_from="2025-05-01", date_to="2025-06-01"),
                PeriodRef(date_from="2025-06-01", date_to="2025-07-01"),
            ],
        )


def test_scalar_operation_rejects_comparison_semantics() -> None:
    with pytest.raises(ValidationError, match="only for compare"):
        AnalysisIntent(
            operation=Operation.SHOW,
            operands=[AnalysisOperand(operand_id="supply", metric="distribution")],
            comparison=ComparisonSpec(
                baseline_operand_id="supply",
                target_operand_id="supply",
            ),
        )


def test_interpretation_schema_expresses_mode_dependent_payloads() -> None:
    schema = interpretation_decision_json_schema()
    assert len(schema["allOf"]) == 3
    executable = schema["allOf"][0]
    assert executable["then"]["required"] == ["draft"]
    assert executable["then"]["properties"]["draft"] == {
        "$ref": "#/$defs/InterpretationDraft"
    }
