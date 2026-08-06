from __future__ import annotations

from decimal import Decimal
import re
from types import SimpleNamespace

import pytest

from balance_chat.binding import InterpretationMutationCompiler, RegistryEntityBinder
from balance_chat.compat.envelope_translation import (
    EnvelopeTranslationError,
    PipelineEnvelopeTranslator,
)
from balance_chat.compat.pipeline_runtime import PipelineRuntime
from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ComparisonSpec,
    ContextContractV2,
    ContextMutation,
    GroupingSpec,
    InterpretationDecision,
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
    _explicit_single_day_period,
    _native_summary_envelope,
    _normalize_series_reduction_intent,
    _comparison_contract_summary,
    _derived_contract_summary,
    _period_comparison_contract_summary,
    _ranking_contract_summary,
    _safe_execution_evidence,
    _peer_destination_mentions,
    _public_native_result,
    _public_pipeline_result,
    _standalone_translation_error,
    _standalone_facts,
    _single_operand_attempt,
    _is_full_balance_show,
)
from balance_chat.reducer import apply_context_transition
from balance_chat.service import TurnProcessingError
from balance_chat.interpretation import InterpretationError
from balance_chat.execution import (
    NativeExecutionResult,
    NativeExecutor,
    PipelineScalarTaskRunner,
    ScalarFact,
    TaskExecutionResult,
)


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


def test_safe_execution_evidence_exposes_contract_without_raw_sql_or_rows() -> None:
    evidence = _safe_execution_evidence({
        "debug": {
            "sql_function": "api.show_balance_day",
            "params": {
                "balance_id": 2010000039953,
                "day": "2025-06-25",
                "dsn": "must-not-leak",
            },
            "rendered_sql": "select secret",
            "raw_rows": [{"secret": True}],
        }
    })

    assert evidence == {
        "sql_function": "api.show_balance_day",
        "sql_params": {"balance_id": 2010000039953, "day": "2025-06-25"},
    }


def test_canonical_balance_day_runtime_uses_postgres_api_without_planner() -> None:
    calls = []

    class Client:
        def __init__(self, dsn):
            assert dsn == "postgresql://configured"

        def show_balance_day(self, balance_id, day):
            calls.append((balance_id, day))
            return [{
                "article_id": 2,
                "gas_day": "2025-06-25",
                "fact_value": Decimal("12.5"),
            }]

    runtime = object.__new__(PipelineRuntime)
    runtime._import_pipeline_module = lambda name: (
        SimpleNamespace(PostgresApiClient=Client)
        if name == "mcp_postgres_api_server"
        else (_ for _ in ()).throw(AssertionError(name))
    )
    runtime.pipeline_runtime = lambda: SimpleNamespace(
        database_dsn="postgresql://configured"
    )

    envelope = runtime.execute_balance_day(
        balance_id=1,
        day="2025-06-25",
        request_id="safe-request",
    )

    assert calls == [(1, "2025-06-25")]
    assert envelope["rows"][0]["fact_value"] == "12.5"
    assert envelope["debug"] == {
        "sql_function": "api.show_balance_day",
        "params": {"balance_id": 1, "day": "2025-06-25"},
    }


def test_volume_contract_removes_plan_fields_and_forces_canonical_unit() -> None:
    from balance_chat.processor import _public_result_row

    assert _public_result_row({
        "article_name": "Распределение",
        "plan": "10",
        "plan_value": "10",
        "fact_value": "12",
        "unit": "млн м3",
    }) == {
        "article_name": "Распределение",
        "fact_value": "12",
        "unit": "тыс. м3",
    }


@pytest.mark.parametrize(
    ("query", "date_from", "date_to"),
    [
        ("за 25.06.2025", "2025-06-25", "2025-06-26"),
        ("за 25/06/2025", "2025-06-25", "2025-06-26"),
        ("за 2025-06-25", "2025-06-25", "2025-06-26"),
        ("за 25 июня 2025 года", "2025-06-25", "2025-06-26"),
    ],
)
def test_explicit_single_day_period_uses_exclusive_end(query, date_from, date_to) -> None:
    period = _explicit_single_day_period(query)

    assert period is not None
    assert period.date_from.isoformat() == date_from
    assert period.date_to.isoformat() == date_to


def test_explicit_single_day_period_rejects_invalid_or_multiple_dates() -> None:
    assert _explicit_single_day_period("за 31.02.2025") is None
    assert _explicit_single_day_period("с 25.06.2025 по 26.06.2025") is None


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


def test_month_bucket_rank_is_translated_to_typed_aggregate() -> None:
    envelope = _envelope()
    plan = envelope["debug"]["resolved_plan"]
    plan["_intent"].update(
        {
            "query": "в каком месяце был максимум поступления в ТГ Томск от ТГ Сургут в 2025",
            "intent": "rank",
            "metric": "incoming",
            "aggregate_type": "max",
            "date_from": "2025-01-01",
            "date_to": "2026-01-01",
            "period_grain": "month",
        }
    )
    plan["operation"] = "show"
    plan["expressions"][0].update(
        {
            "canonical_metric": "incoming",
            "aggregate_type": "sum",
            "geo": [],
            "article": {"id": "ART:1", "label": "от ТГ Сургут"},
        }
    )

    intent = PipelineEnvelopeTranslator().intent(envelope)

    assert intent.operation == Operation.AGGREGATE
    assert intent.operands[0].aggregate_type == "max"
    assert intent.grain == "month"


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


def test_fact_extraction_uses_domain_unit_when_envelope_omits_it() -> None:
    translator = PipelineEnvelopeTranslator()
    envelope = _envelope()
    intent = translator.intent(envelope)
    from balance_chat.planning import NativeMultiOperandPlanner

    task = NativeMultiOperandPlanner().plan(intent.model_copy(update={"operands": [intent.operands[0]]})).tasks[0]
    envelope["rows"][0].pop("unit")
    envelope.pop("unit")
    fact = translator.fact(task, envelope)

    assert fact.unit == "тыс. м3"


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
            "rows": [{
                "balance_id": "2010000040110",
                "article_id": "2010000039983",
                "balance": "ГП ТГ Н.Новгород суточный баланс",
                "article_name": "ТГ Москва",
                "fact_value": 1,
            }],
            "summary": {
                "title": "Результат готов: 1 строк",
                "text": "Операция aggregate выполнена через deterministic execution layer.",
            },
            "warnings": [
                "unified selected balance-level intent over article-level analyzer candidate",
                "unified normalized article_policy from empty to balance_only",
                {"code": "data_quality", "message": "Проверено не за все дни"},
            ],
        }
    )

    assert result["summary"] == {"title": "Результат готов: 1 строк"}
    assert result["rows"] == [{
        "balance": "ГП ТГ Н.Новгород суточный баланс",
        "article_name": "ТГ Москва",
        "fact_value": 1,
        "unit": "тыс. м3",
    }]
    assert result["warnings"] == [
        {"code": "data_quality", "message": "Проверено не за все дни"}
    ]


