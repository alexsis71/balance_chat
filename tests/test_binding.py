from __future__ import annotations

from pathlib import Path

import pytest

from balance_chat.binding import (
    ContextBindingError,
    InterpretationMutationCompiler,
    RegistryEntityBinder,
)
from balance_chat.compat import PipelineRuntime, RuntimeConfig
from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ContextContractV2,
    ContextMutation,
    EntityMention,
    InterpretationDecision,
    OperandEntityRef,
    Operation,
    PeriodRef,
    TransitionOutcome,
)
from balance_chat.reducer import apply_context_transition


def _registry():
    root = Path(__file__).resolve().parents[2] / "pipeline"
    return PipelineRuntime(
        RuntimeConfig(
            pipeline_root=root,
            metadata_manifest=root / "data" / "metadata" / "manifest.json",
        )
    ).load_metadata_registry()


def _state() -> ContextContractV2:
    initial = ContextContractV2(session_id="session")
    intent = AnalysisIntent(
        operation=Operation.SHOW,
        operands=[
            AnalysisOperand(
                operand_id="supply",
                metric="distribution",
                entities=[
                    OperandEntityRef(
                        role="destination",
                        entity=CanonicalEntityRef(
                            entity_id="geo:kazan",
                            entity_type="geo_object",
                            display_name="Казань",
                        ),
                    )
                ],
            )
        ],
        periods=[PeriodRef(date_from="2025-06-01", date_to="2025-09-01")],
    )
    return apply_context_transition(
        initial,
        ContextMutation(
            turn_id="turn-1",
            user_message="Покажи поставки в Казань летом 2025",
            replace_intent=intent,
        ),
        TransitionOutcome.SUCCESS,
    )


def _decision(entity_text: str) -> InterpretationDecision:
    keep_scalar = {"action": "keep", "value": None, "source_scope": None}
    return InterpretationDecision.model_validate(
        {
            "mode": "mutation",
            "normalized_message": f"покажи поставки в {entity_text} летом",
            "confidence": 0.95,
            "draft": {
                "operation": keep_scalar,
                "metrics": {
                    "action": "keep",
                    "values": [],
                    "source_scope": None,
                },
                "aggregate_type": keep_scalar,
                "periods": {
                    "action": "reference",
                    "values": [],
                    "source_scope": "last_successful_scope",
                },
                "entities": {
                    "action": "set",
                    "mentions": [{"text": entity_text, "role": "destination"}],
                },
                "grouping": {
                    "action": "keep",
                    "values": [],
                    "source_scope": None,
                },
                "grain": keep_scalar,
                "reverse_direction": False,
            },
            "metadata_bundle_version": "2026.07.6",
        }
    )


def test_new_geo_keeps_canonical_period_by_reference() -> None:
    compiler = InterpretationMutationCompiler(RegistryEntityBinder(_registry()))
    mutation = compiler.compile(
        _decision("Ярославль"),
        _state(),
        turn_id="turn-2",
        user_message="покажи поставки в Ярославль летом",
    )

    assert mutation.replace_intent.periods == [
        PeriodRef(date_from="2025-06-01", date_to="2025-09-01")
    ]
    entity = mutation.replace_intent.operands[0].entities[0].entity
    assert entity.display_name == "Ярославль"
    assert entity.entity_id


def test_geo_alias_is_bound_to_canonical_name() -> None:
    compiler = InterpretationMutationCompiler(RegistryEntityBinder(_registry()))
    mutation = compiler.compile(
        _decision("Подмосковье"),
        _state(),
        turn_id="turn-2",
        user_message="покажи поставки в Подмосковье летом",
    )

    entity = mutation.replace_intent.operands[0].entities[0].entity
    assert entity.display_name == "Московская область"


