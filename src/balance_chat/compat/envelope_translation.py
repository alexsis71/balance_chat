from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from ..contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ComparisonSpec,
    OperandEntityRef,
    Operation,
    PeriodRef,
)
from ..execution import ScalarFact
from ..planning import ExecutionTask


class EnvelopeTranslationError(ValueError):
    pass


class PipelineEnvelopeTranslator:
    """Translate current unified resolved plans without changing their semantics."""

    def intent(self, envelope: Mapping[str, Any]) -> AnalysisIntent:
        plan = _resolved_plan(envelope)
        raw_intent = plan.get("_intent") if isinstance(plan.get("_intent"), Mapping) else {}
        requested_operation = _operation(raw_intent.get("intent") or plan.get("operation"))
        operation = _operation(plan.get("operation") or raw_intent.get("intent"))
        if requested_operation == Operation.COMPARE and operation != Operation.COMPARE:
            raise EnvelopeTranslationError("resolved_comparison_degraded")
        periods = _periods(plan, raw_intent)
        expressions = [
            item for item in (plan.get("expressions") or []) if isinstance(item, Mapping)
        ]
        expanded: list[Mapping[str, Any]] = []
        for expression in expressions:
            geo_values = expression.get("geo") or []
            if isinstance(geo_values, list) and len(geo_values) > 1:
                for geo in geo_values:
                    expanded.append({**expression, "geo": [geo]})
            else:
                expanded.append(expression)
        operands = [self._operand(item, index) for index, item in enumerate(expanded)]
        if not operands:
            metric = str(raw_intent.get("metric") or "").strip()
            if not metric:
                raise EnvelopeTranslationError("resolved plan has no canonical metric")
            operands = [AnalysisOperand(operand_id="operand_1", metric=metric)]
        if operation == Operation.COMPARE and len(operands) < 2:
            raise EnvelopeTranslationError("resolved_comparison_degraded")
        return AnalysisIntent(
            operation=operation,
            operands=operands,
            periods=periods,
            grain=raw_intent.get("period_grain") or None,
        )

    def peer_entity_intent(
        self,
        envelope: Mapping[str, Any],
        peer_entity_sets: list[list[OperandEntityRef]],
    ) -> AnalysisIntent:
        """Use legacy shared semantics while ignoring degraded GEO expressions."""
        if len(peer_entity_sets) != 2 or any(not items for items in peer_entity_sets):
            raise EnvelopeTranslationError("peer comparison requires two entities")
        plan = _resolved_plan(envelope)
        raw_intent = plan.get("_intent") if isinstance(plan.get("_intent"), Mapping) else {}
        if _operation(raw_intent.get("intent")) != Operation.COMPARE:
            raise EnvelopeTranslationError("peer GEO semantics did not resolve as compare")
        metric = str(raw_intent.get("metric") or "").strip()
        if not metric:
            raise EnvelopeTranslationError("peer GEO comparison has no canonical metric")
        operands = [
            AnalysisOperand(
                operand_id=f"operand_{index + 1}",
                metric=metric,
                aggregate_type=str(raw_intent.get("aggregate_type") or "sum"),
                entities=[item.model_copy(deep=True) for item in peer_entities],
            )
            for index, peer_entities in enumerate(peer_entity_sets)
        ]
        return AnalysisIntent(
            operation=Operation.COMPARE,
            operands=operands,
            periods=_periods(plan, raw_intent),
            grain=raw_intent.get("period_grain") or None,
            comparison=ComparisonSpec(
                baseline_operand_id="operand_1",
                target_operand_id="operand_2",
            ),
        )

    @staticmethod
    def _operand(expression: Mapping[str, Any], index: int) -> AnalysisOperand:
        metric = str(
            expression.get("canonical_metric") or expression.get("metric") or ""
        ).strip()
        if not metric:
            raise EnvelopeTranslationError("resolved expression has no canonical metric")
        entities: list[OperandEntityRef] = []
        for role, entity_type in (("balance", "balance"), ("article", "article")):
            value = expression.get(role)
            if isinstance(value, Mapping) and value.get("id") is not None:
                entities.append(_entity(role, entity_type, value))
        geo_values = expression.get("geo") or []
        if isinstance(geo_values, Mapping):
            geo_values = [geo_values]
        for value in geo_values if isinstance(geo_values, list) else []:
            if not isinstance(value, Mapping) or value.get("id") is None:
                continue
            entities.append(_entity("destination", "geo_object", value))
            break
        route_values = expression.get("route") or []
        if isinstance(route_values, Mapping):
            route_values = [route_values]
        for value in route_values if isinstance(route_values, list) else []:
            if isinstance(value, Mapping) and value.get("id") is not None:
                entities.append(_entity("route", "route", value))
                break
        return AnalysisOperand(
            operand_id=f"operand_{index + 1}",
            metric=metric,
            aggregate_type=str(expression.get("aggregate_type") or "sum"),
            entities=entities,
        )

    def fact(self, task: ExecutionTask, envelope: Mapping[str, Any]) -> ScalarFact | None:
        rows = [item for item in (envelope.get("rows") or []) if isinstance(item, Mapping)]
        if not rows:
            raise EnvelopeTranslationError(f"scalar task {task.task_id} has no rows")
        interpretation = (
            envelope.get("interpretation")
            if isinstance(envelope.get("interpretation"), Mapping)
            else {}
        )
        values: list[Decimal] = []
        units: set[str] = set()
        for row in rows:
            value = next(
                (row.get(key) for key in ("fact_value", "fact", "value", "amount", "volume") if row.get(key) is not None),
                None,
            )
            if value is None:
                raise EnvelopeTranslationError("scalar result has no explicit numeric value field")
            try:
                values.append(Decimal(str(value).replace(" ", "").replace(",", ".")))
            except InvalidOperation as exc:
                raise EnvelopeTranslationError("scalar result value is not numeric") from exc
            unit = str(
                row.get("unit") or envelope.get("unit") or interpretation.get("unit") or ""
            ).strip()
            if not unit:
                raise EnvelopeTranslationError("scalar result has no explicit unit")
            units.add(unit)
        if len(units) != 1:
            raise EnvelopeTranslationError("scalar result rows use different units")
        aggregate = task.scalar_intent.operands[0].aggregate_type
        if aggregate == "sum":
            number = sum(values, Decimal("0"))
        elif aggregate == "min":
            number = min(values)
        elif aggregate == "max":
            number = max(values)
        elif len(values) == 1:
            number = values[0]
        else:
            raise EnvelopeTranslationError(
                f"multiple scalar rows do not support aggregate {aggregate!r}"
            )
        return ScalarFact(
            task_id=task.task_id,
            value=number,
            unit=next(iter(units)),
            label=str(rows[0].get("label") or task.operand_id),
            provenance=[dict(row) for row in rows],
        )


