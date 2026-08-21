from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from balance_chat.semantic_repair.context import build_semantic_shadow_context
from balance_chat.semantic_repair.contracts import (
    ProposalAction,
    SemanticMutation,
    SemanticTransitionProposal,
    semantic_transition_proposal_json_schema,
)


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
