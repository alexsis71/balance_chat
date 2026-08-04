from __future__ import annotations

from types import SimpleNamespace

from balance_chat.binding import InterpretationMutationCompiler, RegistryEntityBinder
from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ContextContractV2,
    ContextMutation,
    InterpretationDecision,
    EntityMention,
    OperandEntityRef,
    Operation,
    PeriodRef,
    ResultReference,
    TransitionOutcome,
)
from balance_chat.conversation import handle_indexes, model_window_payload
from balance_chat.reducer import apply_context_transition


def _operand(name: str, *, aggregate_type: str = "sum") -> AnalysisOperand:
    return AnalysisOperand(
        operand_id="supply",
        metric="distribution",
        aggregate_type=aggregate_type,
        entities=[
            OperandEntityRef(
                role="destination",
                entity=CanonicalEntityRef(
                    entity_id=f"geo:{name.casefold()}",
                    entity_type="geo_object",
                    display_name=name,
                ),
            )
        ],
    )


def _append(
    state: ContextContractV2,
    revision: int,
    name: str,
    *,
    aggregate_type: str = "sum",
) -> ContextContractV2:
    intent = AnalysisIntent(
        operation=(Operation.AGGREGATE if aggregate_type != "sum" else Operation.SHOW),
        operands=[_operand(name, aggregate_type=aggregate_type)],
        periods=[PeriodRef(date_from="2025-04-01", date_to="2025-07-01")],
    )
    turn_id = f"00000000-0000-0000-0000-{revision:012d}"
    return apply_context_transition(
        state,
        ContextMutation(
            turn_id=turn_id,
            user_message=f"Запрос {revision}: поставки в {name}",
            normalized_message=f"Покажи поставки в {name}",
            assistant_summary=f"Ответ для {name}",
            replace_intent=intent,
        ),
        TransitionOutcome.SUCCESS,
        result=ResultReference(
            turn_id=turn_id,
            status=TransitionOutcome.SUCCESS,
            row_count=1,
            facts=[{"value": revision, "unit": "тыс. м3"}],
        ),
    )


def test_conversation_window_keeps_seven_complete_chronological_turns() -> None:
    state = ContextContractV2(session_id="session")
    for revision in range(1, 9):
        state = _append(state, revision, f"Город {revision}")

    assert [item.revision for item in state.conversation_window] == list(range(2, 9))
    assert state.conversation_window[0].user_message.startswith("Запрос 2")
    assert state.conversation_window[-1].assistant_summary == "Ответ для Город 8"
    assert state.conversation_window[-1].entities[0].entity.entity_type == "geo_object"
    assert state.conversation_window[-1].periods[0].period.date_to.isoformat() == "2025-07-01"
    assert state.conversation_window[-1].result.facts[0]["unit"] == "тыс. м3"


def test_context_graph_clones_old_operand_for_extremum_comparison() -> None:
    state = _append(ContextContractV2(session_id="session"), 1, "Самара", aggregate_type="max")
    frame = state.conversation_window[-1]
    source = frame.operands[0].handle
    period = frame.periods[0].handle
    decision = InterpretationDecision.model_validate(
        {
            "contract_version": "1.0",
            "mode": "mutation",
            "normalized_message": "Сравни максимум с минимумом за тот же период",
            "confidence": 0.99,
            "draft": None,
            "intent_graph": {
                "operation": "compare",
                "operands": [
                    {
                        "operand_id": "maximum",
                        "source_operand_handle": source,
                        "aggregate_type": "max",
                    },
                    {
                        "operand_id": "minimum",
                        "source_operand_handle": source,
                        "aggregate_type": "min",
                    },
                ],
                "period_handles": [period],
                "comparison": {
                    "baseline_operand_id": "maximum",
                    "target_operand_id": "minimum",
                },
            },
            "clarification": None,
            "unsupported_capability": None,
            "assumptions": [],
            "metadata_bundle_version": "2026.07.7",
        }
    )

    mutation = InterpretationMutationCompiler(
        RegistryEntityBinder(SimpleNamespace())
    ).compile(decision, state, turn_id="turn-2", user_message="сравни с минимумом")

    intent = mutation.replace_intent
    assert intent.operation == Operation.COMPARE
    assert [item.aggregate_type for item in intent.operands] == ["max", "min"]
    assert [item.entities[0].entity.display_name for item in intent.operands] == [
        "Самара",
        "Самара",
    ]
    assert intent.periods == [PeriodRef(date_from="2025-04-01", date_to="2025-07-01")]


