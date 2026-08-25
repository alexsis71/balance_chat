from balance_chat.semantic_repair.contracts import SemanticTransitionProposal
from balance_chat.semantic_repair.normalization import normalize_proposal


def test_redundant_reference_mutation_is_removed_without_changing_reference() -> None:
    raw = SemanticTransitionProposal.model_validate(
        {
            "action": "patch",
            "mutations": [
                {"kind": "set_operation", "value": "compare"},
                {"kind": "set_comparison", "value": "last_two_results"},
                {"kind": "reference_prior_result", "value": None},
            ],
            "references": [{"kind": "last_two_results", "selector": "first"}],
            "unresolved_mentions": [],
            "clarification_question": None,
            "clarification_reason": None,
            "confidence": 0.9,
            "reason_code": "compare_pair",
        }
    )

    result = normalize_proposal(raw)

    assert [item.kind.value for item in result.proposal.mutations] == [
        "set_operation", "set_comparison"
    ]
    assert result.proposal.references == raw.references
    assert result.actions == ("remove_redundant_reference_prior_result",)
    assert len(raw.mutations) == 3


def test_standalone_reference_selection_is_not_normalized() -> None:
    raw = SemanticTransitionProposal.model_validate(
        {
            "action": "patch",
            "mutations": [{"kind": "reference_prior_result", "value": None}],
            "references": [{"kind": "last_two_results", "selector": "second"}],
        }
    )

    result = normalize_proposal(raw)

    assert result.proposal == raw
    assert result.actions == ()
