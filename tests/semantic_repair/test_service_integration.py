from __future__ import annotations

import pytest

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    ContextMutation,
    Operation,
    PeriodRef,
    TransitionOutcome,
)
from balance_chat.semantic_repair.backend import ShadowModelResponse
from balance_chat.semantic_repair.contracts import (
    ProposalAction,
    SemanticMutation,
    SemanticShadowResult,
    SemanticTransitionProposal,
    ShadowValidationStatus,
)
from balance_chat.semantic_repair.shadow import SemanticShadowRunner
from balance_chat.service import BalanceChatService, TurnProcessResult
from balance_chat.store import InMemoryContextStore


def _intent(*, metric="distribution") -> AnalysisIntent:
    return AnalysisIntent(
        intent_id="00000000-0000-0000-0000-000000000005",
        operation=Operation.SHOW,
        operands=[AnalysisOperand(operand_id="flow", metric=metric)],
        periods=[PeriodRef(date_from="2025-05-01", date_to="2025-06-01")],
    )


class Processor:
    def __init__(self, *, mode="conversation_graph", clarify=False):
        self.mode = mode
        self.clarify = clarify
        self.last_result = None

    def process(self, state, *, message, **_kwargs):
        outcome = (
            TransitionOutcome.CLARIFICATION
            if self.clarify
            else TransitionOutcome.SUCCESS
        )
        self.last_result = TurnProcessResult(
            mutation=ContextMutation(
                turn_id="turn-current",
                user_message=message,
                normalized_message=message,
                replace_intent=_intent(metric="authoritative_metric"),
            ),
            outcome=outcome,
            response={"title": "authoritative", "rows": [{"value": 42}]},
            clarification_questions=(
                [{"question": "Production question?", "options": ["A", "B"]}]
                if self.clarify
                else None
            ),
            diagnostics={
                "interpretation": {"mode": self.mode, "source": "qwen"},
                "execution": {"status": "ok", "task_count": 1},
            },
        )
        return self.last_result


class DangerousShadow:
    def __init__(self, store, processor, *, fail=False, clarify=False):
        self.store = store
        self.processor = processor
        self.fail = fail
        self.clarify = clarify
        self.calls = 0
        self.observed_committed_revision = None
        self.mutation_unchanged = None

    def run(self, *, state, **_kwargs):
        self.calls += 1
        self.observed_committed_revision = self.store.get(state.session_id).revision
        mutation_before = self.processor.last_result.mutation.model_dump(mode="json")
        state.active_dialog_scope.intent.operands[0].metric = "shadow_corruption"
        self.mutation_unchanged = (
            self.processor.last_result.mutation.model_dump(mode="json")
            == mutation_before
        )
        if self.fail:
            raise RuntimeError("shadow failure")
        proposal = SemanticTransitionProposal(
            action=ProposalAction.CLARIFY if self.clarify else ProposalAction.PATCH,
            mutations=([] if self.clarify else [SemanticMutation(kind="swap_direction")]),
            clarification_question=("Shadow question?" if self.clarify else None),
            confidence=1.0,
        )
        return SemanticShadowResult(
            eligible=True,
            invoked=True,
            proposal=proposal,
            validation_status=ShadowValidationStatus.VALID,
            latency_ms=1,
            model="Qwen/Qwen3.8-27B",
        )


def _service(*, shadow=None, mode="conversation_graph", clarify=False):
    store = InMemoryContextStore()
    state = store.create("same-session")
    store.commit(
        state.session_id,
        0,
        ContextMutation(
            turn_id="turn-initial",
            user_message="initial",
            replace_intent=_intent(),
        ),
        TransitionOutcome.SUCCESS,
    )
    processor = Processor(mode=mode, clarify=clarify)
    if shadow == "dangerous":
        shadow = DangerousShadow(store, processor)
    elif shadow == "clarify":
        shadow = DangerousShadow(store, processor, clarify=True)
    elif shadow == "failure":
        shadow = DangerousShadow(store, processor, fail=True)
    return BalanceChatService(store, processor, semantic_shadow=shadow), store, processor, shadow


def _execute(service):
    return service.execute_turn(
        "same-session",
        expected_revision=1,
        message="А теперь наоборот",
        request_id="same-request",
    )


def _without_timestamps(value):
    if isinstance(value, dict):
        return {
            key: _without_timestamps(item)
            for key, item in value.items()
            if key not in {"created_at", "updated_at"}
        }
    if isinstance(value, list):
        return [_without_timestamps(item) for item in value]
    return value


def test_shadow_on_and_off_have_identical_production_output_and_state() -> None:
    service_off, store_off, _, _ = _service()
    service_on, store_on, processor_on, shadow = _service(shadow="dangerous")

    response_off = _execute(service_off)
    response_on = _execute(service_on)

    assert _without_timestamps(response_on) == _without_timestamps(response_off)
    assert _without_timestamps(
        store_on.get("same-session").model_dump(mode="json")
    ) == _without_timestamps(store_off.get("same-session").model_dump(mode="json"))
    assert store_on.get("same-session").revision == 2
    assert store_on.get("same-session").active_dialog_scope.intent.operands[0].metric == (
        "authoritative_metric"
    )
    assert shadow.calls == 1
    assert shadow.observed_committed_revision == 2
    assert shadow.mutation_unchanged is True
    assert processor_on.last_result.mutation.replace_intent.operands[0].metric == (
        "authoritative_metric"
    )
    assert "semantic_shadow" not in response_on
    assert "proposal" not in response_on


def test_shadow_clarification_cannot_replace_production_clarification() -> None:
    service, store, _, shadow = _service(shadow="clarify", clarify=True)

    response = _execute(service)

    assert response["status"] == "needs_clarification"
    assert store.get("same-session").pending_clarification.questions[0].question == (
        "Production question?"
    )
    assert "Shadow question?" not in str(response)
    assert shadow.calls == 1


def test_unexpected_shadow_failure_is_non_blocking_after_commit() -> None:
    service, store, _, shadow = _service(shadow="failure")

    response = _execute(service)

    assert response["status"] == "ok"
    assert response["result"]["title"] == "authoritative"
    assert store.get("same-session").revision == 2
    assert shadow.calls == 1


class CountingBackend:
    def __init__(self):
        self.calls = 0

    def invoke(self, *_args, **_kwargs):
        self.calls += 1
        return ShadowModelResponse(content="{}")


@pytest.mark.parametrize(
    "mode",
    [
        "deterministic_period_patch",
        "deterministic_geo_patch",
        "deterministic_business_entity_patch",
    ],
)
def test_deterministic_patch_service_path_makes_zero_shadow_calls(mode) -> None:
    backend = CountingBackend()
    runner = SemanticShadowRunner(backend, enabled=True)
    service, store, _, _ = _service(shadow=runner, mode=mode)

    response = _execute(service)

    assert response["status"] == "ok"
    assert store.get("same-session").revision == 2
    assert backend.calls == 0
