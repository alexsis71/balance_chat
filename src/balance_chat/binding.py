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
from .conversation import handle_indexes


class ContextBindingError(ValueError):
    pass


class EntityNotFound(ContextBindingError):
    pass


class AmbiguousEntityMention(ContextBindingError):
    pass


class CanonicalRelationNotFound(ContextBindingError):
    def __init__(self, message: str, operand: AnalysisOperand) -> None:
        super().__init__(message)
        self.operand = operand


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
        current_entity_mentions: Sequence[EntityMention] = (),
    ) -> ContextMutation:
        if decision.mode not in {
            InterpretationMode.STANDALONE,
            InterpretationMode.MUTATION,
        }:
            raise ContextBindingError("interpretation mode is not executable")
        if decision.intent_graph is not None:
            return self._compile_graph(
                decision,
                state,
                turn_id=turn_id,
                user_message=user_message,
                current_entity_mentions=current_entity_mentions,
            )
        if decision.draft is None:
            raise ContextBindingError("executable interpretation lacks a draft")
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

    def _compile_graph(
        self,
        decision: InterpretationDecision,
        state: ContextContractV2,
        *,
        turn_id: str,
        user_message: str,
        current_entity_mentions: Sequence[EntityMention] = (),
    ) -> ContextMutation:
        graph = decision.intent_graph
        if graph is None:
            raise ContextBindingError("context intent graph is missing")
        operand_index, entity_index, period_index = handle_indexes(state)
        active_operands = (
            state.active_dialog_scope.intent.operands
            if state.active_dialog_scope is not None else []
        )
        graph_periods = self._referenced_periods(
            graph.period_handles, graph.periods, period_index
        )
        if not graph_periods and state.active_dialog_scope is not None:
            graph_periods = deepcopy(state.active_dialog_scope.intent.periods)
        operands: list[AnalysisOperand] = []
        graph_specs = [item.model_copy(deep=True) for item in graph.operands]
        self._reconcile_current_mentions(
            graph_specs, current_entity_mentions, entity_index
        )
        for spec in graph_specs:
            source = None
            if spec.source_operand_handle:
                source = operand_index.get(spec.source_operand_handle)
                if source is None:
                    raise ContextBindingError(
                        f"unknown source operand handle: {spec.source_operand_handle}"
                    )
                source = source.model_copy(deep=True)
            elif decision.mode == InterpretationMode.MUTATION and len(active_operands) == 1:
                # A mutation is defined over the active operand unless the model
                # explicitly selects another historical operand. This is generic
                # inherit semantics, not language-specific recovery.
                source = active_operands[0].model_copy(deep=True)
            elif decision.mode == InterpretationMode.MUTATION and active_operands:
                requested_periods = (
                    self._referenced_periods(
                        spec.period_handles, spec.periods, period_index
                    )
                    if spec.period_handles or spec.periods
                    else graph_periods
                )
                source = self._unique_active_operand_for_period(
                    active_operands, requested_periods
                )
            if (
                source is None
                and decision.mode == InterpretationMode.MUTATION
                and active_operands
                and spec.entity_mode == "inherit"
                and not spec.entity_handles
                and not spec.entity_mentions
            ):
                raise ContextBindingError(
                    "context graph omitted an ambiguous source operand handle"
                )
            metric = spec.metric or (source.metric if source is not None else None)
            if not metric:
                raise ContextBindingError(
                    f"operand {spec.operand_id} has no metric or source handle"
                )
            entity_mode = (
                "replace"
                if spec.entity_mode == "inherit"
                and (spec.entity_handles or spec.entity_mentions)
                else spec.entity_mode
            )
            entities = self._graph_entities(
                entity_mode,
                source.entities if source is not None else [],
                spec.entity_handles,
                spec.entity_mentions,
                entity_index,
            )
            period_mode = (
                "replace"
                if spec.period_mode == "inherit"
                and (spec.period_handles or spec.periods)
                else spec.period_mode
            )
            periods = self._graph_periods(
                period_mode,
                source.periods if source is not None else [],
                spec.period_handles,
                spec.periods,
                period_index,
            )
            operand = AnalysisOperand(
                operand_id=spec.operand_id,
                metric=metric,
                aggregate_type=(
                    spec.aggregate_type
                    or (source.aggregate_type if source is not None else "sum")
                ),
                entities=entities,
                periods=periods,
                unit=spec.unit if spec.unit is not None else (source.unit if source else None),
            )
            if spec.reverse_direction:
                operand = self._resolve_reverse_operand(operand)
            operands.append(operand)
        periods = graph_periods
        operation = graph.operation
        if len(operands) == 2 and operation in {Operation.SHOW, Operation.AGGREGATE}:
            operation = Operation.COMPARE
        if (
            operation == Operation.SHOW
            and any(item.aggregate_type in {"min", "max", "avg"} for item in operands)
        ):
            operation = Operation.AGGREGATE
        comparison = graph.comparison
        if operation == Operation.COMPARE and comparison is None:
            if len(operands) != 2:
                raise ContextBindingError("compare graph requires exactly two operands")
            comparison = ComparisonSpec(
                baseline_operand_id=operands[0].operand_id,
                target_operand_id=operands[1].operand_id,
            )
        try:
            intent = AnalysisIntent(
                operation=operation,
                operands=operands,
                periods=periods,
                grouping=deepcopy(graph.grouping),
                grain=graph.grain,
                comparison=comparison,
            )
        except Exception as exc:
            raise ContextBindingError(f"context intent graph is invalid: {exc}") from exc
        return ContextMutation(
            turn_id=turn_id,
            user_message=user_message,
            normalized_message=decision.normalized_message,
            replace_intent=intent,
        )

    @staticmethod
    def _reconcile_current_mentions(
        specs,
        mentions: Sequence[EntityMention],
        entity_index: dict[str, OperandEntityRef],
    ) -> None:
        """Attach deterministic current-message tags when the graph omitted them."""
        if not mentions or not specs:
            return
        if len(mentions) == len(specs) and len(specs) > 1:
            for spec, mention in zip(specs, mentions):
                spec.entity_mode = "replace"
                spec.entity_handles = []
                spec.entity_mentions = [mention]
            return
        normalized = {
            (item.text.strip().casefold(), item.role) for spec in specs
            for item in spec.entity_mentions
        }
        normalized.update(
            (
                reference.entity.display_name.strip().casefold(),
                reference.role,
            )
            for spec in specs
            for handle in spec.entity_handles
            if (reference := entity_index.get(handle)) is not None
        )
        missing = [
            item for item in mentions
            if (item.text.strip().casefold(), item.role) not in normalized
        ]
        if not missing:
            return
        if len(specs) == 1:
            if specs[0].entity_mode == "inherit":
                specs[0].entity_mode = "add"
            specs[0].entity_mentions.extend(missing)
            return
        if len(missing) == len(specs):
            for spec, mention in zip(specs, missing):
                spec.entity_mode = "replace"
                spec.entity_mentions = [mention]
            return
        # A single newly mentioned entity in a comparison is the target operand;
        # the baseline remains the explicitly referenced historical operand.
        if len(missing) == 1:
            specs[-1].entity_mode = "replace"
            specs[-1].entity_mentions.append(missing[0])

    @staticmethod
    def _unique_active_operand_for_period(
        operands: Sequence[AnalysisOperand],
        requested: Sequence[PeriodRef],
    ) -> AnalysisOperand | None:
        if not requested:
            return None
        requested_keys = {(item.date_from, item.date_to) for item in requested}
        matches = [
            item for item in operands
            if {(period.date_from, period.date_to) for period in item.periods}
            == requested_keys
        ]
        return matches[0].model_copy(deep=True) if len(matches) == 1 else None

    def _resolve_reverse_operand(self, operand: AnalysisOperand) -> AnalysisOperand:
        by_role = {item.role: item for item in operand.entities}
        balance_ref = by_role.get("balance")
        article_ref = by_role.get("article")
        if balance_ref is None or article_ref is None:
            reversed_operand = self._reverse_operand(operand)
            reversed_roles = {item.role for item in reversed_operand.entities}
            if {"source", "destination"}.issubset(reversed_roles):
                return reversed_operand
            raise CanonicalRelationNotFound(
                "reverse operand has no canonical source and destination",
                reversed_operand,
            )
        registry = self.binder.registry
        source_balance = registry.balance(_metadata_id(balance_ref.entity.entity_id))
        current_article = registry.article(_metadata_id(article_ref.entity.entity_id))
        if source_balance is None or current_article is None:
            raise CanonicalRelationNotFound(
                "reverse source metadata is unavailable", operand
            )
        destination_balance = next(
            (
                registry.balance(value)
                for value in (
                    current_article.canonical_name,
                    *current_article.aliases,
                    article_ref.entity.display_name,
                )
                if registry.balance(value) is not None
            ),
            None,
        )
        candidates: dict[Any, Any] = {}
        if destination_balance is not None:
            for value in (source_balance.canonical_name, *source_balance.aliases):
                for candidate in registry.find_article_candidates(
                    value,
                    balance_id=destination_balance.balance_id,
                ):
                    if (
                        str(candidate.section).strip().casefold() == "распределение"
                        and any(
                            str(part).strip().casefold() == "за пределы"
                            for part in candidate.path
                        )
                    ):
                        candidates[candidate.article_id] = candidate
        if destination_balance is None or len(candidates) != 1:
            raise CanonicalRelationNotFound(
                "reverse canonical relation is absent or ambiguous", operand
            )
        article = next(iter(candidates.values()))
        return operand.model_copy(
            update={
                "entities": [
                    OperandEntityRef(
                        role="balance",
                        entity=CanonicalEntityRef(
                            entity_id=str(destination_balance.balance_id),
                            entity_type="balance",
                            display_name=destination_balance.canonical_name,
                        ),
                    ),
                    OperandEntityRef(
                        role="article",
                        entity=CanonicalEntityRef(
                            entity_id=str(article.article_id),
                            entity_type="article",
                            display_name=article.canonical_name,
                        ),
                    ),
                ]
            },
            deep=True,
        )

    def _graph_entities(
        self,
        mode: str,
        inherited: Sequence[OperandEntityRef],
        handles: Sequence[str],
        mentions: Sequence[EntityMention],
        index: dict[str, OperandEntityRef],
    ) -> list[OperandEntityRef]:
        selected: list[OperandEntityRef] = []
        for handle in handles:
            reference = index.get(handle)
            if reference is None:
                raise ContextBindingError(f"unknown entity handle: {handle}")
            selected.append(reference.model_copy(deep=True))
        selected.extend(self.binder.bind(mentions))
        if mode == "inherit":
            if selected:
                raise ContextBindingError("inherit entity mode cannot select entities")
            return deepcopy(list(inherited))
        if mode == "clear":
            if selected:
                raise ContextBindingError("clear entity mode cannot select entities")
            return []
        if mode == "replace":
            base: list[OperandEntityRef] = []
        elif mode == "add":
            base = deepcopy(list(inherited))
        else:
            raise ContextBindingError(f"unknown entity mode: {mode}")
        by_role = {item.role: item for item in base}
        for item in selected:
            by_role[item.role] = item
        return list(by_role.values())

    @staticmethod
    def _graph_periods(
        mode: str,
        inherited: Sequence[PeriodRef],
        handles: Sequence[str],
        values: Sequence[PeriodRef],
        index: dict[str, PeriodRef],
    ) -> list[PeriodRef]:
        selected = InterpretationMutationCompiler._referenced_periods(
            handles, values, index
        )
        if mode == "inherit":
            if selected:
                raise ContextBindingError("inherit period mode cannot select periods")
            return deepcopy(list(inherited))
        if mode == "clear":
            if selected:
                raise ContextBindingError("clear period mode cannot select periods")
            return []
        if mode == "replace":
            return selected
        if mode == "add":
            output = deepcopy(list(inherited))
            output.extend(item for item in selected if item not in output)
            return output
        raise ContextBindingError(f"unknown period mode: {mode}")

    @staticmethod
    def _referenced_periods(
        handles: Sequence[str],
        values: Sequence[PeriodRef],
        index: dict[str, PeriodRef],
    ) -> list[PeriodRef]:
        output: list[PeriodRef] = []
        for handle in handles:
            period = index.get(handle)
            if period is None:
                raise ContextBindingError(f"unknown period handle: {handle}")
            output.append(period.model_copy(deep=True))
        output.extend(item.model_copy(deep=True) for item in values)
        unique: list[PeriodRef] = []
        seen: set[tuple[Any, Any, Any]] = set()
        for item in output:
            key = (item.date_from, item.date_to, item.label)
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

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
        if entity_action == "keep" and existing:
            if len(metrics) != len(existing):
                raise ContextBindingError(
                    "keeping entities requires preserving operand topology"
                )
            return [
                AnalysisOperand(
                    operand_id=template.operand_id,
                    metric=metrics[index],
                    aggregate_type=template.aggregate_type,
                    entities=deepcopy(template.entities),
                    periods=deepcopy(template.periods),
                    unit=template.unit,
                )
                for index, template in enumerate(existing)
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


def _metadata_id(value: str) -> int | str:
    text = str(value).strip()
    tail = text.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else text
