from __future__ import annotations

import json

import pytest

from balance_chat.semantic_repair.backend import (
    SemanticShadowTimeout,
    SemanticShadowUnavailable,
    ShadowModelResponse,
)
from balance_chat.semantic_repair.contracts import ShadowValidationStatus
from balance_chat.semantic_repair.shadow import SemanticShadowRunner


class Backend:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.calls = []

    def invoke(self, messages, schema, *, request_id):
        self.calls.append((messages, schema, request_id))
        if self.error is not None:
            raise self.error
        return ShadowModelResponse(
            content=self.content,
            model="Qwen/Qwen3.8-27B",
            latency_ms=2,
        )


def _valid_payload():
    return {
        "action": "patch",
        "mutations": [{"kind": "swap_direction", "value": None}],
        "references": [],
        "unresolved_mentions": [],
        "clarification_question": None,
        "confidence": 0.95,
        "reason_code": "reverse_requested",
    }


def test_shadow_disabled_makes_zero_calls(active_state) -> None:
    backend = Backend(json.dumps(_valid_payload()))
    result = SemanticShadowRunner(backend, enabled=False).run(
        state=active_state,
        message="А теперь наоборот",
        interpretation_mode="conversation_graph",
        request_id="request-1",
    )

    assert result.eligible is True
    assert result.invoked is False
    assert result.validation_status == ShadowValidationStatus.SKIPPED
    assert backend.calls == []


@pytest.mark.parametrize(
    "mode",
    [
        "deterministic_period_patch",
        "deterministic_geo_patch",
        "deterministic_business_entity_patch",
    ],
)
def test_deterministic_patch_is_never_shadowed(active_state, mode) -> None:
    backend = Backend(json.dumps(_valid_payload()))
    result = SemanticShadowRunner(backend, enabled=True).run(
        state=active_state,
        message="deterministic follow-up",
        interpretation_mode=mode,
        request_id="request-1",
    )

    assert result.eligible is False
    assert result.invoked is False
    assert backend.calls == []


@pytest.mark.parametrize(
    "mode",
    [None, "", "totally_unknown", "deterministic_future_patch"],
)
def test_unknown_or_missing_mode_fails_closed(active_state, mode) -> None:
    backend = Backend(json.dumps(_valid_payload()))
    result = SemanticShadowRunner(backend, enabled=True).run(
        state=active_state,
        message="ambiguous follow-up",
        interpretation_mode=mode,
        request_id="request-1",
    )

    assert result.eligible is False
    assert result.invoked is False
    assert result.validation_status == ShadowValidationStatus.SKIPPED
    assert backend.calls == []


def test_known_hard_tail_mode_remains_eligible(active_state) -> None:
    backend = Backend(json.dumps(_valid_payload()))
    result = SemanticShadowRunner(backend, enabled=True).run(
        state=active_state,
        message="ambiguous follow-up",
        interpretation_mode="conversation_graph",
        request_id="request-1",
    )

    assert result.eligible is True
    assert result.invoked is True
    assert len(backend.calls) == 1


def test_valid_shadow_proposal_is_recorded_only_as_result(active_state) -> None:
    backend = Backend(json.dumps(_valid_payload()))
    before = active_state.model_dump(mode="json")
    result = SemanticShadowRunner(backend, enabled=True).run(
        state=active_state,
        message="А теперь наоборот",
        interpretation_mode="conversation_graph",
        request_id="request-1",
    )

    assert result.validation_status == ShadowValidationStatus.VALID
    assert result.proposal.mutations[0].kind.value == "swap_direction"
    assert result.model == "Qwen/Qwen3.8-27B"
    assert active_state.model_dump(mode="json") == before
    assert len(backend.calls) == 1


@pytest.mark.parametrize(
    ("content", "status"),
    [
        ("not-json", ShadowValidationStatus.MALFORMED),
        (
            json.dumps(
                {
                    **_valid_payload(),
                    "mutations": [{"kind": "set_geo", "value": "Москва"}],
                }
            ),
            ShadowValidationStatus.REJECTED,
        ),
    ],
)
def test_malformed_and_invalid_output_are_non_blocking(active_state, content, status) -> None:
    result = SemanticShadowRunner(Backend(content), enabled=True).run(
        state=active_state,
        message="Сравни их",
        interpretation_mode="conversation_graph",
        request_id="request-1",
    )

    assert result.validation_status == status
    assert result.invoked is True


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (SemanticShadowTimeout("slow"), ShadowValidationStatus.TIMEOUT),
        (SemanticShadowUnavailable("down"), ShadowValidationStatus.UNAVAILABLE),
        (RuntimeError("unexpected"), ShadowValidationStatus.UNAVAILABLE),
    ],
)
def test_backend_failures_are_contained(active_state, error, status) -> None:
    result = SemanticShadowRunner(Backend(error=error), enabled=True).run(
        state=active_state,
        message="Сравни их",
        interpretation_mode="conversation_graph",
        request_id="request-1",
    )

    assert result.validation_status == status
    assert result.invoked is True
