from __future__ import annotations

from dataclasses import dataclass

from .context import SemanticShadowContext
from .contracts import (
    ProposalAction,
    ReferenceKind,
    SemanticMutationKind,
    SemanticTransitionProposal,
)


@dataclass(frozen=True, slots=True)
class ContextualGateResult:
    accepted: bool
    reasons: tuple[str, ...] = ()


def evaluate_contextual_gate(
    proposal: SemanticTransitionProposal,
    context: SemanticShadowContext,
) -> ContextualGateResult:
    reasons: list[str] = []
    if (
        any(item.kind == ReferenceKind.LAST_TWO_RESULTS for item in proposal.references)
        and context.addressable_result_count < 2
    ):
        reasons.append("g3_insufficient_addressable_results")

    if any(
        item.kind == SemanticMutationKind.SWAP_DIRECTION
        for item in proposal.mutations
    ) and (
        context.directed_relation_count != 1 or context.direction_ambiguous
    ):
        reasons.append("g4_directed_relation_not_unique")

    if proposal.action == ProposalAction.PATCH and proposal.unresolved_mentions:
        reasons.append("g5_patch_has_unresolved_mentions")

    unique_reasons = tuple(dict.fromkeys(reasons))
    return ContextualGateResult(
        accepted=not unique_reasons,
        reasons=unique_reasons,
    )
