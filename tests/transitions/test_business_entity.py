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
    GroupingSpec,
    MutationAction,
    OperandEntityRef,
    Operation,
    PeriodRef,
    RankingSpec,
    TransitionOutcome,
)
from balance_chat.reducer import apply_context_transition, reduce_intent
from balance_chat.transitions.business_entity import (
    BusinessEntityTransitionServices,
    detect_business_entity_followup,
    detect_business_entity_transition,
)


def _normalize(value: object) -> str:
    return " ".join(re.sub(r"[^0-9a-zа-я]+", " ", str(value).casefold()).split())


def _balance(balance_id: int, name: str, *aliases: str):
    return SimpleNamespace(
        balance_id=balance_id,
        canonical_name=name,
        aliases=aliases,
    )


OLD = _balance(1, "ГП ТГ Тест суточный баланс", "ТГ Тест")
UKHTA = _balance(
    2,
    "ГП ТГ Ухта суточный баланс",
    "ГП ТГ Ухта",
    "ТГ Ухта",
    "Газпром трансгаз Ухта",
)
MOSCOW = _balance(
    3,
    "ГП ТГ Москва суточный баланс",
    "ГП ТГ Москва",
    "ТГ Москва",
    "Газпром трансгаз Москва",
)
ROSTOV = SimpleNamespace(
    geo_id="geo:rostov",
    canonical_name="Ростовская область",
    aliases=("Ростовская",),
)
OLD_ARTICLE = SimpleNamespace(
    article_id=11,
    balance_id=1,
    canonical_name="Ростовская область",
    aliases=(),
    section="Распределение",
    path=("Распределение", "Ростовская область"),
)
UKHTA_ARTICLE = SimpleNamespace(
    article_id=21,
    balance_id=2,
    canonical_name="Ростовская область",
    aliases=(),
    section="Распределение",
    path=("Распределение", "Ростовская область"),
)


def _entity(role: str, entity_type: str, entity_id: str, name: str):
    return OperandEntityRef(
        role=role,
        entity=CanonicalEntityRef(
            entity_id=entity_id,
            entity_type=entity_type,
            display_name=name,
        ),
    )


def _make_entity(role: str, entity_type: str, record: object):
    prefix, attribute = {
        "balance": ("BAL:", "balance_id"),
        "article": ("ART:", "article_id"),
    }[entity_type]
    return _entity(
        role,
        entity_type,
        f"{prefix}{getattr(record, attribute)}",
        getattr(record, "canonical_name"),
    )


def _services(*, article=UKHTA_ARTICLE) -> BusinessEntityTransitionServices:
    balances = (OLD, UKHTA, MOSCOW)

    def lookup_balance(value: object):
        normalized = _normalize(value)
        for record in balances:
            if normalized in {
                str(record.balance_id),
                _normalize(record.canonical_name),
                *(_normalize(alias) for alias in record.aliases),
            }:
                return record
        return None

    def lookup_geo(value: object):
        return ROSTOV if _normalize(value) in {
            _normalize(ROSTOV.geo_id),
            _normalize(ROSTOV.canonical_name),
        } else None

    def resolve_direction_article(balance, target, metric):
        if (
            article is not None
            and balance is UKHTA
            and target is ROSTOV
            and metric == "distribution"
        ):
            return article
        return None

    return BusinessEntityTransitionServices(
        lookup_balance=lookup_balance,
        lookup_geo=lookup_geo,
        resolve_direction_article=resolve_direction_article,
        make_entity=_make_entity,
    )


def _intent(*, entities=None, metric="distribution", operation=Operation.SHOW):
    return AnalysisIntent(
        operation=operation,
        operands=[AnalysisOperand(
            operand_id="metric",
            metric=metric,
            aggregate_type="sum",
            unit="тыс. м3",
            entities=entities or [
                _entity(
                    "destination",
                    "geo_object",
                    ROSTOV.geo_id,
                    ROSTOV.canonical_name,
                )
            ],
        )],
        periods=[PeriodRef(date_from="2025-05-01", date_to="2025-06-01")],
        grain="total",
    )


