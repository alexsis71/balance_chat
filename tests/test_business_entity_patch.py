from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from balance_chat.compat import PipelineRuntime, RuntimeConfig
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
from balance_chat.execution import (
    NativeExecutor,
    PipelineScalarTaskRunner,
    ScalarFact,
)
from balance_chat.planning import NativeMultiOperandPlanner
from balance_chat.processor import PipelineV2TurnProcessor
from balance_chat.reducer import apply_context_transition, reduce_intent
from balance_chat.store import InMemoryContextStore


def _normalize(value: object) -> str:
    forms = {
        "самарской": "самарская",
        "ростовской": "ростовская",
        "области": "область",
        "москве": "москва",
    }
    tokens = re.sub(r"[^0-9a-zа-я]+", " ", str(value).casefold()).split()
    return " ".join(forms.get(token, token) for token in tokens)


def _balance(balance_id: int, name: str, *aliases: str):
    return SimpleNamespace(
        balance_id=balance_id,
        canonical_name=name,
        aliases=aliases,
    )


def _geo(geo_id: str, name: str, *aliases: str):
    return SimpleNamespace(geo_id=geo_id, canonical_name=name, aliases=aliases)


OLD = _balance(1, "ГП ТГ Тест суточный баланс", "ТГ Тест")
UKHTA = _balance(
    2,
    "ГП ТГ Ухта суточный баланс",
    "ГП ТГ Ухта",
    "ТГ Ухта",
    "Газпром трансгаз Ухта",
)
MOSCOW_BUSINESS = _balance(
    3,
    "ГП ТГ Москва суточный баланс",
    "ГП ТГ Москва",
    "ТГ Москва",
    "Газпром трансгаз Москва",
)
NIZHNY_BUSINESS = _balance(
    4,
    "ГП ТГ Н.Новгород суточный баланс",
    "ГП ТГ Нижний Новгород",
    "Газпром трансгаз Нижний Новгород",
    "ТГ Н Новгород",
)
ROSTOV = _geo("geo:rostov", "Ростовская область", "Ростовская")
SAMARA = _geo("geo:samara", "Самарская область", "Самарская")
MOSCOW_GEO = _geo("geo:moscow", "Москва", "город Москва")


def _article(article_id: int, balance_id: int, geo):
    return SimpleNamespace(
        article_id=article_id,
        balance_id=balance_id,
        canonical_name=geo.canonical_name,
        aliases=(),
        section="Распределение",
        path=("Распределение", geo.canonical_name),
    )


OLD_ROSTOV = _article(11, OLD.balance_id, ROSTOV)
UKHTA_ROSTOV = _article(21, UKHTA.balance_id, ROSTOV)
UKHTA_SAMARA = _article(22, UKHTA.balance_id, SAMARA)


def _entity(role: str, entity_type: str, entity_id: str, name: str):
    return OperandEntityRef(
        role=role,
        entity=CanonicalEntityRef(
            entity_id=entity_id,
            entity_type=entity_type,
            display_name=name,
        ),
    )


def _balance_entity(record, role="balance"):
    return _entity(role, "balance", f"BAL:{record.balance_id}", record.canonical_name)


def _geo_entity(record):
    return _entity("destination", "geo_object", record.geo_id, record.canonical_name)


def _article_entity(record):
    return _entity("article", "article", f"ART:{record.article_id}", record.canonical_name)