def test_reverse_swaps_direction_and_clears_direction_bound_article() -> None:
    operand = AnalysisOperand(
        operand_id="route",
        metric="distribution",
        entities=[
            OperandEntityRef(
                role="source",
                entity=CanonicalEntityRef(
                    entity_id="geo:a",
                    entity_type="geo_object",
                    display_name="A",
                ),
            ),
            OperandEntityRef(
                role="destination",
                entity=CanonicalEntityRef(
                    entity_id="geo:b",
                    entity_type="geo_object",
                    display_name="B",
                ),
            ),
            OperandEntityRef(
                role="article",
                entity=CanonicalEntityRef(
                    entity_id="1",
                    entity_type="article",
                    display_name="A — B",
                ),
            ),
        ],
    )
    reversed_operand = InterpretationMutationCompiler._reverse_operand(operand)

    by_role = {item.role: item.entity.display_name for item in reversed_operand.entities}
    assert by_role == {"source": "B", "destination": "A"}


def test_peer_geo_comparison_copies_shared_balance_to_each_operand() -> None:
    decision = InterpretationDecision.model_validate(
        {
            "mode": "standalone",
            "normalized_message": "сравни поставки в Казань и Ярославль за май 2025",
            "confidence": 0.99,
            "draft": {
                "operation": {"action": "set", "value": "compare"},
                "metrics": {"action": "set", "values": ["distribution"]},
                "aggregate_type": {"action": "set", "value": "sum"},
                "periods": {
                    "action": "set",
                    "values": [
                        {"date_from": "2025-05-01", "date_to": "2025-06-01"}
                    ],
                },
                "entities": {
                    "action": "set",
                    "mentions": [
                        {"text": "ГП ТГ Казань суточный баланс", "role": "balance"},
                        {"text": "Казань", "role": "destination"},
                        {"text": "Ярославль", "role": "destination"},
                    ],
                },
                "grouping": {"action": "clear"},
                "grain": {"action": "clear"},
                "reverse_direction": False,
            },
            "metadata_bundle_version": "2026.07.6",
        }
    )

    mutation = InterpretationMutationCompiler(RegistryEntityBinder(_registry())).compile(
        decision,
        ContextContractV2(session_id="session"),
        turn_id="turn-1",
        user_message=decision.normalized_message,
    )

    intent = mutation.replace_intent
    assert intent.operation == Operation.COMPARE
    assert len(intent.operands) == 2
    assert [
        [item.role for item in operand.entities] for operand in intent.operands
    ] == [["balance", "destination"], ["balance", "destination"]]
    assert [
        operand.entities[-1].entity.display_name for operand in intent.operands
    ] == ["Казань", "Ярославль"]


def test_multi_operand_keep_preserves_each_operand_entities() -> None:
    state = ContextContractV2(session_id="session")
    intent = AnalysisIntent(
        operation=Operation.COMPARE,
        operands=[
            AnalysisOperand(
                operand_id="kazan",
                metric="distribution",
                entities=[OperandEntityRef(
                    role="destination",
                    entity=CanonicalEntityRef(
                        entity_id="geo:kazan",
                        entity_type="geo_object",
                        display_name="Казань",
                    ),
                )],
            ),
            AnalysisOperand(
                operand_id="yaroslavl",
                metric="distribution",
                entities=[OperandEntityRef(
                    role="destination",
                    entity=CanonicalEntityRef(
                        entity_id="geo:yaroslavl",
                        entity_type="geo_object",
                        display_name="Ярославль",
                    ),
                )],
            ),
        ],
        periods=[PeriodRef(date_from="2025-05-01", date_to="2025-06-01")],
    )
    state = apply_context_transition(
        state,
        ContextMutation(
            turn_id="turn-1",
            user_message="сравни Казань и Ярославль",
            replace_intent=intent,
        ),
        TransitionOutcome.SUCCESS,
    )
    keep = {"action": "keep", "value": None, "source_scope": None}
    decision = InterpretationDecision.model_validate({
        "mode": "mutation",
        "normalized_message": "а за июнь 2025",
        "confidence": 0.99,
        "draft": {
            "operation": keep,
            "metrics": {"action": "keep", "values": [], "source_scope": None},
            "aggregate_type": keep,
            "periods": {"action": "set", "values": [
                {"date_from": "2025-06-01", "date_to": "2025-07-01"}
            ]},
            "entities": {"action": "keep", "mentions": []},
            "grouping": {"action": "keep", "values": [], "source_scope": None},
            "grain": keep,
            "reverse_direction": False,
        },
        "metadata_bundle_version": "2026.07.6",
    })

    mutation = InterpretationMutationCompiler(RegistryEntityBinder(_registry())).compile(
        decision,
        state,
        turn_id="turn-2",
        user_message="а за июнь 2025",
    )

    assert [
        operand.entities[0].entity.display_name
        for operand in mutation.replace_intent.operands
    ] == ["Казань", "Ярославль"]