def test_standalone_full_balance_uses_exact_date_and_dedicated_execution() -> None:
    balance = SimpleNamespace(
        balance_id=2010000039953,
        canonical_name="ГП ТГ Москва суточный баланс",
        aliases=("гп тг москва", "тг москва"),
    )

    class FullBalanceRuntime:
        def __init__(self):
            self.execute_calls = 0

        def _import_pipeline_module(self, name):
            if name == "pipeline_v2.nlp_ru":
                return SimpleNamespace(normalize_query_lemmas=_test_normalize)
            if name == "pipeline_v2.query_analyzer":
                return SimpleNamespace(analyze_query=lambda _query: SimpleNamespace(
                    intent="show",
                    metric="balance",
                    article_policy="balance_only",
                ))
            raise ImportError(name)

        def execute_raw(self, *_args, **_kwargs):
            raise AssertionError("full balance must not execute the legacy standalone path")

        def execute(self, _query, intent, **kwargs):
            self.execute_calls += 1
            assert kwargs["apply_summary"] is False
            assert intent.periods == [PeriodRef(
                date_from="2025-06-25",
                date_to="2025-06-26",
            )]
            return {
                "status": "ok",
                "rows": [
                    {
                        "article_name": "Ресурсы",
                        "article_indent": 0,
                        "gas_day": "2025-06-25",
                        "fact_value": "323248.059",
                    },
                    {
                        "article_name": "  Поступление",
                        "article_indent": 2,
                        "gas_day": "2025-06-25",
                        "fact_value": "323248.059",
                    },
                ],
                "debug": {
                    "sql_function": "api.show_balance_day",
                    "params": {
                        "balance_id": 2010000039953,
                        "day": "2025-06-25",
                    },
                },
            }

    class Registry:
        geo_objects = ()
        geo_groups = ()
        routes = ()
        manifest = SimpleNamespace(bundle_version="2026.08.1")

        @staticmethod
        def balance(value):
            return balance if "москва" in _test_normalize(value) else None

    runtime = FullBalanceRuntime()
    registry = Registry()
    processor = PipelineV2TurnProcessor(
        runtime=runtime,
        registry=registry,
        interpreter=InvalidInterpreter(),
        compiler=InterpretationMutationCompiler(RegistryEntityBinder(registry)),
        executor=object(),
        policy=NeverCalled(),
    )

    processed = processor.process(
        ContextContractV2(session_id="session"),
        message="Покажи баланс ГП ТГ Москва за 25.06.2025.",
        execute_db=True,
        clarification=None,
        request_id="gq-001-regression",
    )

    assert runtime.execute_calls == 1
    assert processed.mutation.replace_intent.periods == [PeriodRef(
        date_from="2025-06-25",
        date_to="2025-06-26",
    )]
    assert processed.diagnostics["interpretation"]["mode"] == "deterministic_full_balance"
    assert processed.diagnostics["execution"] == {
        "status": "ok",
        "task_count": 1,
        "layer": "unified_balance_level",
        "source_execution_count": 1,
        "row_count": 2,
        "elapsed_ms": processed.diagnostics["execution"]["elapsed_ms"],
        "sql_function": "api.show_balance_day",
        "sql_params": {"balance_id": 2010000039953, "day": "2025-06-25"},
    }
    assert [row["unit"] for row in processed.response["rows"]] == [
        "тыс. м3", "тыс. м3",
    ]


@pytest.mark.parametrize(
    ("section", "article_id", "expected_names", "expected_indents"),
    [
        (
            "Ресурсы",
            2010000039710,
            ["Ресурсы", "Поступление", "От ТГ Н.Новгород"],
            [0, 2, 4],
        ),
        (
            "Поступление",
            2010000039711,
            ["Поступление", "От ТГ Н.Новгород"],
            [0, 2],
        ),
        (
            "Распределение",
            2010000039755,
            ["Распределение", "Собств. нужды и потери"],
            [0, 2],
        ),
    ],
)
def test_exact_day_balance_section_uses_one_full_snapshot_and_filters_metadata_subtree(
    section, article_id, expected_names, expected_indents
) -> None:
    balance = SimpleNamespace(
        balance_id=2010000039953,
        canonical_name="ГП ТГ Москва суточный баланс",
        aliases=("гп тг москва", "тг москва"),
    )
    articles = (
        SimpleNamespace(
            article_id=2010000039710,
            balance_id=balance.balance_id,
            canonical_name="Ресурсы",
            aliases=(),
            path=("Ресурсы",),
        ),
        SimpleNamespace(
            article_id=2010000039711,
            balance_id=balance.balance_id,
            canonical_name="Поступление",
            aliases=(),
            path=("Ресурсы", "Поступление"),
        ),
        SimpleNamespace(
            article_id=2010000039714,
            balance_id=balance.balance_id,
            canonical_name="От ТГ Н.Новгород",
            aliases=(),
            path=("Ресурсы", "Поступление", "От ТГ Н.Новгород"),
        ),
        SimpleNamespace(
            article_id=2010000039753,
            balance_id=balance.balance_id,
            canonical_name="Запас газа",
            aliases=(),
            path=("Запас газа",),
        ),
        SimpleNamespace(
            article_id=2010000039755,
            balance_id=balance.balance_id,
            canonical_name="Распределение",
            aliases=(),
            path=("Распределение",),
        ),
        SimpleNamespace(
            article_id=2010000039756,
            balance_id=balance.balance_id,
            canonical_name="Собств. нужды и потери",
            aliases=(),
            path=("Распределение", "Собств. нужды и потери"),
        ),
    )

    class SectionRuntime:
        def __init__(self):
            self.execute_calls = 0

        def _import_pipeline_module(self, name):
            if name == "pipeline_v2.nlp_ru":
                return SimpleNamespace(normalize_query_lemmas=_test_normalize)
            if name == "pipeline_v2.query_analyzer":
                return SimpleNamespace(analyze_query=lambda _query: SimpleNamespace(
                    intent="show",
                    metric="incoming",
                    article_policy=None,
                    article_text=None,
                ))
            raise ImportError(name)

        def execute_raw(self, *_args, **_kwargs):
            raise AssertionError("section snapshot must not use legacy standalone")

        def execute(self, _query, source_intent, **kwargs):
            self.execute_calls += 1
            assert kwargs["apply_summary"] is False
            assert source_intent.operands[0].metric == "balance"
            assert [item.role for item in source_intent.operands[0].entities] == ["balance"]
            return {
                "status": "ok",
                "rows": [
                    {"article_id": item.article_id,
                     "article_name": " " * (2 * (len(item.path) - 1)) + item.canonical_name,
                     "article_indent": 2 * (len(item.path) - 1),
                     "gas_day": "2025-06-25",
                     "fact_value": str(index + 1)}
                    for index, item in enumerate(articles)
                ],
                "debug": {
                    "sql_function": "api.show_balance_day",
                    "params": {"balance_id": balance.balance_id, "day": "2025-06-25"},
                },
            }

    class Registry:
        geo_objects = ()
        geo_groups = ()
        routes = ()
        manifest = SimpleNamespace(bundle_version="2026.08.1")

        @staticmethod
        def balance(value):
            return balance if "москва" in _test_normalize(value) else None

        @staticmethod
        def articles_for_balance(value):
            return articles if int(value) == balance.balance_id else ()

        @staticmethod
        def article(value):
            return next((item for item in articles if item.article_id == int(value)), None)

    runtime = SectionRuntime()
    registry = Registry()
    processor = PipelineV2TurnProcessor(
        runtime=runtime,
        registry=registry,
        interpreter=InvalidInterpreter(),
        compiler=InterpretationMutationCompiler(RegistryEntityBinder(registry)),
        executor=object(),
        policy=NeverCalled(),
    )

    processed = processor.process(
        ContextContractV2(session_id="session"),
        message=f"Покажи раздел {section} баланса ГП ТГ Москва за 25.06.2025.",
        execute_db=True,
        clarification=None,
        request_id=f"section-{article_id}",
    )

    assert runtime.execute_calls == 1
    assert processed.mutation.replace_intent.operands[0].metric == "balance_section"
    assert processed.mutation.replace_intent.operands[0].entities[1].entity.entity_id == (
        f"ART:{article_id}"
    )
    assert [row["article_name"].strip() for row in processed.response["rows"]] == expected_names
    assert [row["article_indent"] for row in processed.response["rows"]] == expected_indents
    assert processed.diagnostics["execution"]["layer"] == "unified_balance_section"
    assert processed.diagnostics["execution"]["source_execution_count"] == 1


