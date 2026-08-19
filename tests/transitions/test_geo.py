from __future__ import annotations

import re
from types import SimpleNamespace

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ContextContractV2,
    ContextMutation,
    MutationAction,
    OperandEntityRef,
    Operation,
    PeriodRef,
    TransitionOutcome,
)
from balance_chat.reducer import apply_context_transition, reduce_intent
from balance_chat.transitions.geo import (
    GeoTransitionServices,
    detect_geo_followup,
    detect_geo_transition,
)
from balance_chat.transitions.types import TransitionKind


ROSTOV = SimpleNamespace(
    geo_id="geo:rostov", canonical_name="Ростовская область", aliases=("Ростовская",)
)
SAMARA = SimpleNamespace(
    geo_id="geo:samara", canonical_name="Самарская область", aliases=("Самарская",)
)


def _normalize(value) -> str:
    text = re.sub(r"[^а-яa-z0-9]+", " ", str(value).casefold()).strip()
    return (
        text.replace("самарской", "самарская")
        .replace("ростовской", "ростовская")
        .replace("области", "область")
    )


def _make_entity(role: str, entity_type: str, record) -> OperandEntityRef:
    entity_id = record.geo_id if entity_type == "geo_object" else f"ART:{record.article_id}"
    return OperandEntityRef(
        role=role,
        entity=CanonicalEntityRef(
            entity_id=entity_id,
            entity_type=entity_type,
            display_name=record.canonical_name,
        ),
    )


def _services() -> GeoTransitionServices:
    def resolve(message: str):
        normalized = _normalize(message)
        return tuple(
            geo
            for geo in (ROSTOV, SAMARA)
            if any(_normalize(label) in normalized for label in (geo.canonical_name, *geo.aliases))
        )

    return GeoTransitionServices(
        normalize_lemmas=_normalize,
        resolve_geo_objects=resolve,
        lookup_balance=lambda value: object() if "тг" in _normalize(value).split() else None,
        resolve_direction_article=lambda _balance, _target, _metric: None,
        make_entity=_make_entity,
    )


def _intent() -> AnalysisIntent:
    return AnalysisIntent(
        operation=Operation.SHOW,
        operands=[
            AnalysisOperand(
                operand_id="metric",
                metric="distribution",
                entities=[_make_entity("destination", "geo_object", ROSTOV)],
            )
        ],
        periods=[PeriodRef(date_from="2025-04-01", date_to="2025-05-01")],
    )


def _state() -> ContextContractV2:
    return apply_context_transition(
        ContextContractV2(session_id="session"),
        ContextMutation(
            turn_id="initial", user_message="initial", replace_intent=_intent()
        ),
        TransitionOutcome.SUCCESS,
    )


def test_geo_candidate_uses_canonical_services_without_processor() -> None:
    candidate = detect_geo_followup(
        "А по Самарской области?", _intent(), _services()
    )

    assert candidate is not None
    assert candidate.geo is SAMARA
    assert candidate.operands[0].entities[0].entity.entity_id == "geo:samara"


def test_geo_transition_produces_the_existing_patch_contract() -> None:
    state = _state()
    decision = detect_geo_transition(
        state, "А по Самарской области?", "turn-2", _services()
    )

    assert decision is not None
    assert decision.kind == TransitionKind.PATCH
    assert decision.interpretation_mode == "deterministic_geo_patch"
    assert decision.evidence_geos == (SAMARA,)
    assert decision.mutation is not None
    assert decision.mutation.replace_intent is None
    assert decision.mutation.patch.operands.action == MutationAction.SET
    effective = reduce_intent(state, decision.mutation)
    assert effective.periods == _intent().periods
    assert effective.operands[0].entities[0].entity.entity_id == "geo:samara"


def test_qualified_business_and_mixed_turns_fail_closed() -> None:
    assert detect_geo_followup("А по ГП ТГ Самара?", _intent(), _services()) is None
    assert detect_geo_followup("А по Самаре за апрель?", _intent(), _services()) is None
