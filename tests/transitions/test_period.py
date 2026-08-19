from __future__ import annotations

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    ContextContractV2,
    ContextMutation,
    MutationAction,
    Operation,
    PeriodRef,
    TransitionOutcome,
)
from balance_chat.reducer import apply_context_transition, reduce_intent
from balance_chat.transitions.period import (
    detect_period_followup,
    detect_period_transition,
    explicit_single_day_period,
)
from balance_chat.transitions.types import TransitionKind


def _intent() -> AnalysisIntent:
    return AnalysisIntent(
        operation=Operation.SHOW,
        operands=[AnalysisOperand(operand_id="metric", metric="distribution")],
        periods=[PeriodRef(date_from="2025-05-01", date_to="2025-06-01")],
    )


def _state() -> ContextContractV2:
    return apply_context_transition(
        ContextContractV2(session_id="session"),
        ContextMutation(
            turn_id="initial",
            user_message="initial",
            replace_intent=_intent(),
        ),
        TransitionOutcome.SUCCESS,
    )


def test_period_transition_produces_the_existing_patch_contract() -> None:
    decision = detect_period_transition(_state(), "А за апрель?", "turn-2")

    assert decision is not None
    assert decision.kind == TransitionKind.PATCH
    assert decision.interpretation_mode == "deterministic_period_patch"
    assert decision.mutation is not None
    assert decision.mutation.replace_intent is None
    assert decision.mutation.patch.periods.action == MutationAction.SET
    assert decision.mutation.patch.periods.value == [
        PeriodRef(date_from="2025-04-01", date_to="2025-05-01")
    ]
    assert reduce_intent(_state(), decision.mutation).periods == [
        PeriodRef(date_from="2025-04-01", date_to="2025-05-01")
    ]


def test_period_transition_serialization_is_stable() -> None:
    state = _state()
    decision = detect_period_transition(state, "А за январь 2025?", "turn-2")

    assert decision is not None and decision.mutation is not None
    serialized = ContextMutation.model_validate(
        decision.mutation.model_dump(mode="json")
    )
    assert serialized.model_dump(mode="json") == decision.mutation.model_dump(
        mode="json"
    )
    assert reduce_intent(state, serialized) == reduce_intent(
        state, decision.mutation
    )


def test_period_candidate_and_explicit_day_parser_are_processor_independent() -> None:
    candidate = detect_period_followup("А за 1 апреля 2025?", _intent())

    assert candidate is not None
    assert candidate.periods == (
        PeriodRef(date_from="2025-04-01", date_to="2025-04-02"),
    )
    assert explicit_single_day_period("за 1 апреля 2025") == candidate.periods[0]
