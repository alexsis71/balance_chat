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
    ResultReference,
    TransitionOutcome,
)
from balance_chat.processor import (
    PipelineV2TurnProcessor,
    _deterministic_grouping_query,
    _deterministic_extremum_comparison,
    _extremum_comparison_summary,
    _deterministic_period_mutation,
    _distribution_own_consumers_mentions,
    _native_summary_envelope,
    _peer_destination_mentions,
    _public_pipeline_result,
    _standalone_translation_error,
    _standalone_facts,
)
from balance_chat.reducer import apply_context_transition
from balance_chat.service import TurnProcessingError
from balance_chat.interpretation import InterpretationError
from balance_chat.execution import NativeExecutionResult, ScalarFact, TaskExecutionResult


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


def test_rank_extremum_is_translated_to_typed_aggregate() -> None:
    envelope = _envelope()
    plan = envelope["debug"]["resolved_plan"]
    plan["_intent"].update(
        {
            "intent": "rank",
            "aggregate_type": "max",
            "date_from": "2025-04-01",
            "date_to": "2025-07-01",
        }
    )
    plan["operation"] = "aggregate"
    plan["expressions"][0].update(
        {
            "aggregate_type": "max",
            "geo": [],
            "article": {"id": "ART:1", "label": "ТГ Москва"},
        }
    )

    intent = PipelineEnvelopeTranslator().intent(envelope)

    assert intent.operation == Operation.AGGREGATE
    assert intent.operands[0].aggregate_type == "max"
    assert intent.operands[0].entities[-1].entity.display_name == "ТГ Москва"
    assert intent.periods[0].date_from.isoformat() == "2025-04-01"
    assert intent.periods[0].date_to.isoformat() == "2025-07-01"


def test_multi_query_is_preserved_as_explicit_composite_attempt() -> None:
    envelope = _envelope()
    envelope["status"] = "no_data"
    plan = envelope["debug"]["resolved_plan"]
    plan["_intent"].update({"intent": "multi_query", "metric": "composite"})
    plan["operation"] = "show"
    plan["expressions"] = []

    intent = PipelineEnvelopeTranslator().intent(envelope)

    assert intent.operation == Operation.MULTI_STEP
    assert intent.operands[0].metric == "composite"
    assert intent.periods[0].date_from.isoformat() == "2025-05-01"


def test_failed_plan_without_period_remains_a_typed_attempt() -> None:
    envelope = _envelope()
    envelope["status"] = "no_data"
    plan = envelope["debug"]["resolved_plan"]
    plan["_intent"].pop("date_from")
    plan["_intent"].pop("date_to")

    intent = PipelineEnvelopeTranslator().intent(envelope)

    assert intent.operation == Operation.SHOW
    assert intent.periods == []


def test_successful_plan_without_period_is_rejected() -> None:
    envelope = _envelope()
    plan = envelope["debug"]["resolved_plan"]
    plan["_intent"].pop("date_from")
    plan["_intent"].pop("date_to")

    with pytest.raises(EnvelopeTranslationError, match="canonical period"):
        PipelineEnvelopeTranslator().intent(envelope)


def test_missing_period_translation_has_specific_public_error_mapping() -> None:
    error = _standalone_translation_error(
        EnvelopeTranslationError("resolved plan has no canonical period")
    )

    assert error.code == "period_required"
    assert str(error) == "period not detected"


def test_n_way_comparison_is_preserved_as_multi_step() -> None:
    envelope = _envelope()
    plan = envelope["debug"]["resolved_plan"]
    plan["_intent"]["intent"] = "compare"
    plan["operation"] = "compare"
    plan["expressions"][0]["geo"] = [
        {"id": "geo:kazan", "label": "Казань"},
        {"id": "geo:samara", "label": "Самара"},
        {"id": "geo:yaroslavl", "label": "Ярославль"},
    ]

    intent = PipelineEnvelopeTranslator().intent(envelope)

    assert intent.operation == Operation.MULTI_STEP
    assert len(intent.operands) == 3


