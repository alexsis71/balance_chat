from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from balance_chat.semantic_repair.context import build_semantic_shadow_context
from balance_chat.semantic_repair.contracts import (
    ProposalAction,
    SemanticMutation,
    SemanticReference,
    SemanticTransitionProposal,
    semantic_transition_proposal_json_schema,
)
from balance_chat.contracts import ContextMutation, ResultReference, TransitionOutcome
from balance_chat.store import InMemoryContextStore


def test_proposal_contract_is_typed_and_forbids_extra_fields() -> None:
    proposal = SemanticTransitionProposal(
        action=ProposalAction.PATCH,
        mutations=[SemanticMutation(kind="swap_direction")],
        confidence=0.9,
    )

    assert proposal.mutations[0].kind.value == "swap_direction"
    with pytest.raises(ValidationError):
        SemanticTransitionProposal.model_validate(
            {"action": "patch", "mutations": [], "authority": "execute"}
        )


def test_endpoint_schema_is_strict_and_bounded() -> None:
    schema = semantic_transition_proposal_json_schema()

    assert schema["additionalProperties"] is False
    assert schema["properties"]["mutations"]["maxItems"] == 3
    assert schema["properties"]["references"]["maxItems"] == 4
    assert set(schema["required"]) == set(schema["properties"])


def test_context_is_bounded_and_excludes_canonical_ids(active_state) -> None:
    context = build_semantic_shadow_context(active_state, "А теперь наоборот")
    serialized = json.dumps(context.model_dump(mode="json"), ensure_ascii=False)

    assert context.current_active_state["operation"] == "show"
    assert "ГП ТГ Томск" in serialized
    assert "BAL:secret" not in serialized
    assert "geo:secret" not in serialized
    assert "entity_id" not in serialized
    assert len(context.recent_semantic_turns) <= 4
    assert len(context.recent_addressable_results) <= 4
    assert context.addressable_result_count == 0
    assert context.directed_relation_count == 0


def test_context_keeps_only_four_recent_turns_and_results(active_state) -> None:
    store = InMemoryContextStore()
    state = store.create(active_state.session_id)
    intent = active_state.active_dialog_scope.intent
    for index in range(6):
        turn_id = f"turn-{index}"
        state = store.commit(
            state.session_id,
            state.revision,
            ContextMutation(
                turn_id=turn_id,
                user_message=f"message-{index}",
                replace_intent=intent,
            ),
            TransitionOutcome.SUCCESS,
            result=ResultReference(
                turn_id=turn_id,
                status=TransitionOutcome.SUCCESS,
                row_count=1,
                facts=[{"value": index}],
            ),
        )

    context = build_semantic_shadow_context(state, "Сравни последние два")

    assert len(context.recent_semantic_turns) == 4
    assert len(context.recent_addressable_results) == 4
    assert context.recent_semantic_turns[0]["user"] == "message-2"
    assert context.recent_semantic_turns[-1]["user"] == "message-5"
    assert context.addressable_result_count == 4
    assert context.recent_addressable_results[-1]["relative_position"] == "most_recent"
    assert context.recent_addressable_results[-2]["relative_position"] == "previous"


def test_selector_product_space_is_explicit() -> None:
    assert SemanticReference(kind="last_two_results", selector="first").selector.value == "first"
    assert SemanticReference(kind="last_two_results", selector="second").selector.value == "second"
    assert SemanticReference(kind="previous_result", selector=None).selector is None

    with pytest.raises(ValidationError):
        SemanticReference(kind="last_two_results", selector="last")
    with pytest.raises(ValidationError):
        SemanticReference(kind="previous_result", selector="first")
