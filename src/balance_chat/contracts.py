from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Operation(StrEnum):
    SHOW = "show"
    AGGREGATE = "aggregate"
    COMPARE = "compare"
    COMPARE_PERIODS = "compare_periods"
    GROUP = "group"
    MULTI_STEP = "multi_step"


class MutationAction(StrEnum):
    KEEP = "keep"
    SET = "set"
    ADD = "add"
    REMOVE = "remove"
    CLEAR = "clear"
    REFERENCE = "reference"


class TransitionOutcome(StrEnum):
    SUCCESS = "success"
    NO_DATA = "no_data"
    ERROR = "error"
    CLARIFICATION = "clarification"


class PeriodRef(ContractModel):
    date_from: date
    date_to: date
    label: str | None = None

    @model_validator(mode="after")
    def validate_exclusive_end(self) -> "PeriodRef":
        if self.date_from >= self.date_to:
            raise ValueError("period must use a non-empty exclusive-end range")
        return self


class MetadataVersionRef(ContractModel):
    bundle_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    bundle_version: str
    schema_version: str


class CanonicalEntityRef(ContractModel):
    entity_id: str
    entity_type: Literal[
        "balance",
        "article",
        "route",
        "geo_object",
        "geo_group",
        "organization",
    ]
    display_name: str


class OperandEntityRef(ContractModel):
    role: Literal["balance", "source", "destination", "route", "article", "subject"]
    entity: CanonicalEntityRef


class AnalysisOperand(ContractModel):
    operand_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    metric: str
    aggregate_type: str = "sum"
    entities: list[OperandEntityRef] = Field(default_factory=list)
    periods: list[PeriodRef] = Field(default_factory=list)
    unit: str | None = None

    @model_validator(mode="after")
    def validate_unique_roles(self) -> "AnalysisOperand":
        roles = [item.role for item in self.entities]
        if len(roles) != len(set(roles)):
            raise ValueError("operand entity roles must be unique")
        return self


class GroupingSpec(ContractModel):
    dimension: Literal[
        "geo",
        "geo_group",
        "balance",
        "article",
        "source",
        "destination",
        "route",
        "period",
    ]
    canonical_group_id: str | None = None
    aggregate_type: str = "sum"


class ComparisonSpec(ContractModel):
    baseline_operand_id: str
    target_operand_id: str
    delta_direction: Literal["target_minus_baseline"] = "target_minus_baseline"
    percent_base: Literal["baseline"] = "baseline"


class AnalysisIntent(ContractModel):
    intent_id: str = Field(default_factory=lambda: str(uuid4()))
    operation: Operation
    operands: list[AnalysisOperand] = Field(min_length=1)
    periods: list[PeriodRef] = Field(default_factory=list)
    grouping: list[GroupingSpec] = Field(default_factory=list)
    grain: Literal["day", "month", "quarter", "year", "total"] | None = None
    comparison: ComparisonSpec | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "AnalysisIntent":
        operand_ids = [operand.operand_id for operand in self.operands]
        if len(operand_ids) != len(set(operand_ids)):
            raise ValueError("operand_id must be unique within an intent")
        if self.comparison:
            referenced = {
                self.comparison.baseline_operand_id,
                self.comparison.target_operand_id,
            }
            if not referenced.issubset(set(operand_ids)):
                raise ValueError("comparison must reference existing operands")
        if self.operation == Operation.COMPARE and len(self.operands) < 2:
            raise ValueError("compare requires at least two operands")
        if self.operation == Operation.COMPARE_PERIODS and len(self.periods) != 2:
            raise ValueError("compare_periods requires exactly two global periods")
        return self


class ContextScope(ContractModel):
    intent: AnalysisIntent
    turn_id: str
    updated_at: datetime = Field(default_factory=utc_now)


class EntityMemoryEntry(ContractModel):
    role: str
    entity: CanonicalEntityRef
    first_turn_id: str
    last_turn_id: str
    mention_count: int = Field(default=1, ge=1)


class ResultReference(ContractModel):
    result_id: str = Field(default_factory=lambda: str(uuid4()))
    turn_id: str
    status: TransitionOutcome
    resolved_plan_hash: str | None = None
    row_count: int | None = Field(default=None, ge=0)
    facts: list[dict[str, Any]] = Field(default_factory=list)


class TurnReference(ContractModel):
    turn_id: str
    user_message: str
    normalized_message: str | None = None
    outcome: TransitionOutcome


class PendingClarification(ContractModel):
    turn_id: str
    questions: list[dict[str, Any]] = Field(min_length=1)


class FieldMutation(ContractModel):
    action: MutationAction
    value: Any = None

    @model_validator(mode="after")
    def validate_action_value(self) -> "FieldMutation":
        if self.action in {MutationAction.KEEP, MutationAction.CLEAR} and self.value is not None:
            raise ValueError(f"{self.action.value} mutation must not carry a value")
        if self.action in {
            MutationAction.SET,
            MutationAction.ADD,
            MutationAction.REMOVE,
            MutationAction.REFERENCE,
        } and self.value is None:
            raise ValueError(f"{self.action.value} mutation requires a value")
        if self.action == MutationAction.REFERENCE and not isinstance(self.value, str):
            raise ValueError("reference mutation value must be a context path")
        return self


class IntentPatch(ContractModel):
    operation: FieldMutation | None = None
    operands: FieldMutation | None = None
    periods: FieldMutation | None = None
    grouping: FieldMutation | None = None
    grain: FieldMutation | None = None
    comparison: FieldMutation | None = None


class ContextMutation(ContractModel):
    turn_id: str
    replace_intent: AnalysisIntent | None = None
    patch: IntentPatch = Field(default_factory=IntentPatch)
    user_message: str
    normalized_message: str | None = None


class ContextContractV2(ContractModel):
    contract_version: Literal["2.0"] = "2.0"
    session_id: str
    revision: int = Field(default=0, ge=0)
    metadata: MetadataVersionRef | None = None
    active_dialog_scope: ContextScope | None = None
    last_attempted_scope: ContextScope | None = None
    last_successful_scope: ContextScope | None = None
    entity_memory: list[EntityMemoryEntry] = Field(default_factory=list)
    result_references: list[ResultReference] = Field(default_factory=list)
    recent_turns: list[TurnReference] = Field(default_factory=list)
    pending_clarification: PendingClarification | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