class _Registry:
    def __init__(self):
        self.balances = (OLD, UKHTA, MOSCOW_BUSINESS, NIZHNY_BUSINESS)
        self.geo_objects = (ROSTOV, SAMARA, MOSCOW_GEO)
        self.geo_groups = ()
        self.routes = ()
        self.manifest = SimpleNamespace(bundle_version="test")
        self.articles = (OLD_ROSTOV, UKHTA_ROSTOV, UKHTA_SAMARA)

    def balance(self, value):
        normalized = _normalize(value)
        for record in self.balances:
            if normalized in {
                str(record.balance_id),
                _normalize(record.canonical_name),
                *(_normalize(alias) for alias in record.aliases),
            }:
                return record
        return None

    def geo(self, value):
        normalized = _normalize(value)
        for record in self.geo_objects:
            if normalized in {
                _normalize(record.geo_id),
                _normalize(record.canonical_name),
                *(_normalize(alias) for alias in record.aliases),
            }:
                return record
        return None

    def article(self, value):
        raw = str(value).rsplit(":", 1)[-1]
        return next(
            (item for item in self.articles if str(item.article_id) == raw), None
        )

    def articles_for_balance(self, balance_id):
        return tuple(
            item for item in self.articles if item.balance_id == balance_id
        )


class _Runtime:
    def __init__(self):
        self.summary_calls = 0
        self.execution_queries: list[str] = []
        self.execution_intents: list[AnalysisIntent] = []
        self.balance_day_calls: list[dict] = []

    @staticmethod
    def _import_pipeline_module(name):
        if name == "pipeline_v2.nlp_ru":
            return SimpleNamespace(normalize_query_lemmas=_normalize)
        raise ImportError(name)

    def execute(self, query, intent, **_kwargs):
        self.execution_queries.append(query)
        self.execution_intents.append(intent)
        return {
            "status": "ok",
            "unit": "тыс. м3",
            "rows": [{"fact_value": "12.5", "unit": "тыс. м3"}],
            "warnings": [],
        }

    def execute_balance_day(self, **kwargs):
        self.balance_day_calls.append(kwargs)
        rows = [
            {
                "article_id": item.article_id,
                "article_name": item.canonical_name,
                "article_scope": item.canonical_name,
                "article_indent": 1,
                "fact_value": "12.5",
                "unit": "тыс. м3",
            }
            for item in (UKHTA_ROSTOV, UKHTA_SAMARA)
        ]
        return {"status": "ok", "unit": "тыс. м3", "rows": rows, "warnings": []}

    def summarize_envelope(self, envelope, **_kwargs):
        self.summary_calls += 1
        return envelope


class _CountingInterpreter:
    def __init__(self):
        self.calls = 0

    def interpret(self, **_kwargs):
        self.calls += 1
        raise AssertionError("contextual interpreter was invoked")


class _NoInterpretationPolicy:
    def should_invoke(self, *_args, **_kwargs):
        return False


class _CapturingPlanner:
    def __init__(self):
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


def _processor():
    runtime = _Runtime()
    interpreter = _CountingInterpreter()
    planner = _CapturingPlanner()
    processor = PipelineV2TurnProcessor(
        runtime=runtime,
        registry=_Registry(),
        interpreter=interpreter,
        compiler=object(),
        executor=NativeExecutor(PipelineScalarTaskRunner(runtime), _fact_extractor),
        planner=planner,
        policy=_NoInterpretationPolicy(),
    )
    return processor, runtime, interpreter, planner


def _intent(*, geo=ROSTOV, month=5, balance=None, article=None, metric="distribution"):
    entities = []
    if balance is not None:
        entities.append(_balance_entity(balance))
    if metric == "distribution":
        entities.append(_geo_entity(geo))
    if article is not None:
        entities.append(_article_entity(article))
    return AnalysisIntent(
        operation=Operation.SHOW,
        operands=[AnalysisOperand(
            operand_id="metric",
            metric=metric,
            aggregate_type="sum",
            entities=entities,
        )],
        periods=[PeriodRef(
            date_from=f"2025-{month:02d}-01",
            date_to=f"2025-{month + 1:02d}-01",
        )],
        grain="total",
    )


def _state(intent):
    return apply_context_transition(
        ContextContractV2(session_id="session"),
        ContextMutation(
            turn_id="initial",
            user_message="initial",
            replace_intent=intent,
        ),
        TransitionOutcome.SUCCESS,
    )


def _by_role(intent):
    return {
        item.role: item.entity
        for item in intent.operands[0].entities
    }