def _stock_graph(aggregate_type=None) -> InterpretationDecision:
    return InterpretationDecision.model_validate({
        "mode": "standalone",
        "normalized_message": "покажи запас газа за май 2025",
        "confidence": 0.99,
        "intent_graph": {
            "operation": "show",
            "operands": [{
                "operand_id": "stock",
                "metric": "stock",
                "aggregate_type": aggregate_type,
                "entity_mode": "clear",
                "period_mode": "replace",
                "periods": [{"date_from": "2025-05-01", "date_to": "2025-06-01"}],
            }],
        },
        "metadata_bundle_version": "2026.07.6",
    })


def test_new_stock_metric_uses_state_default_instead_of_flow_sum() -> None:
    mutation = InterpretationMutationCompiler(RegistryEntityBinder(_registry())).compile(
        _stock_graph(),
        ContextContractV2(session_id="session"),
        turn_id="turn-stock",
        user_message="покажи запас газа за май 2025",
    )

    assert mutation.replace_intent.operands[0].aggregate_type == "last"


def test_stock_sum_is_rejected_before_execution() -> None:
    with pytest.raises(ContextBindingError, match="not valid for metric"):
        InterpretationMutationCompiler(RegistryEntityBinder(_registry())).compile(
            _stock_graph("sum"),
            ContextContractV2(session_id="session"),
            turn_id="turn-stock",
            user_message="суммируй ежедневный запас газа за май 2025",
        )


def test_percent_graph_binds_incoming_viewpoint_and_root_distribution_article() -> None:
    decision = InterpretationDecision.model_validate({
        "mode": "standalone",
        "normalized_message": (
            "какой процент составляет максимум поступления от ТГ Сургут "
            "в ТГ Томск от среднего общего распределения ТГ Томск в 2025"
        ),
        "confidence": 0.99,
        "intent_graph": {
            "operation": "calculate",
            "operands": [
                {
                    "operand_id": "incoming_max",
                    "metric": "incoming",
                    "aggregate_type": "max",
                    "entity_mode": "replace",
                    "entity_mentions": [
                        {"text": "ТГ Сургут", "role": "source"},
                        {"text": "ТГ Томск", "role": "destination"},
                    ],
                    "period_mode": "replace",
                    "periods": [{"date_from": "2025-01-01", "date_to": "2026-01-01"}],
                },
                {
                    "operand_id": "distribution_avg",
                    "metric": "distribution",
                    "aggregate_type": "avg",
                    "entity_mode": "replace",
                    "entity_mentions": [{"text": "ТГ Томск", "role": "balance"}],
                    "period_mode": "replace",
                    "periods": [{"date_from": "2025-01-01", "date_to": "2026-01-01"}],
                },
            ],
            "formula": {
                "operator": "percent_of",
                "numerator_operand_id": "incoming_max",
                "denominator_operand_id": "distribution_avg",
            },
        },
        "metadata_bundle_version": "2026.08.1",
    })

    mutation = InterpretationMutationCompiler(RegistryEntityBinder(_registry())).compile(
        decision,
        ContextContractV2(session_id="session"),
        turn_id="turn-percent",
        user_message=decision.normalized_message,
    )

    intent = mutation.replace_intent
    assert intent.operation == Operation.CALCULATE
    assert intent.formula.operator == "percent_of"
    assert [
        [(entity.role, entity.entity.display_name) for entity in operand.entities]
        for operand in intent.operands
    ] == [
        [
            ("balance", "ГП ТГ Томск суточный баланс"),
            ("article", "от ТГ Сургут"),
        ],
        [
            ("balance", "ГП ТГ Томск суточный баланс"),
            ("article", "Распределение"),
        ],
    ]


