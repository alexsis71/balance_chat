from __future__ import annotations

from copy import deepcopy
from typing import Any, Sequence

from .contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ComparisonSpec,
    ContextContractV2,
    ContextMutation,
    EntityMention,
    GroupingSpec,
    InterpretationDecision,
    InterpretationMode,
    OperandEntityRef,
    Operation,
    PeriodRef,
)


class ContextBindingError(ValueError):
    pass


class EntityNotFound(ContextBindingError):
    pass


class AmbiguousEntityMention(ContextBindingError):
    pass


class RegistryEntityBinder:
    """Read-only adapter over MetadataRegistry; it never invents an ID."""

    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def bind(self, mentions: Sequence[EntityMention]) -> list[OperandEntityRef]:
        return [self._bind_one(mention) for mention in mentions]

    def _bind_one(self, mention: EntityMention) -> OperandEntityRef:
        text = mention.text.strip()
        role = mention.role
        record = None
        entity_type = ""
        entity_id: Any = None
        display_name = ""
        if role == "balance":
            record = self.registry.balance(text)
            if record:
                entity_type, entity_id = "balance", record.balance_id
        elif role == "article":
            candidates = self.registry.find_article_candidates(text)
            if len(candidates) > 1:
                raise AmbiguousEntityMention(f"ambiguous article mention: {text}")
            record = candidates[0] if candidates else None
            if record:
                entity_type, entity_id = "article", record.article_id
        elif role == "route":
            record = self.registry.route(text)
            if record:
                entity_type, entity_id = "route", record.route_id
        else:
            record = self.registry.geo(text)
            if record:
                entity_type, entity_id = "geo_object", record.geo_id
                display_name = _official_geo_display(record.canonical_name)
            else:
                record = self.registry.geo_group(text)
                if record:
                    entity_type, entity_id = "geo_group", record.group_id
                    display_name = _official_geo_display(record.canonical_name)
                else:
                    candidates = self.registry.find_article_candidates(text)
                    if len(candidates) > 1:
                        raise AmbiguousEntityMention(
                            f"ambiguous destination/source article mention: {text}"
                        )
                    record = candidates[0] if candidates else None
                    if record:
                        entity_type, entity_id = "article", record.article_id
        if record is None:
            raise EntityNotFound(f"metadata entity not found for {role}: {text}")
        display_name = display_name or record.canonical_name
        return OperandEntityRef(
            role=role,
            entity=CanonicalEntityRef(
                entity_id=str(entity_id),
                entity_type=entity_type,
                display_name=display_name,
            ),
        )


