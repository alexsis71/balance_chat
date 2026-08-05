from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Sequence

from .contracts import AnalysisIntent, ContextContractV2, Operation
from .interpretation import HybridInterpretationPolicy
from .planning import NativeExecutionPlan


class EvidenceCompletenessError(ValueError):
    """A routed intent cannot be executed from the evidence that was bound."""

    def __init__(self, issues: Sequence[str]) -> None:
        self.issues = tuple(dict.fromkeys(str(item) for item in issues if item))
        super().__init__(", ".join(self.issues) or "evidence is incomplete")


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    invoke_interpreter: bool
    reason: str
    explicit_business_count: int
    explicit_geo_count: int


class RoutingEvidenceGate:
    """One deterministic boundary for routing and pre-DB evidence checks.

    The model may propose semantic structure, but only canonical evidence that
    survives binding is allowed to reach planning and execution.
    """

    _COMPOUND_REQUEST = re.compile(
        r"\b(?:сравн\w*|процент\w*|дол[яиюей]|отношени\w*|"
        r"суммир\w*\s+по|сгруппир\w*|лидер\w*|топ\s*\d*|"
        r"максим\w*|миним\w*|средн\w*)\b",
        re.IGNORECASE,
    )

    def __init__(self, policy: HybridInterpretationPolicy | None = None) -> None:
        self._policy_is_override = policy is not None
        self.policy = policy or HybridInterpretationPolicy()

    def route(
        self,
        message: str,
        state: ContextContractV2,
        *,
        explicit_businesses: Sequence[Any] = (),
        explicit_geos: Sequence[Any] = (),
        likely_typo: bool = False,
        clarification_answer: bool = False,
    ) -> RoutingDecision:
        policy_selected = self.policy.should_invoke(
            message,
            state,
            likely_typo=likely_typo,
            clarification_answer=clarification_answer,
        )
        if policy_selected:
            reason = (
                "active_context" if state.active_dialog_scope is not None
                else "semantic_operation"
            )
            invoke = True
        elif not self._policy_is_override and self._COMPOUND_REQUEST.search(message):
            reason = "compound_semantics"
            invoke = True
        elif len(explicit_businesses) + len(explicit_geos) > 1:
            reason = "multiple_explicit_entities"
            invoke = True
        else:
            reason = "legacy_scalar"
            invoke = False
        return RoutingDecision(
            invoke_interpreter=invoke,
            reason=reason,
            explicit_business_count=len(explicit_businesses),
            explicit_geo_count=len(explicit_geos),
        )

    def validate_bound_intent(
        self,
        intent: AnalysisIntent,
        *,
        explicit_businesses: Sequence[Any] = (),
        explicit_geos: Sequence[Any] = (),
    ) -> None:
        issues: list[str] = []
        effective_periods: dict[str, list[Any]] = {}
        for operand in intent.operands:
            periods = list(operand.periods or intent.periods)
            effective_periods[operand.operand_id] = periods
            if not periods:
                issues.append(f"operand_period_missing:{operand.operand_id}")
            if not operand.metric:
                issues.append(f"operand_metric_missing:{operand.operand_id}")
            if not operand.entities and (explicit_businesses or explicit_geos):
                issues.append(f"operand_entity_scope_missing:{operand.operand_id}")
            roles = {item.role for item in operand.entities}
            if intent.operation in {
                Operation.CALCULATE,
                Operation.COMPARE,
                Operation.RANK,
            } and not (
                "article" in roles
                or "route" in roles
                or {"source", "destination"}.issubset(roles)
                or ("destination" in roles and "geo_object" in {
                    item.entity.entity_type for item in operand.entities
                })
            ):
                issues.append(f"operand_routing_evidence_incomplete:{operand.operand_id}")

        if intent.operation == Operation.COMPARE_PERIODS:
            if len(intent.operands) != 1 or len(intent.periods) != 2:
                issues.append("compare_periods_evidence_incomplete")
        elif intent.operation == Operation.COMPARE and len(intent.operands) < 2:
            issues.append("comparison_operand_missing")
        elif intent.operation == Operation.CALCULATE:
            if intent.formula is None:
                issues.append("formula_missing")
            else:
                operand_ids = {item.operand_id for item in intent.operands}
                references = {
                    intent.formula.numerator_operand_id,
                    intent.formula.denominator_operand_id,
                }
                if len(references) != 2 or not references.issubset(operand_ids):
                    issues.append("formula_operand_evidence_incomplete")
        elif intent.operation == Operation.RANK:
            if intent.ranking is None or len(intent.operands) != 1:
                issues.append("ranking_evidence_incomplete")

        bound_labels = [
            entity.entity.display_name
            for operand in intent.operands
            for entity in operand.entities
        ]
        for record in explicit_businesses:
            if not _record_is_covered(record, bound_labels):
                issues.append(
                    f"explicit_business_not_bound:{getattr(record, 'canonical_name', record)}"
                )
        for record in explicit_geos:
            if not _record_is_covered(record, bound_labels):
                issues.append(
                    f"explicit_geo_not_bound:{getattr(record, 'canonical_name', record)}"
                )

        # A formula must compare facts with aligned effective periods unless
        # the user explicitly requested different periods on its operands.
        if intent.operation == Operation.CALCULATE and intent.formula is not None:
            numerator_periods = effective_periods.get(
                intent.formula.numerator_operand_id, []
            )
            denominator_periods = effective_periods.get(
                intent.formula.denominator_operand_id, []
            )
            if not numerator_periods or not denominator_periods:
                issues.append("formula_period_evidence_incomplete")

        if issues:
            raise EvidenceCompletenessError(issues)

    @staticmethod
    def validate_plan(plan: NativeExecutionPlan) -> None:
        issues: list[str] = []
        task_by_operand = {item.operand_id: item for item in plan.tasks}
        for task in plan.tasks:
            if not task.scalar_intent.periods:
                issues.append(f"task_period_missing:{task.task_id}")
            if len(task.scalar_intent.operands) != 1:
                issues.append(f"task_operand_shape_invalid:{task.task_id}")
        if plan.formula is not None:
            for operand_id in (
                plan.formula.numerator_operand_id,
                plan.formula.denominator_operand_id,
            ):
                if operand_id not in task_by_operand:
                    issues.append(f"formula_task_missing:{operand_id}")
        if plan.ranking is not None and len(plan.tasks) != 1:
            issues.append("ranking_task_shape_invalid")
        if issues:
            raise EvidenceCompletenessError(issues)


def _record_is_covered(record: Any, bound_labels: Sequence[str]) -> bool:
    variants = {
        _semantic_name(value)
        for value in (
            getattr(record, "canonical_name", ""),
            *(getattr(record, "aliases", ()) or ()),
        )
        if value
    }
    variants.discard("")
    for label in bound_labels:
        normalized = _semantic_name(label)
        if any(
            candidate == normalized
            or candidate in normalized
            or normalized in candidate
            for candidate in variants
        ):
            return True
    return False


def _semantic_name(value: Any) -> str:
    normalized = re.sub(r"[^0-9a-zа-яё]+", " ", str(value).casefold()).strip()
    tokens = [
        item for item in normalized.split()
        if item not in {"гп", "тг", "от", "суточный", "баланс", "газа"}
    ]
    return " ".join(tokens)
