from __future__ import annotations

import json

import pytest

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    ContextContractV2,
    ContextMutation,
    Operation,
    PeriodRef,
    TransitionOutcome,
)
from balance_chat.interpretation import (
    HybridInterpretationPolicy,
    InterpretationError,
    UnifiedInterpreter,
    _canonicalize_directives,
)
from balance_chat.reducer import apply_context_transition


class StubBackend:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.payload = None

    def invoke(self, payload):
        self.payload = payload
        return self.response


def _decision(bundle_version: str = "2026.07.6") -> dict:
    keep_scalar = {"action": "keep", "value": None, "source_scope": None}
    return {
        "contract_version": "1.0",
        "mode": "mutation",
        "normalized_message": "сравни с летом",
        "confidence": 0.94,
        "draft": {
            "operation": {
                "action": "set",
                "value": "compare_periods",
                "source_scope": None,
            },
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
            "entities": {"action": "keep", "mentions": []},
            "grouping": {
                "action": "keep",
                "values": [],
                "source_scope": None,
            },
            "grain": keep_scalar,
            "reverse_direction": False,
        },
        "clarification": None,
        "unsupported_capability": None,
        "assumptions": [],
        "metadata_bundle_version": bundle_version,
    }


def _state() -> ContextContractV2:
    initial = ContextContractV2(session_id="session-1")
    return apply_context_transition(
        initial,
        ContextMutation(
            turn_id="turn-1",
            user_message="Покажи поставки за весну 2025",
            replace_intent=AnalysisIntent(
                operation=Operation.SHOW,
                operands=[
                    AnalysisOperand(operand_id="supply", metric="distribution")
                ],
                periods=[
                    PeriodRef(date_from="2025-03-01", date_to="2025-06-01")
                ],
            ),
        ),
        TransitionOutcome.SUCCESS,
    )


def test_interpreter_validates_one_combined_contract() -> None:
    backend = StubBackend(_decision())
    interpreter = UnifiedInterpreter(backend)

    decision = interpreter.interpret(
        message="сравни с летом",
        state=_state(),
        capabilities=["compare_periods", "distribution"],
        domain_hints=["лето = июнь–август"],
        metadata_bundle_version="2026.07.6",
    )

    assert decision.mode == "mutation"
    assert decision.draft.periods.action == "reference"
    user_payload = json.loads(backend.payload["messages"][1]["content"])
    assert user_payload["context"]["last_successful_scope"] is not None


def test_interpreter_rejects_changed_metadata_version() -> None:
    interpreter = UnifiedInterpreter(StubBackend(_decision("other")))
    with pytest.raises(InterpretationError, match="metadata bundle"):
        interpreter.interpret(
            message="сравни с летом",
            state=_state(),
            capabilities=[],
            domain_hints=[],
            metadata_bundle_version="2026.07.6",
        )


def test_interpreter_receives_pending_clarification_and_typed_answer() -> None:
    from balance_chat.contracts import PendingClarification, ClarificationContract

    backend = StubBackend(_decision())
    interpreter = UnifiedInterpreter(backend)
    state = ContextContractV2(
        session_id="session",
        pending_clarification=PendingClarification(
            turn_id="turn-1",
            questions=[ClarificationContract(
                clarification_id="clarify-1",
                question="Какой период?",
                options=["май 2025", "июнь 2025"],
            )],
        ),
    )
    answer = {
        "source_turn_id": "turn-1",
        "clarification_id": "clarify-1",
        "selected_option": "май 2025",
    }

    interpreter.interpret(
        message="май 2025",
        state=state,
        capabilities=["show"],
        domain_hints=[],
        metadata_bundle_version="2026.07.6",
        clarification_answer=answer,
    )

    model_input = json.loads(backend.payload["messages"][1]["content"])
    assert model_input["clarification_answer"] == answer
    assert model_input["context"]["pending_clarification"]["turn_id"] == "turn-1"


def test_interpreter_receives_bounded_evidence_validation_feedback() -> None:
    backend = StubBackend(_decision())

    UnifiedInterpreter(backend).interpret(
        message="сравни с летом",
        state=_state(),
        capabilities=["compare_periods"],
        domain_hints=[],
        metadata_bundle_version="2026.07.6",
        validation_feedback=[
            "operand_entity_scope_missing:op1",
            "explicit_business_not_bound:ТГ Томск",
        ],
    )

    model_input = json.loads(backend.payload["messages"][1]["content"])
    assert model_input["validation_feedback"] == [
        "operand_entity_scope_missing:op1",
        "explicit_business_not_bound:ТГ Томск",
    ]


def test_empty_set_directives_are_canonicalized_to_explicit_clear() -> None:
    raw = _decision()
    raw["draft"]["grouping"] = {"action": "set", "values": []}
    raw["draft"]["grain"] = {"action": "set", "value": None}

    normalized, fields = _canonicalize_directives(raw)

    assert normalized["draft"]["grouping"]["action"] == "clear"
    assert normalized["draft"]["grain"]["action"] == "clear"
    assert fields == ["grain", "grouping"]


def test_hybrid_policy_skips_unambiguous_standalone_turn() -> None:
    policy = HybridInterpretationPolicy()
    assert not policy.should_invoke(
        "Покажи поставки в Казань за май 2025",
        ContextContractV2(session_id="session-1"),
    )


def test_empty_context_exposes_only_standalone_interpretation_modes() -> None:
    raw = _decision()
    raw["mode"] = "standalone"
    backend = StubBackend(raw)
    interpreter = UnifiedInterpreter(backend)

    interpreter.interpret(
        message="покажи распределение за май 2025",
        state=ContextContractV2(session_id="session-1"),
        capabilities=["show", "distribution"],
        domain_hints=[],
        metadata_bundle_version="2026.07.6",
    )

    assert backend.payload["allowed_modes"] == [
        "standalone", "clarify", "unsupported"
    ]


def test_hybrid_policy_invokes_for_context_reference() -> None:
    policy = HybridInterpretationPolicy()
    assert policy.should_invoke("А сравни их", _state())
