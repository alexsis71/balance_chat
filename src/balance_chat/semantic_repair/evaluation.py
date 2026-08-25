from __future__ import annotations

from collections import Counter
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import mean, median
import subprocess
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
from .contracts import (
    ContextualGateStatus,
    SemanticShadowResult,
    ShadowValidationStatus,
    semantic_transition_proposal_json_schema,
)
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
            raw_signature = _raw_proposal_signature(result)
            signature = _proposal_signature(result)
            gated_signature = (
                signature
                if result.contextual_gate_status
                in {ContextualGateStatus.ACCEPTED, ContextualGateStatus.NOT_EVALUATED}
                else None
            )
            raw_semantic_match = _semantic_match(expected, raw_signature)
            semantic_match = _semantic_match(expected, signature)
            gated_semantic_match = _semantic_match(expected, gated_signature)
            exact_match = _exact_match(expected, result)
            unsafe_patch = _unsafe_patch(raw_signature, raw_semantic_match)
            unsafe_post_gate = _unsafe_patch(gated_signature, gated_semantic_match)
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
            if result.invoked:
                counters["proposal_exact_match"] += int(exact_match)
                counters["proposal_semantic_match"] += int(semantic_match)
            else:
                counters["deterministic_controls"] += 1
                counters["deterministic_controls_skipped"] += int(
                    result.validation_status == ShadowValidationStatus.SKIPPED
                    and not result.eligible
                )
            counters["unsafe_patch"] += int(unsafe_patch)
            counters["unsafe_post_gate"] += int(unsafe_post_gate)
            counters["raw_semantic_match"] += int(raw_semantic_match and result.invoked)
            counters["normalized_semantic_match"] += int(semantic_match and result.invoked)
            counters["gated_safe_match"] += int(gated_semantic_match and result.invoked)
            counters["normalized_outputs"] += int(bool(result.normalization_actions))
            counters["context_gate_accepted"] += int(
                result.contextual_gate_status == ContextualGateStatus.ACCEPTED
            )
            counters["context_gate_rejected"] += int(
                result.contextual_gate_status == ContextualGateStatus.REJECTED
            )
            for reason in result.contextual_gate_reasons:
                counters[f"gate_reason_{reason}"] += 1
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
                    "raw_proposal": result.raw_payload,
                    "raw_schema_validator_status": (
                        result.raw_validation_status.value
                        if result.raw_validation_status else None
                    ),
                    "raw_validation_errors": result.raw_validation_errors,
                    "raw_proposal_signature": raw_signature,
                    "normalized_proposal": (
                        result.normalized_proposal.model_dump(mode="json")
                        if result.normalized_proposal else None
                    ),
                    "normalization_actions": result.normalization_actions,
                    "normalized_proposal_signature": signature,
                    "contextual_gate_status": result.contextual_gate_status.value,
                    "contextual_gate_reasons": result.contextual_gate_reasons,
                    "post_gate_proposal_signature": gated_signature,
                    "expected_proposal": expected,
                    "exact_match": exact_match,
                    "raw_model_semantic_match": raw_semantic_match,
                    "semantic_match": semantic_match,
                    "post_normalization_semantic_match": semantic_match,
                    "post_context_gate_safe_match": gated_semantic_match,
                    "unsafe_patch": unsafe_patch,
                    "unsafe_raw": unsafe_patch,
                    "unsafe_post_gate": unsafe_post_gate,
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
    per_kind = _per_kind_metrics(cases)
    swap = _swap_metrics(cases)
    mode_collapse = _mode_collapse(cases)
    return {
        "schema_version": "2.0",
        "corpus_version": corpus.get("version"),
        "endpoint_health": dict(endpoint_health or {}),
        "served_models": sorted(models),
        "metrics": metrics,
        "per_kind_metrics": per_kind,
        "swap_direction_metrics": swap,
        "mode_collapse": mode_collapse,
        "gate_effectiveness": _gate_effectiveness(cases),
        "latency_by_outcome": _latency_by_outcome(cases),
        "cases": cases,
    }


def _proposal_signature(result: SemanticShadowResult) -> dict[str, Any] | None:
    proposal = result.proposal
    if result.validation_status != ShadowValidationStatus.VALID or proposal is None:
        return None
    return _proposal_model_signature(proposal)


def _proposal_model_signature(proposal: Any) -> dict[str, Any]:
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
        "clarification_reason": (
            proposal.clarification_reason.value
            if proposal.clarification_reason else None
        ),
    }


def _raw_proposal_signature(result: SemanticShadowResult) -> dict[str, Any] | None:
    if (
        result.raw_validation_status != ShadowValidationStatus.VALID
        or result.raw_payload is None
    ):
        return None
    return _payload_signature(result.raw_payload)