@pytest.mark.parametrize(
    ("message", "metric", "article_id", "direction_role", "direction_type"),
    [
        (
            "Покажи поступление в ГП ТГ Москва от ГП ТГ Н.Новгород за 25.06.2025.",
            "incoming", 2010000039714, "source", "balance",
        ),
        (
            "Покажи распределение из ГП ТГ Москва в ГП ТГ Н.Новгород за 25.06.2025.",
            "distribution", 2010000039766, "destination", "balance",
        ),
        (
            "Покажи распределение из ГП ТГ Москва в Московскую область за 25.06.2025.",
            "distribution", 2010000039808, "destination", "geo_object",
        ),
    ],
)
def test_exact_day_directed_flow_binds_typed_roles_and_one_canonical_article(
    message, metric, article_id, direction_role, direction_type
) -> None:
    moscow = SimpleNamespace(
        balance_id=2010000039953,
        canonical_name="ГП ТГ Москва суточный баланс",
        aliases=("гп тг москва", "тг москва"),
    )
    novgorod = SimpleNamespace(
        balance_id=2010000040110,
        canonical_name="ГП ТГ Н.Новгород суточный баланс",
        aliases=("гп тг н новгород", "тг н новгород"),
    )
    moscow_region = SimpleNamespace(
        geo_id="geo:cdb5df36713e7e382d70",
        canonical_name="московская область",
        aliases=("московская обл", "подмосковье"),
    )
    articles = (
        SimpleNamespace(
            article_id=2010000039714,
            balance_id=moscow.balance_id,
            canonical_name="От ТГ Н.Новгород",
            aliases=(),
            section="Ресурсы",
            path=("Ресурсы", "Поступление", "От ТГ Н.Новгород"),
        ),
        SimpleNamespace(
            article_id=2010000039766,
            balance_id=moscow.balance_id,
            canonical_name="ТГ Н.-Новгород",
            aliases=(),
            section="Распределение",
            path=("Распределение", "За пределы", "ТГ Н.-Новгород"),
        ),
        SimpleNamespace(
            article_id=2010000039808,
            balance_id=moscow.balance_id,
            canonical_name="Московская обл.",
            aliases=(),
            section="Распределение",
            path=("Распределение", "Собственные потребители", "Московская обл."),
        ),
    )

    class DirectedRuntime:
        def __init__(self):
            self.execute_calls = 0

        def _import_pipeline_module(self, name):
            if name == "pipeline_v2.nlp_ru":
                return SimpleNamespace(normalize_query_lemmas=_test_normalize)
            if name == "pipeline_v2.query_analyzer":
                return SimpleNamespace(analyze_query=lambda query: SimpleNamespace(
                    intent="show",
                    metric=("incoming" if "поступление" in query.casefold() else "distribution"),
                    article_policy=None,
                    article_text=None,
                ))
            raise ImportError(name)

        def execute_raw(self, *_args, **_kwargs):
            raise AssertionError("directed flow must not use legacy standalone")

        def execute(self, *_args, **_kwargs):
            raise AssertionError("directed flow must not initialize Planner/LLM")

        def execute_balance_day(self, *, balance_id, day, request_id=None):
            self.execute_calls += 1
            assert balance_id == moscow.balance_id
            assert day == "2025-06-25"
            assert request_id
            return {
                "status": "ok",
                "rows": [
                    {
                        "article_id": item.article_id,
                        "article_name": "    " + item.canonical_name,
                        "article_indent": 4,
                        "gas_day": "2025-06-25",
                        "fact_value": str(index + 1),
                    }
                    for index, item in enumerate(articles)
                ],
                "debug": {
                    "sql_function": "api.show_balance_day",
                    "params": {"balance_id": moscow.balance_id, "day": "2025-06-25"},
                },
            }

    class Registry:
        geo_objects = (moscow_region,)
        geo_groups = ()
        routes = ()
        manifest = SimpleNamespace(bundle_version="2026.08.1")

        @staticmethod
        def balance(value):
            normalized = _test_normalize(value)
            if "москва" in normalized and "область" not in normalized:
                return moscow
            if "новгород" in normalized:
                return novgorod
            return None

        @staticmethod
        def articles_for_balance(value):
            return articles if int(value) == moscow.balance_id else ()

        @staticmethod
        def article(value):
            return next((item for item in articles if item.article_id == int(value)), None)

    runtime = DirectedRuntime()
    registry = Registry()
    processor = PipelineV2TurnProcessor(
        runtime=runtime,
        registry=registry,
        interpreter=InvalidInterpreter(),
        compiler=InterpretationMutationCompiler(RegistryEntityBinder(registry)),
        executor=object(),
        policy=NeverCalled(),
    )

    processed = processor.process(
        ContextContractV2(session_id="session"),
        message=message,
        execute_db=True,
        clarification=None,
        request_id=f"directed-{article_id}",
    )

    operand = processed.mutation.replace_intent.operands[0]
    assert runtime.execute_calls == 1
    assert operand.metric == metric
    assert [(item.role, item.entity.entity_type) for item in operand.entities] == [
        ("balance", "balance"),
        (direction_role, direction_type),
        ("article", "article"),
    ]
    assert operand.entities[-1].entity.entity_id == f"ART:{article_id}"
    assert len(processed.response["rows"]) == 1
    assert processed.response["rows"][0]["article_name"].strip() == (
        next(item.canonical_name for item in articles if item.article_id == article_id)
    )
    assert processed.response["rows"][0]["unit"] == "тыс. м3"
    assert processed.diagnostics["execution"]["layer"] == "unified_directed_flow"
    assert processed.diagnostics["execution"]["source_execution_count"] == 1