def test_role_tagged_current_evidence_completes_omitted_percent_entities() -> None:
    decision = InterpretationDecision.model_validate({
        "mode": "standalone",
        "normalized_message": (
            "какой процент составляет максимум поступления от ТГ Сургут "
            "в ТГ Томск от среднего общего распределения ТГ Томск в 2025"
        ),
        "confidence": 0.95,
        "intent_graph": {
            "operation": "calculate",
            "operands": [
                {
                    "operand_id": "incoming_max",
                    "metric": "incoming",
                    "aggregate_type": "max",
                    "periods": [{"date_from": "2025-01-01", "date_to": "2026-01-01"}],
                },
                {
                    "operand_id": "distribution_avg",
                    "metric": "distribution",
                    "aggregate_type": "avg",
                    "periods": [{"date_from": "2025-01-01", "date_to": "2026-01-01"}],
                },
            ],
            "formula": {
                "operator": "percent_of",
                "numerator_operand_id": "incoming_max",
                "denominator_operand_id": "distribution_avg",
            },
        },
        "metadata_bundle_version": "2026.08.1",
    })

    mutation = InterpretationMutationCompiler(RegistryEntityBinder(_registry())).compile(
        decision,
        ContextContractV2(session_id="session"),
        turn_id="turn-percent-evidence",
        user_message=decision.normalized_message,
        current_entity_mentions=[
            EntityMention(text="ТГ Сургут", role="source"),
            EntityMention(text="ТГ Томск", role="destination"),
            EntityMention(text="ТГ Томск", role="balance"),
        ],
    )

    assert [
        [(entity.role, entity.entity.display_name) for entity in operand.entities]
        for operand in mutation.replace_intent.operands
    ] == [
        [
            ("balance", "ГП ТГ Томск суточный баланс"),
            ("article", "от ТГ Сургут"),
        ],
        [
            ("balance", "ГП ТГ Томск суточный баланс"),
            ("article", "Распределение"),
        ],
    ]
    assert [operand.unit for operand in mutation.replace_intent.operands] == [
        "тыс. м3",
        "тыс. м3",
    ]


def test_n_way_geo_evidence_expands_incomplete_compare_graph_before_binding() -> None:
    decision = InterpretationDecision.model_validate({
        "mode": "standalone",
        "normalized_message": (
            "сравни поставки в Самарскую область, Казань и "
            "Ярославскую область летом 2025"
        ),
        "confidence": 0.95,
        "intent_graph": {
            "operation": "compare",
            "operands": [{
                "operand_id": "supply",
                "metric": "supply",
                "aggregate_type": "sum",
                "periods": [
                    {"date_from": "2025-06-01", "date_to": "2025-09-01"}
                ],
            }],
            # Reproduce the malformed but schema-valid Qwen response observed
            # in live acceptance. Pre-binding evidence must replace this pair.
            "comparison": {
                "baseline_operand_id": "supply",
                "target_operand_id": "supply",
            },
        },
        "metadata_bundle_version": "2026.08.1",
    })

    mutation = InterpretationMutationCompiler(RegistryEntityBinder(_registry())).compile(
        decision,
        ContextContractV2(session_id="session"),
        turn_id="turn-n-way",
        user_message=decision.normalized_message,
        current_entity_mentions=[
            EntityMention(text="самарская обл", role="destination"),
            EntityMention(text="казань", role="destination"),
            EntityMention(text="ярославская", role="destination"),
        ],
    )

    intent = mutation.replace_intent
    assert [item.operand_id for item in intent.operands] == [
        "supply_1",
        "supply_2",
        "supply_3",
    ]
    assert [
        item.entities[0].entity.display_name for item in intent.operands
    ] == ["Самарская обл", "Казань", "Ярославская"]
    assert intent.comparison.baseline_operand_id == "supply_1"
    assert intent.comparison.target_operand_id == "supply_2"


