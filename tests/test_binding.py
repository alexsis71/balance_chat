from __future__ import annotations

from pathlib import Path

from balance_chat.binding import InterpretationMutationCompiler, RegistryEntityBinder
from balance_chat.compat import PipelineRuntime, RuntimeConfig
from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ContextContractV2,
    ContextMutation,
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
