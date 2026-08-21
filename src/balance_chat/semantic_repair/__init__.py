"""Shadow-only semantic transition proposals with no production authority."""

from .contracts import (
    ProposalAction,
    ReferenceKind,
    ReferenceSelector,
    SemanticMutation,
    SemanticMutationKind,
    SemanticReference,
    SemanticShadowResult,
    SemanticTransitionProposal,
    ShadowValidationStatus,
    semantic_transition_proposal_json_schema,
)
from .shadow import SemanticShadowRunner
from .validator import ProposalValidationResult, ProposalValidator

__all__ = [
    "ProposalAction",
    "ProposalValidationResult",
    "ProposalValidator",
    "ReferenceKind",
    "ReferenceSelector",
    "SemanticMutation",
    "SemanticMutationKind",
    "SemanticReference",
    "SemanticShadowResult",
    "SemanticShadowRunner",
    "SemanticTransitionProposal",
    "ShadowValidationStatus",
    "semantic_transition_proposal_json_schema",
]
