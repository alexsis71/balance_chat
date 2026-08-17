from __future__ import annotations

from decimal import Decimal
import re
from types import SimpleNamespace

import pytest

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ContextContractV2,
    ContextMutation,
    IntentPatch,
    MutationAction,
    OperandEntityRef,
    Operation,
    PeriodRef,
    TransitionOutcome,
)
from balance_chat.execution import (
    NativeExecutor,
    PipelineScalarTaskRunner,
    ScalarFact,
)
from balance_chat.planning import NativeMultiOperandPlanner
from balance_chat.processor import PipelineV2TurnProcessor
from balance_chat.reducer import reduce_intent
from balance_chat.store import InMemoryContextStore


_LEMMA_MAP = {
    "самарской": "самарская",
    "ростовской": "ростовская",
    "московской": "московская",
    "области": "область",
    "москве": "москва",
    "татарстану": "татарстан",
    "германии": "германия",
    "польше": "польша",
}


def _normalize_lemmas(value) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    tokens = re.sub(r"[^0-9a-zа-я]+", " ", text).split()
    return " ".join(_LEMMA_MAP.get(token, token) for token in tokens)


def _geo(geo_id: str, canonical_name: str, *aliases: str):
    return SimpleNamespace(
        geo_id=geo_id,
        canonical_name=canonical_name,
        aliases=aliases,
    )


ROSTOV = _geo("geo:rostov", "Ростовская область", "Ростовская")
SAMARA = _geo("geo:samara", "Самарская область", "Самарская")
MOSCOW = _geo("geo:moscow", "Москва", "город Москва")
TATARSTAN = _geo("geo:tatarstan", "Татарстан", "Татария")
GERMANY = _geo("geo:germany", "Германия")
POLAND = _geo("geo:poland", "Польша")


def _entity(
    role: str,
    entity_type: str,
    entity_id: str,
    display_name: str,
) -> OperandEntityRef:
    return OperandEntityRef(
        role=role,
        entity=CanonicalEntityRef(
            entity_id=entity_id,
            entity_type=entity_type,
            display_name=display_name,
        ),
    )


BALANCE = SimpleNamespace(
    balance_id=1,
    canonical_name="ГП ТГ Тест суточный баланс",
    aliases=("ТГ Тест",),
)
ROSTOV_ARTICLE = SimpleNamespace(
    article_id=11,
    balance_id=1,
    canonical_name="Ростовская область",
    aliases=(),
    section="Распределение",
    path=("Распределение", "Ростовская область"),
)
SAMARA_ARTICLE = SimpleNamespace(
    article_id=12,
    balance_id=1,
    canonical_name="Самарская область",
    aliases=(),
    section="Распределение",
    path=("Распределение", "Самарская область"),
)
MOSCOW_ARTICLE = SimpleNamespace(
    article_id=13,
    balance_id=1,
    canonical_name="Москва",
    aliases=(),
    section="Распределение",
    path=("Распределение", "Москва"),
)


class _Registry:
    def __init__(self, *, include_poland: bool = True, articles=()) -> None:
        self.geo_objects = (
            ROSTOV,
            SAMARA,
            MOSCOW,
            TATARSTAN,
            GERMANY,
            *((POLAND,) if include_poland else ()),
        )
        self.geo_groups = (
            SimpleNamespace(canonical_name="Европа"),
            SimpleNamespace(canonical_name="Дальнее зарубежье"),
        )
        self.routes = ()
        self.manifest = SimpleNamespace(bundle_version="test")
        self._articles = {item.article_id: item for item in articles}

    @staticmethod
    def balance(value):
        normalized = _normalize_lemmas(value)
        if normalized in {
            "1",
            "гп тг тест суточный баланс",
            "тг тест",
            "гп тг москва",
            "газпром трансгаз москва",
            "тг ухта",
            "гп тг нижний новгород",
        }:
            return BALANCE
        return None

    def article(self, value):
        return self._articles.get(value)

    def articles_for_balance(self, balance_id):
        if balance_id != 1:
            return ()
        return tuple(self._articles.values())


class _Runtime:
    def __init__(self) -> None:
        self.summary_calls = 0
        self.execution_queries: list[str] = []

    @staticmethod
    def _import_pipeline_module(name):
        if name == "pipeline_v2.nlp_ru":
            return SimpleNamespace(normalize_query_lemmas=_normalize_lemmas)
        raise ImportError(name)

    def execute(self, query, _intent, **_kwargs):
        self.execution_queries.append(query)
        return {"status": "ok", "rows": [{"fact_value": "12.5"}]}

    def summarize_envelope(self, envelope, **_kwargs):
        self.summary_calls += 1
        return envelope


