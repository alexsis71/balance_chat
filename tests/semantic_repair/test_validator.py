from __future__ import annotations

import pytest

from balance_chat.semantic_repair.validator import ProposalValidator


def _proposal(**updates):
    payload = {
        "action": "patch",
        "mutations": [{"kind": "swap_direction", "value": None}],
        "references": [],
        "unresolved_mentions": [],
        "clarification_question": None,
        "confidence": 0.9,
        "reason_code": "reverse_requested",
    }
    payload.update(updates)
    return payload


def test_valid_swap_direction_proposal() -> None:
    result = ProposalValidator().validate(_proposal())

    assert result.valid is True
    assert result.proposal is not None


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (
            _proposal(mutations=[{"kind": "set_geo", "value": "Москва"}]),
            "unknown_mutation_kind",
        ),
        (
            _proposal(references=[{"kind": "arbitrary_pointer", "selector": None}]),
            "unknown_reference_kind",
        ),
        (
            _proposal(
                mutations=[
                    {"kind": "swap_direction", "value": None},
                    {"kind": "set_operation", "value": "compare"},
                ]
            ),
            "unsupported_multi_field_combination",
        ),
        (_proposal(confidence=1.5), "invalid_confidence"),
        (
            _proposal(unresolved_mentions=["BAL:2010000039953"]),
            "canonical_id_forbidden",
        ),
    ],
)
def test_validator_rejects_unsafe_payload(payload, error) -> None:
    result = ProposalValidator().validate(payload)

    assert result.valid is False
    assert error in result.errors


def test_patch_requires_mutation() -> None:
    result = ProposalValidator().validate(_proposal(mutations=[]))

    assert result.valid is False
    assert "patch_requires_mutation" in result.errors


def test_clarify_requires_question_and_cannot_mutate() -> None:
    result = ProposalValidator().validate(
        _proposal(action="clarify", clarification_question=None)
    )

    assert result.valid is False
    assert "clarify_requires_question" in result.errors
    assert "clarify_cannot_mutate" in result.errors


def test_mutation_count_and_output_size_are_bounded() -> None:
    result = ProposalValidator(max_mutations=3, max_output_chars=256).validate(
        _proposal(
            mutations=[
                {"kind": "set_operation", "value": "compare"},
                {"kind": "set_comparison", "value": "last_two_results"},
                {"kind": "reference_prior_result", "value": None},
                {"kind": "swap_direction", "value": None},
            ]
        ),
        output_chars=257,
    )

    assert result.valid is False
    assert "too_many_mutations" in result.errors
    assert "output_too_large" in result.errors


def test_reference_prior_result_requires_bounded_result_reference() -> None:
    result = ProposalValidator().validate(
        _proposal(
            mutations=[{"kind": "reference_prior_result", "value": None}],
            references=[{"kind": "active_state", "selector": None}],
        )
    )

    assert result.valid is False
    assert "prior_result_reference_required" in result.errors