def test_multi_period_multi_source_comparison_is_preserved_as_multi_step() -> None:
    envelope = _envelope()
    plan = envelope["debug"]["resolved_plan"]
    plan["_intent"].update(
        {
            "intent": "compare",
            "periods": [
                {"date_from": "2025-01-01", "date_to": "2025-02-01"},
                {"date_from": "2025-02-01", "date_to": "2025-03-01"},
                {"date_from": "2025-03-01", "date_to": "2025-04-01"},
            ],
        }
    )
    plan["operation"] = "compare_periods"
    plan["periods"] = [
        ["2025-01-01", "2025-02-01"],
        ["2025-02-01", "2025-03-01"],
        ["2025-03-01", "2025-04-01"],
    ]
    base = {**plan["expressions"][0], "geo": [{"label": "Самарская область"}]}
    plan["expressions"] = [
        {**base, "balance": {"id": "BAL:1", "label": "Баланс 1"}},
        {**base, "balance": {"id": "BAL:2", "label": "Баланс 2"}},
    ]

    intent = PipelineEnvelopeTranslator().intent(envelope)

    assert intent.operation == Operation.MULTI_STEP
    assert len(intent.operands) == 2
    assert len(intent.periods) == 3


def test_pipeline_grouping_contract_is_preserved() -> None:
    envelope = _envelope()
    plan = envelope["debug"]["resolved_plan"]
    plan["group_by"] = ["geo"]
    plan["grouping"] = {
        "dimensions": ["geo"],
        "geo_group": "GGRP:russian-regions",
    }

    intent = PipelineEnvelopeTranslator().intent(envelope)

    assert intent.operation == Operation.GROUP
    assert intent.grouping[0].dimension == "geo"
    assert intent.grouping[0].canonical_group_id == "GGRP:russian-regions"
    assert intent.grouping[0].aggregate_type == "sum"
    assert len(intent.operands) == 2


def test_unknown_pipeline_grouping_dimension_is_rejected() -> None:
    envelope = _envelope()
    envelope["debug"]["resolved_plan"]["group_by"] = ["organization"]

    with pytest.raises(EnvelopeTranslationError, match="grouping dimensions"):
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


def test_scalar_fact_uses_business_label_instead_of_operand_id() -> None:
    from balance_chat.planning import NativeMultiOperandPlanner

    intent = AnalysisIntent(
        operation=Operation.AGGREGATE,
        operands=[AnalysisOperand(
            operand_id="operand_1",
            metric="distribution",
            aggregate_type="max",
            entities=[OperandEntityRef(
                role="article",
                entity=CanonicalEntityRef(
                    entity_id="2010000039766",
                    entity_type="article",
                    display_name="ТГ Н.-Новгород",
                ),
            )],
        )],
        periods=[PeriodRef(date_from="2025-04-01", date_to="2025-07-01")],
    )
    task = NativeMultiOperandPlanner().plan(intent).tasks[0]
    fact = PipelineEnvelopeTranslator().fact(task, {
        "status": "ok",
        "unit": "тыс. м3",
        "rows": [{
            "fact_value": "94602.041",
            "article_name": "    ТГ Н.-Новгород",
            "gas_day": "2025-04-10",
        }],
    })

    assert fact.label == "ТГ Н.-Новгород"
    assert fact.label != "operand_1"


def test_daily_balance_unit_overrides_generic_result_envelope_unit() -> None:
    from balance_chat.planning import NativeMultiOperandPlanner

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
                        entity_id="2010000039953",
                        entity_type="balance",
                        display_name="ГП ТГ Москва суточный баланс",
                    ),
                ),
                OperandEntityRef(
                    role="article",
                    entity=CanonicalEntityRef(
                        entity_id="2010000039766",
                        entity_type="article",
                        display_name="ТГ Н.-Новгород",
                    ),
                ),
            ],
        )],
        periods=[PeriodRef(date_from="2025-04-01", date_to="2025-07-01")],
    )
    task = NativeMultiOperandPlanner().plan(intent).tasks[0]

    fact = PipelineEnvelopeTranslator().fact(task, {
        "status": "ok",
        "interpretation": {"unit": "млн м3"},
        "rows": [{"fact_value": "2308.253", "article_name": "ТГ Н.-Новгород"}],
    })

    assert fact.unit == "тыс. м3"
    assert fact.value == Decimal("2308.253")


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


def test_public_result_suppresses_deterministic_execution_phrase() -> None:
    result = _public_pipeline_result(
        {
            "status": "ok",
            "rows": [{"fact_value": 1}],
            "summary": {
                "title": "Результат готов: 1 строк",
                "text": "Операция aggregate выполнена через deterministic execution layer.",
            },
        }
    )

    assert result["summary"] == {"title": "Результат готов: 1 строк"}


