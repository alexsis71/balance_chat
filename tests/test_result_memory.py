from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    MetadataVersionRef,
    Operation,
    PeriodRef,
)
from balance_chat.execution import (
    NativeExecutionResult,
    ScalarFact,
    TaskExecutionResult,
)
from balance_chat.result_memory import PipelineResultMemoryAdapter, ResultMemoryError
from balance_chat.compat import PipelineRuntime, RuntimeConfig


class FakeMemoryStore:
    def __init__(self) -> None:
        self.persisted = None
        self.retrieval_args = None

    def persist_result(self, **kwargs):
        self.persisted = kwargs
        return {"persisted": True, "chunks": 1}

    def retrieve(self, **kwargs):
        self.retrieval_args = kwargs
        return [
            {
                "result_ref": "result-1",
                "turn_ref": "turn-1",
                "chunk_kind": "summary",
                "content": "x" * 5000,
                "similarity": 0.9,
                "metric": "distribution",
                "periods": [],
            },
            {
                "result_ref": "result-2",
                "turn_ref": "turn-2",
                "chunk_kind": "rows",
                "content": "y" * 5000,
                "similarity": 0.8,
                "metric": "distribution",
                "periods": [],
            },
        ]

    def diagnostics(self):
        return {}


def _intent() -> AnalysisIntent:
    return AnalysisIntent(
        operation=Operation.AGGREGATE,
        operands=[AnalysisOperand(operand_id="supply", metric="distribution")],
        periods=[PeriodRef(date_from="2025-05-01", date_to="2025-06-01")],
    )


def _result(status: str = "ok") -> NativeExecutionResult:
    return NativeExecutionResult(
        operation=Operation.AGGREGATE,
        status=status,
        task_results=[
            TaskExecutionResult(
                task_id="task_1",
                status=status,
                fact=(
                    ScalarFact(
                        task_id="task_1",
                        value=Decimal("42.5"),
                        unit="тыс. м3",
                        label="Поставки",
                        provenance=[{"row": 1}],
                    )
                    if status == "ok"
                    else None
                ),
                envelope={"status": status},
            )
        ],
    )


def _metadata() -> MetadataVersionRef:
    return MetadataVersionRef(
        bundle_id="sha256:" + "a" * 64,
        bundle_version="2026.07.6",
        schema_version="1.0",
    )


def test_persist_translates_only_deterministic_facts() -> None:
    store = FakeMemoryStore()
    adapter = PipelineResultMemoryAdapter(store)
    persisted = adapter.persist(
        session_id="session-1",
        revision=3,
        turn_id="turn-1",
        query="Покажи поставки",
        intent=_intent(),
        result=_result(),
        metadata=_metadata(),
        result_id="result-1",
    )

    assert persisted["persisted"]
    envelope = store.persisted["envelope"]
    assert envelope["rows"][0]["value"] == "42.5"
    assert store.persisted["session_revision"] == 3
    assert store.persisted["result_ref"].resolved_plan_hash.startswith("sha256:")

    root = Path(__file__).resolve().parents[2] / "pipeline"
    runtime = PipelineRuntime(
        RuntimeConfig(
            pipeline_root=root,
            metadata_manifest=root / "data" / "metadata" / "manifest.json",
        )
    )
    chunker_type = runtime._import_pipeline_module(
        "chat_context.rag_store"
    ).DeterministicResultChunker
    artifact = chunker_type().build(**store.persisted)
    assert artifact.metric == "distribution"
    assert artifact.periods == [
        {"date_from": "2025-05-01", "date_to": "2025-06-01"}
    ]
    assert artifact.chunks


def test_unsuccessful_result_is_not_written_as_memory() -> None:
    with pytest.raises(ResultMemoryError, match="only successful"):
        PipelineResultMemoryAdapter(FakeMemoryStore()).persist(
            session_id="session-1",
            revision=3,
            turn_id="turn-1",
            query="query",
            intent=_intent(),
            result=_result("no_data"),
            metadata=_metadata(),
            result_id="result-1",
        )


def test_retrieval_is_bounded_and_filtered_by_canonical_scope() -> None:
    store = FakeMemoryStore()
    adapter = PipelineResultMemoryAdapter(
        store,
        max_chunks=2,
        max_chunk_chars=3000,
        max_total_chars=4500,
    )
    chunks = adapter.retrieve(
        session_id="session-1",
        query="сравни с предыдущим",
        metadata_bundle_version="2026.07.6",
        intent=_intent(),
    )

    assert [len(item.content) for item in chunks] == [3000, 1500]
    assert store.retrieval_args["metric"] == "distribution"
    assert store.retrieval_args["periods"] == [
        {"date_from": "2025-05-01", "date_to": "2025-06-01"}
    ]
    assert adapter.safe_diagnostics(chunks) == {
        "retrieved_chunks": 2,
        "result_refs": ["result-1", "result-2"],
    }
    internal = adapter.for_interpretation(chunks)
    assert internal[0]["content"] == "x" * 3000
    assert "similarity" not in internal[0]
