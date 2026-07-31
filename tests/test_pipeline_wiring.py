from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from balance_chat.compat.envelope_translation import (
    EnvelopeTranslationError,
    PipelineEnvelopeTranslator,
)
from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ContextContractV2,
    ContextMutation,
    MetadataVersionRef,
    OperandEntityRef,
    Operation,
    PeriodRef,
    TransitionOutcome,
)
from balance_chat.processor import (
    PipelineV2TurnProcessor,
    _deterministic_period_mutation,
    _peer_destination_mentions,
)
from balance_chat.reducer import apply_context_transition


def _envelope():
    return {
        "status": "ok",
        "unit": "тыс. м3",
        "rows": [{"fact_value": "12.5", "unit": "тыс. м3"}],
        "debug": {
            "resolved_plan": {
                "_intent": {
                    "intent": "show",
                    "metric": "distribution",
                    "date_from": "2025-05-01",
                    "date_to": "2025-06-01",
                },
                "expressions": [
                    {
                        "canonical_metric": "distribution",
                        "balance": {"id": 1, "label": "Баланс"},
                        "geo": [
                            {"id": "geo:kazan", "label": "Казань"},
                            {"id": "geo:yaroslavl", "label": "Ярославль"},
                        ],
                    }
                ],
            }
        },
    }


def test_current_pipeline_multi_region_plan_becomes_separate_operands() -> None:
    intent = PipelineEnvelopeTranslator().intent(_envelope())

    assert len(intent.operands) == 2
    assert [
        operand.entities[-1].entity.display_name for operand in intent.operands
    ] == ["Казань", "Ярославль"]
    assert intent.periods[0].date_to.isoformat() == "2025-06-01"


def test_degraded_comparison_is_rejected_explicitly() -> None:
    envelope = _envelope()
    envelope["debug"]["resolved_plan"]["_intent"]["intent"] = "compare"
    envelope["debug"]["resolved_plan"]["operation"] = "show"
    envelope["debug"]["resolved_plan"]["expressions"][0]["geo"] = [
        {"id": "geo:kazan", "label": "Казань"}
    ]
    with pytest.raises(EnvelopeTranslationError, match="resolved_comparison_degraded"):
        PipelineEnvelopeTranslator().intent(envelope)


def test_peer_geo_adapter_uses_shared_semantics_not_degraded_expressions() -> None:
    envelope = _envelope()
    envelope["debug"]["resolved_plan"]["_intent"]["intent"] = "compare"
    envelope["debug"]["resolved_plan"]["operation"] = "show"
    envelope["debug"]["resolved_plan"]["expressions"][0]["geo"] = [
        {"id": "geo:kazan", "label": "Казань"}
    ]
    destinations = [
        CanonicalEntityRef(
            entity_id="geo:kazan",
            entity_type="geo_object",
            display_name="Казань",
        ),
        CanonicalEntityRef(
            entity_id="geo:yaroslavl",
            entity_type="geo_object",
            display_name="Ярославская область",
        ),
    ]

    intent = PipelineEnvelopeTranslator().peer_entity_intent(
        envelope,
        [
            [OperandEntityRef(role="destination", entity=item)]
            for item in destinations
        ],
    )

    assert intent.operation == Operation.COMPARE
    assert [
        operand.entities[0].entity.entity_id for operand in intent.operands
    ] == ["geo:kazan", "geo:yaroslavl"]
    assert intent.comparison.baseline_operand_id == "operand_1"
    assert intent.comparison.target_operand_id == "operand_2"


def test_fact_extraction_requires_explicit_unit() -> None:
    translator = PipelineEnvelopeTranslator()
    envelope = _envelope()
    intent = translator.intent(envelope)
    from balance_chat.planning import NativeMultiOperandPlanner

    task = NativeMultiOperandPlanner().plan(intent.model_copy(update={"operands": [intent.operands[0]]})).tasks[0]
    envelope["rows"][0].pop("unit")
    envelope.pop("unit")
    with pytest.raises(EnvelopeTranslationError, match="unit"):
        translator.fact(task, envelope)


