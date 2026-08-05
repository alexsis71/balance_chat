from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from balance_chat.compat import (
    PipelineRuntime,
    RuntimeConfig,
    UnsupportedLegacyShape,
    project_legacy_context_override,
)
from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    GroupingSpec,
    Operation,
    PeriodRef,
)


def _intent() -> AnalysisIntent:
    return AnalysisIntent(
        operation=Operation.SHOW,
        operands=[AnalysisOperand(operand_id="supply", metric="distribution")],
        periods=[PeriodRef(date_from="2025-05-01", date_to="2025-06-01")],
    )


def test_scalar_intent_projects_to_legacy_override() -> None:
    payload = project_legacy_context_override(_intent())
    assert payload["operation"] == "show"
    assert payload["periods"] == [
        {"date_from": "2025-05-01", "date_to": "2025-06-01"}
    ]
    assert payload["metric"] == "distribution"


def test_umbrella_metric_projects_to_supported_execution_metric() -> None:
    intent = _intent().model_copy(
        update={
            "operands": [
                AnalysisOperand(operand_id="supply", metric="supply")
            ]
        },
        deep=True,
    )

    payload = project_legacy_context_override(intent)

    assert intent.operands[0].metric == "supply"
    assert payload["metric"] == "distribution"


def test_grouping_is_not_silently_discarded() -> None:
    intent = _intent().model_copy(
        update={"grouping": [GroupingSpec(dimension="geo_group")]}
    )
    with pytest.raises(UnsupportedLegacyShape, match="grouping"):
        project_legacy_context_override(intent)


def test_multi_operand_is_not_silently_reduced() -> None:
    intent = AnalysisIntent(
        operation=Operation.COMPARE,
        operands=[
            AnalysisOperand(operand_id="supply", metric="distribution"),
            AnalysisOperand(operand_id="needs", metric="own_needs"),
        ],
    )
    with pytest.raises(UnsupportedLegacyShape, match="operation|operand"):
        project_legacy_context_override(intent)


def test_runtime_loads_existing_strict_metadata_bundle() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    pipeline_root = repo_root / "pipeline"
    config = RuntimeConfig(
        pipeline_root=pipeline_root,
        metadata_manifest=pipeline_root / "data" / "metadata" / "manifest.json",
    )
    registry = PipelineRuntime(config).load_metadata_registry()
    assert registry.manifest.state == "ready"


def test_runtime_calls_unified_strict_without_fallback() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    pipeline_root = repo_root / "pipeline"
    runtime = PipelineRuntime(
        RuntimeConfig(
            pipeline_root=pipeline_root,
            metadata_manifest=pipeline_root / "data" / "metadata" / "manifest.json",
        )
    )
    captured: dict = {}

    def execute_query(query: str, **kwargs: object) -> dict:
        captured.update(query=query, **kwargs)
        return {"status": "ok"}

    runtime._import_pipeline_module = lambda _: SimpleNamespace(execute_query=execute_query)
    result = runtime.execute("Покажи поставки", _intent(), execute_db=False)

    assert result == {"status": "ok"}
    assert captured["backend_override"] == "unified_strict"
    assert captured["allow_multi_step"] is False
    assert captured["context_override"]["periods"][0]["date_to"] == "2025-06-01"


def test_raw_runtime_keeps_summary_and_followups_disabled() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    pipeline_root = repo_root / "pipeline"
    runtime = PipelineRuntime(
        RuntimeConfig(
            pipeline_root=pipeline_root,
            metadata_manifest=pipeline_root / "data" / "metadata" / "manifest.json",
        )
    )
    captured: dict = {}

    def execute_query(query: str, **kwargs: object) -> dict:
        captured.update(query=query, **kwargs)
        return {"status": "ok"}

    runtime._import_pipeline_module = lambda _: SimpleNamespace(execute_query=execute_query)
    runtime.execute_raw("Покажи поставки", execute_db=True)

    assert captured["apply_summary"] is False
    assert captured["allow_multi_step"] is False


def test_summary_bridge_does_not_repeat_pipeline_execution() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    pipeline_root = repo_root / "pipeline"
    runtime = PipelineRuntime(
        RuntimeConfig(
            pipeline_root=pipeline_root,
            metadata_manifest=pipeline_root / "data" / "metadata" / "manifest.json",
        )
    )
    calls: list[str] = []

    def apply_summary(envelope: dict, request_id: str) -> dict:
        calls.append(request_id)
        return {**envelope, "summary": {"generated_by": "llm", "text": "Вывод"}}

    runtime._import_pipeline_module = lambda _: SimpleNamespace(
        _apply_configured_summary=apply_summary,
        execute_query=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("summary must not execute the query again")
        ),
    )
    result = runtime.summarize_envelope(
        {"status": "ok", "rows": [{"fact_value": 1}]}, request_id="summary"
    )

    assert calls == ["summary"]
    assert result["summary"]["generated_by"] == "llm"
