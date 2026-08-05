from types import SimpleNamespace

import pytest

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ContextContractV2,
    FormulaSpec,
    OperandEntityRef,
    Operation,
    PeriodRef,
)
from balance_chat.gating import EvidenceCompletenessError, RoutingEvidenceGate
from balance_chat.planning import NativeMultiOperandPlanner


QUERY = (
    "какой процент составляет максимум поступления от ТГ Сургут в ТГ Томск "
    "от среднего общего распределения ТГ Томск в 2025"
)


def _record(name: str, *aliases: str):
    return SimpleNamespace(canonical_name=name, aliases=aliases)


def _entity(role: str, entity_id: str, entity_type: str, name: str):
    return OperandEntityRef(
        role=role,
        entity=CanonicalEntityRef(
            entity_id=entity_id,
            entity_type=entity_type,
            display_name=name,
        ),
    )


def _percent_intent(*, include_surgut: bool = True) -> AnalysisIntent:
    period = PeriodRef(date_from="2025-01-01", date_to="2026-01-01")
    incoming_entities = [
        _entity("balance", "balance-tomsk", "balance", "ГП ТГ Томск суточный баланс"),
    ]
    if include_surgut:
        incoming_entities.append(
            _entity("article", "article-surgut", "article", "от ТГ Сургут")
        )
    return AnalysisIntent(
        operation=Operation.CALCULATE,
        operands=[
            AnalysisOperand(
                operand_id="incoming_max",
                metric="incoming",
                aggregate_type="max",
                entities=incoming_entities,
                periods=[period],
            ),
            AnalysisOperand(
                operand_id="distribution_avg",
                metric="distribution",
                aggregate_type="avg",
                entities=[
                    _entity(
                        "balance", "balance-tomsk", "balance",
                        "ГП ТГ Томск суточный баланс",
                    ),
                    _entity(
                        "article", "article-distribution", "article", "Распределение"
                    ),
                ],
                periods=[period],
            ),
        ],
        formula=FormulaSpec(
            operator="percent_of",
            numerator_operand_id="incoming_max",
            denominator_operand_id="distribution_avg",
        ),
    )


def test_gate_routes_compound_standalone_to_interpreter() -> None:
    decision = RoutingEvidenceGate().route(
        QUERY,
        ContextContractV2(session_id="session"),
        explicit_businesses=[_record("ТГ Сургут"), _record("ТГ Томск")],
    )

    assert decision.invoke_interpreter
    assert decision.reason in {"semantic_operation", "compound_semantics"}


def test_gate_marks_mixed_business_geo_comparison_as_compound_not_scalar() -> None:
    decision = RoutingEvidenceGate().route(
        (
            "сравни объем поставок в Казань и объем собственных потребителей "
            "ГП ТГ Казань за май 2025"
        ),
        ContextContractV2(session_id="session"),
        explicit_businesses=[_record("ГП ТГ Казань")],
        explicit_geos=[_record("Казань")],
    )

    assert decision.invoke_interpreter
    assert decision.reason == "compound_semantics"


def test_gate_accepts_complete_percent_routing_evidence_and_plan() -> None:
    gate = RoutingEvidenceGate()
    intent = _percent_intent()

    gate.validate_bound_intent(
        intent,
        explicit_businesses=[_record("ТГ Сургут"), _record("ТГ Томск")],
    )
    gate.validate_plan(NativeMultiOperandPlanner().plan(intent))


def test_gate_rejects_dropped_explicit_business_before_database() -> None:
    with pytest.raises(EvidenceCompletenessError) as captured:
        RoutingEvidenceGate().validate_bound_intent(
            _percent_intent(include_surgut=False),
            explicit_businesses=[_record("ТГ Сургут"), _record("ТГ Томск")],
        )

    assert "operand_routing_evidence_incomplete:incoming_max" in captured.value.issues
    assert "explicit_business_not_bound:ТГ Сургут" in captured.value.issues


def test_gate_rejects_formula_operand_without_period() -> None:
    intent = _percent_intent()
    intent = intent.model_copy(
        update={
            "operands": [
                intent.operands[0].model_copy(update={"periods": []}),
                intent.operands[1],
            ]
        },
        deep=True,
    )

    with pytest.raises(EvidenceCompletenessError) as captured:
        RoutingEvidenceGate().validate_bound_intent(intent)

    assert "operand_period_missing:incoming_max" in captured.value.issues
    assert "formula_period_evidence_incomplete" in captured.value.issues