def _entity(role: str, entity_type: str, value: Mapping[str, Any]) -> OperandEntityRef:
    return OperandEntityRef(
        role=role,
        entity=CanonicalEntityRef(
            entity_id=str(value["id"]),
            entity_type=entity_type,
            display_name=str(value.get("label") or value.get("name") or value["id"]),
        ),
    )


def _operation(value: Any) -> Operation:
    normalized = str(value or "show").strip().lower()
    aliases = {"comparison": "compare", "multi_step": "multi_step"}
    try:
        return Operation(aliases.get(normalized, normalized))
    except ValueError as exc:
        raise EnvelopeTranslationError(f"unsupported resolved operation: {normalized}") from exc


def _periods(plan: Mapping[str, Any], raw_intent: Mapping[str, Any]) -> list[PeriodRef]:
    output: list[PeriodRef] = []
    for item in plan.get("periods") or raw_intent.get("periods") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            output.append(PeriodRef(date_from=item[0], date_to=item[1]))
        elif isinstance(item, Mapping) and item.get("date_from") and item.get("date_to"):
            output.append(PeriodRef(date_from=item["date_from"], date_to=item["date_to"]))
    if not output and raw_intent.get("date_from") and raw_intent.get("date_to"):
        output.append(
            PeriodRef(date_from=raw_intent["date_from"], date_to=raw_intent["date_to"])
        )
    if not output:
        raise EnvelopeTranslationError("resolved plan has no canonical period")
    return output


def _resolved_plan(envelope: Mapping[str, Any]) -> dict[str, Any]:
    debug = envelope.get("debug") if isinstance(envelope.get("debug"), Mapping) else {}
    for key in (
        "resolved_plan",
        "pipeline_unified_resolved_plan",
        "pipeline_v2_resolved_plan",
    ):
        if isinstance(debug.get(key), Mapping) and debug[key]:
            return dict(debug[key])
    raise EnvelopeTranslationError("result envelope has no resolved plan")
