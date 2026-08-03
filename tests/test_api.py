from __future__ import annotations

from fastapi.testclient import TestClient

from balance_chat.api import create_app
from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    ContextMutation,
    Operation,
    PeriodRef,
    TransitionOutcome,
)
from balance_chat.service import BalanceChatService, TurnProcessResult, TurnProcessingError
from balance_chat.store import InMemoryContextStore


class FakeProcessor:
    def __init__(self) -> None:
        self.calls = 0
        self.last_clarification = None

    def process(self, state, *, message, clarification, **_):
        self.calls += 1
        self.last_clarification = clarification
        if message == "уточни":
            return TurnProcessResult(
                mutation=ContextMutation(
                    turn_id="turn-clarify",
                    user_message=message,
                    replace_intent=_intent(),
                ),
                outcome=TransitionOutcome.CLARIFICATION,
                clarification_questions=[
                    {
                        "question": "Какой период?",
                        "options": ["май 2025", "июнь 2025"],
                    }
                ],
            )
        return TurnProcessResult(
            mutation=ContextMutation(
                turn_id=f"turn-{self.calls}",
                user_message=message,
                normalized_message=message,
                replace_intent=_intent(),
            ),
            outcome=TransitionOutcome.SUCCESS,
            response={"title": "Готово", "rows": [{"value": 42}]},
            diagnostics={
                "result_memory": {
                    "retrieved_chunks": 0,
                    "content": "must not leave the service",
                    "embedding": [0.1, 0.2],
                }
            },
        )


class InvalidContractProcessor:
    def process(self, *_args, **_kwargs):
        raise TurnProcessingError(
            "interpretation contract validation failed",
            code="interpretation_contract_invalid",
        )


def _intent() -> AnalysisIntent:
    return AnalysisIntent(
        operation=Operation.SHOW,
        operands=[AnalysisOperand(operand_id="supply", metric="distribution")],
        periods=[PeriodRef(date_from="2025-05-01", date_to="2025-06-01")],
    )


def _client():
    processor = FakeProcessor()
    service = BalanceChatService(InMemoryContextStore(), processor)
    return TestClient(create_app(service)), processor


def test_revision_aware_session_and_turn_api() -> None:
    client, processor = _client()
    created = client.post("/api/v2/chat/sessions").json()
    session_id = created["session"]["session_id"]

    response = client.post(
        "/api/v2/chat",
        json={
            "session_id": session_id,
            "expected_revision": 0,
            "message": "Покажи поставки за май 2025",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session"]["revision"] == 1
    assert body["context"]["active"]["intent"]["operands"][0]["metric"] == "distribution"
    assert body["diagnostics"]["result_memory"]["retrieved_chunks"] == 0
    assert "content" not in body["diagnostics"]["result_memory"]
    assert "embedding" not in body["diagnostics"]["result_memory"]
    assert processor.calls == 1


def test_stale_revision_is_rejected_before_processor() -> None:
    client, processor = _client()
    session_id = client.post("/api/v2/chat/sessions").json()["session"]["session_id"]
    payload = {
        "session_id": session_id,
        "expected_revision": 0,
        "message": "Покажи поставки",
    }
    assert client.post("/api/v2/chat", json=payload).status_code == 200
    stale = client.post("/api/v2/chat", json=payload)

    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "revision_conflict"
    assert processor.calls == 1


def test_clarification_contract_is_exposed_to_ui() -> None:
    client, _ = _client()
    session_id = client.post("/api/v2/chat/sessions").json()["session"]["session_id"]
    response = client.post(
        "/api/v2/chat",
        json={
            "session_id": session_id,
            "expected_revision": 0,
            "message": "уточни",
        },
    ).json()

    assert response["status"] == "needs_clarification"
    question = response["context"]["pending_clarification"]["questions"][0]
    assert question["options"] == ["май 2025", "июнь 2025"]


def test_typed_clarification_answer_reaches_processor() -> None:
    client, processor = _client()
    session_id = client.post("/api/v2/chat/sessions").json()["session"]["session_id"]
    clarified = client.post(
        "/api/v2/chat",
        json={
            "session_id": session_id,
            "expected_revision": 0,
            "message": "уточни",
        },
    ).json()
    pending = clarified["context"]["pending_clarification"]
    question = pending["questions"][0]

    response = client.post(
        "/api/v2/chat",
        json={
            "session_id": session_id,
            "expected_revision": 1,
            "message": "май 2025",
            "clarification": {
                "source_turn_id": pending["turn_id"],
                "clarification_id": question["clarification_id"],
                "selected_option": "май 2025",
            },
        },
    )

    assert response.status_code == 200
    assert processor.last_clarification.selected_option == "май 2025"


def test_delete_session_is_idempotent_and_ui_is_served() -> None:
    client, _ = _client()
    session_id = client.post("/api/v2/chat/sessions").json()["session"]["session_id"]
    assert client.delete(f"/api/v2/chat/sessions/{session_id}").json()["deleted"]
    assert not client.delete(f"/api/v2/chat/sessions/{session_id}").json()["deleted"]
    ui = client.get("/")
    assert ui.status_code == 200
    assert "Активный контекст" in ui.text
    assert "История" in ui.text
    assert "Этапы выполнения" in ui.text
    assert "Техническая панель" not in ui.text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/styles.css").status_code == 200


def test_health_endpoint_exposes_only_bounded_checks() -> None:
    client, _ = _client()

    response = client.get("/api/v2/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"api": {"ready": True}},
    }


def test_degraded_health_returns_service_unavailable() -> None:
    processor = FakeProcessor()
    service = BalanceChatService(InMemoryContextStore(), processor)
    client = TestClient(
        create_app(
            service,
            health_check=lambda: {
                "status": "degraded",
                "checks": {"context_model": {"ready": False}},
            },
        )
    )

    response = client.get("/api/v2/health")

    assert response.status_code == 503
    assert response.json()["checks"]["context_model"] == {"ready": False}


def test_invalid_interpretation_contract_returns_http_422() -> None:
    service = BalanceChatService(InMemoryContextStore(), InvalidContractProcessor())
    client = TestClient(create_app(service))
    session_id = client.post("/api/v2/chat/sessions").json()["session"]["session_id"]

    response = client.post(
        "/api/v2/chat",
        json={
            "session_id": session_id,
            "expected_revision": 0,
            "message": "суммируй данные по областям",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "interpretation_contract_invalid"
