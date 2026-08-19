from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from types import MappingProxyType
from typing import Any, Callable, Sequence

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
class GeoTransitionServices:
    normalize_lemmas: Callable[[Any], str] | None
    resolve_geo_objects: Callable[[str], Sequence[Any]]
    lookup_balance: Callable[[Any], Any] | None
    resolve_direction_article: Callable[[Any, Any, str], Any | None]
    make_entity: Callable[[str, str, Any], OperandEntityRef]


@dataclass(frozen=True, slots=True)
class GeoPatchCandidate:
    geo: Any
    operands: tuple[AnalysisOperand, ...]


def _normalize_text(value: str) -> str:
    text = str(value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^0-9a-zа-я]+", " ", text).split())


def _metadata_numeric_id(value: Any) -> int:
    match = re.search(r"(\d+)$", str(value).strip())
    return int(match.group(1)) if match else -1


def _whole_geo_mention_matches(
    mention: str,
    geo: Any,
    normalize_lemmas: Callable[[Any], str],
) -> bool:
    def _tokens(value: str) -> list[str]:
        region_types = {"область": "обл", "област": "обл"}
        return [
            region_types.get(token, token)
            for token in normalize_lemmas(value).split()
        ]

    mention_tokens = _tokens(mention)
    if not mention_tokens:
        return False
    for label in (geo.canonical_name, *(geo.aliases or ())):
        label_tokens = _tokens(label)
        if len(label_tokens) != len(mention_tokens):
            continue
        threshold = 0.84 if len(label_tokens) == 1 else 0.76
        if all(
            SequenceMatcher(None, left, right).ratio() >= threshold
            for left, right in zip(mention_tokens, label_tokens)
        ):
            return True
    return False


def detect_geo_followup(
    message: str,
    active_intent: AnalysisIntent,
    services: GeoTransitionServices,
) -> GeoPatchCandidate | None:
    normalize_lemmas = services.normalize_lemmas
    if (
        active_intent.operation not in {Operation.SHOW, Operation.AGGREGATE}
        or len(active_intent.operands) != 1
        or active_intent.grouping
        or active_intent.comparison is not None
        or active_intent.formula is not None
        or active_intent.ranking is not None
        or normalize_lemmas is None
    ):
        return None
    operand = active_intent.operands[0]
    if operand.metric not in {"distribution", "export"}:
        return None
    followup = re.fullmatch(
        r"(?:а\s+)?(?:по|для)\s+(?P<geo>.+)", _normalize_text(message)
    )
    if followup is None:
        return None
    mention = followup.group("geo")
    mention_tokens = normalize_lemmas(mention).split()
    qualified_business = (
        "тг" in mention_tokens
        or mention_tokens[:2] == ["газпром", "трансгаз"]
        or mention_tokens[:3] == ["ооо", "газпром", "трансгаз"]
    )
    if (
        qualified_business
        and services.lookup_balance is not None
        and services.lookup_balance(mention) is not None
    ):
        return None
    matches = list(services.resolve_geo_objects(mention))
    if (
        len(matches) != 1
        or not _whole_geo_mention_matches(mention, matches[0], normalize_lemmas)
    ):
        return None
    geo = matches[0]
    destinations = [
        (index, item)
        for index, item in enumerate(operand.entities)
        if item.role == "destination" and item.entity.entity_type == "geo_object"
    ]
    roles = {item.role for item in operand.entities}
    if len(destinations) != 1 or roles & {"source", "route"}:
        return None
    article_indexes = [
        index for index, item in enumerate(operand.entities) if item.role == "article"
    ]
    entities = [item.model_copy(deep=True) for item in operand.entities]
    destination_index, _destination = destinations[0]
    entities[destination_index] = services.make_entity(
        "destination", "geo_object", geo
    )
    if article_indexes:
        balances = [
            item
            for item in operand.entities
            if item.role == "balance" and item.entity.entity_type == "balance"
        ]
        if (
            operand.metric != "distribution"
            or len(article_indexes) != 1
            or len(balances) != 1
            or services.lookup_balance is None
        ):
            return None
        balance = services.lookup_balance(
            _metadata_numeric_id(balances[0].entity.entity_id)
        )
        article = (
            services.resolve_direction_article(balance, geo, "distribution")
            if balance is not None
            else None
        )
        if article is None:
            return None
        entities[article_indexes[0]] = services.make_entity(
            "article", "article", article
        )
    updated_operand = operand.model_copy(update={"entities": entities}, deep=True)
    return GeoPatchCandidate(geo=geo, operands=(updated_operand,))


def detect_geo_transition(
    state: ContextContractV2,
    message: str,
    turn_id: str,
    services: GeoTransitionServices,
) -> TransitionDecision | None:
    scope = state.active_dialog_scope
    if scope is None:
        return None
    candidate = detect_geo_followup(message, scope.intent, services)
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
        interpretation_mode="deterministic_geo_patch",
        evidence_geos=(candidate.geo,),
        diagnostic_event="deterministic_geo_patch_recognized",
        diagnostic_fields=MappingProxyType(
            {"mutation_mode": "geo_patch", "geo_id": str(candidate.geo.geo_id)}
        ),
    )