def test_native_summary_receives_exact_extremum_date() -> None:
    from balance_chat.execution import NativeExecutionResult, ScalarFact, TaskExecutionResult
    from balance_chat.planning import NativeMultiOperandPlanner

    intent = AnalysisIntent(
        operation=Operation.AGGREGATE,
        operands=[AnalysisOperand(
            operand_id="transport",
            metric="distribution",
            aggregate_type="max",
        )],
        periods=[PeriodRef(date_from="2025-04-01", date_to="2025-07-01")],
    )
    plan = NativeMultiOperandPlanner().plan(intent)
    native = NativeExecutionResult(
        operation=Operation.AGGREGATE,
        status="ok",
        task_results=[TaskExecutionResult(
            task_id="task_1",
            status="ok",
            fact=ScalarFact(
                task_id="task_1",
                value=Decimal("367869.34"),
                unit="тыс. м3",
                label="ТГ Москва",
                periods=[{"date_from": "2025-04-01", "date_to": "2025-07-01"}],
                extremum_at="2025-04-10",
            ),
            envelope={"status": "ok"},
        )],
    )

    envelope = _native_summary_envelope(native, plan, "Когда был максимум?", intent)

    assert envelope["rows"] == [{
        "label": "ТГ Москва",
        "fact_value": "367869.34",
        "unit": "тыс. м3",
        "aggregate_type": "max",
        "gas_day": "2025-04-10",
        "date_from": "2025-04-01",
        "date_to": "2025-07-01",
    }]


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


class SummaryFlagRuntime(RawRuntime):
    def __init__(self):
        self.summary_calls = 0

    def summarize_envelope(self, envelope, **_kwargs):
        self.summary_calls += 1
        return {**envelope, "summary": {
            "generated_by": "llm",
            "title": "Максимум поставок",
            "text": "Максимум достигнут 10 апреля 2025 года.",
        }}


class NeverCalled:
    def should_invoke(self, *_args, **_kwargs):
        return False


class InvalidInterpreter:
    def interpret(self, **_kwargs):
        raise InterpretationError("invalid structured output")


class _ReverseRegistry:
    def __init__(self, *, reverse_exists: bool = True):
        self.geo_objects = ()
        self.geo_groups = ()
        self.routes = ()
        self.source = SimpleNamespace(
            balance_id=10,
            canonical_name="ГП ТГ Н.Новгород суточный баланс",
            aliases=("тг н новгород",),
        )
        self.destination = SimpleNamespace(
            balance_id=20,
            canonical_name="ГП ТГ Москва суточный баланс",
            aliases=("тг москва",),
        )
        self.current_article = SimpleNamespace(
            article_id=11,
            balance_id=10,
            canonical_name="ТГ Москва",
            aliases=(),
            section="Распределение",
            path=("Распределение", "За пределы", "ТГ Москва"),
        )
        self.reverse_article = SimpleNamespace(
            article_id=21,
            balance_id=20,
            canonical_name="ТГ Н.-Новгород",
            aliases=(),
            section="Распределение",
            path=("Распределение", "За пределы", "ТГ Н.-Новгород"),
        )
        self.reverse_exists = reverse_exists

    def balance(self, value):
        if value == 10 or "н новгород" in _test_normalize(value):
            return self.source
        if value == 20 or "москва" in _test_normalize(value):
            return self.destination
        return None

    def article(self, value):
        return self.current_article if value == 11 else None

    def find_article_candidates(self, value, *, balance_id=None):
        if (
            self.reverse_exists
            and balance_id == 20
            and "н новгород" in _test_normalize(value)
        ):
            return (self.reverse_article,)
        return ()


def _test_normalize(value):
    import re
    return " ".join(re.sub(r"[^0-9a-zа-я]+", " ", str(value).casefold()).split())


class _NoInterpretationRuntime:
    def _import_pipeline_module(self, _name):
        raise ImportError


class _ReverseExecutor:
    def __init__(self):
        self.calls = 0

    def execute(self, plan, **_kwargs):
        self.calls += 1
        task = plan.tasks[0]
        return NativeExecutionResult(
            operation=plan.operation,
            status="ok",
            task_results=[TaskExecutionResult(
                task_id=task.task_id,
                status="ok",
                fact=ScalarFact(
                    task_id=task.task_id,
                    value=Decimal("94602.041"),
                    unit="тыс. м3",
                    label="ТГ Н.-Новгород",
                ),
                envelope={"status": "ok"},
            )],
        )


