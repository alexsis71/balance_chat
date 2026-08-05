from __future__ import annotations

from typing import Any

from ..contracts import AnalysisIntent, Operation
from ..domain import execution_metric


class UnsupportedLegacyShape(ValueError):
    pass


_SUPPORTED_OPERATIONS = {
    Operation.SHOW,
    Operation.AGGREGATE,
    Operation.COMPARE_PERIODS,
}
_SUPPORTED_ENTITY_ROLES = {"balance", "source", "destination", "route", "article"}


def _entity_payload(reference: Any) -> dict[str, str]:
    entity = reference.entity
    return {
        "entity_id": entity.entity_id,
        "entity_type": entity.entity_type,
        "display_name": entity.display_name,
    }


def project_legacy_context_override(intent: AnalysisIntent) -> dict[str, Any]:
    """Project only shapes faithfully representable by ContextExecutionOverride.

    Raising is intentional: a multi-operand or grouped request must not silently
    degrade to a scalar legacy query.
    """

    if intent.operation not in _SUPPORTED_OPERATIONS:
        raise UnsupportedLegacyShape(
            f"operation {intent.operation.value!r} is not representable by legacy override"
        )
    if len(intent.operands) != 1:
        raise UnsupportedLegacyShape("legacy override requires exactly one operand")
    if intent.grouping:
        raise UnsupportedLegacyShape("legacy override cannot preserve grouping")
    if intent.comparison is not None:
        raise UnsupportedLegacyShape("legacy override cannot preserve operand comparison")

    operand = intent.operands[0]
    periods = operand.periods or intent.periods
    if not periods:
        raise UnsupportedLegacyShape("legacy override requires at least one period")
    if intent.operation == Operation.COMPARE_PERIODS and len(periods) != 2:
        raise UnsupportedLegacyShape("compare_periods requires exactly two periods")

    roles: dict[str, dict[str, str]] = {}
    for reference in operand.entities:
        if reference.role not in _SUPPORTED_ENTITY_ROLES:
            raise UnsupportedLegacyShape(
                f"entity role {reference.role!r} is not representable by legacy override"
            )
        roles[reference.role] = _entity_payload(reference)

    return {
        "operation": intent.operation.value,
        "periods": [
            {
                "date_from": period.date_from.isoformat(),
                "date_to": period.date_to.isoformat(),
            }
            for period in periods
        ],
        # Preserve the richer V2 semantic metric in the intent/result graph,
        # but project only the execution metric understood by unified_strict.
        # Article binding keeps consumption/own_needs distinct.
        "metric": execution_metric(operand.metric),
        "aggregate_type": operand.aggregate_type,
        "balance": roles.get("balance"),
        "source": roles.get("source"),
        "destination": roles.get("destination"),
        "route": roles.get("route"),
        "article": roles.get("article"),
    }