class _CountingInterpreter:
    def __init__(self) -> None:
        self.calls = 0

    def interpret(self, **_kwargs):
        self.calls += 1
        raise AssertionError("contextual interpreter was invoked")


class _NoInterpretationPolicy:
    def should_invoke(self, *_args, **_kwargs):
        return False


class _CapturingPlanner:
    def __init__(self) -> None:
        self.intents: list[AnalysisIntent] = []

    def plan(self, intent):
        self.intents.append(intent)
        return NativeMultiOperandPlanner().plan(intent)


def _fact_extractor(task, _envelope):
    return ScalarFact(
        task_id=task.task_id,
        value=Decimal("12.5"),
        unit="тыс. м3",
        label="Распределение",
    )


def _processor(*, registry=None):
    runtime = _Runtime()
    interpreter = _CountingInterpreter()
    planner = _CapturingPlanner()
    executor = NativeExecutor(
        PipelineScalarTaskRunner(runtime),
        _fact_extractor,
    )
    processor = PipelineV2TurnProcessor(
        runtime=runtime,
        registry=registry or _Registry(),
        interpreter=interpreter,
        compiler=object(),
        executor=executor,
        planner=planner,
        policy=_NoInterpretationPolicy(),
    )
    return processor, runtime, interpreter, planner


def _intent(
    *,
    geo=ROSTOV,
    metric: str = "distribution",
    operation: Operation = Operation.SHOW,
    month: int = 5,
    entities: list[OperandEntityRef] | None = None,
) -> AnalysisIntent:
    if entities is None:
        entities = [_entity("destination", "geo_object", geo.geo_id, geo.canonical_name)]
    return AnalysisIntent(
        operation=operation,
        operands=[
            AnalysisOperand(
                operand_id="metric",
                metric=metric,
                aggregate_type="sum",
                entities=entities,
            )
        ],
        periods=[
            PeriodRef(
                date_from=f"2025-{month:02d}-01",
                date_to=f"2025-{month + 1:02d}-01",
            )
        ],
        grain="total",
    )


def _state(intent: AnalysisIntent | None = None) -> ContextContractV2:
    state = ContextContractV2(session_id="session")
    if intent is None:
        return state
    from balance_chat.reducer import apply_context_transition

    return apply_context_transition(
        state,
        ContextMutation(
            turn_id="turn-1",
            user_message="initial",
            replace_intent=intent,
        ),
        TransitionOutcome.SUCCESS,
    )


def _candidate(processor, message: str, intent: AnalysisIntent | None = None):
    return processor._detect_geo_followup(message, intent or _intent())


def _intent_with_operation(operation: Operation) -> AnalysisIntent:
    return _intent().model_copy(update={"operation": operation}, deep=True)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("А по Самарской области?", SAMARA),
        ("А по Москве?", MOSCOW),
        ("А по Татарстану?", TATARSTAN),
        ("А по Ростовской?", ROSTOV),
        ("А для Германии?", GERMANY),
        ("А по Польше?", POLAND),
        ("По Самарской области", SAMARA),
        ("Для Германии", GERMANY),
    ],
)
def test_detector_resolves_canonical_geo_and_morphology(message, expected) -> None:
    processor, *_ = _processor()

    candidate = _candidate(processor, message)

    assert candidate is not None
    assert candidate.geo is expected
    destination = next(
        item
        for item in candidate.operands[0].entities
        if item.role == "destination"
    )
    assert destination.entity.entity_id == expected.geo_id
    assert destination.entity.display_name == expected.canonical_name


def test_geo_patch_is_patch_only_and_changes_only_operands() -> None:
    processor, *_ = _processor()
    state = _state(_intent())

    produced = processor._deterministic_geo_patch(
        state, "А по Самарской области?", "turn-2"
    )

    assert produced is not None
    mutation, candidate = produced
    assert candidate.geo is SAMARA
    assert mutation.replace_intent is None
    assert mutation.patch.operands.action == MutationAction.SET
    assert mutation.patch.model_copy(update={"operands": None}) == IntentPatch()
    effective = reduce_intent(state, mutation)
    active = state.active_dialog_scope.intent
    assert effective.model_copy(update={"operands": active.operands}, deep=True) == active
    assert effective.operands == list(candidate.operands)
    serialized = ContextMutation.model_validate(mutation.model_dump(mode="json"))
    assert reduce_intent(state, serialized) == effective