def test_additive_scalar_rows_are_summed_with_provenance() -> None:
    translator = PipelineEnvelopeTranslator()
    envelope = _envelope()
    envelope["rows"] = [
        {"fact_value": "10", "unit": "тыс. м3"},
        {"fact_value": "2.5", "unit": "тыс. м3"},
    ]
    intent = translator.intent(envelope)
    from balance_chat.planning import NativeMultiOperandPlanner

    task = NativeMultiOperandPlanner().plan(
        intent.model_copy(update={"operands": [intent.operands[0]]})
    ).tasks[0]
    fact = translator.fact(task, envelope)

    assert fact.value == Decimal("12.5")
    assert len(fact.provenance) == 2


class RawRuntime:
    def execute_raw(self, *_args, **_kwargs):
        return _envelope()


class NeverCalled:
    def should_invoke(self, *_args, **_kwargs):
        return False


def test_standalone_processor_uses_current_resolved_plan_without_llm() -> None:
    processor = PipelineV2TurnProcessor(
        runtime=RawRuntime(),
        registry=object(),
        interpreter=object(),
        compiler=object(),
        executor=object(),
        policy=NeverCalled(),
    )
    state = ContextContractV2(
        session_id="session",
        metadata=MetadataVersionRef(
            bundle_id="sha256:" + "a" * 64,
            bundle_version="2026.07.6",
            schema_version="1.0",
        ),
    )
    processed = processor.process(
        state,
        message="Покажи поставки в Казань и Ярославль за май 2025",
        execute_db=False,
        clarification=None,
        request_id="request",
    )

    assert processed.outcome == "success"
    assert len(processed.mutation.replace_intent.operands) == 2
    assert processed.diagnostics["interpretation"]["source"] == "pipeline"


def test_explicit_compare_with_month_preserves_canonical_baseline() -> None:
    state = apply_context_transition(
        ContextContractV2(session_id="session"),
        ContextMutation(
            turn_id="turn-1",
            user_message="май",
            replace_intent=AnalysisIntent(
                operation=Operation.SHOW,
                operands=[AnalysisOperand(operand_id="supply", metric="distribution")],
                periods=[PeriodRef(date_from="2025-05-01", date_to="2025-06-01")],
            ),
        ),
        TransitionOutcome.SUCCESS,
    )
    mutation = _deterministic_period_mutation(
        state, "сравни с июнем 2025", "turn-2"
    )

    assert mutation.replace_intent.operation == Operation.COMPARE_PERIODS
    assert [
        (item.date_from.isoformat(), item.date_to.isoformat())
        for item in mutation.replace_intent.periods
    ] == [
        ("2025-05-01", "2025-06-01"),
        ("2025-06-01", "2025-07-01"),
    ]


def test_peer_entity_detection_does_not_turn_route_into_entity_comparison() -> None:
    assert _peer_destination_mentions(
        "Сравни поставки в Казань и Ярославль за май 2025"
    ) == ["Казань", "Ярославль"]
    assert _peer_destination_mentions(
        "Сравни транзит из Казани в Ярославль за май 2025"
    ) is None


def test_peer_articles_include_their_canonical_balances() -> None:
    articles = {
        "Казань": SimpleNamespace(
            article_id=1,
            balance_id=10,
            canonical_name="Казань",
            section="Распределение",
        ),
        "Ярославль": SimpleNamespace(
            article_id=2,
            balance_id=20,
            canonical_name="Ярославль",
            section="Распределение",
        ),
    }
    balances = {
        10: SimpleNamespace(balance_id=10, canonical_name="ГП ТГ Казань"),
        20: SimpleNamespace(balance_id=20, canonical_name="ГП ТГ Ухта"),
    }
    registry = SimpleNamespace(
        find_article_candidates=lambda value: (articles[value],),
        balance=lambda value: balances[value],
    )
    processor = object.__new__(PipelineV2TurnProcessor)
    processor.registry = registry

    peers = processor._matched_peer_entities(
        "Сравни поставки газа в Казань и Ярославль за май 2025"
    )

    assert [[item.role for item in operand] for operand in peers] == [
        ["balance", "article"],
        ["balance", "article"],
    ]
    assert [operand[0].entity.display_name for operand in peers] == [
        "ГП ТГ Казань",
        "ГП ТГ Ухта",
    ]
