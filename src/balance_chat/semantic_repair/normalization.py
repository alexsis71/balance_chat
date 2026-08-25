from __future__ import annotations

from dataclasses import dataclass

from .contracts import (
    ReferenceKind,
    SemanticMutationKind,
    SemanticTransitionProposal,
)


@dataclass(frozen=True, slots=True)
class ProposalNormalizationResult:
    proposal: SemanticTransitionProposal
    actions: tuple[str, ...] = ()


def normalize_proposal(
    proposal: SemanticTransitionProposal,
) -> ProposalNormalizationResult:
    """Remove only provably redundant shadow-contract representation."""

    kinds = {item.kind for item in proposal.mutations}
    has_semantic_operation = bool(
        kinds
        & {
            SemanticMutationKind.SET_OPERATION,
            SemanticMutationKind.SET_COMPARISON,
        }
    )
    has_bounded_result_reference = any(
        item.kind in {ReferenceKind.PREVIOUS_RESULT, ReferenceKind.LAST_TWO_RESULTS}
        for item in proposal.references
    )
    if not (
        has_semantic_operation
        and has_bounded_result_reference
        and SemanticMutationKind.REFERENCE_PRIOR_RESULT in kinds
    ):
        return ProposalNormalizationResult(proposal=proposal)
    mutations = [
        item
        for item in proposal.mutations
        if item.kind != SemanticMutationKind.REFERENCE_PRIOR_RESULT
    ]
    return ProposalNormalizationResult(
        proposal=proposal.model_copy(update={"mutations": mutations}, deep=True),
        actions=("remove_redundant_reference_prior_result",),
    )
