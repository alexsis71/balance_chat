from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ContextContractV2,
    ContextMutation,
    OperandEntityRef,
    Operation,
    PeriodRef,
    TransitionOutcome,
)
from balance_chat.reducer import apply_context_transition, reduce_intent
from balance_chat.transitions import (
    GeoTransitionServices,
    TransitionKind,
    detect_deterministic_transition,
)


GEOS = tuple(
    SimpleNamespace(geo_id=f"geo:{key}", canonical_name=name, aliases=aliases)
    for key, name, aliases in (
        ("rostov", "Ростовская область", ("Ростовская",)),
        ("samara", "Самарская область", ("Самарская", "Самара")),
        ("moscow", "Москва", ()),
        ("tatarstan", "Татарстан", ()),
        ("germany", "Германия", ()),
        ("poland", "Польша", ()),
    )
)

_FORMS = {
    "ростовской": "ростовская",
    "самарской": "самарская",
    "москве": "москва",
    "татарстану": "татарстан",
    "германии": "германия",
    "польше": "польша",
    "области": "область",
}


def _normalize(value) -> str:
    tokens = re.sub(r"[^а-яa-z0-9]+", " ", str(value).casefold()).split()
    return " ".join(_FORMS.get(token, token) for token in tokens)


def _entity(role: str, entity_type: str, record) -> OperandEntityRef:
    return OperandEntityRef(
        role=role,
        entity=CanonicalEntityRef(
            entity_id=record.geo_id,
            entity_type=entity_type,
            display_name=record.canonical_name,
        ),
    )


def _services() -> GeoTransitionServices:
    def resolve(message: str):
        normalized = _normalize(message)
        return tuple(
            geo
            for geo in GEOS
            if any(
                _normalize(label) in normalized
                for label in (geo.canonical_name, *geo.aliases)
            )
        )

    return GeoTransitionServices(
        normalize_lemmas=_normalize,
        resolve_geo_objects=resolve,
        lookup_balance=lambda value: (
            object()
            if "тг" in _normalize(value).split()
            or _normalize(value).startswith("газпром трансгаз ")
            else None
        ),
        resolve_direction_article=lambda _balance, _target, _metric: None,
        make_entity=_entity,
    )


def _state(*, active: bool = True) -> ContextContractV2:
    empty = ContextContractV2(session_id="session")
    if not active:
        return empty
    intent = AnalysisIntent(
        operation=Operation.SHOW,
        operands=[
            AnalysisOperand(
                operand_id="metric",
                metric="distribution",
                entities=[_entity("destination", "geo_object", GEOS[0])],
            )
        ],
        periods=[PeriodRef(date_from="2025-05-01", date_to="2025-06-01")],
    )
    return apply_context_transition(
        empty,
        ContextMutation(
            turn_id="initial", user_message="initial", replace_intent=intent
        ),
        TransitionOutcome.SUCCESS,
    )


@pytest.mark.parametrize(
    ("message", "expected_mode", "active"),
    [
        ("А за апрель?", "deterministic_period_patch", True),
        ("А за январь 2025?", "deterministic_period_patch", True),
        ("За февраль?", "deterministic_period_patch", True),
        ("Покажи за июнь", "deterministic_period_patch", True),
        ("А за декабрь?", "deterministic_period_patch", True),
        ("А по Самарской области?", "deterministic_geo_patch", True),
        ("А по Москве?", "deterministic_geo_patch", True),
        ("А по Татарстану?", "deterministic_geo_patch", True),
        ("А по Ростовской?", "deterministic_geo_patch", True),
        ("А для Германии?", "deterministic_geo_patch", True),
        ("А по Польше?", "deterministic_geo_patch", True),
        ("А по Москве за апрель?", None, True),
        ("Сравни с Самарской областью", None, True),
        ("А по ГП ТГ Москва?", None, True),
        ("А для Газпром трансгаз Москва?", None, True),
        ("А Москва и Самара?", None, True),
        ("Москва или Ростов?", None, True),
        ("Покажи максимум по Москве", None, True),
        ("Почему в Москве меньше?", None, True),
        ("Разбей по областям", None, True),
        ("Покажи по всем областям", None, True),
        ("Покажи поставки по областям", None, True),
        ("А по Европе?", None, True),
        ("А по неизвестной области?", None, True),
        ("Покажи экспорт в Германию", None, True),
        ("Из Москвы в Самару", None, True),
        ("А за прошлый месяц?", None, True),
        ("За какой апрель?", None, True),
        ("А за апрель?", None, False),
        ("А по Самарской области?", None, False),
    ],
)
def test_representative_turn_equivalence_matrix(
    message: str,
    expected_mode: str | None,
    active: bool,
) -> None:
    state = _state(active=active)
    decision = detect_deterministic_transition(
        message=message,
        state=state,
        turn_id="turn-2",
        clarification_provided=False,
        geo_services=_services(),
    )

    assert decision.interpretation_mode == expected_mode
    assert decision.kind == (
        TransitionKind.PATCH if expected_mode else TransitionKind.NO_MATCH
    )
    if decision.mutation is not None:
        effective = reduce_intent(state, decision.mutation)
        assert effective.operation == Operation.SHOW
        assert effective.operands[0].metric == "distribution"


def test_clarification_disables_both_deterministic_transitions() -> None:
    decision = detect_deterministic_transition(
        message="А за апрель?",
        state=_state(),
        turn_id="turn-2",
        clarification_provided=True,
        geo_services=_services(),
    )

    assert decision.kind == TransitionKind.NO_MATCH