def _route_state():
    return apply_context_transition(
        ContextContractV2(session_id="session"),
        ContextMutation(
            turn_id="turn-1",
            user_message="транспорт из Новгорода в Москву",
            replace_intent=AnalysisIntent(
                operation=Operation.AGGREGATE,
                operands=[AnalysisOperand(
                    operand_id="operand_1",
                    metric="distribution",
                    aggregate_type="max",
                    entities=[
                        OperandEntityRef(
                            role="balance",
                            entity=CanonicalEntityRef(
                                entity_id="BAL:10",
                                entity_type="balance",
                                display_name="ГП ТГ Н.Новгород суточный баланс",
                            ),
                        ),
                        OperandEntityRef(
                            role="article",
                            entity=CanonicalEntityRef(
                                entity_id="ART:11",
                                entity_type="article",
                                display_name="ТГ Москва",
                            ),
                        ),
                    ],
                )],
                periods=[PeriodRef(date_from="2025-04-01", date_to="2025-07-01")],
            ),
        ),
        TransitionOutcome.SUCCESS,
    )


@pytest.mark.parametrize(
    "message",
    ["и наоборот", "и наоборот из ТГ Москва в ТГ Н. Новгород"],
)
def test_reverse_followup_resolves_opposite_balance_article_without_qwen(message) -> None:
    registry = _ReverseRegistry()
    executor = _ReverseExecutor()
    processor = PipelineV2TurnProcessor(
        runtime=_NoInterpretationRuntime(),
        registry=registry,
        interpreter=object(),
        compiler=object(),
        executor=executor,
    )

    processed = processor.process(
        _route_state(),
        message=message,
        execute_db=True,
        clarification=None,
        request_id="request",
    )

    assert processed.outcome == TransitionOutcome.SUCCESS
    assert executor.calls == 1
    assert processed.diagnostics["interpretation"]["mode"] == "deterministic_reverse"
    entities = {
        item.role: item.entity.display_name
        for item in processed.mutation.replace_intent.operands[0].entities
    }
    assert entities == {
        "balance": "ГП ТГ Москва суточный баланс",
        "article": "ТГ Н.-Новгород",
    }
    assert processed.response["facts"][0]["label"] == "ТГ Н.-Новгород"


def test_reverse_without_curated_relation_is_no_data_before_execution() -> None:
    executor = _ReverseExecutor()
    processor = PipelineV2TurnProcessor(
        runtime=_NoInterpretationRuntime(),
        registry=_ReverseRegistry(reverse_exists=False),
        interpreter=object(),
        compiler=object(),
        executor=executor,
    )

    processed = processor.process(
        _route_state(),
        message="и наоборот",
        execute_db=True,
        clarification=None,
        request_id="request",
    )

    assert processed.outcome == TransitionOutcome.NO_DATA
    assert executor.calls == 0
    assert processed.response["warnings"][0]["code"] == "reverse_relation_not_found"
    assert processed.diagnostics["execution"]["task_count"] == 0


def test_compare_with_minimum_inherits_exact_route_and_period() -> None:
    state = _route_state()

    mutation = _deterministic_extremum_comparison(
        state,
        "сравни с минимумом за тот же период",
        "turn-2",
    )

    assert mutation is not None
    intent = mutation.replace_intent
    assert intent.operation == Operation.COMPARE
    assert [item.aggregate_type for item in intent.operands] == ["max", "min"]
    assert intent.periods == state.active_dialog_scope.intent.periods
    assert intent.operands[0].entities == intent.operands[1].entities
    assert intent.operands[0].entities == state.active_dialog_scope.intent.operands[0].entities
    assert intent.comparison.baseline_operand_id == "extremum_baseline"
    assert intent.comparison.target_operand_id == "extremum_target"


def test_extremum_comparison_does_not_treat_minimum_as_metadata_entity() -> None:
    mutation = _deterministic_extremum_comparison(
        _route_state(),
        "Сравни с минимальным значением",
        "turn-2",
    )

    assert mutation is not None
    assert all(
        entity.entity.display_name != "минимум"
        for operand in mutation.replace_intent.operands
        for entity in operand.entities
    )