def _payload_signature(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "action": payload.get("action"),
        "mutations": sorted(
            (
                {"kind": item.get("kind"), "value": item.get("value")}
                for item in payload.get("mutations") or []
                if isinstance(item, Mapping)
            ),
            key=lambda item: (str(item["kind"]), str(item["value"])),
        ),
        "references": sorted(
            (
                {"kind": item.get("kind"), "selector": item.get("selector")}
                for item in payload.get("references") or []
                if isinstance(item, Mapping)
            ),
            key=lambda item: (str(item["kind"]), str(item["selector"])),
        ),
        "unresolved_mentions": sorted(payload.get("unresolved_mentions") or []),
        "clarification_reason": payload.get("clarification_reason"),
    }


def _semantic_match(expected: Mapping[str, Any], actual: Mapping[str, Any] | None) -> bool:
    if not bool(expected.get("shadow_invoked", True)):
        return actual is None
    if actual is None or actual.get("action") != expected.get("action"):
        return False
    if expected.get("action") == "clarify":
        return (
            "clarification_reason" not in expected
            or actual.get("clarification_reason") == expected.get("clarification_reason")
        )
    if expected.get("action") == "unsupported":
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


def _unsafe_patch(
    signature: Mapping[str, Any] | None,
    semantic_match: bool,
) -> bool:
    return bool(
        signature is not None
        and signature.get("action") == "patch"
        and not semantic_match
    )


def _expected_kind(expected: Mapping[str, Any], kind: str) -> bool:
    if kind in {"clarify", "unsupported"}:
        return expected.get("action") == kind
    return any(item.get("kind") == kind for item in expected.get("mutations") or [])


def _emits_kind(signature: Mapping[str, Any] | None, kind: str) -> bool:
    if signature is None:
        return False
    if kind in {"clarify", "unsupported"}:
        return signature.get("action") == kind
    return any(item.get("kind") == kind for item in signature.get("mutations") or [])


def _per_kind_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = (
        "swap_direction",
        "set_operation",
        "set_comparison",
        "reference_prior_result",
        "clarify",
        "unsupported",
    )
    output: dict[str, Any] = {}
    for kind in kinds:
        expected_cases = emissions = true_positives = false_positives = 0
        false_negatives = validator_rejects = gate_rejects = 0
        stable_expected = 0
        for case in cases:
            expected = case["expected_proposal"]
            case_expected = _expected_kind(expected, kind)
            expected_cases += int(case_expected)
            stable_expected += int(case_expected and case["stable"])
            for run in case["runs"]:
                raw = run["raw_proposal_signature"]
                post_gate = run["post_gate_proposal_signature"]
                emitted = _emits_kind(raw, kind)
                accepted_emission = _emits_kind(post_gate, kind)
                emissions += int(emitted)
                true_positives += int(case_expected and accepted_emission and run["post_context_gate_safe_match"])
                false_positives += int(not case_expected and accepted_emission)
                false_negatives += int(case_expected and not accepted_emission)
                validator_rejects += int(
                    case_expected and run["raw_schema_validator_status"] == "rejected"
                )
                gate_rejects += int(
                    emitted and run["contextual_gate_status"] == "rejected"
                )
        output[kind] = {
            "expected_unique_cases": expected_cases,
            "emissions": emissions,
            "true_positives": true_positives,
            "false_positives": false_positives,
            "false_negatives": false_negatives,
            "validator_rejects": validator_rejects,
            "context_gate_rejects": gate_rejects,
            "precision": _ratio(true_positives, true_positives + false_positives),
            "recall": _ratio(true_positives, true_positives + false_negatives),
            "stability": _ratio(stable_expected, expected_cases),
        }
    return output


def _swap_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    positives = [
        case for case in cases
        if _expected_kind(case["expected_proposal"], "swap_direction")
    ]
    negatives = [case for case in cases if case not in positives]
    positive_runs = [run for case in positives for run in case["runs"]]
    negative_runs = [run for case in negatives for run in case["runs"]]
    correct = sum(
        _emits_kind(run["post_gate_proposal_signature"], "swap_direction")
        and run["post_context_gate_safe_match"]
        for run in positive_runs
    )
    false_positive_raw = sum(
        _emits_kind(run["raw_proposal_signature"], "swap_direction")
        for run in negative_runs
    )
    false_positive_post_gate = sum(
        _emits_kind(run["post_gate_proposal_signature"], "swap_direction")
        for run in negative_runs
    )
    gate_rejects = sum(
        _emits_kind(run["raw_proposal_signature"], "swap_direction")
        and run["contextual_gate_status"] == "rejected"
        for case in cases for run in case["runs"]
    )
    return {
        "positive_unique_cases": len(positives),
        "positive_total_runs": len(positive_runs),
        "correct_swaps": correct,
        "false_negatives": len(positive_runs) - correct,
        "non_direction_unique_cases": len(negatives),
        "non_direction_total_runs": len(negative_runs),
        "false_positive_swap_emissions_raw": false_positive_raw,
        "false_positive_swap_emissions_post_gate": false_positive_post_gate,
        "precision": _ratio(correct, correct + false_positive_post_gate),
        "recall": _ratio(correct, len(positive_runs)),
        "false_positive_rate": _ratio(false_positive_post_gate, len(negative_runs)),
        "g4_rejects": gate_rejects,
        "stability": _ratio(sum(case["stable"] for case in positives), len(positives)),
    }