def test_native_business_patch_reaches_planner_execution_and_commit_without_llm():
    processor, runtime, interpreter, planner = _processor()
    state = _state(_intent())

    processed = processor.process(
        state,
        message="Только из ГП ТГ Ухта",
        execute_db=True,
        clarification=None,
        request_id="business-native",
    )

    executed = planner.intents[-1]
    roles = _by_role(executed)
    assert processed.outcome == TransitionOutcome.SUCCESS
    assert processed.diagnostics["interpretation"]["mode"] == (
        "deterministic_business_entity_patch"
    )
    assert processed.mutation.replace_intent is None
    assert processed.mutation.patch.operands.action == MutationAction.SET
    assert roles["balance"].entity_id == "BAL:2"
    assert roles["destination"].entity_id == ROSTOV.geo_id
    assert "ГП ТГ Ухта" in runtime.execution_queries[-1]
    assert "Ростовская область" in runtime.execution_queries[-1]
    assert runtime.summary_calls == interpreter.calls == 0

    store = InMemoryContextStore()
    store.create("commit-session")
    initial = store.commit(
        "commit-session",
        0,
        ContextMutation(
            turn_id="initial",
            user_message="initial",
            replace_intent=state.active_dialog_scope.intent,
        ),
        TransitionOutcome.SUCCESS,
    )
    committed = store.commit(
        "commit-session",
        initial.revision,
        processed.mutation,
        processed.outcome,
    )
    assert executed == reduce_intent(initial, processed.mutation)
    assert executed == committed.active_dialog_scope.intent
    assert executed == store.get("commit-session").active_dialog_scope.intent


def test_direction_bound_article_is_rebound_before_balance_day_execution():
    processor, runtime, interpreter, planner = _processor()
    state = _state(_intent(balance=OLD, article=OLD_ROSTOV))

    processed = processor.process(
        state,
        message="Только из ТГ Ухта",
        execute_db=True,
        clarification=None,
        request_id="business-directed",
    )

    roles = _by_role(planner.intents[-1])
    assert processed.outcome == TransitionOutcome.SUCCESS
    assert processed.diagnostics["execution"]["layer"] == "unified_directed_flow"
    assert roles["balance"].entity_id == "BAL:2"
    assert roles["destination"].entity_id == ROSTOV.geo_id
    assert roles["article"].entity_id == "ART:21"
    assert runtime.balance_day_calls[-1]["balance_id"] == UKHTA.balance_id
    assert processed.response["rows"][0]["article_name"] == (
        UKHTA_ROSTOV.canonical_name
    )
    assert runtime.summary_calls == interpreter.calls == 0


def test_full_balance_snapshot_switches_balance_and_skips_summary():
    processor, runtime, interpreter, planner = _processor()
    state = _state(_intent(balance=MOSCOW_BUSINESS, metric="balance"))

    processed = processor.process(
        state,
        message="А для ГП ТГ Нижний Новгород?",
        execute_db=True,
        clarification=None,
        request_id="business-balance",
    )

    roles = _by_role(planner.intents[-1])
    assert processed.outcome == TransitionOutcome.SUCCESS
    assert processed.diagnostics["execution"]["layer"] == "unified_balance_level"
    assert roles["balance"].entity_id == "BAL:4"
    assert runtime.execution_intents[-1].operands[0].entities[0].entity.entity_id == "BAL:4"
    assert runtime.summary_calls == interpreter.calls == 0


@pytest.mark.parametrize(
    "message",
    [
        "А для ГП ТГ Ухта за апрель?",
        "А из ГП ТГ Ухта в Самарскую область?",
        "Сравни с ГП ТГ Ухта",
        "А для ТГ Ухта и ТГ Москва?",
    ],
)
def test_mixed_or_ambiguous_business_turn_uses_existing_contextual_fallback(message):
    processor, runtime, interpreter, planner = _processor()

    with pytest.raises(AssertionError, match="contextual interpreter was invoked"):
        processor.process(
            _state(_intent()),
            message=message,
            execute_db=True,
            clarification=None,
            request_id="business-fallback",
        )

    assert interpreter.calls == 1
    assert runtime.execution_queries == []
    assert planner.intents == []