def test_extremum_comparison_fallback_uses_typed_percent_base_and_dates() -> None:
    from balance_chat.execution import ComparisonResult

    intent = _deterministic_extremum_comparison(
        _route_state(),
        "сравни с минимумом за тот же период",
        "turn-2",
    ).replace_intent
    native = NativeExecutionResult(
        operation=Operation.COMPARE,
        status="ok",
        task_results=[
            TaskExecutionResult(
                task_id="task_1",
                status="ok",
                fact=ScalarFact(
                    task_id="task_1",
                    value=Decimal("367869.336"),
                    unit="тыс. м3",
                    label="ТГ Москва",
                    extremum_at="2025-04-10",
                ),
                envelope={"status": "ok"},
            ),
            TaskExecutionResult(
                task_id="task_2",
                status="ok",
                fact=ScalarFact(
                    task_id="task_2",
                    value=Decimal("253845.571"),
                    unit="тыс. м3",
                    label="ТГ Москва",
                    extremum_at="2025-04-20",
                ),
                envelope={"status": "ok"},
            ),
        ],
        comparison=ComparisonResult(
            baseline_task_id="task_1",
            target_task_id="task_2",
            baseline_value=Decimal("367869.336"),
            target_value=Decimal("253845.571"),
            delta=Decimal("-114023.765"),
            percent_change=Decimal("-30.9957242535703"),
            unit="тыс. м3",
        ),
    )

    summary = _extremum_comparison_summary(
        native,
        intent,
        upstream_summary={"generated_by": "deterministic_fallback"},
    )

    assert summary["generated_by"] == "deterministic_contract"
    assert "10.04.2025" in summary["text"]
    assert "20.04.2025" in summary["text"]
    assert "114\u00a0023,77" in summary["text"]
    assert "31,00%" in summary["text"]
    assert "44,92%" not in summary["text"]


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


def test_standalone_db_result_requests_one_final_llm_summary() -> None:
    runtime = SummaryFlagRuntime()
    processor = PipelineV2TurnProcessor(
        runtime=runtime,
        registry=object(),
        interpreter=object(),
        compiler=object(),
        executor=object(),
        policy=NeverCalled(),
    )
    state = ContextContractV2(session_id="session")

    processed = processor.process(
        state,
        message="Когда был максимум поставок за май 2025",
        execute_db=True,
        clarification=None,
        request_id="request",
    )

    assert runtime.summary_calls == 1
    assert processed.response["summary"]["generated_by"] == "llm"
    assert processed.diagnostics["summary"]["generated_by"] == "llm"


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

    assert _deterministic_grouping_query(
        state, "суммируй данные по областям"
    ) == (
        "Суммируй поставки газа по областям за период с "
        "2025-04-01 по 2025-04-30"
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


def test_context_grouping_reuses_current_successful_result_reference() -> None:
    state = apply_context_transition(
        ContextContractV2(session_id="session"),
        ContextMutation(
            turn_id="turn-1",
            user_message="поставки по областям",
            replace_intent=AnalysisIntent(
                operation=Operation.SHOW,
                operands=[AnalysisOperand(
                    operand_id="regions",
                    metric="distribution",
                    unit="тыс. м3",
                )],
                periods=[PeriodRef(date_from="2025-04-01", date_to="2025-05-01")],
            ),
        ),
        TransitionOutcome.SUCCESS,
        result=ResultReference(
            turn_id="turn-1",
            status=TransitionOutcome.SUCCESS,
            row_count=2,
            facts=[
                {"article_scope": "Ульяновская обл.", "fact_value": 10},
                {"article_scope": "Ульяновская", "fact_value": 5},
            ],
        ),
    )

    class NoRepeatRuntime(RawRuntime):
        def _import_pipeline_module(self, name):
            if name == "pipeline_v2.nlp_ru":
                return SimpleNamespace(normalize_query_lemmas=lambda value: value.casefold().rstrip("."))
            raise ImportError

        def execute_raw(self, *_args, **_kwargs):
            raise AssertionError("current result must be grouped without repeating DB execution")

    geo = SimpleNamespace(
        geo_id="geo:ulyanovsk",
        canonical_name="Ульяновская область",
        aliases=("Ульяновская обл.", "Ульяновская"),
    )
    registry = SimpleNamespace(geo_objects=(geo,), geo_groups=(), routes=())
    processor = PipelineV2TurnProcessor(
        runtime=NoRepeatRuntime(),
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

    assert processed.response["facts"][0]["canonical_name"] == "Ульяновская область"
    assert processed.response["facts"][0]["value"] == "15"
    assert processed.diagnostics["execution"]["grouping_source"] == "result_reference"


def test_standalone_result_reference_rows_retain_interpretation_unit() -> None:
    facts = _standalone_facts(
        {
            "interpretation": {"unit": "тыс.м3"},
            "rows": [{"article_scope": "Самарская обл.", "fact_value": 12}],
        }
    )

    assert facts == [
        {
            "article_scope": "Самарская обл.",
            "fact_value": 12,
            "unit": "тыс.м3",
        }
    ]

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