def test_geo_patch_preserves_production_entity_order_and_rebinds_article() -> None:
    registry = _Registry(
        articles=(ROSTOV_ARTICLE, SAMARA_ARTICLE, MOSCOW_ARTICLE)
    )
    processor, *_ = _processor(registry=registry)
    entities = [
        _entity("balance", "balance", "BAL:1", BALANCE.canonical_name),
        _entity("destination", "geo_object", ROSTOV.geo_id, ROSTOV.canonical_name),
        _entity("article", "article", "ART:11", ROSTOV_ARTICLE.canonical_name),
    ]

    candidate = _candidate(processor, "А по Самарской области?", _intent(entities=entities))

    assert candidate is not None
    assert [item.role for item in candidate.operands[0].entities] == [
        "balance",
        "destination",
        "article",
    ]
    assert [item.entity.entity_id for item in candidate.operands[0].entities] == [
        "BAL:1",
        SAMARA.geo_id,
        "ART:12",
    ]


def test_geo_patch_preserves_non_geo_entities_without_article() -> None:
    processor, *_ = _processor()
    subject = _entity("subject", "organization", "org:1", "Потребитель")
    active = _intent(
        metric="export",
        geo=GERMANY,
        entities=[subject, _entity("destination", "geo_object", GERMANY.geo_id, "Германия")],
    )

    candidate = _candidate(processor, "А по Польше?", active)

    assert candidate is not None
    assert candidate.operands[0].entities[0] == subject
    assert candidate.operands[0].entities[1].entity.entity_id == POLAND.geo_id


@pytest.mark.parametrize(
    "message",
    [
        "А для ГП ТГ Москва?",
        "А по Газпром трансгаз Москва?",
        "А для ТГ Ухта?",
        "А по ГП ТГ Нижний Новгород?",
    ],
)
def test_business_entity_has_priority_over_geo(message: str) -> None:
    processor, *_ = _processor()

    assert _candidate(processor, message) is None


@pytest.mark.parametrize(
    "message",
    [
        "А по Москве за апрель?",
        "Сравни с Москвой",
        "А Москва и Самара?",
        "Москва или Ростов?",
        "Покажи максимум по Москве",
        "Почему в Москве меньше?",
        "Разбей по областям",
        "Покажи по всем областям",
        "Покажи поставки по областям",
        "А по Москве и за апрель?",
    ],
)
def test_mixed_or_complex_geo_turn_falls_back(message: str) -> None:
    processor, *_ = _processor()

    assert _candidate(processor, message) is None


@pytest.mark.parametrize(
    "message",
    [
        "Сравни Москву с Самарой",
        "Сравни с Ростовской областью",
        "Сравни показатели по Москве",
        "Москва больше Самары?",
        "Кто больше Москва или Самара?",
        "Что выше по Москве и Ростову?",
        "Покажи топ по Москве",
        "Покажи минимум по Самарской области",
        "Максимум для Германии",
        "Рейтинг регионов включая Москву",
        "Разбей по регионам России",
        "Сгруппируй по областям",
        "Покажи отдельно Москву и Самару",
        "Детализация по Самарской области",
        "Покажи по всем регионам",
        "Почему по Москве меньше?",
        "Объясни снижение в Самарской области",
        "Что случилось в Ростовской области?",
        "Почему Германия выросла?",
        "Есть аномалия по Москве?",
        "А по Москве за март?",
        "А по Самаре за 2024 год?",
        "Для Германии в августе",
        "По Ростовской области вчера",
        "По Татарстану за квартал",
        "Москва и Самара",
        "Самара, Москва и Ростов",
        "Германия или Польша",
        "По Москве либо Татарстану",
        "Между Москвой и Самарой",
        "А для ГП ТГ Москва",
        "А по Газпром трансгаз Москва",
        "Для ТГ Ухта",
        "По ГП ТГ Нижний Новгород",
        "А по ТГ Москва и Самара",
        "По маршруту Москва Самара",
        "А по газопроводу Москва Ростов",
        "Через Москву в Самару",
        "Маршрут Германия Польша",
        "Из Москвы в Самару",
        "Покажи экспорт в Германию",
        "Покажи распределение в Москву",
        "Сколько газа в Самарскую область?",
        "Дай данные по Татарстану за май",
        "Новый запрос по Ростовской области",
        "А там по Москве?",
        "А что по ней в Самаре?",
        "По этому региону Москва",
        "А по соседней с Москвой области?",
        "Для этой Германии?",
        "По Европе",
        "Для Дальнего зарубежья",
        "Покажи по областям",
        "По регионам России",
        "По неизвестной области",
    ],
)
def test_false_positive_attack_has_no_partial_geo_patch(message: str) -> None:
    processor, *_ = _processor()

    assert _candidate(processor, message) is None


