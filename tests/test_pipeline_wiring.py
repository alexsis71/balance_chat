from __future__ import annotations

from decimal import Decimal

import pytest

from balance_chat.compat.envelope_translation import (
    EnvelopeTranslationError,
    PipelineEnvelopeTranslator,
)
from balance_chat.contracts import ContextContractV2, MetadataVersionRef
from balance_chat.processor import PipelineV2TurnProcessor, _deterministic_period_mutation
from balance_chat.contracts import AnalysisIntent, AnalysisOperand, ContextMutation, Operation, PeriodRef, TransitionOutcome
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