@pytest.mark.parametrize(
    "sequence",
    [
        ("А за апрель?", "А по Самарской области?", "Только из ГП ТГ Ухта"),
        ("А за апрель?", "Только из ГП ТГ Ухта", "А по Самарской области?"),
        ("А по Самарской области?", "Только из ГП ТГ Ухта", "А за апрель?"),
        ("Только из ГП ТГ Ухта", "А по Самарской области?", "А за апрель?"),
    ],
)
def test_period_geo_business_permutations_preserve_independent_dimensions(sequence):
    store = InMemoryContextStore()
    state = store.create("sequence")
    state = store.commit(
        "sequence",
        0,
        ContextMutation(
            turn_id="initial",
            user_message="initial",
            replace_intent=_intent(),
        ),
        TransitionOutcome.SUCCESS,
    )
    processor, runtime, interpreter, planner = _processor()

    for expected_revision, message in enumerate(sequence, start=2):
        processed = processor.process(
            state,
            message=message,
            execute_db=True,
            clarification=None,
            request_id=f"sequence-{expected_revision}",
        )
        executed = planner.intents[-1]
        effective = reduce_intent(state, processed.mutation)
        state = store.commit(
            "sequence", state.revision, processed.mutation, processed.outcome
        )
        assert state.revision == expected_revision
        assert executed == effective == state.active_dialog_scope.intent
        assert state.active_dialog_scope.intent == store.get(
            "sequence"
        ).active_dialog_scope.intent

    final = state.active_dialog_scope.intent
    roles = _by_role(final)
    assert final.periods == [
        PeriodRef(date_from="2025-04-01", date_to="2025-05-01")
    ]
    assert roles["balance"].entity_id == "BAL:2"
    assert roles["destination"].entity_id == SAMARA.geo_id
    assert runtime.summary_calls == interpreter.calls == 0


def test_geo_moscow_and_qualified_business_moscow_remain_distinct_categories():
    processor, _runtime, interpreter, planner = _processor()
    state = _state(_intent())

    geo = processor.process(
        state,
        message="А по Москве?",
        execute_db=True,
        clarification=None,
        request_id="geo-moscow",
    )
    business = processor.process(
        state,
        message="А для ТГ Москва?",
        execute_db=True,
        clarification=None,
        request_id="business-moscow",
    )

    assert geo.diagnostics["interpretation"]["mode"] == "deterministic_geo_patch"
    assert _by_role(planner.intents[-2])["destination"].entity_id == MOSCOW_GEO.geo_id
    assert business.diagnostics["interpretation"]["mode"] == (
        "deterministic_business_entity_patch"
    )
    assert _by_role(planner.intents[-1])["balance"].entity_id == "BAL:3"
    assert interpreter.calls == 0


def test_ready_metadata_aliases_and_known_short_nizhny_gap():
    root = Path(__file__).resolve().parents[2] / "pipeline"
    registry = PipelineRuntime(
        RuntimeConfig(
            pipeline_root=root,
            metadata_manifest=root / "data" / "metadata" / "manifest.json",
        )
    ).load_metadata_registry()

    ukhta_ids = {
        registry.balance(value).balance_id
        for value in (
            "ГП ТГ Ухта",
            "ТГ Ухта",
            "Газпром трансгаз Ухта",
        )
    }
    moscow_ids = {
        registry.balance(value).balance_id
        for value in (
            "ГП ТГ Москва",
            "ТГ Москва",
            "Газпром трансгаз Москва",
        )
    }
    assert len(ukhta_ids) == len(moscow_ids) == 1
    assert registry.balance("ГП ТГ Нижний Новгород") is not None
    assert registry.balance("Газпром трансгаз Нижний Новгород") is not None
    assert registry.balance("ТГ Нижний Новгород") is None
