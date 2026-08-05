from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain_invariants import CANONICAL_VOLUME_UNIT, canonical_volume_unit


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
    RANK = "rank"
    CALCULATE = "calculate"
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
    unit: str = CANONICAL_VOLUME_UNIT

    @field_validator("unit", mode="before")
    @classmethod
    def normalize_volume_unit(cls, value: Any) -> str:
        return canonical_volume_unit(value) or CANONICAL_VOLUME_UNIT

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


class FormulaSpec(ContractModel):
    operator: Literal["delta", "ratio", "percent_of", "percent_change"]
    numerator_operand_id: str
    denominator_operand_id: str
    zero_division: Literal["no_data"] = "no_data"


class RankingSpec(ContractModel):
    direction: Literal["max", "min"]
    grain: Literal["day", "month", "quarter", "year"]
    bucket_aggregate: Literal["sum", "avg", "min", "max", "first", "last"]
    limit: int = Field(default=1, ge=1, le=100)
    return_dimension: Literal["period", "date", "entity"] = "period"


class AnalysisIntent(ContractModel):
    intent_id: str = Field(default_factory=lambda: str(uuid4()))
    operation: Operation
    operands: list[AnalysisOperand] = Field(min_length=1)
    periods: list[PeriodRef] = Field(default_factory=list)
    grouping: list[GroupingSpec] = Field(default_factory=list)
    grain: Literal["day", "month", "quarter", "year", "total"] | None = None
    comparison: ComparisonSpec | None = None
    formula: FormulaSpec | None = None
    ranking: RankingSpec | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "AnalysisIntent":
        operand_ids = [operand.operand_id for operand in self.operands]
        if len(operand_ids) != len(set(operand_ids)):
            raise ValueError("operand_id must be unique within an intent")
        if self.operation != Operation.COMPARE and self.comparison is not None:
            raise ValueError("comparison semantics are valid only for compare")
        if self.comparison:
            if (
                self.comparison.baseline_operand_id
                == self.comparison.target_operand_id
            ):
                raise ValueError("comparison requires two distinct operands")
            referenced = {
                self.comparison.baseline_operand_id,
                self.comparison.target_operand_id,
            }
            if not referenced.issubset(set(operand_ids)):
                raise ValueError("comparison must reference existing operands")
        if self.operation == Operation.COMPARE and len(self.operands) < 2:
            raise ValueError("compare requires at least two operands")
        if self.operation == Operation.COMPARE_PERIODS:
            if len(self.operands) != 1:
                raise ValueError("compare_periods requires exactly one operand")
            if len(self.periods) != 2:
                raise ValueError("compare_periods requires exactly two global periods")
        if self.operation == Operation.GROUP and not self.grouping:
            raise ValueError("group requires a canonical grouping specification")
        if self.operation != Operation.GROUP and self.grouping:
            raise ValueError("grouping specification is valid only for group")
        if self.operation == Operation.CALCULATE:
            if self.formula is None:
                raise ValueError("calculate requires a formula")
            formula_operands = {
                self.formula.numerator_operand_id,
                self.formula.denominator_operand_id,
            }
            if not formula_operands.issubset(set(operand_ids)):
                raise ValueError("formula must reference existing operands")
            if len(formula_operands) != 2:
                raise ValueError("formula requires two distinct operands")
        elif self.formula is not None:
            raise ValueError("formula semantics are valid only for calculate")
        if self.operation == Operation.RANK:
            if self.ranking is None:
                raise ValueError("rank requires ranking semantics")
            if len(self.operands) != 1:
                raise ValueError("rank requires exactly one operand")
        elif self.ranking is not None:
            raise ValueError("ranking semantics are valid only for rank")
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


class ResultMemoryWrite(ContractModel):
    turn_id: str
    query: str
    intent: AnalysisIntent
    result_id: str
    facts: list[dict[str, Any]] = Field(min_length=1)
    summary: dict[str, Any] = Field(default_factory=dict)


class TurnReference(ContractModel):
    turn_id: str
    user_message: str
    normalized_message: str | None = None
    outcome: TransitionOutcome


class ResolvedEntityTag(ContractModel):
    """Canonical entity occurrence exposed to the conversational model by handle."""

    handle: str = Field(pattern=r"^e_[0-9a-f]{16}$")
    operand_handle: str
    role: Literal["balance", "source", "destination", "route", "article", "subject"]
    entity: CanonicalEntityRef


class ResolvedPeriodTag(ContractModel):
    """Canonical exclusive-end period exposed to the model by handle."""

    handle: str = Field(pattern=r"^p_[0-9a-f]{16}$")
    owner_handle: str
    period: PeriodRef


class ResolvedOperandSnapshot(ContractModel):
    handle: str
    operand: AnalysisOperand


class ResolvedResultSnapshot(ContractModel):
    handle: str
    result_id: str
    row_count: int | None = Field(default=None, ge=0)
    facts: list[dict[str, Any]] = Field(default_factory=list, max_length=24)


