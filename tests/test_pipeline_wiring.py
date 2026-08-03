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
    _deterministic_grouping_query,
    _deterministic_period_mutation,
    _distribution_own_consumers_mentions,
    _peer_destination_mentions,
)
from balance_chat.reducer import apply_context_transition
from balance_chat.service import TurnProcessingError
from balance_chat.interpretation import InterpretationError


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


def test_multi_source_geo_period_comparison_becomes_one_canonical_operand() -> None:
    envelope = _envelope()
    plan = envelope["debug"]["resolved_plan"]
    plan["_intent"].update(
        {
            "intent": "compare",
            "geo": "самарская область",
            "periods": [
                {"date_from": "2025-12-01", "date_to": "2026-03-01"},
                {"date_from": "2025-06-01", "date_to": "2025-09-01"},
            ],
        }
    )
    plan["operation"] = "compare_periods"
    plan["periods"] = [
        ["2025-12-01", "2026-03-01"],
        ["2025-06-01", "2025-09-01"],
    ]
    base = plan["expressions"][0]
    base["geo"] = [{"label": "самарская область"}]
    plan["expressions"] = [
        {**base, "balance": {"id": "BAL:1", "label": "Баланс 1"}},
        {**base, "balance": {"id": "BAL:2", "label": "Баланс 2"}},
        {**base, "balance": {"id": "BAL:3", "label": "Баланс 3"}},
    ]
    geo = SimpleNamespace(
        geo_id="GEO:samara-region",
        canonical_name="Самарская область",
    )

    intent = PipelineEnvelopeTranslator().intent(envelope, canonical_geos=[geo])

    assert intent.operation == Operation.COMPARE_PERIODS
    assert len(intent.operands) == 1
    assert intent.operands[0].entities[0].role == "destination"
    assert intent.operands[0].entities[0].entity.entity_id == "GEO:samara-region"
    assert [
        (period.date_from.isoformat(), period.date_to.isoformat())
        for period in intent.periods
    ] == [
        ("2025-12-01", "2026-03-01"),
        ("2025-06-01", "2025-09-01"),
    ]


def test_multi_source_show_becomes_one_canonical_geo_operand() -> None:
    envelope = _envelope()
    plan = envelope["debug"]["resolved_plan"]
    base = plan["expressions"][0]
    base["geo"] = [{"label": "Самарская область"}]
    plan["expressions"] = [
        {**base, "balance": {"id": "BAL:1", "label": "Баланс 1"}},
        {**base, "balance": {"id": "BAL:2", "label": "Баланс 2"}},
    ]
    geo = SimpleNamespace(
        geo_id="GEO:samara-region",
        canonical_name="Самарская область",
    )

    intent = PipelineEnvelopeTranslator().intent(envelope, canonical_geos=[geo])

    assert intent.operation == Operation.SHOW
    assert len(intent.operands) == 1
    assert intent.operands[0].entities[0].entity.entity_id == "GEO:samara-region"


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
    assert fact.source_row_count == 2


def test_public_native_fact_does_not_expose_provenance() -> None:
    from balance_chat.execution import NativeExecutionResult, TaskExecutionResult, ScalarFact
    from balance_chat.processor import _public_native_result

    native = NativeExecutionResult(
        operation=Operation.AGGREGATE,
        status="ok",
        task_results=[TaskExecutionResult(
            task_id="task_1",
            status="ok",
            fact=ScalarFact(
                task_id="task_1",
                value=Decimal("1"),
                unit="тыс. м3",
                label="Поставки",
                provenance=[{"raw": "secret"}],
            ),
            envelope={"status": "ok"},
        )],
    )

    assert "provenance" not in _public_native_result(native)["facts"][0]


def test_extremum_fact_preserves_winning_date() -> None:
    translator = PipelineEnvelopeTranslator()
    envelope = _envelope()
    envelope["rows"] = [
        {"fact_value": "10", "unit": "тыс. м3", "gas_day": "2025-05-03"},
        {"fact_value": "25", "unit": "тыс. м3", "gas_day": "2025-05-17"},
    ]
    intent = translator.intent(envelope)
    intent.operands[0].aggregate_type = "max"
    from balance_chat.planning import NativeMultiOperandPlanner
    task = NativeMultiOperandPlanner().plan(
        intent.model_copy(update={"operands": [intent.operands[0]]})
    ).tasks[0]

    fact = translator.fact(task, envelope)

    assert fact.value == Decimal("25")
    assert fact.extremum_at.isoformat() == "2025-05-17"
    assert fact.dimension == {"name": "gas_day", "value": "2025-05-17"}
    assert fact.periods == [{"date_from": "2025-05-01", "date_to": "2025-06-01"}]


class RawRuntime:
    def execute_raw(self, *_args, **_kwargs):
        return _envelope()


class NeverCalled:
    def should_invoke(self, *_args, **_kwargs):
        return False


class InvalidInterpreter:
    def interpret(self, **_kwargs):
        raise InterpretationError("invalid structured output")


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


