from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any, Callable

from ..contracts import (
    AnalysisIntent,
    AnalysisOperand,
    ContextContractV2,
    ContextMutation,
    FieldMutation,
    IntentPatch,
    MutationAction,
    OperandEntityRef,
    Operation,
)
from .types import TransitionDecision, TransitionKind


@dataclass(frozen=True, slots=True)
class BusinessEntityTransitionServices:
    lookup_balance: Callable[[Any], Any] | None
    lookup_geo: Callable[[Any], Any] | None
    resolve_direction_article: Callable[[Any, Any, str], Any | None]
    make_entity: Callable[[str, str, Any], OperandEntityRef]


@dataclass(frozen=True, slots=True)
class BusinessEntityPatchCandidate:
    business: Any
    operands: tuple[AnalysisOperand, ...]


_FOLLOWUP = re.compile(
    r"(?:(?:а\s+)?(?:теперь\s+)?(?:для|по)|только\s+из)\s+(?P<business>.+)"
)


def _normalize_text(value: str) -> str:
    text = str(value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^0-9a-zа-я]+", " ", text).split())


def _is_qualified_business(mention: str) -> bool:
    tokens = mention.split()
    return (
        len(tokens) > 1
        and (
            tokens[0] == "тг"
            or tokens[:2] == ["гп", "тг"]
            or tokens[:2] == ["газпром", "трансгаз"]
            or tokens[:3] == ["ооо", "газпром", "трансгаз"]
        )
    )


def _metadata_id(value: Any) -> str:
    return str(value or "").strip().rsplit(":", 1)[-1]


def _lookup_geo(reference: OperandEntityRef, services: BusinessEntityTransitionServices):
    lookup = services.lookup_geo
    if not callable(lookup):
        return None
    for value in (reference.entity.entity_id, reference.entity.display_name):
        if (record := lookup(value)) is not None:
            return record
    return None


def _replace_full_balance(
    operand: AnalysisOperand,
    business: Any,
    services: BusinessEntityTransitionServices,
) -> AnalysisOperand | None:
    if operand.metric != "balance" or len(operand.entities) != 1:
        return None
    current = operand.entities[0]
    if current.role != "balance" or current.entity.entity_type != "balance":
        return None
    if _metadata_id(current.entity.entity_id) == str(business.balance_id):
        return None
    return operand.model_copy(
        update={
            "entities": [services.make_entity("balance", "balance", business)]
        },
        deep=True,
    )


def _replace_distribution_viewpoint(
    operand: AnalysisOperand,
    business: Any,
    services: BusinessEntityTransitionServices,
) -> AnalysisOperand | None:
    if operand.metric != "distribution":
        return None
    allowed_roles = {"balance", "destination", "article"}
    if any(item.role not in allowed_roles for item in operand.entities):
        return None
    balances = [
        (index, item)
        for index, item in enumerate(operand.entities)
        if item.role == "balance" and item.entity.entity_type == "balance"
    ]
    destinations = [
        (index, item)
        for index, item in enumerate(operand.entities)
        if item.role == "destination" and item.entity.entity_type == "geo_object"
    ]
    articles = [
        (index, item)
        for index, item in enumerate(operand.entities)
        if item.role == "article" and item.entity.entity_type == "article"
    ]
    if len(balances) > 1 or len(destinations) != 1 or len(articles) > 1:
        return None
    if len(balances) + len(destinations) + len(articles) != len(operand.entities):
        return None
    if balances and _metadata_id(balances[0][1].entity.entity_id) == str(
        business.balance_id
    ):
        return None
    if articles and (not balances or operand.aggregate_type != "sum"):
        return None

    entities = [item.model_copy(deep=True) for item in operand.entities]
    replacement = services.make_entity("balance", "balance", business)
    if balances:
        entities[balances[0][0]] = replacement
    else:
        entities.insert(0, replacement)
    if articles:
        geo = _lookup_geo(destinations[0][1], services)
        article = (
            services.resolve_direction_article(business, geo, "distribution")
            if geo is not None
            else None
        )
        if article is None:
            return None
        article_index = articles[0][0] + (0 if balances else 1)
        entities[article_index] = services.make_entity(
            "article", "article", article
        )
    return operand.model_copy(update={"entities": entities}, deep=True)


def detect_business_entity_followup(
    message: str,
    active_intent: AnalysisIntent,
    services: BusinessEntityTransitionServices,
) -> BusinessEntityPatchCandidate | None:
    if (
        active_intent.operation not in {Operation.SHOW, Operation.AGGREGATE}
        or len(active_intent.operands) != 1
        or active_intent.grouping
        or active_intent.comparison is not None
        or active_intent.formula is not None
        or active_intent.ranking is not None
        or not callable(services.lookup_balance)
    ):
        return None
    match = _FOLLOWUP.fullmatch(_normalize_text(message))
    if match is None:
        return None
    mention = match.group("business")
    if not _is_qualified_business(mention):
        return None
    business = services.lookup_balance(mention)
    if business is None:
        return None
    operand = active_intent.operands[0]
    updated = (
        _replace_full_balance(operand, business, services)
        if active_intent.operation == Operation.SHOW
        else None
    ) or _replace_distribution_viewpoint(operand, business, services)
    if updated is None:
        return None
    return BusinessEntityPatchCandidate(
        business=business,
        operands=(updated,),
    )


def detect_business_entity_transition(
    state: ContextContractV2,
    message: str,
    turn_id: str,
    services: BusinessEntityTransitionServices,
) -> TransitionDecision | None:
    scope = state.active_dialog_scope
    if scope is None:
        return None
    candidate = detect_business_entity_followup(message, scope.intent, services)
    if candidate is None:
        return None
    mutation = ContextMutation(
        turn_id=turn_id,
        user_message=message,
        normalized_message=message,
        patch=IntentPatch(
            operands=FieldMutation(
                action=MutationAction.SET,
                value=list(candidate.operands),
            )
        ),
    )
    return TransitionDecision(
        kind=TransitionKind.PATCH,
        mutation=mutation,
        interpretation_mode="deterministic_business_entity_patch",
        evidence_businesses=(candidate.business,),
        diagnostic_event="deterministic_business_entity_patch_recognized",
        diagnostic_fields=MappingProxyType(
            {
                "mutation_mode": "business_entity_patch",
                "balance_id": str(candidate.business.balance_id),
            }
        ),
    )
