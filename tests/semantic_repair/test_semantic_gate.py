from balance_chat.semantic_repair.context import SemanticShadowContext
from balance_chat.semantic_repair.contracts import SemanticTransitionProposal
from balance_chat.semantic_repair.semantic_gate import evaluate_contextual_gate


def _context(*, results=2, operands=None):
    return SemanticShadowContext(
        current_active_state={"operation": "show", "operands": operands or []},
        recent_semantic_turns=[],
        recent_addressable_results=[{"recency": index + 1} for index in range(results)],
        current_user_message="test",
    )


def _proposal(mutations, references=None, unresolved=None):
    return SemanticTransitionProposal.model_validate(
        {
            "action": "patch", "mutations": mutations,
            "references": references or [], "unresolved_mentions": unresolved or [],
        }
    )


def test_g3_rejects_pair_reference_with_only_one_result() -> None:
    proposal = _proposal(
        [{"kind": "set_operation", "value": "compare"}],
        [{"kind": "last_two_results", "selector": None}],
    )

    result = evaluate_contextual_gate(proposal, _context(results=1))

    assert result.accepted is False
    assert result.reasons == ("g3_insufficient_addressable_results",)


def test_g4_accepts_exactly_one_explicit_directed_relation() -> None:
    proposal = _proposal([{"kind": "swap_direction", "value": None}])
    operand = {"metric": "distribution", "entities": [
        {"role": "source"}, {"role": "destination"}
    ]}

    assert evaluate_contextual_gate(proposal, _context(operands=[operand])).accepted is True


def test_g4_rejects_zero_and_two_directed_relations() -> None:
    proposal = _proposal([{"kind": "swap_direction", "value": None}])
    operand = {"metric": "distribution", "entities": [
        {"role": "source"}, {"role": "destination"}
    ]}

    assert evaluate_contextual_gate(proposal, _context()).accepted is False
    assert evaluate_contextual_gate(proposal, _context(operands=[operand, operand])).accepted is False


def test_g4_accepts_reachable_full_balance_directed_shape() -> None:
    proposal = _proposal([{"kind": "swap_direction", "value": None}])
    operand = {"metric": "distribution", "entities": [
        {"role": "balance"}, {"role": "destination"}, {"role": "article"}
    ]}

    assert evaluate_contextual_gate(proposal, _context(operands=[operand])).accepted is True


def test_g5_rejects_patch_with_unresolved_mentions() -> None:
    proposal = _proposal(
        [{"kind": "reference_prior_result", "value": None}],
        [{"kind": "previous_result", "selector": None}], ["тот результат"],
    )

    result = evaluate_contextual_gate(proposal, _context())

    assert result.accepted is False
    assert result.reasons == ("g5_patch_has_unresolved_mentions",)