def test_two_named_seasons_inherit_operand_and_context_year() -> None:
    state = apply_context_transition(
        ContextContractV2(session_id="session"),
        ContextMutation(
            turn_id="turn-1",
            user_message="Самара зимой и летом 2025",
            replace_intent=AnalysisIntent(
                operation=Operation.COMPARE_PERIODS,
                operands=[AnalysisOperand(operand_id="samara", metric="distribution")],
                periods=[
                    PeriodRef(date_from="2025-12-01", date_to="2026-03-01"),
                    PeriodRef(date_from="2025-06-01", date_to="2025-09-01"),
                ],
            ),
        ),
        TransitionOutcome.SUCCESS,
    )

    mutation = _deterministic_period_mutation(
        state, "сравни лето и осень", "turn-2"
    )

    assert mutation.replace_intent.operands[0].operand_id == "samara"
    assert [
        (item.date_from.isoformat(), item.date_to.isoformat())
        for item in mutation.replace_intent.periods
    ] == [
        ("2025-06-01", "2025-09-01"),
        ("2025-09-01", "2025-12-01"),
    ]


def test_peer_entity_detection_does_not_turn_route_into_entity_comparison() -> None:
    assert _peer_destination_mentions(
        "Сравни поставки в Казань и Ярославль за май 2025"
    ) == ["Казань", "Ярославль"]
    assert _peer_destination_mentions(
        "Сравни транзит из Казани в Ярославль за май 2025"
    ) is None


def test_mixed_metric_comparison_is_not_a_peer_city_comparison() -> None:
    message = (
        "Сравни объем поставок в Казань и объем собственных потребителей "
        "ГП ТГ Казань за май 2025"
    )

    assert _distribution_own_consumers_mentions(message) == (
        "Казань",
        "ГП ТГ Казань",
    )


def test_context_grouping_query_inherits_metric_and_period() -> None:
    state = apply_context_transition(
        ContextContractV2(session_id="session"),
        ContextMutation(
            turn_id="turn-1",
            user_message="по областям",
            replace_intent=AnalysisIntent(
                operation=Operation.SHOW,
                operands=[
                    AnalysisOperand(operand_id="regions", metric="distribution")
                ],
                periods=[PeriodRef(date_from="2025-04-01", date_to="2025-05-01")],
            ),
        ),
        TransitionOutcome.SUCCESS,
    )


def test_context_grouping_executes_canonical_group_contract() -> None:
    state = apply_context_transition(
        ContextContractV2(session_id="session"),
        ContextMutation(
            turn_id="turn-1",
            user_message="поставки по областям",
            replace_intent=AnalysisIntent(
                operation=Operation.SHOW,
                operands=[AnalysisOperand(operand_id="regions", metric="distribution")],
                periods=[PeriodRef(date_from="2025-04-01", date_to="2025-05-01")],
            ),
        ),
        TransitionOutcome.SUCCESS,
    )

    class GroupedRuntime(RawRuntime):
        def _import_pipeline_module(self, _name):
            raise ImportError

        def execute_raw(self, *_args, **_kwargs):
            return {
                "status": "ok",
                "interpretation": {"unit": "тыс. м3"},
                "rows": [
                    {"geo_id": "GEO:ul", "geo": "Ульяновская область", "fact_value": 10},
                    {"geo_id": "GEO:ul", "geo": "ульяновская обл", "fact_value": 5},
                ],
            }

    registry = SimpleNamespace(geo_objects=(), geo_groups=(), routes=())
    processor = PipelineV2TurnProcessor(
        runtime=GroupedRuntime(),
        registry=registry,
        interpreter=object(),
        compiler=object(),
        executor=object(),
        policy=NeverCalled(),
    )

    processed = processor.process(
        state,
        message="суммируй данные по областям",
        execute_db=True,
        clarification=None,
        request_id="request",
    )

    assert processed.mutation.replace_intent.operation == Operation.GROUP
    assert processed.response["facts"][0]["entity_id"] == "GEO:ul"
    assert processed.response["facts"][0]["value"] == "15"

    assert _deterministic_grouping_query(
        state, "суммируй данные по областям"
    ) == (
        "Суммируй поставки газа по областям за период с "
        "2025-04-01 по 2025-04-30"
    )


def test_invalid_interpretation_contract_maps_to_typed_turn_error() -> None:
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
    registry = SimpleNamespace(
        geo_objects=(),
        geo_groups=(),
        routes=(),
        manifest=SimpleNamespace(bundle_version="2026.07.7"),
    )
    processor = PipelineV2TurnProcessor(
        runtime=RawRuntime(),
        registry=registry,
        interpreter=InvalidInterpreter(),
        compiler=object(),
        executor=object(),
    )

    with pytest.raises(TurnProcessingError) as raised:
        processor.process(
            state,
            message="а повтори результат",
            execute_db=False,
            clarification=None,
            request_id="request",
        )

    assert raised.value.code == "interpretation_contract_invalid"


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


def test_mixed_metric_articles_are_bound_to_separate_operands() -> None:
    distribution = SimpleNamespace(
        article_id=1,
        balance_id=10,
        canonical_name="Казань",
        section="Распределение",
    )
    own_consumers = SimpleNamespace(
        article_id=2,
        balance_id=10,
        canonical_name="Собственные потребители",
        section="Распределение",
    )
    balance = SimpleNamespace(balance_id=10, canonical_name="ГП ТГ Казань")
    registry = SimpleNamespace(
        find_article_candidates=lambda value: (distribution,) if value == "Казань" else (),
        balance=lambda value: balance if value in {10, "ГП ТГ Казань"} else None,
        articles_for_balance=lambda value: (distribution, own_consumers),
    )
    processor = object.__new__(PipelineV2TurnProcessor)
    processor.registry = registry

    operands = processor._matched_distribution_own_consumers(
        "Сравни объем поставок в Казань и объем собственных потребителей "
        "ГП ТГ Казань за май 2025"
    )

    assert [operand[1].entity.display_name for operand in operands] == [
        "Казань",
        "Собственные потребители",
    ]