def test_own_needs_and_distribution_share_balance_but_bind_distinct_articles() -> None:
    decision = InterpretationDecision.model_validate({
        "mode": "standalone",
        "normalized_message": (
            "какой процент собственных нужд от общего распределения "
            "в ГП ТГ Москва за март 2025"
        ),
        "confidence": 0.95,
        "intent_graph": {
            "operation": "calculate",
            "operands": [
                {
                    "operand_id": "needs",
                    "metric": "own_needs",
                    "aggregate_type": "sum",
                    "periods": [
                        {"date_from": "2025-03-01", "date_to": "2025-04-01"}
                    ],
                },
                {
                    "operand_id": "distribution",
                    "metric": "distribution",
                    "aggregate_type": "sum",
                    "periods": [
                        {"date_from": "2025-03-01", "date_to": "2025-04-01"}
                    ],
                },
            ],
            "formula": {
                "operator": "percent_of",
                "numerator_operand_id": "needs",
                "denominator_operand_id": "distribution",
            },
        },
        "metadata_bundle_version": "2026.08.1",
    })

    mutation = InterpretationMutationCompiler(RegistryEntityBinder(_registry())).compile(
        decision,
        ContextContractV2(session_id="session"),
        turn_id="turn-needs",
        user_message=decision.normalized_message,
        current_entity_mentions=[EntityMention(
            text="ГП ТГ Москва суточный баланс",
            role="balance",
        )],
    )

    assert [
        next(
            entity.entity.display_name
            for entity in operand.entities
            if entity.role == "article"
        )
        for operand in mutation.replace_intent.operands
    ] == ["Собств. нужды и потери", "Распределение"]


def test_compare_periods_collapses_duplicate_source_operands_to_canonical_shape() -> None:
    state = _state()
    source = state.conversation_window[-1].operands[0].handle
    decision = InterpretationDecision.model_validate({
        "mode": "mutation",
        "normalized_message": "сравни весну и лето",
        "confidence": 0.95,
        "intent_graph": {
            "operation": "compare_periods",
            "operands": [
                {
                    "operand_id": "spring",
                    "source_operand_handle": source,
                    "metric": "distribution",
                    "aggregate_type": "sum",
                    "periods": [
                        {"date_from": "2025-03-01", "date_to": "2025-06-01"}
                    ],
                },
                {
                    "operand_id": "summer",
                    "source_operand_handle": source,
                    "metric": "distribution",
                    "aggregate_type": "sum",
                    "periods": [
                        {"date_from": "2025-06-01", "date_to": "2025-09-01"}
                    ],
                },
            ],
            "comparison": {
                "baseline_operand_id": "spring",
                "target_operand_id": "summer",
            },
        },
        "metadata_bundle_version": "2026.08.1",
    })

    mutation = InterpretationMutationCompiler(RegistryEntityBinder(_registry())).compile(
        decision,
        state,
        turn_id="turn-compare-periods",
        user_message=decision.normalized_message,
    )

    intent = mutation.replace_intent
    assert intent.operation == Operation.COMPARE_PERIODS
    assert len(intent.operands) == 1
    assert intent.operands[0].entities[0].entity.display_name == "Казань"
    assert [(item.date_from.isoformat(), item.date_to.isoformat()) for item in intent.periods] == [
        ("2025-03-01", "2025-06-01"),
        ("2025-06-01", "2025-09-01"),
    ]