class InterpretationMutationCompiler:
    """Materialize an LLM draft into a fully validated deterministic mutation."""

    def __init__(self, binder: RegistryEntityBinder) -> None:
        self.binder = binder

    def compile(
        self,
        decision: InterpretationDecision,
        state: ContextContractV2,
        *,
        turn_id: str,
        user_message: str,
    ) -> ContextMutation:
        if decision.mode not in {
            InterpretationMode.STANDALONE,
            InterpretationMode.MUTATION,
        } or decision.draft is None:
            raise ContextBindingError("interpretation mode is not executable")
        draft = decision.draft
        base = (
            state.active_dialog_scope.intent.model_copy(deep=True)
            if decision.mode == InterpretationMode.MUTATION
            and state.active_dialog_scope is not None
            else None
        )
        operation = self._scalar(
            draft.operation, base.operation.value if base else None, state, "operation"
        )
        if not operation:
            raise ContextBindingError("operation is required")
        try:
            operation_enum = Operation(operation)
        except ValueError as exc:
            raise ContextBindingError(f"unsupported operation: {operation}") from exc

        operands = [item.model_copy(deep=True) for item in (base.operands if base else [])]
        metrics = self._metrics(draft.metrics, operands, state)
        bound_entities = self._entities(draft.entities, operands)
        operands = self._materialize_operands(
            operands,
            metrics,
            bound_entities,
            draft.entities.action,
            operation_enum,
        )
        aggregate_type = self._scalar(
            draft.aggregate_type,
            operands[0].aggregate_type if operands else None,
            state,
            "aggregate_type",
        )
        if aggregate_type:
            operands = [
                operand.model_copy(update={"aggregate_type": aggregate_type})
                for operand in operands
            ]
        if draft.reverse_direction:
            operands = [self._reverse_operand(operand) for operand in operands]

        periods = self._periods(draft.periods, base.periods if base else [], state)
        grouping = self._grouping(draft.grouping, base.grouping if base else [], state)
        grain = self._scalar(
            draft.grain, base.grain if base else None, state, "grain"
        )
        comparison = None
        if operation_enum == Operation.COMPARE:
            if len(operands) < 2:
                raise ContextBindingError("compare requires at least two bound operands")
            comparison = ComparisonSpec(
                baseline_operand_id=operands[0].operand_id,
                target_operand_id=operands[1].operand_id,
            )
        intent = AnalysisIntent(
            operation=operation_enum,
            operands=operands,
            periods=periods,
            grouping=grouping,
            grain=grain,
            comparison=comparison,
        )
        return ContextMutation(
            turn_id=turn_id,
            user_message=user_message,
            normalized_message=decision.normalized_message,
            replace_intent=intent,
        )

    def _metrics(self, directive, operands, state) -> list[str]:
        current = [item.metric for item in operands]
        action = directive.action
        values = list(directive.values)
        if action == "keep":
            return current
        if action == "clear":
            return []
        if action == "reference":
            scope = self._scope(state, directive.source_scope)
            return [item.metric for item in scope.intent.operands]
        if action == "set":
            return values
        if action == "add":
            return current + [value for value in values if value not in current]
        if action == "remove":
            return [value for value in current if value not in values]
        raise ContextBindingError(f"invalid metrics action: {action}")

    def _entities(self, directive, operands) -> list[OperandEntityRef]:
        current = [deepcopy(item) for item in operands for item in item.entities]
        current = list(
            {
                (item.role, item.entity.entity_type, item.entity.entity_id): item
                for item in current
            }.values()
        )
        if directive.action == "keep":
            return current
        if directive.action == "clear":
            return []
        bound = self.binder.bind(directive.mentions)
        if directive.action == "set":
            return bound
        if directive.action == "add":
            existing = {
                (item.role, item.entity.entity_type, item.entity.entity_id): item
                for item in current
            }
            for item in bound:
                existing[(item.role, item.entity.entity_type, item.entity.entity_id)] = item
            return list(existing.values())
        if directive.action == "remove":
            removed = {
                (item.role, item.entity.entity_type, item.entity.entity_id)
                for item in bound
            }
            return [
                item
                for item in current
                if (item.role, item.entity.entity_type, item.entity.entity_id) not in removed
            ]
        raise ContextBindingError(f"invalid entities action: {directive.action}")

    @staticmethod
    def _materialize_operands(
        existing: list[AnalysisOperand],
        metrics: list[str],
        entities: list[OperandEntityRef],
        entity_action: str,
        operation: Operation,
    ) -> list[AnalysisOperand]:
        if not metrics:
            raise ContextBindingError("at least one metric is required")
        if operation == Operation.COMPARE and entity_action == "set" and entities:
            by_role: dict[str, list[OperandEntityRef]] = {}
            for entity in entities:
                by_role.setdefault(entity.role, []).append(entity)
            varying_roles = [role for role, values in by_role.items() if len(values) > 1]
            if len(varying_roles) > 1:
                raise ContextBindingError(
                    "compare has multiple varying entity dimensions"
                )
            if varying_roles:
                if len(metrics) != 1:
                    raise ContextBindingError(
                        "comparison matrix requires an explicit decomposition"
                    )
                varying_role = varying_roles[0]
                shared = [
                    deepcopy(entity)
                    for role, values in by_role.items()
                    if role != varying_role
                    for entity in values
                ]
                return [
                    AnalysisOperand(
                        operand_id=f"operand_{index + 1}",
                        metric=metrics[0],
                        entities=[*deepcopy(shared), deepcopy(entity)],
                    )
                    for index, entity in enumerate(by_role[varying_role])
                ]
        output: list[AnalysisOperand] = []
        for index, metric in enumerate(metrics):
            template = existing[min(index, len(existing) - 1)] if existing else None
            output.append(
                AnalysisOperand(
                    operand_id=(
                        template.operand_id
                        if template and len(metrics) == len(existing)
                        else f"operand_{index + 1}"
                    ),
                    metric=metric,
                    aggregate_type=template.aggregate_type if template else "sum",
                    entities=deepcopy(entities),
                    periods=deepcopy(template.periods) if template else [],
                    unit=template.unit if template else None,
                )
            )
        return output

    def _periods(self, directive, current, state) -> list[PeriodRef]:
        return self._list_directive(directive, current, state, "periods")

    def _grouping(self, directive, current, state) -> list[GroupingSpec]:
        return self._list_directive(directive, current, state, "grouping")

    def _list_directive(self, directive, current, state, field):
        if directive.action == "keep":
            return deepcopy(current)
        if directive.action == "clear":
            return []
        if directive.action == "reference":
            return deepcopy(getattr(self._scope(state, directive.source_scope).intent, field))
        values = deepcopy(directive.values)
        if directive.action == "set":
            return values
        if directive.action == "add":
            return list(current) + [item for item in values if item not in current]
        if directive.action == "remove":
            return [item for item in current if item not in values]
        raise ContextBindingError(f"invalid {field} action: {directive.action}")

    def _scalar(self, directive, current, state, field):
        if directive.action == "keep":
            return current
        if directive.action == "clear":
            return None
        if directive.action == "set":
            return directive.value
        if directive.action == "reference":
            return getattr(self._scope(state, directive.source_scope).intent, field)
        raise ContextBindingError(f"invalid {field} action: {directive.action}")

    @staticmethod
    def _scope(state: ContextContractV2, name: str | None):
        scope = getattr(state, str(name), None)
        if scope is None:
            raise ContextBindingError(f"referenced context scope is empty: {name}")
        return scope

    @staticmethod
    def _reverse_operand(operand: AnalysisOperand) -> AnalysisOperand:
        entities = []
        source = next((item for item in operand.entities if item.role == "source"), None)
        destination = next(
            (item for item in operand.entities if item.role == "destination"), None
        )
        for item in operand.entities:
            if item.role == "article":
                continue
            if item.role == "source" and destination:
                entities.append(destination.model_copy(update={"role": "source"}))
            elif item.role == "destination" and source:
                entities.append(source.model_copy(update={"role": "destination"}))
            else:
                entities.append(item)
        return operand.model_copy(update={"entities": entities})


def _official_geo_display(value: str) -> str:
    text = str(value).strip()
    return text[:1].upper() + text[1:] if text else text
