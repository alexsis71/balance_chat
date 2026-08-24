from __future__ import annotations

from threading import Event, Lock, Thread

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
from balance_chat.store import InMemoryContextStore, RevisionConflict


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
        state.revision = 9999
        state.conversation_window.clear()
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
    assert store_on.get("same-session").conversation_window
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


class RecordingStore(InMemoryContextStore):
    def __init__(self):
        super().__init__()
        self.events = []
        self.commit_requests = []
        self.release_requests = []
        self._events_lock = Lock()

    def _record(self, event):
        with self._events_lock:
            self.events.append(event)

    def reserve(self, session_id, expected_revision, request_id):
        self._record(("reserve", request_id))
        return super().reserve(session_id, expected_revision, request_id)

    def commit(self, session_id, expected_revision, mutation, outcome, **kwargs):
        result = super().commit(
            session_id, expected_revision, mutation, outcome, **kwargs
        )
        self.commit_requests.append(mutation.turn_id)
        self._record(("commit", mutation.turn_id))
        return result

    def save_request_result(self, session_id, request_id, response):
        result = super().save_request_result(session_id, request_id, response)
        self._record(("save_request_result", request_id))
        return result

    def release(self, session_id, request_id):
        self.release_requests.append(request_id)
        self._record(("release", request_id))
        return super().release(session_id, request_id)


class PerRequestProcessor(Processor):
    def process(self, state, *, message, request_id, **kwargs):
        result = super().process(
            state, message=message, request_id=request_id, **kwargs
        )
        result.mutation.turn_id = request_id
        return result


class BlockingFirstShadow:
    def __init__(self, events):
        self.events = events
        self.started = Event()
        self.unblock = Event()
        self.calls = []
        self._lock = Lock()

    def run(self, *, request_id, **_kwargs):
        with self._lock:
            self.calls.append(request_id)
            first = len(self.calls) == 1
        self.events.append(("shadow_start", request_id))
        if first:
            self.started.set()
            assert self.unblock.wait(timeout=5)
        self.events.append(("shadow_end", request_id))
        return SemanticShadowResult(
            eligible=True,
            invoked=True,
            validation_status=ShadowValidationStatus.MALFORMED,
        )


def _recording_service():
    store = RecordingStore()
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
    store.events.clear()
    store.commit_requests.clear()
    processor = PerRequestProcessor(mode="conversation_graph")
    shadow = BlockingFirstShadow(store.events)
    return BalanceChatService(store, processor, semantic_shadow=shadow), store, shadow


def test_same_session_turn_succeeds_while_previous_shadow_is_blocked() -> None:
    service, store, shadow = _recording_service()
    turn_a = {}

    thread = Thread(
        target=lambda: turn_a.setdefault(
            "response",
            service.execute_turn(
                "same-session",
                expected_revision=1,
                message="turn A",
                request_id="request-a",
            ),
        )
    )
    thread.start()
    assert shadow.started.wait(timeout=5)

    response_b = service.execute_turn(
        "same-session",
        expected_revision=2,
        message="turn B",
        request_id="request-b",
    )
    shadow.unblock.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert turn_a["response"]["session"]["revision"] == 2
    assert response_b["session"]["revision"] == 3
    assert store.get("same-session").revision == 3
    assert store.release_requests.count("request-a") == 1
    assert store.release_requests.count("request-b") == 1
    assert store.events.index(("commit", "request-a")) < store.events.index(
        ("save_request_result", "request-a")
    )
    assert store.events.index(("save_request_result", "request-a")) < store.events.index(
        ("release", "request-a")
    )
    assert store.events.index(("release", "request-a")) < store.events.index(
        ("shadow_start", "request-a")
    )
    assert store.events.index(("shadow_start", "request-a")) < store.events.index(
        ("shadow_end", "request-a")
    )


