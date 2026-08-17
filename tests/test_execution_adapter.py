from __future__ import annotations

import pytest

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    ContextContractV2,
    ContextMutation,
    FieldMutation,
    IntentPatch,
    MutationAction,
    Operation,
    PeriodRef,
    TransitionOutcome,
)
from balance_chat.execution_adapter import ReducerExecutionAdapter
from balance_chat.reducer import ContextReductionError, apply_context_transition


def _intent(*, operation: Operation = Operation.SHOW) -> AnalysisIntent:
    return AnalysisIntent(
        operation=operation,
        operands=[AnalysisOperand(operand_id="supply", metric="distribution")],
        periods=[PeriodRef(date_from="2025-06-01", date_to="2025-07-01")],
        grain="total",
    )


def _active_state() -> ContextContractV2:
    return apply_context_transition(
        ContextContractV2(session_id="session-1"),
        ContextMutation(
            turn_id="turn-1",
            user_message="initial",
            replace_intent=_intent(),
        ),
        TransitionOutcome.SUCCESS,
    )


def test_replace_only_returns_the_replacement_intent() -> None:
    state = ContextContractV2(session_id="session-1")
    replacement = _intent()
    mutation = ContextMutation(
        turn_id="turn-1",
        user_message="compare",
        replace_intent=replacement,
    )

    effective = ReducerExecutionAdapter().effective_intent(state, mutation)

    assert effective == replacement


def test_patch_overlays_replacement_using_reducer_semantics() -> None:
    state = ContextContractV2(session_id="session-1")
    replacement = _intent()
    periods = [PeriodRef(date_from="2025-07-01", date_to="2025-08-01")]
    mutation = ContextMutation(
        turn_id="turn-1",
        user_message="july",
        replace_intent=replacement,
        patch=IntentPatch(
            periods=FieldMutation(action=MutationAction.SET, value=periods)
        ),
    )

    effective = ReducerExecutionAdapter().effective_intent(state, mutation)

    assert effective.periods == periods
    assert effective.operation == replacement.operation


def test_patch_without_a_base_propagates_reducer_error() -> None:
    state = ContextContractV2(session_id="session-1")
    mutation = ContextMutation(
        turn_id="turn-1",
        user_message="july",
        patch=IntentPatch(
            periods=FieldMutation(
                action=MutationAction.REFERENCE,
                value="last_successful_scope.intent.periods",
            )
        ),
    )

    with pytest.raises(
        ContextReductionError, match="initial transition requires replace_intent"
    ):
        ReducerExecutionAdapter().effective_intent(state, mutation)


def test_empty_mutation_fails_closed_instead_of_reexecuting_active_intent() -> None:
    state = _active_state()
    mutation = ContextMutation(turn_id="turn-2", user_message="repeat")

    with pytest.raises(ContextReductionError, match="empty executable mutation"):
        ReducerExecutionAdapter().effective_intent(state, mutation)


def test_reducer_errors_propagate_without_masking() -> None:
    state = _active_state()
    mutation = ContextMutation(
        turn_id="turn-2",
        user_message="invalid reference",
        patch=IntentPatch(
            periods=FieldMutation(
                action=MutationAction.REFERENCE,
                value="result_references.0.facts",
            )
        ),
    )

    with pytest.raises(ContextReductionError, match="unsupported context reference"):
        ReducerExecutionAdapter().effective_intent(state, mutation)


def test_materialization_does_not_mutate_state_or_mutation() -> None:
    state = _active_state()
    mutation = ContextMutation(
        turn_id="turn-2",
        user_message="july",
        patch=IntentPatch(
            periods=FieldMutation(
                action=MutationAction.SET,
                value=[PeriodRef(date_from="2025-07-01", date_to="2025-08-01")],
            )
        ),
    )
    state_before = state.model_dump(mode="python")
    mutation_before = mutation.model_dump(mode="python")

    ReducerExecutionAdapter().effective_intent(state, mutation)

    assert state.model_dump(mode="python") == state_before
    assert mutation.model_dump(mode="python") == mutation_before
