from __future__ import annotations

from collections import Counter
import argparse
import json
import math
import os
from pathlib import Path
from statistics import mean, median
from typing import Any, Mapping

from ..compat import PipelineRuntime, RuntimeConfig
from ..contracts import (
    AnalysisIntent,
    AnalysisOperand,
    ContextContractV2,
    ContextScope,
    Operation,
)
from .backend import PipelineSemanticShadowBackend
from .context import SemanticShadowContext
from .contracts import SemanticShadowResult, ShadowValidationStatus
from .shadow import SemanticShadowRunner


def load_corpus(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("semantic shadow corpus must contain cases")
    if not isinstance(payload.get("contexts"), dict):
        raise ValueError("semantic shadow corpus must contain contexts")
    return payload


def evaluate_corpus(
    runner: SemanticShadowRunner,
    corpus: Mapping[str, Any],
    *,
    repeat_override: int | None = None,
    endpoint_health: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    contexts = corpus.get("contexts") or {}
    cases: list[dict[str, Any]] = []
    counters: Counter[str] = Counter()
    latencies: list[int] = []
    context_sizes: list[int] = []
    models: set[str] = set()
    for raw_case in corpus.get("cases") or []:
        case_id = str(raw_case["id"])
        context_payload = dict(contexts[raw_case["context"]])
        if raw_case.get("message_override"):
            context_payload["current_user_message"] = raw_case["message_override"]
        context = SemanticShadowContext.model_validate(context_payload)
        expected = dict(raw_case["expected"])
        repeats = max(1, int(repeat_override or raw_case.get("repeat") or 1))
        runs: list[dict[str, Any]] = []
        signatures: list[dict[str, Any] | None] = []
        for repeat_index in range(repeats):
            mode = str(raw_case.get("interpretation_mode") or "conversation_graph")
            if bool(expected.get("shadow_invoked", True)):
                result = runner.run_context(
                    context,
                    request_id=f"pr5-eval:{case_id}:{repeat_index + 1}",
                )
            else:
                result = runner.run(
                    state=_control_state(case_id),
                    message=context.current_user_message,
                    interpretation_mode=mode,
                    request_id=f"pr5-eval:{case_id}:{repeat_index + 1}",
                )
            signature = _proposal_signature(result)
            semantic_match = _semantic_match(expected, signature)
            exact_match = _exact_match(expected, result)
            unsafe_patch = bool(
                result.validation_status == ShadowValidationStatus.VALID
                and result.proposal is not None
                and result.proposal.action.value == "patch"
                and not semantic_match
            )
            classification = _classification(
                expected=expected,
                result=result,
                semantic_match=semantic_match,
                production=raw_case.get("current_production_outcome") or {},
            )
            counters["runs"] += 1
            counters["eligible_turns"] += int(result.eligible)
            counters["shadow_invocations"] += int(result.invoked)
            counters[f"status_{result.validation_status.value}"] += 1
            counters["proposal_exact_match"] += int(exact_match)
            counters["proposal_semantic_match"] += int(semantic_match)
            counters["unsafe_patch"] += int(unsafe_patch)
            if result.validation_status == ShadowValidationStatus.VALID:
                counters["valid_proposals"] += 1
                counters["valid_semantic_match"] += int(semantic_match)
                if result.proposal is not None:
                    counters[f"actual_action_{result.proposal.action.value}"] += 1
            if expected.get("action") == "patch":
                counters["expected_patch"] += 1
                counters["correct_patch"] += int(semantic_match)
                counters["wrong_patch"] += int(
                    result.validation_status == ShadowValidationStatus.VALID
                    and not semantic_match
                )
            if expected.get("action") == "clarify":
                counters["expected_clarify"] += 1
                counters["correct_clarify"] += int(semantic_match)
                counters["wrong_clarify"] += int(not semantic_match)
            if expected.get("action") == "unsupported":
                counters["expected_unsupported"] += 1
                counters["unsupported_correct"] += int(semantic_match)
            if result.latency_ms is not None and result.invoked:
                latencies.append(result.latency_ms)
            if result.invoked:
                context_sizes.append(result.context_size_chars)
            if result.model:
                models.add(result.model)
            signatures.append(signature)
            runs.append(
                {
                    "repeat": repeat_index + 1,
                    "shadow": result.model_dump(mode="json"),
                    "proposal_signature": signature,
                    "exact_match": exact_match,
                    "semantic_match": semantic_match,
                    "unsafe_patch": unsafe_patch,
                    "classification": classification,
                }
            )
        stable = all(item == signatures[0] for item in signatures[1:])
        if repeats > 1:
            counters["repeated_cases"] += 1
            counters["stable_repeated_cases"] += int(stable)
        cases.append(
            {
                "id": case_id,
                "category": raw_case.get("category"),
                "message": context.current_user_message,
                "active_state_summary": context.current_active_state,
                "current_production_outcome": raw_case.get("current_production_outcome"),
                "expected_proposal": expected,
                "runs": runs,
                "stable": stable,
            }
        )
    metrics = _metrics(counters, latencies, context_sizes)
    return {
        "schema_version": "1.0",
        "corpus_version": corpus.get("version"),
        "endpoint_health": dict(endpoint_health or {}),
        "served_models": sorted(models),
        "metrics": metrics,
        "cases": cases,
    }


def _proposal_signature(result: SemanticShadowResult) -> dict[str, Any] | None:
    proposal = result.proposal
    if result.validation_status != ShadowValidationStatus.VALID or proposal is None:
        return None
    return {
        "action": proposal.action.value,
        "mutations": sorted(
            (
                {"kind": item.kind.value, "value": item.value}
                for item in proposal.mutations
            ),
            key=lambda item: (item["kind"], str(item["value"])),
        ),
        "references": sorted(
            (
                {
                    "kind": item.kind.value,
                    "selector": item.selector.value if item.selector else None,
                }
                for item in proposal.references
            ),
            key=lambda item: (item["kind"], str(item["selector"])),
        ),
        "unresolved_mentions": sorted(proposal.unresolved_mentions),
    }


def _semantic_match(expected: Mapping[str, Any], actual: Mapping[str, Any] | None) -> bool:
    if not bool(expected.get("shadow_invoked", True)):
        return actual is None
    if actual is None or actual.get("action") != expected.get("action"):
        return False
    if expected.get("action") in {"clarify", "unsupported"}:
        return True
    expected_mutations = sorted(
        (
            {"kind": item["kind"], "value": item.get("value")}
            for item in expected.get("mutations") or []
        ),
        key=lambda item: (item["kind"], str(item["value"])),
    )
    expected_references = sorted(
        (
            {"kind": item["kind"], "selector": item.get("selector")}
            for item in expected.get("references") or []
        ),
        key=lambda item: (item["kind"], str(item["selector"])),
    )
    return (
        actual.get("mutations") == expected_mutations
        and actual.get("references") == expected_references
    )


def _exact_match(expected: Mapping[str, Any], result: SemanticShadowResult) -> bool:
    signature = _proposal_signature(result)
    if not _semantic_match(expected, signature):
        return False
    if signature is None:
        return not bool(expected.get("shadow_invoked", True))
    expected_mentions = sorted(expected.get("unresolved_mentions") or [])
    return signature.get("unresolved_mentions") == expected_mentions


def _classification(
    *,
    expected: Mapping[str, Any],
    result: SemanticShadowResult,
    semantic_match: bool,
    production: Mapping[str, Any],
) -> str:
    if not result.invoked:
        return "SHADOW_NEUTRAL"
    if semantic_match:
        return (
            "SHADOW_IMPROVEMENT"
            if production.get("semantic_match") is False
            else "SHADOW_NEUTRAL"
        )
    if (
        result.validation_status == ShadowValidationStatus.VALID
        and result.proposal is not None
        and result.proposal.action.value == "patch"
    ):
        return "SHADOW_REGRESSION_IF_APPLIED"
    return "SHADOW_ABSTAINED"


def _metrics(
    counters: Counter[str],
    latencies: list[int],
    context_sizes: list[int],
) -> dict[str, Any]:
    invocations = counters["shadow_invocations"]
    valid = counters["valid_proposals"]
    eligible_hard_tail = counters["runs"] - (
        counters["runs"] - counters["eligible_turns"]
    )
    return {
        "eligible_turns": counters["eligible_turns"],
        "shadow_invocations": invocations,
        "valid_proposals": valid,
        "malformed_proposals": counters["status_malformed"],
        "validator_rejections": counters["status_rejected"],
        "timeouts": counters["status_timeout"],
        "unavailable": counters["status_unavailable"],
        "proposal_exact_match": _ratio(counters["proposal_exact_match"], counters["runs"]),
        "proposal_semantic_match": _ratio(
            counters["proposal_semantic_match"], counters["runs"]
        ),
        "valid_typed_output_rate": _ratio(valid, invocations),
        "semantic_proposal_precision": _ratio(
            counters["valid_semantic_match"], valid
        ),
        "correct_patch": counters["correct_patch"],
        "wrong_patch": counters["wrong_patch"],
        "correct_clarify": counters["correct_clarify"],
        "wrong_clarify": counters["wrong_clarify"],
        "unsupported_correct": counters["unsupported_correct"],
        "unsafe_patch": counters["unsafe_patch"],
        "unsafe_transition_rate": _ratio(counters["unsafe_patch"], valid),
        "correct_clarification_rate": _ratio(
            counters["correct_clarify"], counters["expected_clarify"]
        ),
        "repair_coverage": _ratio(counters["correct_patch"], eligible_hard_tail),
        "avg_latency_ms": round(mean(latencies), 2) if latencies else None,
        "p50_latency_ms": round(median(latencies), 2) if latencies else None,
        "p95_latency_ms": _percentile(latencies, 0.95),
        "avg_context_size_chars": (
            round(mean(context_sizes), 2) if context_sizes else None
        ),
        "repeated_cases": counters["repeated_cases"],
        "stable_repeated_cases": counters["stable_repeated_cases"],
        "stability_rate": _ratio(
            counters["stable_repeated_cases"], counters["repeated_cases"]
        ),
        "tool_call_count": 0,
        "repeated_tool_calls": 0,
        "tool_errors": 0,
    }


def _ratio(value: int, total: int) -> float | None:
    return round(value / total, 4) if total else None


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _control_state(case_id: str) -> ContextContractV2:
    intent = AnalysisIntent(
        intent_id=f"control-{case_id}",
        operation=Operation.SHOW,
        operands=[AnalysisOperand(operand_id="control", metric="distribution")],
    )
    return ContextContractV2(
        session_id=f"control-{case_id}",
        revision=1,
        active_dialog_scope=ContextScope(intent=intent, turn_id="control-turn"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate PR5 semantic shadow proposals")
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--pipeline-root",
        default=os.getenv("BALANCE_CHAT_PIPELINE_ROOT"),
    )
    parser.add_argument(
        "--metadata-manifest",
        default=os.getenv("BALANCE_CHAT_METADATA_MANIFEST"),
    )
    parser.add_argument("--profile", default="context")
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--repeat", type=int)
    args = parser.parse_args(argv)
    if not args.pipeline_root or not args.metadata_manifest:
        parser.error("pipeline root and metadata manifest are required")
    runtime = PipelineRuntime(
        RuntimeConfig(
            Path(args.pipeline_root), Path(args.metadata_manifest)
        ).validated()
    )
    configured_client = runtime.pipeline_runtime().inference_client
    health = configured_client.health(args.profile)
    backend = PipelineSemanticShadowBackend(
        runtime,
        profile=args.profile,
        timeout_s=args.timeout,
        max_tokens=args.max_tokens,
    )
    report = evaluate_corpus(
        SemanticShadowRunner(backend, enabled=True),
        load_corpus(args.corpus),
        repeat_override=args.repeat,
        endpoint_health={
            "profile": health.get("profile"),
            "reachable": health.get("reachable"),
            "structured_outputs": health.get("structured_outputs"),
            "configured_model": health.get("model"),
        },
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print("served_models=" + ",".join(report["served_models"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
