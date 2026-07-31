from __future__ import annotations

import pytest
import tempfile
from pathlib import Path

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ContextContractV2,
    ContextMutation,
    FieldMutation,
    IntentPatch,
    MutationAction,
    OperandEntityRef,
    Operation,
    PeriodRef,
    TransitionOutcome,
)
from balance_chat.reducer import ContextReductionError, apply_context_transition
from balance_chat.store import InMemoryContextStore, RevisionConflict, SQLiteContextStore


def _entity(entity_id: str, name: str) -> OperandEntityRef:
    return OperandEntityRef(
        role="destination",
        entity=CanonicalEntityRef(
            entity_id=entity_id,
            entity_type="geo_object",
            display_name=name,
        ),
    )


def _intent(
    destination_id: str = "geo:kazan",
    destination_name: str = "Казань",
) -> AnalysisIntent:
    return AnalysisIntent(
        operation=Operation.SHOW,
        operands=[
            AnalysisOperand(
                operand_id="supply",
                metric="distribution",
                entities=[_entity(destination_id, destination_name)],
            )
        ],
        periods=[PeriodRef(date_from="2025-06-01", date_to="2025-09-01")],
        grain="total",
    )


def _initial_mutation(turn_id: str = "turn-1") -> ContextMutation:
    return ContextMutation(
        turn_id=turn_id,
        replace_intent=_intent(),
        user_message="Покажи поставки в Казань летом 2025",
    )


def test_success_updates_all_scopes() -> None:
    state = ContextContractV2(session_id="session-1")
    updated = apply_context_transition(
        state,
        _initial_mutation(),
        TransitionOutcome.SUCCESS,
    )
    assert updated.revision == 1
    assert updated.active_dialog_scope is not None
    assert updated.last_attempted_scope is not None
    assert updated.last_successful_scope is not None
    assert updated.entity_memory[0].entity.display_name == "Казань"


def test_no_data_does_not_replace_last_successful_scope() -> None:
    state = apply_context_transition(
        ContextContractV2(session_id="session-1"),
        _initial_mutation(),
        TransitionOutcome.SUCCESS,
    )
    no_data = ContextMutation(
        turn_id="turn-2",
        replace_intent=_intent("geo:unknown", "Неизвестный GEO"),
        user_message="Покажи поставки в неизвестный GEO",
    )
    updated = apply_context_transition(state, no_data, TransitionOutcome.NO_DATA)
    successful = updated.last_successful_scope.intent.operands[0]
    attempted = updated.last_attempted_scope.intent.operands[0]
    assert successful.entities[0].entity.display_name == "Казань"
    assert attempted.entities[0].entity.display_name == "Неизвестный GEO"


def test_reference_preserves_canonical_period_without_reparsing() -> None:
    state = apply_context_transition(
        ContextContractV2(session_id="session-1"),
        _initial_mutation(),
        TransitionOutcome.SUCCESS,
    )
    mutation = ContextMutation(
        turn_id="turn-2",
        user_message="А в Ярославль?",
        replace_intent=_intent("geo:yaroslavl", "Ярославль"),
        patch=IntentPatch(
            periods=FieldMutation(
                action=MutationAction.REFERENCE,
                value="last_successful_scope.intent.periods",
            )
        ),
    )
    updated = apply_context_transition(state, mutation, TransitionOutcome.SUCCESS)
    assert updated.active_dialog_scope.intent.periods == state.last_successful_scope.intent.periods
    assert (
        updated.active_dialog_scope.intent.operands[0].entities[0].entity.display_name
        == "Ярославль"
    )


def test_invalid_reference_fails_explicitly() -> None:
    state = ContextContractV2(session_id="session-1")
    mutation = ContextMutation(
        turn_id="turn-1",
        replace_intent=_intent(),
        user_message="test",
        patch=IntentPatch(
            periods=FieldMutation(
                action=MutationAction.REFERENCE,
                value="result_references.0.facts",
            )
        ),
    )
    with pytest.raises(ContextReductionError, match="unsupported context reference"):
        apply_context_transition(state, mutation, TransitionOutcome.SUCCESS)


def test_store_rejects_stale_revision() -> None:
    store = InMemoryContextStore()
    store.create("session-1")
    store.commit(
        "session-1",
        0,
        _initial_mutation(),
        TransitionOutcome.SUCCESS,
    )
    with pytest.raises(RevisionConflict):
        store.commit(
            "session-1",
            0,
            ContextMutation(
                turn_id="turn-2",
                replace_intent=_intent(),
                user_message="Повтори",
            ),
            TransitionOutcome.SUCCESS,
        )


def test_sqlite_store_recovers_context_after_reopen() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        path = Path(temporary_directory) / "context.sqlite3"
        writer = SQLiteContextStore(path)
        writer.create("session-1")
        writer.commit(
            "session-1",
            0,
            _initial_mutation(),
            TransitionOutcome.SUCCESS,
        )

        recovered = SQLiteContextStore(path).get("session-1")

    assert recovered.revision == 1
    assert (
        recovered.last_successful_scope.intent.operands[0].entities[0].entity.display_name
        == "Казань"
    )
