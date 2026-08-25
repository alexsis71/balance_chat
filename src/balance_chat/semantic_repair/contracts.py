from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from ..contracts import ContractModel


class ProposalAction(StrEnum):
    PATCH = "patch"
    REBUILD = "rebuild"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"


class SemanticMutationKind(StrEnum):
    SET_OPERATION = "set_operation"
    SWAP_DIRECTION = "swap_direction"
    SET_COMPARISON = "set_comparison"
    REFERENCE_PRIOR_RESULT = "reference_prior_result"


class ReferenceKind(StrEnum):
    ACTIVE_STATE = "active_state"
    PREVIOUS_RESULT = "previous_result"
    LAST_TWO_RESULTS = "last_two_results"
    PREVIOUS_PERIOD = "previous_period"
    PREVIOUS_ENTITY = "previous_entity"


class ReferenceSelector(StrEnum):
    FIRST = "first"
    SECOND = "second"


class ClarificationReason(StrEnum):
    MISSING_SECOND_RESULT = "missing_second_result"
    AMBIGUOUS_RESULT_REFERENCE = "ambiguous_result_reference"
    AMBIGUOUS_DIRECTION = "ambiguous_direction"
    ENTITY_ROLE_AMBIGUOUS = "entity_role_ambiguous"


class SemanticMutation(ContractModel):
    kind: SemanticMutationKind
    value: str | None = Field(default=None, max_length=80)


class SemanticReference(ContractModel):
    kind: ReferenceKind
    selector: ReferenceSelector | None = None

    @model_validator(mode="after")
    def validate_selector_product_space(self) -> "SemanticReference":
        if self.kind != ReferenceKind.LAST_TWO_RESULTS and self.selector is not None:
            raise ValueError("selector is valid only for last_two_results")
        return self


class SemanticTransitionProposal(ContractModel):
    action: ProposalAction
    mutations: list[SemanticMutation] = Field(default_factory=list)
    references: list[SemanticReference] = Field(default_factory=list)
    unresolved_mentions: list[str] = Field(default_factory=list)
    clarification_question: str | None = Field(default=None, max_length=500)
    clarification_reason: ClarificationReason | None = None
    confidence: float | None = None
    reason_code: str | None = Field(default=None, max_length=80)


class ShadowValidationStatus(StrEnum):
    VALID = "valid"
    REJECTED = "rejected"
    MALFORMED = "malformed"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    SKIPPED = "skipped"


class ContextualGateStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NOT_EVALUATED = "not_evaluated"


class SemanticShadowResult(ContractModel):
    eligible: bool
    invoked: bool
    proposal: SemanticTransitionProposal | None = None
    validation_status: ShadowValidationStatus
    validation_errors: list[str] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0)
    model: str | None = None
    context_turn_count: int = Field(default=0, ge=0, le=4)
    context_result_count: int = Field(default=0, ge=0, le=4)
    context_size_chars: int = Field(default=0, ge=0)
    raw_payload: dict[str, Any] | None = None
    raw_validation_status: ShadowValidationStatus | None = None
    raw_validation_errors: list[str] = Field(default_factory=list)
    normalized_proposal: SemanticTransitionProposal | None = None
    normalization_actions: list[str] = Field(default_factory=list)
    contextual_gate_status: ContextualGateStatus = ContextualGateStatus.NOT_EVALUATED
    contextual_gate_reasons: list[str] = Field(default_factory=list)


def semantic_transition_proposal_json_schema() -> dict[str, Any]:
    """Return the small strict schema sent to the OpenAI-compatible endpoint."""

    nullable_string = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": [item.value for item in ProposalAction],
            },
            "mutations": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [item.value for item in SemanticMutationKind],
                        },
                        "value": nullable_string,
                    },
                    "required": ["kind", "value"],
                },
            },
            "references": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "kind": {"const": ReferenceKind.LAST_TWO_RESULTS.value},
                                "selector": {
                                    "anyOf": [
                                        {
                                            "type": "string",
                                            "enum": [item.value for item in ReferenceSelector],
                                        },
                                        {"type": "null"},
                                    ]
                                },
                            },
                            "required": ["kind", "selector"],
                        },
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": [
                                        item.value
                                        for item in ReferenceKind
                                        if item != ReferenceKind.LAST_TWO_RESULTS
                                    ],
                                },
                                "selector": {"type": "null"},
                            },
                            "required": ["kind", "selector"],
                        },
                    ]
                },
            },
            "unresolved_mentions": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string", "maxLength": 200},
            },
            "clarification_question": nullable_string,
            "clarification_reason": {
                "anyOf": [
                    {
                        "type": "string",
                        "enum": [item.value for item in ClarificationReason],
                    },
                    {"type": "null"},
                ]
            },
            "confidence": {
                "anyOf": [
                    {"type": "number", "minimum": 0, "maximum": 1},
                    {"type": "null"},
                ]
            },
            "reason_code": nullable_string,
        },
        "required": [
            "action",
            "mutations",
            "references",
            "unresolved_mentions",
            "clarification_question",
            "clarification_reason",
            "confidence",
            "reason_code",
        ],
    }
