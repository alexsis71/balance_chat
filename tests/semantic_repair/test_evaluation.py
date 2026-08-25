from __future__ import annotations

from dataclasses import dataclass
import json
from types import SimpleNamespace

from balance_chat.semantic_repair.backend import (
    PipelineSemanticShadowBackend,
    ShadowModelResponse,
)
from balance_chat.semantic_repair.evaluation import (
    _set_thinking_override,
    evaluate_corpus,
    load_corpus,
)
from balance_chat.semantic_repair.shadow import SemanticShadowRunner


class ScriptedBackend:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = 0

    def invoke(self, *_args, **_kwargs):
        self.calls += 1
        return ShadowModelResponse(
            content=json.dumps(next(self.outputs)),
            model="Qwen/Qwen3.8-27B",
        )


class ConstantBackend:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def invoke(self, *_args, **_kwargs):
        self.calls += 1
        return ShadowModelResponse(
            content=json.dumps(self.output),
            model="fake-shadow-model",
        )


@dataclass(frozen=True)
class _InferenceProfileStub:
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 180.0
    connect_retries: int = 1
    max_tokens: int = 8192


class _InferenceClientStub:
    def __init__(self, profiles):
        self.profiles = profiles


def test_shadow_backend_allows_larger_offline_thinking_budget() -> None:
    runtime = SimpleNamespace(
        _import_pipeline_module=lambda name: (
            SimpleNamespace(InferenceClient=_InferenceClientStub)
            if name == "inference.client"
            else SimpleNamespace()
        ),
        pipeline_runtime=lambda: SimpleNamespace(
            inference_client=SimpleNamespace(
                profiles={"context": _InferenceProfileStub()}
            )
        ),
    )

    backend = PipelineSemanticShadowBackend(runtime, max_tokens=2048)

    assert backend.max_tokens == 2048
    assert backend._client.profiles["context"].max_tokens == 2048


def test_thinking_override_is_scoped_to_selected_evaluation_profile() -> None:
    runtime = SimpleNamespace(
        cfg={
            "inference": {
                "profiles": {
                    "context": {"chat_template_kwargs": {"enable_thinking": False}},
                    "language": {"chat_template_kwargs": {"enable_thinking": False}},
                }
            }
        }
    )

    _set_thinking_override(runtime, profile="context", enabled=True)

    profiles = runtime.cfg["inference"]["profiles"]
    assert profiles["context"]["chat_template_kwargs"]["enable_thinking"] is True
    assert profiles["language"]["chat_template_kwargs"]["enable_thinking"] is False


def _payload(action, mutations=None, references=None, question=None):
    return {
        "action": action,
        "mutations": mutations or [],
        "references": references or [],
        "unresolved_mentions": [],
        "clarification_question": question,
        "clarification_reason": None,
        "confidence": 0.9,
        "reason_code": "test",
    }


def test_evaluator_reports_quality_safety_latency_and_control_skip() -> None:
    context = {
        "current_active_state": {"operation": "show", "operands": []},
        "recent_semantic_turns": [],
        "recent_addressable_results": [],
        "current_user_message": "А теперь наоборот",
    }
    corpus = {
        "version": "test",
        "contexts": {"flow": context, "geo": {**context, "current_user_message": "А по Москве?"}},
        "cases": [
            {
                "id": "reverse",
                "category": "direction",
                "context": "flow",
                "current_production_outcome": {"semantic_match": False},
                "expected": {
                    "action": "patch",
                    "mutations": [{"kind": "swap_direction", "value": None}],
                    "references": [],
                    "shadow_invoked": True,
                },
                "repeat": 2,
            },
            {
                "id": "geo-control",
                "category": "control",
                "context": "geo",
                "interpretation_mode": "deterministic_geo_patch",
                "current_production_outcome": {"semantic_match": True},
                "expected": {"shadow_invoked": False},
            },
        ],
    }
    backend = ScriptedBackend(
        [
            _payload("patch", [{"kind": "swap_direction", "value": None}]),
            _payload("patch", [{"kind": "swap_direction", "value": None}]),
        ]
    )

    report = evaluate_corpus(
        SemanticShadowRunner(backend, enabled=True), corpus
    )

    assert backend.calls == 2
    assert report["served_models"] == ["Qwen/Qwen3.8-27B"]
    assert report["metrics"]["valid_typed_output_rate"] == 1.0
    assert report["metrics"]["semantic_proposal_precision"] == 1.0
    assert report["metrics"]["unsafe_transition_rate"] == 0.0
    assert report["metrics"]["stability_rate"] == 1.0
    assert report["metrics"]["deterministic_controls_skipped"] == 1
    assert report["metrics"]["tools_exposed"] is False
    assert report["metrics"]["tool_call_count"] is None
    assert report["metrics"]["repeated_tool_calls"] is None
    assert report["metrics"]["tool_errors"] is None
    assert report["cases"][0]["runs"][0]["classification"] == "SHADOW_IMPROVEMENT"
    assert report["cases"][1]["runs"][0]["classification"] == "SHADOW_NEUTRAL"


def test_v2_corpus_is_versioned_and_all_model_cases_are_repeatable() -> None:
    corpus = load_corpus("tests/semantic_repair/corpus_v2.json")
    backend = ConstantBackend(_payload("unsupported"))

    report = evaluate_corpus(
        SemanticShadowRunner(backend, enabled=True),
        corpus,
        repeat_override=3,
    )

    assert len(corpus["cases"]) == 60
    assert backend.calls == 180
    assert report["schema_version"] == "2.0"
    assert report["metrics"]["shadow_invocations"] == 180
    assert report["swap_direction_metrics"]["positive_unique_cases"] == 16
    assert all(len(case["runs"]) == 3 for case in report["cases"])


def test_committed_corpus_fake_backend_schema_and_eligibility_counts() -> None:
    backend = ConstantBackend(_payload("unsupported"))

    report = evaluate_corpus(
        SemanticShadowRunner(backend, enabled=True),
        load_corpus("tests/semantic_repair/corpus.json"),
    )

    assert backend.calls == 26
    assert report["metrics"]["eligible_turns"] == 26
    assert report["metrics"]["shadow_invocations"] == 26
    assert report["metrics"]["deterministic_controls"] == 3
    assert report["metrics"]["deterministic_controls_skipped"] == 3
    assert report["metrics"]["tools_exposed"] is False
    assert report["metrics"]["tool_call_count"] is None