@pytest.mark.parametrize(
    ("intent", "message"),
    [
        (_intent_with_operation(Operation.COMPARE), "А по Москве?"),
        (_intent_with_operation(Operation.COMPARE_PERIODS), "А по Москве?"),
        (_intent_with_operation(Operation.CALCULATE), "А по Москве?"),
        (_intent_with_operation(Operation.RANK), "А по Москве?"),
        (_intent_with_operation(Operation.GROUP), "А по Москве?"),
        (_intent(metric="storage_injection"), "А по Москве?"),
        (_intent(metric="incoming"), "А по Москве?"),
    ],
)
def test_unsupported_active_shape_falls_back(intent, message) -> None:
    processor, *_ = _processor()

    assert _candidate(processor, message, intent) is None


def test_multiple_operands_fall_back() -> None:
    processor, *_ = _processor()
    active = _intent().model_copy(
        update={"operands": [_intent().operands[0], _intent().operands[0].model_copy(update={"operand_id": "second"})]},
        deep=True,
    )

    assert _candidate(processor, "А по Москве?", active) is None


def test_missing_or_non_geo_destination_falls_back() -> None:
    processor, *_ = _processor()
    missing = _intent(entities=[])
    business_destination = _intent(
        entities=[_entity("destination", "balance", "BAL:2", "ТГ Москва")]
    )

    assert _candidate(processor, "А по Москве?", missing) is None
    assert _candidate(processor, "А по Москве?", business_destination) is None


def test_unresolvable_distribution_article_falls_back() -> None:
    processor, *_ = _processor(registry=_Registry(articles=(ROSTOV_ARTICLE,)))
    active = _intent(
        entities=[
            _entity("balance", "balance", "BAL:1", BALANCE.canonical_name),
            _entity("destination", "geo_object", ROSTOV.geo_id, ROSTOV.canonical_name),
            _entity("article", "article", "ART:11", ROSTOV_ARTICLE.canonical_name),
        ]
    )

    assert _candidate(processor, "А по Самарской области?", active) is None


def test_geo_patch_requires_active_context() -> None:
    processor, *_ = _processor()

    assert processor._deterministic_geo_patch(
        _state(), "А по Самарской области?", "turn-1"
    ) is None


def test_contextual_fallback_still_invokes_interpreter() -> None:
    processor, _runtime, interpreter, _planner = _processor()

    with pytest.raises(AssertionError, match="contextual interpreter was invoked"):
        processor.process(
            _state(_intent()),
            message="Сравни с Самарской областью",
            execute_db=False,
            clarification=None,
            request_id="fallback",
        )

    assert interpreter.calls == 1


def test_missing_canonical_country_falls_back() -> None:
    processor, *_ = _processor(registry=_Registry(include_poland=False))

    assert _candidate(
        processor,
        "А по Польше?",
        _intent(metric="export", geo=GERMANY),
    ) is None


def test_geo_patch_changes_planner_and_scalar_execution_geo_without_llm() -> None:
    processor, runtime, interpreter, planner = _processor()

    processed = processor.process(
        _state(_intent()),
        message="А по Самарской области?",
        execute_db=True,
        clarification=None,
        request_id="geo",
    )

    assert processed.outcome == TransitionOutcome.SUCCESS
    assert processed.diagnostics["interpretation"]["mode"] == "deterministic_geo_patch"
    assert processed.mutation.replace_intent is None
    assert runtime.summary_calls == 0
    assert interpreter.calls == 0
    assert planner.intents[-1].operands[0].entities[-1].entity.entity_id == SAMARA.geo_id
    assert "Самарская область" in runtime.execution_queries[-1]
    assert "Ростовская область" not in runtime.execution_queries[-1]