def test_context_graph_can_compare_entities_from_non_adjacent_turns() -> None:
    state = ContextContractV2(session_id="session")
    state = _append(state, 1, "Ярославль")
    state = _append(state, 2, "Самара")
    state = _append(state, 3, "Казань")
    first = state.conversation_window[0].operands[0].handle
    third = state.conversation_window[2].operands[0].handle
    period = state.conversation_window[1].periods[0].handle
    decision = InterpretationDecision.model_validate(
        {
            "mode": "mutation",
            "normalized_message": "Сравни Ярославль и Казань за тот же период",
            "confidence": 0.98,
            "draft": None,
            "intent_graph": {
                "operation": "compare",
                "operands": [
                    {"operand_id": "yaroslavl", "source_operand_handle": first},
                    {"operand_id": "kazan", "source_operand_handle": third},
                ],
                "period_handles": [period],
            },
            "clarification": None,
            "unsupported_capability": None,
            "assumptions": [],
            "metadata_bundle_version": "2026.07.7",
        }
    )

    mutation = InterpretationMutationCompiler(
        RegistryEntityBinder(SimpleNamespace())
    ).compile(decision, state, turn_id="turn-4", user_message="сравни их")

    assert [
        item.entities[0].entity.display_name
        for item in mutation.replace_intent.operands
    ] == ["Ярославль", "Казань"]
    assert mutation.replace_intent.periods[0].date_from.isoformat() == "2025-04-01"


def test_single_active_operand_is_the_generic_mutation_base_when_handle_is_omitted() -> None:
    state = _append(ContextContractV2(session_id="session"), 1, "Самара")
    decision = InterpretationDecision.model_validate(
        {
            "mode": "mutation",
            "normalized_message": "Покажи за лето 2025",
            "confidence": 0.95,
            "draft": None,
            "intent_graph": {
                "operation": "show",
                "operands": [{"operand_id": "supply", "metric": "distribution"}],
                "periods": [
                    {
                        "date_from": "2025-06-01",
                        "date_to": "2025-09-01",
                        "label": "Лето 2025",
                    }
                ],
            },
            "clarification": None,
            "unsupported_capability": None,
            "assumptions": [],
            "metadata_bundle_version": "2026.07.7",
        }
    )

    mutation = InterpretationMutationCompiler(
        RegistryEntityBinder(SimpleNamespace())
    ).compile(decision, state, turn_id="turn-2", user_message="а за лето?")

    assert mutation.replace_intent.operands[0].entities[0].entity.display_name == "Самара"
    assert mutation.replace_intent.periods[0].date_from.isoformat() == "2025-06-01"


def test_current_message_geo_tag_replaces_stale_geo_without_phrase_rules() -> None:
    state = _append(ContextContractV2(session_id="session"), 1, "Самара")
    decision = InterpretationDecision.model_validate(
        {
            "mode": "mutation",
            "normalized_message": "Покажи поставки в Казань за тот же период",
            "confidence": 0.95,
            "draft": None,
            "intent_graph": {
                "operation": "show",
                "operands": [{"operand_id": "supply", "metric": "distribution"}],
            },
            "clarification": None,
            "unsupported_capability": None,
            "assumptions": [],
            "metadata_bundle_version": "2026.07.7",
        }
    )
    registry = SimpleNamespace(
        geo=lambda text: SimpleNamespace(
            geo_id="geo:kazan", canonical_name="Казань"
        ) if text == "Казань" else None,
        geo_group=lambda _text: None,
        find_article_candidates=lambda _text: (),
    )

    mutation = InterpretationMutationCompiler(RegistryEntityBinder(registry)).compile(
        decision,
        state,
        turn_id="turn-2",
        user_message="теперь в Казань",
        current_entity_mentions=[EntityMention(text="Казань", role="destination")],
    )

    entities = mutation.replace_intent.operands[0].entities
    assert len(entities) == 1
    assert entities[0].entity.display_name == "Казань"


def test_current_geo_updates_role_without_clearing_business_entities() -> None:
    period = PeriodRef(date_from="2025-04-01", date_to="2025-07-01")
    route = AnalysisOperand(
        operand_id="route",
        metric="distribution",
        aggregate_type="sum",
        entities=[
            OperandEntityRef(
                role="balance",
                entity=CanonicalEntityRef(
                    entity_id="balance:nn",
                    entity_type="balance",
                    display_name="ГП ТГ Н.Новгород суточный баланс",
                ),
            ),
            OperandEntityRef(
                role="article",
                entity=CanonicalEntityRef(
                    entity_id="article:route",
                    entity_type="article",
                    display_name="Распределение",
                ),
            ),
        ],
    )
    state = apply_context_transition(
        ContextContractV2(session_id="session"),
        ContextMutation(
            turn_id="turn-1",
            user_message="распределение из ТГ Нижний Новгород",
            replace_intent=AnalysisIntent(
                operation=Operation.SHOW,
                operands=[route],
                periods=[period],
            ),
        ),
        TransitionOutcome.SUCCESS,
    )
    decision = InterpretationDecision.model_validate(
        {
            "mode": "mutation",
            "normalized_message": "Суммарное распределение в Нижний Новгород",
            "confidence": 0.95,
            "draft": None,
            "intent_graph": {
                "operation": "show",
                "operands": [{
                    "operand_id": "route",
                    "source_operand_handle": "t0001.o.route",
                    "metric": "distribution",
                    "aggregate_type": "sum",
                }],
            },
            "clarification": None,
            "unsupported_capability": None,
            "assumptions": [],
            "metadata_bundle_version": "2026.08.1",
        }
    )
    registry = SimpleNamespace(
        geo=lambda text: SimpleNamespace(
            geo_id="geo:nizhny-novgorod", canonical_name="нижний новгород"
        ) if text == "Нижний Новгород" else None,
        geo_group=lambda _text: None,
        find_article_candidates=lambda _text: (),
    )

    mutation = InterpretationMutationCompiler(RegistryEntityBinder(registry)).compile(
        decision,
        state,
        turn_id="turn-2",
        user_message="Суммарное распределение из ТГ Нижний Новгород в Нижний Новгород",
        current_entity_mentions=[
            EntityMention(text="Нижний Новгород", role="destination")
        ],
    )

    by_role = {
        item.role: item.entity.display_name
        for item in mutation.replace_intent.operands[0].entities
    }
    assert by_role == {
        "balance": "ГП ТГ Н.Новгород суточный баланс",
        "article": "Распределение",
        "destination": "Нижний новгород",
    }