def _mode_collapse(cases: list[dict[str, Any]]) -> dict[str, Any]:
    signatures: Counter[str] = Counter()
    categories: dict[str, set[str]] = {}
    for case in cases:
        for run in case["runs"]:
            signature = run["raw_proposal_signature"]
            if signature is None or run["raw_model_semantic_match"]:
                continue
            key = json.dumps(signature, ensure_ascii=False, sort_keys=True)
            signatures[key] += 1
            categories.setdefault(key, set()).add(str(case.get("category")))
    if not signatures:
        return {"most_frequent_erroneous_signature": None, "count": 0, "categories": []}
    key, count = signatures.most_common(1)[0]
    return {
        "most_frequent_erroneous_signature": json.loads(key),
        "count": count,
        "categories": sorted(categories[key]),
    }


def _gate_effectiveness(cases: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Counter[str]] = {}
    for case in cases:
        for run in case["runs"]:
            for reason in run["contextual_gate_reasons"]:
                counter = output.setdefault(reason, Counter())
                counter["affected"] += 1
                counter["unsafe_blocked"] += int(run["unsafe_raw"])
                counter["correct_blocked"] += int(run["post_normalization_semantic_match"])
    return {
        reason: {
            "affected": counts["affected"],
            "unsafe_blocked": counts["unsafe_blocked"],
            "correct_outputs_blocked": counts["correct_blocked"],
        }
        for reason, counts in sorted(output.items())
    }


def _latency_by_outcome(cases: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[int]] = {"all": [], "correct": [], "unsafe": []}
    for case in cases:
        for run in case["runs"]:
            latency = run["shadow"].get("latency_ms")
            if latency is None:
                continue
            groups["all"].append(latency)
            if run["raw_model_semantic_match"]:
                groups["correct"].append(latency)
            if run["unsafe_raw"]:
                groups["unsafe"].append(latency)
    return {
        key: {
            "count": len(values),
            "avg_ms": round(mean(values), 2) if values else None,
            "p50_ms": round(median(values), 2) if values else None,
            "p95_ms": _percentile(values, 0.95),
        }
        for key, values in groups.items()
    }


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
    eligible_hard_tail = counters["eligible_turns"]
    return {
        "eligible_turns": counters["eligible_turns"],
        "shadow_invocations": invocations,
        "valid_proposals": valid,
        "malformed_proposals": counters["status_malformed"],
        "validator_rejections": counters["status_rejected"],
        "timeouts": counters["status_timeout"],
        "unavailable": counters["status_unavailable"],
        "proposal_exact_match": _ratio(counters["proposal_exact_match"], invocations),
        "proposal_semantic_match": _ratio(
            counters["proposal_semantic_match"], invocations
        ),
        "raw_model_semantic_match": _ratio(counters["raw_semantic_match"], invocations),
        "post_normalization_semantic_match": _ratio(
            counters["normalized_semantic_match"], invocations
        ),
        "post_context_gate_safe_match": _ratio(
            counters["gated_safe_match"], invocations
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
        "unsafe_raw": counters["unsafe_patch"],
        "unsafe_post_gate": counters["unsafe_post_gate"],
        "normalized_outputs": counters["normalized_outputs"],
        "normalization_failures": 0,
        "context_gate_accepted": counters["context_gate_accepted"],
        "context_gate_rejected": counters["context_gate_rejected"],
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
        "deterministic_controls": counters["deterministic_controls"],
        "deterministic_controls_skipped": counters[
            "deterministic_controls_skipped"
        ],
        "tools_exposed": False,
        "tool_call_count": None,
        "repeated_tool_calls": None,
        "tool_errors": None,
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
    parser.add_argument("--mtp-method", default="mtp")
    parser.add_argument("--speculative-tokens", type=int, default=1)
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
    runner = SemanticShadowRunner(backend, enabled=True)
    report = evaluate_corpus(
        runner,
        load_corpus(args.corpus),
        repeat_override=args.repeat,
        endpoint_health={
            "profile": health.get("profile"),
            "reachable": health.get("reachable"),
            "structured_outputs": health.get("structured_outputs"),
            "configured_model": health.get("model"),
        },
    )
    prompt_bytes = runner.system_prompt.encode("utf-8")
    schema_bytes = json.dumps(
        semantic_transition_proposal_json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    corpus_bytes = Path(args.corpus).read_bytes()
    report["provenance"] = {
        "configured_model": health.get("model"),
        "served_models": report["served_models"],
        "temperature": 0.0,
        "max_tokens": args.max_tokens,
        "mtp_method": args.mtp_method,
        "num_speculative_tokens": args.speculative_tokens,
        "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "contract_schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
        "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
        "branch_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
    }
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