def test_contextual_full_balance_preserves_hierarchical_rows() -> None:
    balance = OperandEntityRef(
        role="balance",
        entity=CanonicalEntityRef(
            entity_id="2010000040765",
            entity_type="balance",
            display_name="ГП ТГ Томск суточный баланс",
        ),
    )
    intent = AnalysisIntent(
        operation=Operation.SHOW,
        operands=[AnalysisOperand(
            operand_id="tomsk",
            metric="balance",
            aggregate_type="sum",
            entities=[balance],
            periods=[PeriodRef(
                date_from="2025-04-01",
                date_to="2025-04-02",
            )],
        )],
    )
    mutation = ContextMutation(
        turn_id="turn-2",
        user_message="покажи баланс ТГ Томск за 1 апреля 2025 года",
        normalized_message="покажи баланс ТГ Томск за 1 апреля 2025 года",
        replace_intent=intent,
    )

    class FullBalanceRuntime(_NoInterpretationRuntime):
        def __init__(self):
            self.execute_calls = 0
            self.summary_calls = 0

        def execute(self, query, supplied_intent, **kwargs):
            self.execute_calls += 1
            assert supplied_intent == intent
            assert kwargs["apply_summary"] is False
            return {
                "status": "ok",
                "unit": "тыс. м3",
                "rows": [
                    {
                        "gas_day": "2025-04-01",
                        "article_name": "Ресурсы",
                        "article_scope": "Ресурсы",
                        "article_indent": 0,
                        "fact_value": "50389.485",
                    },
                    {
                        "gas_day": "2025-04-01",
                        "article_name": "  Поступление",
                        "article_scope": "Поступление",
                        "article_indent": 2,
                        "fact_value": "49984.825",
                    },
                ],
                "warnings": [
                    "unified normalized article_policy from empty to balance_only"
                ],
            }

        def summarize_envelope(self, envelope, **_kwargs):
            self.summary_calls += 1
            return {
                **envelope,
                "summary": {
                    "generated_by": "deterministic_contract",
                    "title": "Полный баланс",
                    "text": "Ресурсы и поступление показаны отдельными строками.",
                },
            }

    class ScalarExecutorMustNotRun:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("full balance must not use scalar execution")

    runtime = FullBalanceRuntime()
    registry = SimpleNamespace(
        geo_objects=(),
        geo_groups=(),
        routes=(),
        manifest=SimpleNamespace(bundle_version="2026.08.1"),
    )
    processor = PipelineV2TurnProcessor(
        runtime=runtime,
        registry=registry,
        interpreter=InvalidInterpreter(),
        compiler=InterpretationMutationCompiler(RegistryEntityBinder(registry)),
        executor=ScalarExecutorMustNotRun(),
        policy=NeverCalled(),
    )

    processed = processor._execute_mutation(
        ContextContractV2(session_id="session"),
        mutation,
        normalized_message=mutation.normalized_message,
        execute_db=True,
        request_id="request",
        started=0,
        interpretation_mode="mutation",
        memory_chunks=[],
    )

    assert runtime.execute_calls == 1
    assert runtime.summary_calls == 1
    assert processed.outcome == TransitionOutcome.SUCCESS
    assert len(processed.response["rows"]) == 2
    assert [row["article_scope"] for row in processed.response["rows"]] == [
        "Ресурсы",
        "Поступление",
    ]
    assert "facts" not in processed.response
    assert processed.result_reference.row_count == 2
    assert len(processed.result_reference.facts) == 2
    assert processed.diagnostics["execution"]["layer"] == "unified_balance_level"
    assert processed.response["warnings"] == []


def test_full_balance_detection_rejects_article_level_intent() -> None:
    intent = AnalysisIntent(
        operation=Operation.SHOW,
        operands=[AnalysisOperand(
            operand_id="resources",
            metric="balance",
            entities=[
                OperandEntityRef(
                    role="balance",
                    entity=CanonicalEntityRef(
                        entity_id="balance:tomsk",
                        entity_type="balance",
                        display_name="ГП ТГ Томск суточный баланс",
                    ),
                ),
                OperandEntityRef(
                    role="article",
                    entity=CanonicalEntityRef(
                        entity_id="article:resources",
                        entity_type="article",
                        display_name="Ресурсы",
                    ),
                ),
            ],
        )],
    )

    assert not _is_full_balance_show(intent)


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


def test_public_month_bucket_rank_returns_winning_month_only() -> None:
    envelope = _envelope()
    plan = envelope["debug"]["resolved_plan"]
    plan["_intent"].update(
        {
            "query": "в каком месяце был максимум поступления в ТГ Томск от ТГ Сургут в 2025",
            "intent": "rank",
            "metric": "incoming",
            "aggregate_type": "max",
            "period_grain": "month",
        }
    )
    plan["operation"] = "show"
    envelope["rows"] = [
        {
            "date_from": "2025-01-01",
            "date_to": "2025-02-01",
            "fact_value": "10",
            "article_scope": "от ТГ Сургут",
        },
        {
            "date_from": "2025-12-01",
            "date_to": "2026-01-01",
            "fact_value": "25",
            "article_scope": "от ТГ Сургут",
        },
    ]
    envelope["warnings"] = [
        "unified normalized additive month ranking to bucket sums",
        "unified analyzer disagreement detected; canonical intent selected by slot confidence",
    ]

    result = _public_pipeline_result(envelope)

    assert len(result["rows"]) == 1
    assert result["rows"][0]["date_from"] == "2025-12-01"
    assert "декабрь 2025" in result["summary"]["text"]
    assert "25,00 тыс. м3" in result["summary"]["text"]
    assert result["summary"]["generated_by"] == "deterministic_contract"
    assert result["warnings"] == []


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