def _state(intent: AnalysisIntent | None = None):
    state = ContextContractV2(session_id="session")
    if intent is None:
        return state
    return apply_context_transition(
        state,
        ContextMutation(
            turn_id="turn-1",
            user_message="initial",
            replace_intent=intent,
        ),
        TransitionOutcome.SUCCESS,
    )


@pytest.mark.parametrize(
    "message",
    [
        "А для ГП ТГ Ухта?",
        "А для ТГ Ухта?",
        "А для Газпром трансгаз Ухта?",
        "Только из ГП ТГ Ухта",
        "А теперь для ГП ТГ Ухта",
    ],
)
def test_aliases_resolve_through_registry_to_one_canonical_balance(message: str):
    candidate = detect_business_entity_followup(message, _intent(), _services())

    assert candidate is not None
    assert candidate.business is UKHTA
    assert candidate.operands[0].entities[0] == _make_entity(
        "balance", "balance", UKHTA
    )


def test_distribution_patch_changes_only_business_component():
    active = _intent()
    state = _state(active)

    decision = detect_business_entity_transition(
        state,
        "Только из ГП ТГ Ухта",
        "turn-2",
        _services(),
    )

    assert decision is not None
    mutation = decision.mutation
    assert mutation is not None
    assert mutation.replace_intent is None
    assert mutation.patch.operands.action == MutationAction.SET
    assert decision.evidence_businesses == (UKHTA,)
    effective = reduce_intent(state, mutation)
    assert effective.model_copy(update={"operands": active.operands}, deep=True) == active
    assert effective.operands[0].metric == "distribution"
    assert effective.operands[0].aggregate_type == "sum"
    assert effective.operands[0].unit == "тыс. м3"
    assert effective.periods == active.periods
    assert [item.role for item in effective.operands[0].entities] == [
        "balance",
        "destination",
    ]
    assert effective.operands[0].entities[1] == active.operands[0].entities[0]


def test_direction_article_is_rebound_for_new_business_viewpoint():
    active = _intent(entities=[
        _make_entity("balance", "balance", OLD),
        _entity("destination", "geo_object", ROSTOV.geo_id, ROSTOV.canonical_name),
        _make_entity("article", "article", OLD_ARTICLE),
    ])

    candidate = detect_business_entity_followup(
        "Только из ГП ТГ Ухта", active, _services()
    )

    assert candidate is not None
    by_role = {item.role: item.entity for item in candidate.operands[0].entities}
    assert by_role["balance"].entity_id == "BAL:2"
    assert by_role["destination"].entity_id == ROSTOV.geo_id
    assert by_role["article"].entity_id == "ART:21"


def test_stale_direction_article_fails_closed_when_rebinding_is_absent():
    active = _intent(entities=[
        _make_entity("balance", "balance", OLD),
        _entity("destination", "geo_object", ROSTOV.geo_id, ROSTOV.canonical_name),
        _make_entity("article", "article", OLD_ARTICLE),
    ])

    assert detect_business_entity_followup(
        "Только из ГП ТГ Ухта", active, _services(article=None)
    ) is None


def test_full_balance_snapshot_replaces_its_only_balance():
    active = _intent(
        metric="balance",
        entities=[_make_entity("balance", "balance", OLD)],
    )

    candidate = detect_business_entity_followup(
        "А для ГП ТГ Москва?", active, _services()
    )

    assert candidate is not None
    assert candidate.business is MOSCOW
    assert candidate.operands[0].entities == [
        _make_entity("balance", "balance", MOSCOW)
    ]