class ResolvedTurnFrame(ContractModel):
    """Authoritative, bounded semantic record of one conversational turn."""

    turn_handle: str = Field(pattern=r"^t[0-9]{4,}$")
    turn_id: str
    revision: int = Field(ge=1)
    user_message: str
    normalized_message: str | None = None
    assistant_summary: str | None = Field(default=None, max_length=4000)
    outcome: TransitionOutcome
    intent: AnalysisIntent
    operands: list[ResolvedOperandSnapshot] = Field(default_factory=list)
    entities: list[ResolvedEntityTag] = Field(default_factory=list)
    periods: list[ResolvedPeriodTag] = Field(default_factory=list)
    result: ResolvedResultSnapshot | None = None


class ClarificationContract(ContractModel):
    clarification_id: str = Field(default_factory=lambda: str(uuid4()))
    question: str = Field(min_length=1, max_length=1000)
    options: list[str] = Field(min_length=2, max_length=4)


class ClarificationAnswer(ContractModel):
    source_turn_id: str
    clarification_id: str
    selected_option: str = Field(min_length=1, max_length=1000)


class PendingClarification(ContractModel):
    turn_id: str
    questions: list[ClarificationContract] = Field(min_length=1)


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
    assistant_summary: str | None = Field(default=None, max_length=4000)


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
    conversation_window: list[ResolvedTurnFrame] = Field(default_factory=list)
    pending_clarification: PendingClarification | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class InterpretationMode(StrEnum):
    STANDALONE = "standalone"
    MUTATION = "mutation"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"


class EntityMention(ContractModel):
    text: str = Field(min_length=1)
    role: Literal["balance", "source", "destination", "route", "article", "subject"]


class ScalarDirective(ContractModel):
    action: Literal["keep", "set", "clear", "reference"]
    value: str | None = None
    source_scope: Literal[
        "active_dialog_scope",
        "last_attempted_scope",
        "last_successful_scope",
    ] | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "ScalarDirective":
        if self.action == "set" and not self.value:
            raise ValueError("set directive requires value")
        if self.action == "reference" and self.source_scope is None:
            raise ValueError("reference directive requires source_scope")
        if self.action != "set" and self.value is not None:
            raise ValueError(f"{self.action} directive cannot carry value")
        if self.action != "reference" and self.source_scope is not None:
            raise ValueError("source_scope is valid only for reference")
        return self


class PeriodDirective(ContractModel):
    action: Literal["keep", "set", "add", "remove", "clear", "reference"]
    values: list[PeriodRef] = Field(default_factory=list)
    source_scope: Literal[
        "active_dialog_scope",
        "last_attempted_scope",
        "last_successful_scope",
    ] | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "PeriodDirective":
        if self.action in {"set", "add", "remove"} and not self.values:
            raise ValueError(f"{self.action} period directive requires values")
        if self.action not in {"set", "add", "remove"} and self.values:
            raise ValueError(f"{self.action} period directive cannot carry values")
        if self.action == "reference" and self.source_scope is None:
            raise ValueError("reference period directive requires source_scope")
        if self.action != "reference" and self.source_scope is not None:
            raise ValueError("source_scope is valid only for reference")
        return self


class StringListDirective(ContractModel):
    action: Literal["keep", "set", "add", "remove", "clear", "reference"]
    values: list[str] = Field(default_factory=list)
    source_scope: Literal[
        "active_dialog_scope",
        "last_attempted_scope",
        "last_successful_scope",
    ] | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "StringListDirective":
        if self.action in {"set", "add", "remove"} and not self.values:
            raise ValueError(f"{self.action} list directive requires values")
        if self.action not in {"set", "add", "remove"} and self.values:
            raise ValueError(f"{self.action} list directive cannot carry values")
        if self.action == "reference" and self.source_scope is None:
            raise ValueError("reference list directive requires source_scope")
        if self.action != "reference" and self.source_scope is not None:
            raise ValueError("source_scope is valid only for reference")
        return self


class EntityDirective(ContractModel):
    action: Literal["keep", "set", "add", "remove", "clear"]
    mentions: list[EntityMention] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_payload(self) -> "EntityDirective":
        if self.action in {"set", "add", "remove"} and not self.mentions:
            raise ValueError(f"{self.action} entity directive requires mentions")
        if self.action in {"keep", "clear"} and self.mentions:
            raise ValueError(f"{self.action} entity directive cannot carry mentions")
        return self


class ContextOperandDraft(ContractModel):
    """One operand assembled from an earlier handle and/or new textual mentions."""

    operand_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    source_operand_handle: str | None = None
    metric: str | None = None
    aggregate_type: str | None = None
    entity_mode: Literal["inherit", "replace", "add", "clear"] = "inherit"
    entity_handles: list[str] = Field(default_factory=list)
    entity_mentions: list[EntityMention] = Field(default_factory=list)
    period_mode: Literal["inherit", "replace", "add", "clear"] = "inherit"
    period_handles: list[str] = Field(default_factory=list)
    periods: list[PeriodRef] = Field(default_factory=list)
    unit: str | None = None
    reverse_direction: bool = False

    @field_validator("unit", mode="before")
    @classmethod
    def normalize_volume_unit(cls, value: Any) -> str | None:
        return canonical_volume_unit(value)