class _GraphInterpreter:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    def interpret(self, *, state, message, metadata_bundle_version, **_kwargs):
        frame = state.conversation_window[-1]
        source = frame.operands[0].handle
        period = frame.periods[0].handle
        if self.kind == "reverse":
            graph = {
                "operation": state.active_dialog_scope.intent.operation.value,
                "operands": [{
                    "operand_id": "reversed",
                    "source_operand_handle": source,
                    "reverse_direction": True,
                }],
                "period_handles": [period],
            }
        else:
            graph = {
                "operation": "group",
                "operands": [{
                    "operand_id": "grouped",
                    "source_operand_handle": source,
                }],
                "period_handles": [period],
                "grouping": [{"dimension": "geo_group"}],
            }
        return InterpretationDecision.model_validate({
            "mode": "mutation",
            "normalized_message": message,
            "confidence": 0.99,
            "draft": None,
            "intent_graph": graph,
            "clarification": None,
            "unsupported_capability": None,
            "assumptions": [],
            "metadata_bundle_version": metadata_bundle_version,
        })


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
def test_reverse_followup_resolves_opposite_balance_article_from_context_graph(message) -> None:
    registry = _ReverseRegistry()
    registry.manifest = SimpleNamespace(bundle_version="2026.07.7")
    executor = _ReverseExecutor()
    processor = PipelineV2TurnProcessor(
        runtime=_NoInterpretationRuntime(),
        registry=registry,
        interpreter=_GraphInterpreter("reverse"),
        compiler=InterpretationMutationCompiler(RegistryEntityBinder(registry)),
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
    assert processed.diagnostics["interpretation"]["mode"] == "mutation"
    assert processed.diagnostics["interpretation"]["source"] == "qwen"
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
    registry = _ReverseRegistry(reverse_exists=False)
    registry.manifest = SimpleNamespace(bundle_version="2026.07.7")
    processor = PipelineV2TurnProcessor(
        runtime=_NoInterpretationRuntime(),
        registry=registry,
        interpreter=_GraphInterpreter("reverse"),
        compiler=InterpretationMutationCompiler(RegistryEntityBinder(registry)),
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


def test_percent_summary_names_each_operand_balance() -> None:
    from balance_chat.execution import DerivedResult

    intent = AnalysisIntent(
        operation=Operation.CALCULATE,
        operands=[
            AnalysisOperand(
                operand_id="incoming_max",
                metric="incoming",
                aggregate_type="max",
                entities=[OperandEntityRef(
                    role="balance",
                    entity=CanonicalEntityRef(
                        entity_id="tomsk",
                        entity_type="balance",
                        display_name="ГП ТГ Томск суточный баланс",
                    ),
                )],
            ),
            AnalysisOperand(
                operand_id="distribution_avg",
                metric="distribution",
                aggregate_type="avg",
                entities=[OperandEntityRef(
                    role="balance",
                    entity=CanonicalEntityRef(
                        entity_id="surgut",
                        entity_type="balance",
                        display_name="ГП ТГ Сургут суточный баланс",
                    ),
                )],
            ),
        ],
        periods=[PeriodRef(date_from="2025-01-01", date_to="2026-01-01")],
        formula={
            "operator": "percent_of",
            "numerator_operand_id": "incoming_max",
            "denominator_operand_id": "distribution_avg",
        },
    )
    native = NativeExecutionResult(
        operation=Operation.CALCULATE,
        status="ok",
        task_results=[
            TaskExecutionResult(
                task_id="task_1",
                status="ok",
                fact=ScalarFact(
                    task_id="task_1", value="10", unit="тыс. м3", label="от ТГ Сургут"
                ),
                envelope={"status": "ok"},
            ),
            TaskExecutionResult(
                task_id="task_2",
                status="ok",
                fact=ScalarFact(
                    task_id="task_2", value="20", unit="тыс. м3", label="Распределение"
                ),
                envelope={"status": "ok"},
            ),
        ],
        derived=DerivedResult(
            operator="percent_of",
            numerator_task_id="task_1",
            denominator_task_id="task_2",
            numerator_value="10",
            denominator_value="20",
            value="50",
            unit="%",
        ),
    )

    summary = _derived_contract_summary(native, intent)

    assert "от ТГ Сургут; баланс: ГП ТГ Томск суточный баланс" in summary["text"]
    assert "Распределение; баланс: ГП ТГ Сургут суточный баланс" in summary["text"]


def test_comparison_contract_summary_uses_target_minus_baseline_direction() -> None:
    from balance_chat.execution import ComparisonMember, ComparisonSetResult

    native = NativeExecutionResult(
        operation=Operation.COMPARE,
        status="ok",
        task_results=[],
        comparison_set=ComparisonSetResult(
            baseline_task_id="task_1",
            members=[
                ComparisonMember(
                    task_id="task_1",
                    operand_id="supply",
                    label="Казань",
                    value=Decimal("157697"),
                    delta_from_baseline=Decimal("0"),
                    percent_change_from_baseline=Decimal("0"),
                    unit="тыс. м3",
                ),
                ComparisonMember(
                    task_id="task_2",
                    operand_id="consumption",
                    label="Собственные потребители",
                    value=Decimal("1151926"),
                    delta_from_baseline=Decimal("994229"),
                    percent_change_from_baseline=Decimal("630.4679"),
                    unit="тыс. м3",
                ),
            ],
        ),
    )
    intent = AnalysisIntent(
        operation=Operation.COMPARE,
        operands=[
            AnalysisOperand(operand_id="supply", metric="distribution"),
            AnalysisOperand(operand_id="consumption", metric="consumption"),
        ],
    )

    summary = _comparison_contract_summary(native, intent)

    assert "выше" in summary["text"]
    assert "994\u00a0229,00" in summary["text"]
    assert summary["metrics"]["delta"] == "994229"
    assert summary["generated_by"] == "deterministic_contract"


def test_period_comparison_summary_uses_exclusive_end_and_canonical_delta() -> None:
    from balance_chat.execution import ComparisonResult

    native = NativeExecutionResult(
        operation=Operation.COMPARE_PERIODS,
        status="ok",
        task_results=[
            TaskExecutionResult(
                task_id="period_1",
                status="ok",
                fact=ScalarFact(
                    task_id="period_1",
                    value=Decimal("3030107.675"),
                    unit="тыс. м3",
                    label="Самарская обл",
                    periods=[{"date_from": "2025-03-01", "date_to": "2025-06-01"}],
                ),
                envelope={"status": "ok"},
            ),
            TaskExecutionResult(
                task_id="period_2",
                status="ok",
                fact=ScalarFact(
                    task_id="period_2",
                    value=Decimal("2052445.503"),
                    unit="тыс. м3",
                    label="Самарская обл",
                    periods=[{"date_from": "2025-06-01", "date_to": "2025-09-01"}],
                ),
                envelope={"status": "ok"},
            ),
        ],
        comparison=ComparisonResult(
            baseline_task_id="period_1",
            target_task_id="period_2",
            baseline_value=Decimal("3030107.675"),
            target_value=Decimal("2052445.503"),
            delta=Decimal("-977662.172"),
            percent_change=Decimal("-32.2649"),
            unit="тыс. м3",
        ),
    )

    summary = _period_comparison_contract_summary(
        native,
        AnalysisIntent(
            operation=Operation.COMPARE_PERIODS,
            operands=[AnalysisOperand(operand_id="supply", metric="distribution")],
            periods=[
                PeriodRef(date_from="2025-03-01", date_to="2025-06-01"),
                PeriodRef(date_from="2025-06-01", date_to="2025-09-01"),
            ],
        ),
    )

    assert "01.03.2025–31.05.2025" in summary["text"]
    assert "01.06.2025–31.08.2025" in summary["text"]
    assert "ниже" in summary["text"]
    assert summary["metrics"]["delta"] == "-977662.172"


def test_ranking_summary_formats_day_and_month_for_public_output() -> None:
    from balance_chat.execution import RankingResult

    def summary(grain: str, day: str):
        fact = ScalarFact(
            task_id="rank_source",
            value=Decimal("12"),
            unit="тыс. м3",
            label="Показатель",
            periods=[{"date_from": day, "date_to": day}],
        )
        native = NativeExecutionResult(
            operation=Operation.RANK,
            status="ok",
            task_results=[],
            ranking=RankingResult(
                direction="max",
                grain=grain,
                selected=[fact],
                source_row_count=1,
            ),
        )
        return _ranking_contract_summary(
            native,
            AnalysisIntent(
                operation=Operation.RANK,
                operands=[AnalysisOperand(operand_id="ranked", metric="incoming")],
                ranking={
                    "direction": "max",
                    "grain": grain,
                    "bucket_aggregate": "sum",
                },
            ),
        )

    assert "26.08.2025" in summary("day", "2025-08-26")["text"]
    assert "декабрь 2025" in summary("month", "2025-12-01")["text"]


def test_public_ranking_result_recursively_hides_provenance_and_ids() -> None:
    from balance_chat.execution import RankingResult

    fact = ScalarFact(
        task_id="rank_source",
        value=Decimal("12"),
        unit="тыс. м3",
        label="Самарская",
        periods=[{"date_from": "2025-08-26", "date_to": "2025-08-27"}],
        extremum_at="2025-08-26",
        provenance=[{
            "balance_id": "201",
            "article_ids": ["301"],
            "balance": "ГП ТГ Самара суточный баланс",
        }],
    )
    native = NativeExecutionResult(
        operation=Operation.RANK,
        status="ok",
        task_results=[],
        ranking=RankingResult(
            direction="max",
            grain="day",
            selected=[fact],
            source_row_count=1,
        ),
    )

    public = _public_native_result(native)
    serialized = str(public)

    assert public["ranking"]["selected"][0].get("provenance") is None
    assert "balance_id" not in serialized
    assert "article_ids" not in serialized


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
                "interpretation": {"unit": "млн м3"},
                "rows": [
                    {"geo_id": "GEO:ul", "geo": "Ульяновская область", "fact_value": 10},
                    {"geo_id": "GEO:ul", "geo": "ульяновская обл", "fact_value": 5},
                ],
            }

    registry = SimpleNamespace(
        geo_objects=(), geo_groups=(), routes=(),
        manifest=SimpleNamespace(bundle_version="2026.07.7"),
    )
    processor = PipelineV2TurnProcessor(
        runtime=GroupedRuntime(),
        registry=registry,
        interpreter=_GraphInterpreter("group"),
        compiler=InterpretationMutationCompiler(RegistryEntityBinder(registry)),
        executor=object(),
        policy=NeverCalled(),
    )

    processed = processor.process(
        state,
        message="суммируй данные по областям только по суточным балансам",
        execute_db=True,
        clarification=None,
        request_id="request",
    )

    assert processed.mutation.replace_intent.operation == Operation.GROUP
    assert processed.response["facts"][0]["entity_id"] == "GEO:ul"
    assert processed.response["facts"][0]["value"] == "15"
    assert processed.response["facts"][0]["unit"] == "тыс. м3"
    assert processed.mutation.replace_intent.operands[0].unit == "тыс. м3"


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
    registry = SimpleNamespace(
        geo_objects=(geo,), geo_groups=(), routes=(),
        manifest=SimpleNamespace(bundle_version="2026.07.7"),
    )
    processor = PipelineV2TurnProcessor(
        runtime=NoRepeatRuntime(),
        registry=registry,
        interpreter=_GraphInterpreter("group"),
        compiler=InterpretationMutationCompiler(RegistryEntityBinder(registry)),
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


def test_context_temporal_grouping_requeries_typed_series_instead_of_grouping_scalar() -> None:
    state = apply_context_transition(
        ContextContractV2(session_id="session"),
        ContextMutation(
            turn_id="turn-1",
            user_message="покажи среднее распределение за 2025",
            replace_intent=AnalysisIntent(
                operation=Operation.AGGREGATE,
                operands=[AnalysisOperand(
                    operand_id="distribution",
                    metric="distribution",
                    aggregate_type="avg",
                    unit="тыс. м3",
                )],
                periods=[PeriodRef(date_from="2025-01-01", date_to="2026-01-01")],
            ),
        ),
        TransitionOutcome.SUCCESS,
        result=ResultReference(
            turn_id="turn-1",
            status=TransitionOutcome.SUCCESS,
            row_count=1,
            facts=[{"value": "416297.83", "unit": "тыс. м3"}],
        ),
    )

    class TemporalGroupInterpreter:
        def interpret(self, *, state, message, metadata_bundle_version, **_kwargs):
            frame = state.conversation_window[-1]
            return InterpretationDecision.model_validate({
                "mode": "mutation",
                "normalized_message": message,
                "confidence": 0.99,
                "draft": None,
                "intent_graph": {
                    "operation": "group",
                    "operands": [{
                        "operand_id": "distribution",
                        "source_operand_handle": frame.operands[0].handle,
                    }],
                    "period_handles": [frame.periods[0].handle],
                    # Reproduce the live model leaking the previous scalar avg
                    # into a new additive monthly series and omitting grain.
                    "grouping": [{"dimension": "period", "aggregate_type": "avg"}],
                },
                "clarification": None,
                "unsupported_capability": None,
                "assumptions": [],
                "metadata_bundle_version": metadata_bundle_version,
            })

    class TemporalRuntime(RawRuntime):
        def __init__(self):
            self.queries = []

        def execute(self, query, _intent, **_kwargs):
            self.queries.append(query)
            return {
                "status": "ok",
                "unit": "тыс. м3",
                "rows": [
                    {
                        "fact_value": "100",
                        "date_from": "2025-01-01",
                        "date_to": "2025-02-01",
                    },
                    {
                        "fact_value": "200",
                        "date_from": "2025-02-01",
                        "date_to": "2025-03-01",
                    },
                ],
            }

    runtime = TemporalRuntime()
    translator = PipelineEnvelopeTranslator()
    registry = SimpleNamespace(
        geo_objects=(), geo_groups=(), routes=(),
        manifest=SimpleNamespace(bundle_version="2026.07.7"),
    )
    processor = PipelineV2TurnProcessor(
        runtime=runtime,
        registry=registry,
        interpreter=TemporalGroupInterpreter(),
        compiler=InterpretationMutationCompiler(RegistryEntityBinder(registry)),
        executor=NativeExecutor(
            PipelineScalarTaskRunner(runtime),
            translator.fact,
            translator.series,
        ),
        policy=NeverCalled(),
    )

    processed = processor.process(
        state,
        message="покажи тот же показатель по месяцам",
        execute_db=True,
        clarification=None,
        request_id="request",
    )

    assert len(runtime.queries) == 1
    assert "по месяцам" in runtime.queries[0]
    assert "Покажи распределение" in runtime.queries[0]
    assert [item["value"] for item in processed.response["facts"]] == ["100", "200"]
    assert processed.result_reference.row_count == 2
    assert processed.mutation.replace_intent.grain == "month"
    assert processed.mutation.replace_intent.grouping[0].aggregate_type == "sum"


def test_average_after_monthly_series_inherits_grain_and_canonical_scope() -> None:
    entities = [
        OperandEntityRef(
            role="balance",
            entity=CanonicalEntityRef(
                entity_id="BAL:tomsk",
                entity_type="balance",
                display_name="ГП ТГ Томск суточный баланс",
            ),
        ),
        OperandEntityRef(
            role="article",
            entity=CanonicalEntityRef(
                entity_id="ART:distribution",
                entity_type="article",
                display_name="Распределение",
            ),
        ),
    ]
    state = apply_context_transition(
        ContextContractV2(session_id="session"),
        ContextMutation(
            turn_id="turn-1",
            user_message="покажи распределение ТГ Томск по месяцам",
            replace_intent=AnalysisIntent(
                operation=Operation.GROUP,
                operands=[AnalysisOperand(
                    operand_id="distribution",
                    metric="distribution",
                    entities=entities,
                )],
                periods=[PeriodRef(date_from="2025-01-01", date_to="2026-01-01")],
                grouping=[GroupingSpec(dimension="period")],
                grain="month",
            ),
        ),
        TransitionOutcome.SUCCESS,
        result=ResultReference(
            turn_id="turn-1",
            status=TransitionOutcome.SUCCESS,
            row_count=12,
            facts=[{"value": index} for index in range(12)],
        ),
    )
    current = AnalysisIntent(
        operation=Operation.AGGREGATE,
        operands=[AnalysisOperand(
            operand_id="distribution",
            metric="distribution",
            aggregate_type="avg",
            entities=entities,
        )],
        periods=[PeriodRef(date_from="2025-01-01", date_to="2026-01-01")],
    )

    normalized = _normalize_series_reduction_intent(
        state, current, "выведи среднее за год"
    )

    assert normalized.grain == "month"
    assert normalized.operands[0].entities == entities


def test_same_scope_two_operand_comparison_is_canonicalized_to_compare_periods() -> None:
    from balance_chat.processor import _normalize_same_scope_period_comparison_intent

    entity = OperandEntityRef(
        role="destination",
        entity=CanonicalEntityRef(
            entity_id="GEO:samara",
            entity_type="geo_object",
            display_name="Самарская область",
        ),
    )
    spring = AnalysisOperand(
        operand_id="spring",
        metric="distribution",
        aggregate_type="sum",
        entities=[entity],
        periods=[PeriodRef(date_from="2025-03-01", date_to="2025-06-01")],
    )
    summer = spring.model_copy(
        update={
            "operand_id": "summer",
            "periods": [PeriodRef(date_from="2025-06-01", date_to="2025-09-01")],
        },
        deep=True,
    )
    intent = AnalysisIntent(
        operation=Operation.COMPARE,
        operands=[spring, summer],
        comparison=ComparisonSpec(
            baseline_operand_id="spring",
            target_operand_id="summer",
        ),
    )

    normalized = _normalize_same_scope_period_comparison_intent(intent)

    assert normalized.operation == Operation.COMPARE_PERIODS
    assert len(normalized.operands) == 1
    assert normalized.operands[0].periods == []
    assert [(item.date_from.isoformat(), item.date_to.isoformat()) for item in normalized.periods] == [
        ("2025-03-01", "2025-06-01"),
        ("2025-06-01", "2025-09-01"),
    ]


def test_same_scope_extremum_comparison_is_not_collapsed_to_period_comparison() -> None:
    from balance_chat.processor import _normalize_same_scope_period_comparison_intent

    first = AnalysisOperand(
        operand_id="maximum",
        metric="distribution",
        aggregate_type="max",
        periods=[PeriodRef(date_from="2025-03-01", date_to="2025-06-01")],
    )
    second = AnalysisOperand(
        operand_id="minimum",
        metric="distribution",
        aggregate_type="min",
        periods=[PeriodRef(date_from="2025-06-01", date_to="2025-09-01")],
    )
    intent = AnalysisIntent(
        operation=Operation.COMPARE,
        operands=[first, second],
        comparison=ComparisonSpec(
            baseline_operand_id="maximum",
            target_operand_id="minimum",
        ),
    )

    assert _normalize_same_scope_period_comparison_intent(intent) is intent


def test_failed_reverse_from_comparison_builds_valid_single_operand_attempt() -> None:
    first = AnalysisOperand(operand_id="first", metric="distribution")
    second = AnalysisOperand(operand_id="second", metric="distribution")
    comparison = AnalysisIntent(
        operation=Operation.COMPARE,
        operands=[first, second],
        comparison=ComparisonSpec(
            baseline_operand_id="first",
            target_operand_id="second",
        ),
        periods=[PeriodRef(date_from="2025-04-01", date_to="2025-07-01")],
    )
    attempted_operand = AnalysisOperand(
        operand_id="reversed",
        metric="distribution",
        aggregate_type="sum",
        periods=[PeriodRef(date_from="2025-04-01", date_to="2025-07-01")],
    )

    attempted = _single_operand_attempt(comparison, attempted_operand)

    assert attempted.operation == Operation.SHOW
    assert attempted.operands == [attempted_operand]
    assert attempted.comparison is None


def test_standalone_result_reference_rows_use_canonical_unit_and_no_plan() -> None:
    facts = _standalone_facts(
        {
            "interpretation": {"unit": "тыс.м3"},
            "rows": [{
                "article_scope": "Самарская обл.",
                "plan_value": 11,
                "fact_value": 12,
            }],
        }
    )

    assert facts == [
        {
            "article_scope": "Самарская обл.",
            "fact_value": 12,
            "unit": "тыс. м3",
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


def test_current_geo_tagger_handles_inflection_and_preserves_mention_order() -> None:
    processor = object.__new__(PipelineV2TurnProcessor)
    processor._normalize_lemmas = lambda value: (
        str(value).casefold()
        .replace("самару", "самар")
        .replace("сравни", "")
        .replace(" и ", " ")
        .replace(" за лето 2025", "")
        .strip()
    )
    processor.registry = SimpleNamespace(
        geo_objects=(
            SimpleNamespace(geo_id="samara", canonical_name="самара", aliases=()),
            SimpleNamespace(geo_id="kazan", canonical_name="казань", aliases=()),
            SimpleNamespace(
                geo_id="samara-region",
                canonical_name="самарская обл",
                aliases=("самарская",),
            ),
        )
    )

    tagged = processor._tagged_geo_objects("Сравни Самару и Казань за лето 2025")

    assert [item.geo_id for item in tagged] == ["samara", "kazan"]


def test_current_geo_tagger_does_not_replace_qualified_balance_and_article() -> None:
    processor = object.__new__(PipelineV2TurnProcessor)
    processor._normalize_lemmas = lambda value: " ".join(
        re.sub(r"[^0-9a-zа-я]+", " ", str(value).casefold())
        .replace("москву", "москва")
        .split()
    )
    processor.registry = SimpleNamespace(
        geo_objects=(
            SimpleNamespace(geo_id="moscow", canonical_name="москва", aliases=()),
            SimpleNamespace(
                geo_id="novgorod",
                canonical_name="новгород",
                aliases=("н новгород", "нижний новгород"),
            ),
        )
    )

    qualified = processor._tagged_geo_objects(
        "суммарный объём из ГП ТГ Москва в ТГ Н.Новгород"
    )
    standalone = processor._tagged_geo_objects("покажи поставки в Москву")
    mixed_roles = processor._tagged_geo_objects(
        "Суммарное распределение из ТГ Нижний Новгород в Нижний Новгород"
    )

    assert qualified == []
    assert [item.geo_id for item in standalone] == ["moscow"]
    assert [item.geo_id for item in mixed_roles] == ["novgorod"]


def test_single_qualified_business_is_balance_even_after_preposition() -> None:
    balance = SimpleNamespace(
        balance_id=10,
        canonical_name="ГП ТГ Москва суточный баланс",
    )
    processor = object.__new__(PipelineV2TurnProcessor)
    processor._normalize_lemmas = _test_normalize
    processor.registry = SimpleNamespace(
        balance=lambda value: balance if "москва" in _test_normalize(value) else None
    )

    mentions = processor._tagged_business_entity_mentions(
        "какой процент собственных нужд от распределения в ГП ТГ Москва"
    )

    assert [(item.text, item.role) for item in mentions] == [
        ("ГП ТГ Москва суточный баланс", "balance")
    ]


def test_distinct_qualified_businesses_keep_directional_roles() -> None:
    tomsk = SimpleNamespace(
        balance_id=10,
        canonical_name="ГП ТГ Томск суточный баланс",
    )
    surgut = SimpleNamespace(
        balance_id=20,
        canonical_name="ГП ТГ Сургут суточный баланс",
    )
    processor = object.__new__(PipelineV2TurnProcessor)
    processor._normalize_lemmas = _test_normalize
    processor.registry = SimpleNamespace(
        balance=lambda value: (
            tomsk if "томск" in _test_normalize(value)
            else surgut if "сургут" in _test_normalize(value)
            else None
        )
    )

    mentions = processor._tagged_business_entity_mentions(
        "покажи поступление в ТГ Томск от ТГ Сургут в 2025"
    )

    assert [(item.text, item.role) for item in mentions] == [
        ("ТГ томск", "destination"),
        ("ТГ сургут", "source"),
    ]


def test_business_first_geo_second_contract_is_used_on_first_turn() -> None:
    balance = SimpleNamespace(
        balance_id="balance-nn",
        canonical_name="ГП ТГ Н.Новгород суточный баланс",
        aliases=("гп тг нижний новгород",),
    )
    geo = SimpleNamespace(
        geo_id="geo-nn",
        canonical_name="нижний новгород",
        aliases=(),
    )

    class Registry:
        def __init__(self):
            self.manifest = SimpleNamespace(bundle_version="2026.08.1")
            self.geo_objects = (geo,)
            self.geo_groups = ()
            self.routes = ()

        @staticmethod
        def balance(value):
            normalized = _test_normalize(value)
            return balance if normalized in {
                "гп тг нижний новгород",
                "гп тг н новгород суточный баланс",
            } else None

        @staticmethod
        def geo(value):
            return geo if _test_normalize(value) == "нижний новгород" else None

        @staticmethod
        def geo_group(_value):
            return None

        @staticmethod
        def find_article_candidates(_value, **_kwargs):
            return ()

    class Runtime(_NoInterpretationRuntime):
        semantic_calls = 0

        def execute_raw(self, *_args, **_kwargs):
            self.semantic_calls += 1
            envelope = _envelope()
            envelope["debug"]["resolved_plan"]["expressions"][0]["geo"] = []
            envelope["debug"]["resolved_plan"]["expressions"][0]["aggregate_type"] = "sum"
            return envelope

    class Interpreter:
        def interpret(self, **_kwargs):
            raise AssertionError("role-separated standalone must not use context interpretation")

    class Executor:
        plan = None

        def execute(self, plan, **_kwargs):
            self.plan = plan
            return NativeExecutionResult(
                operation=plan.operation,
                status="no_data",
                task_results=[TaskExecutionResult(
                    task_id=plan.tasks[0].task_id,
                    status="no_data",
                    envelope={"status": "no_data"},
                )],
            )

    registry = Registry()
    runtime = Runtime()
    interpreter = Interpreter()
    executor = Executor()
    processor = PipelineV2TurnProcessor(
        runtime=runtime,
        registry=registry,
        interpreter=interpreter,
        compiler=InterpretationMutationCompiler(RegistryEntityBinder(registry)),
        executor=executor,
        policy=NeverCalled(),
    )
    processor._normalize_lemmas = _test_normalize

    processed = processor.process(
        ContextContractV2(session_id="session"),
        message=(
            "Суммарное распределение из ТГ Нижний Новгород "
            "в Нижний Новгород за 2 квартал 2025"
        ),
        execute_db=False,
        clarification=None,
        request_id="request",
    )

    assert runtime.semantic_calls == 1
    assert processed.diagnostics["interpretation"]["mode"] == (
        "deterministic_role_separated"
    )
    entities = processed.mutation.replace_intent.operands[0].entities
    assert [(item.role, item.entity.entity_type) for item in entities] == [
        ("balance", "balance"),
        ("destination", "geo_object"),
    ]
    assert all(item.entity.display_name != "в т.ч. Бишня" for item in entities)


def test_daily_balance_row_overrides_generic_unit_without_balance_entity() -> None:
    from balance_chat.planning import NativeMultiOperandPlanner

    intent = AnalysisIntent(
        operation=Operation.AGGREGATE,
        operands=[AnalysisOperand(
            operand_id="route",
            metric="distribution",
            aggregate_type="sum",
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
        "unit": "млн м3",
        "rows": [{
            "fact_value": "69937.11",
            "balance": "ГП ТГ Москва суточный баланс",
            "article_name": "ТГ Н.-Новгород",
        }],
    })

    assert fact.unit == "тыс. м3"
    assert fact.value == Decimal("69937.11")