@pytest.mark.parametrize(
    "message",
    [
        "А по Москве?",
        "А для ГП ТГ Ухта за апрель?",
        "А для ТГ Ухта в Самарскую область?",
        "А из ГП ТГ Ухта в Самарскую область?",
        "Сравни с ГП ТГ Ухта",
        "Сравни ТГ Ухта и ТГ Москва",
        "Кто больше ТГ Ухта или ТГ Москва?",
        "Покажи максимум для ТГ Ухта",
        "Покажи минимум по ГП ТГ Москва",
        "Покажи рейтинг трансгазов включая ТГ Ухта",
        "Разбей по трансгазам",
        "Сгруппируй по трансгазам",
        "Покажи по всем трансгазам",
        "Почему у ТГ Ухта меньше?",
        "Объясни снижение у ГП ТГ Ухта",
        "Что случилось с ТГ Москва?",
        "Есть аномалия у ТГ Ухта?",
        "А для ТГ Ухта и ТГ Москва?",
        "ТГ Ухта или ТГ Москва?",
        "Между ТГ Ухта и ТГ Москва",
        "Из ТГ Томск в ТГ Сургут",
        "А из Томска в Сургут?",
        "Маршрут ТГ Ухта Москва",
        "Через ТГ Ухта в Самару",
        "По маршруту ГП ТГ Ухта Ростов",
        "Покажи распределение из ГП ТГ Ухта",
        "Покажи поступление в ТГ Москва",
        "Сколько газа у ТГ Ухта за май?",
        "Дай данные ГП ТГ Москва за апрель",
        "Новый запрос для ТГ Ухта",
        "А там для ТГ Ухта?",
        "А что по ней у ТГ Москва?",
        "По этому трансгазу ТГ Ухта",
        "Для соседнего с ТГ Москва баланса",
        "Для неизвестного ТГ?",
        "А для ТГ Неизвестный?",
        "А по ГП ТГ Несуществующий?",
        "А для Газпром трансгаз Неизвестный?",
        "А для Ухты?",
        "А по Москве и для ТГ Ухта?",
        "А для ТГ Ухта и по Москве?",
        "Только из ТГ Ухта за май",
        "Только из ТГ Ухта в Ростов",
        "Только в ТГ Ухта",
        "А теперь наоборот",
        "А для первой ТГ?",
        "А для той же ТГ?",
        "А для двух трансгазов?",
        "А по ТГ Ухта по месяцам?",
        "А для ТГ Ухта максимум?",
    ],
)
def test_business_adversarial_phrases_do_not_partially_patch(message: str):
    assert detect_business_entity_followup(message, _intent(), _services()) is None


def _unsupported_active_intents() -> list[AnalysisIntent]:
    base = _intent()
    comparison_operand = base.operands[0].model_copy(
        update={"operand_id": "comparison"}, deep=True
    )
    return [
        AnalysisIntent(
            operation=Operation.COMPARE,
            operands=[base.operands[0], comparison_operand],
            periods=base.periods,
        ),
        AnalysisIntent(
            operation=Operation.RANK,
            operands=base.operands,
            periods=base.periods,
            ranking=RankingSpec(
                direction="max",
                grain="day",
                bucket_aggregate="sum",
                return_dimension="period",
            ),
        ),
        AnalysisIntent(
            operation=Operation.GROUP,
            operands=base.operands,
            periods=base.periods,
            grouping=[GroupingSpec(dimension="geo")],
        ),
        _intent(metric="incoming"),
        _intent(metric="balance_section", entities=[
            _make_entity("balance", "balance", OLD),
            _make_entity("article", "article", OLD_ARTICLE),
        ]),
        _intent(entities=[
            _make_entity("balance", "balance", OLD),
            _entity("destination", "balance", "BAL:3", MOSCOW.canonical_name),
            _make_entity("article", "article", OLD_ARTICLE),
        ]),
    ]


@pytest.mark.parametrize(
    "active",
    _unsupported_active_intents(),
)
def test_unsafe_or_ambiguous_active_shapes_fail_closed(active: AnalysisIntent):
    assert detect_business_entity_followup(
        "А для ГП ТГ Ухта?", active, _services()
    ) is None


def test_no_active_state_and_same_entity_are_no_match():
    assert detect_business_entity_transition(
        _state(), "А для ГП ТГ Ухта?", "turn-1", _services()
    ) is None
    active = _intent(entities=[
        _make_entity("balance", "balance", UKHTA),
        _entity("destination", "geo_object", ROSTOV.geo_id, ROSTOV.canonical_name),
    ])
    assert detect_business_entity_followup(
        "А для ГП ТГ Ухта?", active, _services()
    ) is None