class ContextIntentGraph(ContractModel):
    """Handle-based semantic graph returned for a contextual turn."""

    operation: Operation
    operands: list[ContextOperandDraft] = Field(min_length=1)
    period_handles: list[str] = Field(default_factory=list)
    periods: list[PeriodRef] = Field(default_factory=list)
    grouping: list[GroupingSpec] = Field(default_factory=list)
    grain: Literal["day", "month", "quarter", "year", "total"] | None = None
    comparison: ComparisonSpec | None = None
    formula: FormulaSpec | None = None
    ranking: RankingSpec | None = None


class GroupingDirective(ContractModel):
    action: Literal["keep", "set", "add", "remove", "clear", "reference"]
    values: list[GroupingSpec] = Field(default_factory=list)
    source_scope: Literal[
        "active_dialog_scope",
        "last_attempted_scope",
        "last_successful_scope",
    ] | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "GroupingDirective":
        if self.action in {"set", "add", "remove"} and not self.values:
            raise ValueError(f"{self.action} grouping directive requires values")
        if self.action not in {"set", "add", "remove"} and self.values:
            raise ValueError(f"{self.action} grouping directive cannot carry values")
        if self.action == "reference" and self.source_scope is None:
            raise ValueError("reference grouping directive requires source_scope")
        if self.action != "reference" and self.source_scope is not None:
            raise ValueError("source_scope is valid only for reference")
        return self


class InterpretationDraft(ContractModel):
    operation: ScalarDirective
    metrics: StringListDirective
    aggregate_type: ScalarDirective
    periods: PeriodDirective
    entities: EntityDirective
    grouping: GroupingDirective
    grain: ScalarDirective
    reverse_direction: bool = False


class InterpretationDecision(ContractModel):
    contract_version: Literal["1.0"] = "1.0"
    mode: InterpretationMode
    normalized_message: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    draft: InterpretationDraft | None = None
    intent_graph: ContextIntentGraph | None = None
    clarification: ClarificationContract | None = None
    unsupported_capability: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    metadata_bundle_version: str

    @model_validator(mode="after")
    def validate_mode_payload(self) -> "InterpretationDecision":
        if self.mode in {InterpretationMode.STANDALONE, InterpretationMode.MUTATION}:
            if (self.draft is None) == (self.intent_graph is None):
                raise ValueError(
                    f"{self.mode.value} requires exactly one of draft or intent_graph"
                )
        elif self.draft is not None or self.intent_graph is not None:
            raise ValueError(
                f"{self.mode.value} cannot carry a draft or intent_graph"
            )
        if self.mode == InterpretationMode.CLARIFY:
            if self.clarification is None:
                raise ValueError("clarify requires clarification contract")
        elif self.clarification is not None:
            raise ValueError("clarification payload is valid only for clarify")
        if self.mode == InterpretationMode.UNSUPPORTED:
            if not self.unsupported_capability:
                raise ValueError("unsupported requires capability name")
        elif self.unsupported_capability is not None:
            raise ValueError("supported interpretation cannot declare unsupported capability")
        return self


def interpretation_decision_json_schema(
    allowed_modes: list[str] | None = None,
) -> dict[str, Any]:
    """Return a vLLM-compatible strict schema without unsupported conditionals."""
    schema = InterpretationDecision.model_json_schema()
    schema["required"] = list(InterpretationDecision.model_fields)
    if allowed_modes:
        schema["properties"]["mode"] = {"enum": list(allowed_modes), "type": "string"}
        if "standalone" in allowed_modes and "mutation" not in allowed_modes:
            # A first turn has no authoritative context to mutate. Keeping the
            # legacy draft branch in the structured-output schema lets the
            # model combine mode=standalone with invalid clear/reference
            # directives even though the runtime contract rejects that pair.
            schema["properties"]["draft"] = {"type": "null"}
            graph = schema.get("$defs", {}).get("ContextIntentGraph", {})
            operand = schema.get("$defs", {}).get("ContextOperandDraft", {})
            graph_properties = graph.get("properties", {})
            operand_properties = operand.get("properties", {})
            for field in ("period_handles",):
                if isinstance(graph_properties.get(field), dict):
                    graph_properties[field]["maxItems"] = 0
            for field in ("entity_handles", "period_handles"):
                if isinstance(operand_properties.get(field), dict):
                    operand_properties[field]["maxItems"] = 0
            if isinstance(operand_properties.get("source_operand_handle"), dict):
                operand_properties["source_operand_handle"] = {"type": "null"}
    return schema
