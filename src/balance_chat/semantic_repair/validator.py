from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any, Mapping

from pydantic import ValidationError

from .contracts import (
    ProposalAction,
    ReferenceKind,
    ReferenceSelector,
    SemanticMutationKind,
    SemanticTransitionProposal,
)


_CANONICAL_ID = re.compile(
    r"(?:\b(?:balance|article|route|geo|entity)_id\b|"
    r"\b(?:BAL|ART|ROUTE):[A-Za-z0-9_.:-]+|\bgeo:[A-Za-z0-9_.:-]+|"
    r"\b[0-9]{10,}\b)",
    re.IGNORECASE,
)
_ALLOWED_OPERATION_VALUES = {"compare", "calculate"}
_ALLOWED_COMPARISON_VALUES = {
    "absolute_difference",
    "percent_difference",
    "larger_value",
    "last_two_results",
    "named_periods",
}


@dataclass(frozen=True, slots=True)
class ProposalValidationResult:
    valid: bool
    proposal: SemanticTransitionProposal | None
    errors: tuple[str, ...] = ()


class ProposalValidator:
    def __init__(
        self,
        *,
        max_mutations: int = 3,
        max_references: int = 4,
        max_unresolved_mentions: int = 8,
        max_output_chars: int = 8192,
    ) -> None:
        self.max_mutations = max(1, int(max_mutations))
        self.max_references = max(1, int(max_references))
        self.max_unresolved_mentions = max(1, int(max_unresolved_mentions))
        self.max_output_chars = max(256, int(max_output_chars))

    def validate(
        self,
        payload: Mapping[str, Any],
        *,
        output_chars: int | None = None,
    ) -> ProposalValidationResult:
        errors: list[str] = []
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        size = len(serialized) if output_chars is None else int(output_chars)
        if size > self.max_output_chars:
            errors.append("output_too_large")
        if _CANONICAL_ID.search(serialized):
            errors.append("canonical_id_forbidden")

        action_value = payload.get("action")
        if action_value not in {item.value for item in ProposalAction}:
            errors.append("unknown_action")

        mutations = payload.get("mutations", [])
        references = payload.get("references", [])
        unresolved = payload.get("unresolved_mentions", [])
        if not isinstance(mutations, list):
            errors.append("mutations_must_be_list")
            mutations = []
        if not isinstance(references, list):
            errors.append("references_must_be_list")
            references = []
        if not isinstance(unresolved, list):
            errors.append("unresolved_mentions_must_be_list")
            unresolved = []
        if len(mutations) > self.max_mutations:
            errors.append("too_many_mutations")
        if len(references) > self.max_references:
            errors.append("too_many_references")
        if len(unresolved) > self.max_unresolved_mentions:
            errors.append("too_many_unresolved_mentions")

        mutation_kinds: list[str] = []
        for mutation in mutations:
            if not isinstance(mutation, Mapping):
                errors.append("invalid_mutation")
                continue
            kind = str(mutation.get("kind") or "")
            mutation_kinds.append(kind)
            if kind not in {item.value for item in SemanticMutationKind}:
                errors.append("unknown_mutation_kind")
                continue
            value = mutation.get("value")
            if kind == SemanticMutationKind.SET_OPERATION:
                if value not in _ALLOWED_OPERATION_VALUES:
                    errors.append("invalid_operation_value")
            elif kind == SemanticMutationKind.SET_COMPARISON:
                if value not in _ALLOWED_COMPARISON_VALUES:
                    errors.append("invalid_comparison_value")
            elif value is not None:
                errors.append("mutation_value_forbidden")
        if len(mutation_kinds) != len(set(mutation_kinds)):
            errors.append("duplicate_mutation_kind")

        reference_kinds: list[str] = []
        for reference in references:
            if not isinstance(reference, Mapping):
                errors.append("invalid_reference")
                continue
            kind = str(reference.get("kind") or "")
            reference_kinds.append(kind)
            if kind not in {item.value for item in ReferenceKind}:
                errors.append("unknown_reference_kind")
                continue
            selector = reference.get("selector")
            if selector is not None and selector not in {
                item.value for item in ReferenceSelector
            }:
                errors.append("unknown_reference_selector")
            if kind != ReferenceKind.LAST_TWO_RESULTS and selector in {
                ReferenceSelector.FIRST,
                ReferenceSelector.SECOND,
            }:
                errors.append("reference_selector_mismatch")
        if len(reference_kinds) != len(set(reference_kinds)):
            errors.append("duplicate_reference_kind")

        confidence = payload.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            errors.append("invalid_confidence")

        question = payload.get("clarification_question")
        if action_value == ProposalAction.PATCH:
            if not mutations:
                errors.append("patch_requires_mutation")
            if question:
                errors.append("patch_cannot_clarify")
            kinds = set(mutation_kinds)
            if SemanticMutationKind.SWAP_DIRECTION in kinds and len(kinds) != 1:
                errors.append("unsupported_multi_field_combination")
            if (
                SemanticMutationKind.REFERENCE_PRIOR_RESULT in kinds
                and not set(reference_kinds).intersection(
                    {ReferenceKind.PREVIOUS_RESULT, ReferenceKind.LAST_TWO_RESULTS}
                )
            ):
                errors.append("prior_result_reference_required")
        elif action_value == ProposalAction.CLARIFY:
            if not isinstance(question, str) or not question.strip():
                errors.append("clarify_requires_question")
            if mutations:
                errors.append("clarify_cannot_mutate")
        elif action_value == ProposalAction.UNSUPPORTED:
            if mutations or references or question:
                errors.append("unsupported_must_be_empty")
        elif action_value == ProposalAction.REBUILD:
            errors.append("rebuild_not_supported_in_pr5")

        proposal: SemanticTransitionProposal | None = None
        try:
            proposal = SemanticTransitionProposal.model_validate(payload)
        except ValidationError:
            errors.append("proposal_schema_invalid")
        unique_errors = tuple(dict.fromkeys(errors))
        return ProposalValidationResult(
            valid=not unique_errors,
            proposal=proposal,
            errors=unique_errors,
        )