def test_cached_retry_during_shadow_does_not_repeat_commit_or_shadow() -> None:
    service, store, shadow = _recording_service()
    turn_a = {}
    thread = Thread(
        target=lambda: turn_a.setdefault(
            "response",
            service.execute_turn(
                "same-session",
                expected_revision=1,
                message="turn A",
                request_id="request-a",
            ),
        )
    )
    thread.start()
    assert shadow.started.wait(timeout=5)

    retry = service.execute_turn(
        "same-session",
        expected_revision=1,
        message="turn A",
        request_id="request-a",
    )
    shadow.unblock.set()
    thread.join(timeout=5)

    assert retry == turn_a["response"]
    assert store.commit_requests == ["request-a"]
    assert shadow.calls == ["request-a"]
    assert store.release_requests == ["request-a"]


class FixedOutcomeShadow:
    def __init__(self, status):
        self.status = status
        self.calls = 0

    def run(self, **_kwargs):
        self.calls += 1
        if self.status == "raise":
            raise RuntimeError("shadow failed")
        return SemanticShadowResult(
            eligible=True,
            invoked=True,
            validation_status=self.status,
        )


@pytest.mark.parametrize(
    "status",
    [
        ShadowValidationStatus.VALID,
        ShadowValidationStatus.TIMEOUT,
        ShadowValidationStatus.UNAVAILABLE,
        ShadowValidationStatus.MALFORMED,
        "raise",
    ],
)
def test_shadow_outcome_after_release_preserves_following_turn(status) -> None:
    store = RecordingStore()
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
    store.release_requests.clear()
    shadow = FixedOutcomeShadow(status)
    service = BalanceChatService(
        store,
        PerRequestProcessor(mode="conversation_graph"),
        semantic_shadow=shadow,
    )

    first = service.execute_turn(
        "same-session",
        expected_revision=1,
        message="first",
        request_id="request-a",
    )
    second = service.execute_turn(
        "same-session",
        expected_revision=2,
        message="second",
        request_id="request-b",
    )

    assert first["session"]["revision"] == 2
    assert second["session"]["revision"] == 3
    assert store.get("same-session").revision == 3
    assert store.release_requests == ["request-a", "request-b"]


class ProcessorFailure:
    def process(self, *_args, **_kwargs):
        raise RuntimeError("processor failed")


class CommitFailureStore(RecordingStore):
    fail_commit = False

    def commit(self, *args, **kwargs):
        if self.fail_commit:
            raise RuntimeError("commit failed")
        return super().commit(*args, **kwargs)


class PostReserveConflictStore(RecordingStore):
    def reserve(self, session_id, expected_revision, request_id):
        super().reserve(session_id, expected_revision, request_id)
        with self._lock:
            self._states[session_id].revision += 1


@pytest.mark.parametrize("failure", ["revision", "processor", "commit"])
def test_authoritative_failure_releases_acquired_reservation_once(failure) -> None:
    store = PostReserveConflictStore() if failure == "revision" else CommitFailureStore()
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
    store.release_requests.clear()
    if failure == "commit":
        store.fail_commit = True
    processor = ProcessorFailure() if failure == "processor" else PerRequestProcessor()
    service = BalanceChatService(store, processor)

    expected_error = RevisionConflict if failure == "revision" else RuntimeError
    with pytest.raises(expected_error):
        service.execute_turn(
            "same-session",
            expected_revision=1,
            message="failing turn",
            request_id="request-failure",
        )

    assert store.release_requests == ["request-failure"]


class ReleaseFailureStore(RecordingStore):
    def release(self, session_id, request_id):
        self.release_requests.append(request_id)
        self._record(("release", request_id))
        raise RuntimeError("release failed")


def test_shadow_is_skipped_when_reservation_release_fails() -> None:
    store = ReleaseFailureStore()
    state = store.create("same-session")
    InMemoryContextStore.commit(
        store,
        state.session_id,
        0,
        ContextMutation(
            turn_id="turn-initial",
            user_message="initial",
            replace_intent=_intent(),
        ),
        TransitionOutcome.SUCCESS,
    )
    shadow = FixedOutcomeShadow(ShadowValidationStatus.VALID)
    service = BalanceChatService(store, PerRequestProcessor(), semantic_shadow=shadow)

    response = service.execute_turn(
        "same-session",
        expected_revision=1,
        message="turn",
        request_id="request-a",
    )

    assert response["session"]["revision"] == 2
    assert store.release_requests == ["request-a"]
    assert shadow.calls == 0
