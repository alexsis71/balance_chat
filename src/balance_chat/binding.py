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
from .domain_invariants import CANONICAL_VOLUME_UNIT
from .conversation import handle_indexes
from .domain import metric_definition


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
        return self.bind_operand(mentions)

    def bind_operand(
        self,
        mentions: Sequence[EntityMention],
        inherited: Sequence[OperandEntityRef] = (),
        *,
        metric: str | None = None,
    ) -> list[OperandEntityRef]:
        """Bind one operand with balance-scoped article resolution.

        Article aliases are not globally unique. Resolve an explicit balance
        first and use it as the namespace for the remaining mentions.
        """
        viewpoint = self._bind_directed_viewpoint(mentions, metric)
        if viewpoint is not None:
            return viewpoint
        ordered = sorted(mentions, key=lambda item: item.role != "balance")
        balance_id: int | None = next(
            (
                _metadata_id(item.entity.entity_id)
                for item in inherited
                if item.role == "balance" and isinstance(_metadata_id(item.entity.entity_id), int)
            ),
            None,
        )
        output: list[OperandEntityRef] = []
        for mention in ordered:
            reference = self._bind_one(mention, balance_id=balance_id)
            output.append(reference)
            if reference.role == "balance":
                value = _metadata_id(reference.entity.entity_id)
                balance_id = value if isinstance(value, int) else None
        return output

    def _bind_directed_viewpoint(
        self,
        mentions: Sequence[EntityMention],
        metric: str | None,
    ) -> list[OperandEntityRef] | None:
        """Resolve a directional phrase through one explicit accounting viewpoint."""
        by_role = {item.role: item for item in mentions}
        if metric == "incoming" and {"source", "destination"}.issubset(by_role):
            balance_mention = by_role["destination"]
            article_mention = by_role["source"]
            section = "ресурсы"
            path_token = "поступление"
            article_variants = (
                article_mention.text,
                f"от {article_mention.text.removeprefix('от ').strip()}",
            )
        elif metric == "distribution" and {"source", "destination"}.issubset(by_role):
            balance_mention = by_role["source"]
            article_mention = by_role["destination"]
            section = "распределение"
            path_token = None
            article_variants = (article_mention.text,)
        else:
            return None
        balance = self.registry.balance(balance_mention.text)
        if balance is None:
            return None
        candidates: dict[Any, Any] = {}
        for value in article_variants:
            for candidate in self.registry.find_article_candidates(
                value, balance_id=balance.balance_id
            ):
                normalized_path = {
                    str(item).strip().casefold() for item in candidate.path
                }
                if str(candidate.section).strip().casefold() != section:
                    continue
                if path_token is not None and path_token not in normalized_path:
                    continue
                candidates[candidate.article_id] = candidate
        if len(candidates) != 1:
            return None
        article = next(iter(candidates.values()))
        return [
            OperandEntityRef(
                role="balance",
                entity=CanonicalEntityRef(
                    entity_id=str(balance.balance_id),
                    entity_type="balance",
                    display_name=balance.canonical_name,
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

    def _bind_one(
        self, mention: EntityMention, *, balance_id: int | None = None
    ) -> OperandEntityRef:
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
            candidates = self.registry.find_article_candidates(text, balance_id=balance_id)
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
                    candidates = self.registry.find_article_candidates(
                        text, balance_id=balance_id
                    )
                    if not candidates and balance_id is not None:
                        # A destination can be a canonical article in a peer
                        # balance rather than in the accounting balance that
                        # was mentioned alongside it. Global lookup is allowed
                        # only when it is still unique.
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
        for operand in operands:
            self._validate_metric_aggregate(
                operand.metric, operand.aggregate_type
            )
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
        graph = (
            decision.intent_graph.model_copy(deep=True)
            if decision.intent_graph is not None else None
        )
        if graph is None:
            raise ContextBindingError("context intent graph is missing")
        graph = self._normalize_graph_shape(graph, current_entity_mentions)
        operand_index, entity_index, period_index = handle_indexes(state)
        standalone_empty = (
            decision.mode == InterpretationMode.STANDALONE
            and not state.conversation_window
            and state.active_dialog_scope is None
        )
        if standalone_empty:
            unknown_global_periods = [
                handle for handle in graph.period_handles
                if handle not in period_index
            ]
            if unknown_global_periods:
                if not graph.periods:
                    raise ContextBindingError(
                        "standalone graph invented period handles without explicit periods"
                    )
                graph.period_handles = []
            for spec in graph.operands:
                if spec.source_operand_handle is not None:
                    raise ContextBindingError(
                        "standalone graph cannot reference a source operand"
                    )
                unknown_periods = [
                    handle for handle in spec.period_handles
                    if handle not in period_index
                ]
                if unknown_periods:
                    if not spec.periods and not graph.periods:
                        raise ContextBindingError(
                            "standalone operand invented period handles without explicit periods"
                        )
                    spec.period_handles = []
                if current_entity_mentions:
                    # First-turn tags are textual canonical candidates, not
                    # historical handles. No entity handle is valid yet.
                    spec.entity_handles = []
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
                metric=metric,
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
            aggregate_type = (
                spec.aggregate_type
                or (
                    source.aggregate_type
                    if source is not None and source.metric == metric
                    else metric_definition(metric).default_aggregate
                )
            )
            self._validate_metric_aggregate(metric, aggregate_type)
            operand = AnalysisOperand(
                operand_id=spec.operand_id,
                metric=metric,
                aggregate_type=aggregate_type,
                entities=entities,
                periods=periods,
                unit=spec.unit if spec.unit is not None else (source.unit if source else None),
            )
            operand = self._bind_metric_article(operand)
            if any(
                item.entity.entity_type in {"balance", "article"}
                for item in operand.entities
            ):
                operand = operand.model_copy(update={"unit": CANONICAL_VOLUME_UNIT}, deep=True)
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
            if len(operands) < 2:
                raise ContextBindingError("compare graph requires at least two operands")
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
                formula=graph.formula,
                ranking=graph.ranking,
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
    def _normalize_graph_shape(graph, mentions: Sequence[EntityMention]):
        """Complete only graph shapes proved by ordered current-turn evidence.

        The interpreter describes semantic intent, while the deterministic
        extractor is authoritative for exact current-message entity spans.  A
        homogeneous GEO comparison with one model operand and N distinct GEO
        tags has one unambiguous expansion: the same scalar measure for every
        GEO in mention order.  No heterogeneous metric or role assignment is
        guessed here.
        """
        if graph.operation == Operation.COMPARE_PERIODS:
            # Qwen may express the same scalar source twice, one explicit
            # period per operand, even though the canonical contract is one
            # operand plus two global periods. Collapse only when the source
            # and scalar semantics are identical and both periods are exact.
            if len(graph.operands) == 2:
                first, second = graph.operands
                same_source = (
                    first.source_operand_handle
                    and first.source_operand_handle == second.source_operand_handle
                )
                same_semantics = (
                    first.metric == second.metric
                    and first.aggregate_type == second.aggregate_type
                    and first.entity_handles == second.entity_handles
                    and first.entity_mentions == second.entity_mentions
                )
                if (
                    same_source
                    and same_semantics
                    and len(first.periods) == 1
                    and len(second.periods) == 1
                ):
                    operand = first.model_copy(
                        update={
                            "period_mode": "clear",
                            "period_handles": [],
                            "periods": [],
                        },
                        deep=True,
                    )
                    return graph.model_copy(
                        update={
                            "operands": [operand],
                            "period_handles": [],
                            "periods": [
                                first.periods[0].model_copy(deep=True),
                                second.periods[0].model_copy(deep=True),
                            ],
                            "comparison": None,
                        },
                        deep=True,
                    )
            if len(graph.operands) == 1 and len(graph.periods) == 2:
                return graph.model_copy(update={"comparison": None}, deep=True)
            return graph
        if graph.operation != Operation.COMPARE or len(graph.operands) != 1:
            return graph
        destinations: list[EntityMention] = []
        seen: set[tuple[str, str]] = set()
        for mention in mentions:
            if mention.role != "destination":
                continue
            key = (mention.text.strip().casefold(), mention.role)
            if key in seen:
                continue
            seen.add(key)
            destinations.append(mention.model_copy(deep=True))
        if len(destinations) < 2:
            return graph
        template = graph.operands[0]
        base_id = template.operand_id
        operands = []
        for index, mention in enumerate(destinations, start=1):
            operands.append(
                template.model_copy(
                    update={
                        "operand_id": f"{base_id}_{index}",
                        "entity_mode": "replace",
                        "entity_handles": [],
                        "entity_mentions": [mention],
                    },
                    deep=True,
                )
            )
        return graph.model_copy(
            update={
                "operands": operands,
                "comparison": ComparisonSpec(
                    baseline_operand_id=operands[0].operand_id,
                    target_operand_id=operands[1].operand_id,
                ),
            },
            deep=True,
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
        # Current-turn evidence is authoritative. When the model preserves the
        # operand metrics but omits textual mentions, materialize only a
        # uniquely determined metric topology from role-tagged spans.
        role_mentions: dict[str, list[EntityMention]] = {}
        for mention in mentions:
            role_mentions.setdefault(mention.role, []).append(mention)
        for spec in specs:
            if spec.entity_handles or spec.entity_mentions:
                continue
            selected: list[EntityMention] = []
            if spec.metric == "incoming":
                if len(role_mentions.get("source", [])) == 1 and len(
                    role_mentions.get("destination", [])
                ) == 1:
                    selected = [
                        role_mentions["source"][0],
                        role_mentions["destination"][0],
                    ]
            elif spec.metric == "distribution":
                if len(role_mentions.get("balance", [])) == 1:
                    selected = [role_mentions["balance"][0]]
                elif len(role_mentions.get("source", [])) == 1 and len(
                    role_mentions.get("destination", [])
                ) == 1:
                    selected = [
                        role_mentions["source"][0],
                        role_mentions["destination"][0],
                    ]
            elif len(role_mentions.get("balance", [])) == 1:
                selected = [role_mentions["balance"][0]]
            if selected:
                spec.entity_mode = "replace"
                spec.entity_handles = []
                spec.entity_mentions = list(selected)
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
        *,
        metric: str | None = None,
    ) -> list[OperandEntityRef]:
        selected: list[OperandEntityRef] = []
        for handle in handles:
            reference = index.get(handle)
            if reference is None:
                raise ContextBindingError(f"unknown entity handle: {handle}")
            selected.append(reference.model_copy(deep=True))
        inherited_scope = [*inherited, *selected]
        selected.extend(
            self.binder.bind_operand(mentions, inherited_scope, metric=metric)
        )
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

    def _bind_metric_article(self, operand: AnalysisOperand) -> AnalysisOperand:
        """Add only a uniquely curated metric article inside an explicit balance."""
        definition = metric_definition(operand.metric)
        if not definition.canonical_articles:
            return operand
        by_role = {item.role: item for item in operand.entities}
        if "article" in by_role or "balance" not in by_role:
            return operand
        balance_id = _metadata_id(by_role["balance"].entity.entity_id)
        if not isinstance(balance_id, int):
            return operand
        articles = self.binder.registry.articles_for_balance(balance_id)
        for canonical_name in definition.canonical_articles:
            matches = [
                item
                for item in articles
                if str(item.canonical_name).strip().casefold()
                == canonical_name.casefold()
            ]
            root_matches = [
                item
                for item in matches
                if tuple(str(part).strip().casefold() for part in item.path)
                == (canonical_name.casefold(),)
            ]
            if len(root_matches) == 1:
                matches = root_matches
            if len(matches) == 1:
                article = matches[0]
                return operand.model_copy(
                    update={
                        "entities": [
                            *operand.entities,
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
        return operand

    @staticmethod
    def _validate_metric_aggregate(metric: str, aggregate_type: str) -> None:
        definition = metric_definition(metric)
        if aggregate_type not in definition.allowed_aggregates:
            raise ContextBindingError(
                f"aggregate {aggregate_type!r} is not valid for metric {metric!r}"
            )

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
            aggregate_type = (
                template.aggregate_type
                if template is not None and template.metric == metric
                else metric_definition(metric).default_aggregate
            )
            output.append(
                AnalysisOperand(
                    operand_id=(
                        template.operand_id
                        if template and len(metrics) == len(existing)
                        else f"operand_{index + 1}"
                    ),
                    metric=metric,
                    aggregate_type=aggregate_type,
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