def test_period_then_geo_then_period_persists_executed_intent() -> None:
    store = InMemoryContextStore()
    store.create("session")
    initial = ContextMutation(
        turn_id="turn-initial",
        user_message="Покажи распределение газа в Ростовскую область за май 2025",
        replace_intent=_intent(),
    )
    state = store.commit("session", 0, initial, TransitionOutcome.SUCCESS)
    processor, runtime, interpreter, planner = _processor()

    period_april = processor.process(
        state,
        message="А за апрель?",
        execute_db=True,
        clarification=None,
        request_id="period-april",
    )
    state = store.commit(
        "session", state.revision, period_april.mutation, period_april.outcome
    )
    geo_samara = processor.process(
        state,
        message="А по Самарской области?",
        execute_db=True,
        clarification=None,
        request_id="geo-samara",
    )
    executed = planner.intents[-1]
    effective = reduce_intent(state, geo_samara.mutation)
    state = store.commit(
        "session", state.revision, geo_samara.mutation, geo_samara.outcome
    )
    reloaded = store.get("session")
    period_may = processor.process(
        reloaded,
        message="А за май?",
        execute_db=True,
        clarification=None,
        request_id="period-may",
    )
    final = store.commit(
        "session", reloaded.revision, period_may.mutation, period_may.outcome
    )

    assert [item.date_from.month for item in effective.periods] == [4]
    assert next(
        item.entity.entity_id
        for item in effective.operands[0].entities
        if item.role == "destination"
    ) == SAMARA.geo_id
    assert effective == executed == state.active_dialog_scope.intent
    assert state.active_dialog_scope.intent == reloaded.active_dialog_scope.intent
    assert final.revision == 4
    assert final.active_dialog_scope.intent.periods[0].date_from.month == 5
    assert next(
        item.entity.entity_id
        for item in final.active_dialog_scope.intent.operands[0].entities
        if item.role == "destination"
    ) == SAMARA.geo_id
    assert period_april.diagnostics["interpretation"]["mode"] == "deterministic_period_patch"
    assert geo_samara.diagnostics["interpretation"]["mode"] == "deterministic_geo_patch"
    assert period_may.diagnostics["interpretation"]["mode"] == "deterministic_period_patch"
    assert runtime.summary_calls == 0
    assert interpreter.calls == 0


def test_repeated_geo_replacement_preserves_period() -> None:
    processor, runtime, interpreter, planner = _processor()
    state = _state(_intent(month=4))

    for request_id, message, expected in [
        ("samara", "А по Самарской области?", SAMARA),
        ("moscow", "А по Москве?", MOSCOW),
        ("rostov", "А по Ростовской области?", ROSTOV),
    ]:
        processed = processor.process(
            state,
            message=message,
            execute_db=True,
            clarification=None,
            request_id=request_id,
        )
        state = __import__("balance_chat.reducer", fromlist=["apply_context_transition"]).apply_context_transition(
            state, processed.mutation, processed.outcome
        )
        assert state.active_dialog_scope.intent.periods[0].date_from.month == 4
        assert next(
            item.entity.entity_id
            for item in state.active_dialog_scope.intent.operands[0].entities
            if item.role == "destination"
        ) == expected.geo_id

    assert runtime.summary_calls == 0
    assert interpreter.calls == 0
    assert len(planner.intents) == 3


def test_country_export_replacement_preserves_august() -> None:
    processor, runtime, interpreter, planner = _processor()
    active = _intent(metric="export", geo=GERMANY, month=8)

    processed = processor.process(
        _state(active),
        message="А по Польше?",
        execute_db=True,
        clarification=None,
        request_id="country",
    )

    effective = planner.intents[-1]
    assert processed.outcome == TransitionOutcome.SUCCESS
    assert effective.periods[0].date_from.month == 8
    assert effective.operands[0].metric == "export"
    assert effective.operands[0].entities[0].entity.entity_id == POLAND.geo_id
    assert runtime.summary_calls == interpreter.calls == 0


def test_period_geo_overlap_reaches_contextual_interpreter() -> None:
    processor, _runtime, interpreter, _planner = _processor()

    with pytest.raises(AssertionError, match="contextual interpreter was invoked"):
        processor.process(
            _state(_intent()),
            message="А по Москве за апрель?",
            execute_db=False,
            clarification=None,
            request_id="mixed",
        )

    assert interpreter.calls == 1