def test_explicit_handles_override_default_inherit_modes() -> None:
    state = _append(ContextContractV2(session_id="session"), 1, "Самара")
    frame = state.conversation_window[-1]
    decision = InterpretationDecision.model_validate(
        {
            "mode": "mutation",
            "normalized_message": "Покажи минимум за тот же период",
            "confidence": 0.95,
            "draft": None,
            "intent_graph": {
                "operation": "show",
                "operands": [{
                    "operand_id": "minimum",
                    "aggregate_type": "min",
                    "entity_handles": [frame.entities[0].handle],
                    "period_handles": [frame.periods[0].handle],
                }],
            },
            "clarification": None,
            "unsupported_capability": None,
            "assumptions": [],
            "metadata_bundle_version": "2026.07.7",
        }
    )

    mutation = InterpretationMutationCompiler(
        RegistryEntityBinder(SimpleNamespace())
    ).compile(decision, state, turn_id="turn-2", user_message="минимум")

    assert mutation.replace_intent.operation == Operation.AGGREGATE
    assert mutation.replace_intent.operands[0].aggregate_type == "min"
    assert mutation.replace_intent.operands[0].entities[0].entity.display_name == "Самара"


def test_single_followup_selects_unique_operand_matching_active_period() -> None:
    spring = PeriodRef(date_from="2025-03-01", date_to="2025-06-01")
    summer = PeriodRef(date_from="2025-06-01", date_to="2025-09-01")
    base = _operand("Самарская область")
    comparison = AnalysisIntent(
        operation=Operation.COMPARE,
        operands=[
            base.model_copy(update={"operand_id": "spring", "periods": [spring]}),
            base.model_copy(update={"operand_id": "summer", "periods": [summer]}),
        ],
        periods=[summer],
        comparison={
            "baseline_operand_id": "spring",
            "target_operand_id": "summer",
        },
    )
    state = apply_context_transition(
        ContextContractV2(session_id="session"),
        ContextMutation(
            turn_id="turn-1",
            user_message="сравни весну и лето",
            replace_intent=comparison,
        ),
        TransitionOutcome.SUCCESS,
    )
    decision = InterpretationDecision.model_validate(
        {
            "mode": "mutation",
            "normalized_message": "Покажи максимум за лето",
            "confidence": 0.95,
            "draft": None,
            "intent_graph": {
                "operation": "show",
                "operands": [{
                    "operand_id": "maximum",
                    "metric": "distribution",
                    "aggregate_type": "max",
                }],
            },
            "clarification": None,
            "unsupported_capability": None,
            "assumptions": [],
            "metadata_bundle_version": "2026.07.7",
        }
    )

    mutation = InterpretationMutationCompiler(
        RegistryEntityBinder(SimpleNamespace())
    ).compile(decision, state, turn_id="turn-2", user_message="максимум летом")

    operand = mutation.replace_intent.operands[0]
    assert operand.entities[0].entity.display_name == "Самарская область"
    assert operand.aggregate_type == "max"


def test_model_payload_and_handle_indexes_are_bounded_to_same_window() -> None:
    state = ContextContractV2(session_id="session")
    for revision in range(1, 9):
        state = _append(state, revision, f"Город {revision}")

    payload = model_window_payload(state)
    operands, entities, periods = handle_indexes(state)

    assert len(payload) == 7
    assert payload[0]["turn_handle"] == "t0002"
    assert payload[-1]["turn_handle"] == "t0008"
    assert set(operands) == {item["operands"][0]["handle"] for item in payload}
    assert len(entities) == 7
    assert len(periods) == 1  # the canonical same period intentionally reuses one handle
