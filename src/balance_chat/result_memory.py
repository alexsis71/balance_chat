from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Mapping, Protocol, Sequence

from pydantic import Field

from .contracts import (
    AnalysisIntent,
    ContractModel,
    MetadataVersionRef,
    PeriodRef,
)
from .execution import NativeExecutionResult


class ResultMemoryError(RuntimeError):
    pass


class ResultMemoryStore(Protocol):
    def persist_result(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def retrieve(self, **kwargs: Any) -> list[dict[str, Any]]: ...

    def diagnostics(self) -> dict[str, Any]: ...


class RetrievedMemoryChunk(ContractModel):
    result_ref: str
    turn_ref: str
    chunk_kind: str
    content: str
    similarity: float
    metric: str | None = None
    periods: list[dict[str, str]] = Field(default_factory=list)


@dataclass(frozen=True)
class _Turn:
    turn_id: str
    message: str


@dataclass(frozen=True)
class _ResultRef:
    result_id: str
    turn_id: str
    status: str
    operation: str | None
    unit: str | None
    facts: list[dict[str, Any]]
    source_refs: list[Any]
    resolved_plan_hash: str
    metadata_bundle_version: str
    metadata_schema_version: str


class PipelineResultMemoryAdapter:
    """Translate V2 facts to the already deployed pgvector result store."""

    def __init__(
        self,
        store: ResultMemoryStore,
        *,
        max_chunks: int = 5,
        max_chunk_chars: int = 4000,
        max_total_chars: int = 12000,
        ttl_s: int = 3600,
    ) -> None:
        self.store = store
        self.max_chunks = max(1, min(int(max_chunks), 8))
        self.max_chunk_chars = max(256, int(max_chunk_chars))
        self.max_total_chars = max(self.max_chunk_chars, int(max_total_chars))
        self.ttl_s = max(1, int(ttl_s))

    def persist(
        self,
        *,
        session_id: str,
        revision: int,
        turn_id: str,
        query: str,
        intent: AnalysisIntent,
        result: NativeExecutionResult,
        metadata: MetadataVersionRef,
        result_id: str,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        if result.status != "ok":
            raise ResultMemoryError("only successful authoritative results may be persisted")
        facts = [
            task.fact.model_dump(mode="json")
            for task in result.task_results
            if task.fact is not None
        ]
        if not facts:
            raise ResultMemoryError("successful result has no deterministic facts")
        plan_hash = _sha256(
            {
                "intent": intent.model_dump(mode="json"),
                "facts": facts,
                "metadata_bundle_id": metadata.bundle_id,
            }
        )
        envelope = {
            "status": "ok",
            "question": query,
            "rows": facts,
            "summary": _deterministic_summary(result),
            "debug": {"resolved_plan": _resolved_plan(intent)},
        }
        reference = _ResultRef(
            result_id=result_id,
            turn_id=turn_id,
            status="ok",
            operation=intent.operation.value,
            unit=facts[0].get("unit"),
            facts=facts,
            source_refs=[],
            resolved_plan_hash=plan_hash,
            metadata_bundle_version=metadata.bundle_version,
            metadata_schema_version=metadata.schema_version,
        )
        try:
            return dict(
                self.store.persist_result(
                    session_id=session_id,
                    session_revision=revision,
                    expires_at=expires_at
                    or datetime.now(timezone.utc) + timedelta(seconds=self.ttl_s),
                    turn=_Turn(turn_id=turn_id, message=query),
                    envelope=envelope,
                    result_ref=reference,
                    metadata=metadata,
                )
            )
        except Exception as exc:
            raise ResultMemoryError("could not persist V2 result memory") from exc

    def retrieve(
        self,
        *,
        session_id: str,
        query: str,
        metadata_bundle_version: str,
        intent: AnalysisIntent | None = None,
    ) -> list[RetrievedMemoryChunk]:
        metric = _single_metric(intent)
        periods = _period_dicts(intent.periods if intent else [])
        try:
            raw = self.store.retrieve(
                session_id=session_id,
                query=query,
                metadata_bundle_version=metadata_bundle_version,
                metric=metric,
                periods=periods,
                limit=self.max_chunks,
            )
        except Exception as exc:
            raise ResultMemoryError("could not retrieve V2 result memory") from exc
        bounded: list[RetrievedMemoryChunk] = []
        total = 0
        for item in raw[: self.max_chunks]:
            content = str(item.get("content") or "")[: self.max_chunk_chars]
            remaining = self.max_total_chars - total
            if remaining <= 0:
                break
            content = content[:remaining]
            if not content:
                continue
            bounded.append(
                RetrievedMemoryChunk.model_validate({**item, "content": content})
            )
            total += len(content)
        return bounded

    @staticmethod
    def safe_diagnostics(chunks: Sequence[RetrievedMemoryChunk]) -> dict[str, Any]:
        return {
            "retrieved_chunks": len(chunks),
            "result_refs": list(dict.fromkeys(item.result_ref for item in chunks)),
        }

    @staticmethod
    def for_interpretation(
        chunks: Sequence[RetrievedMemoryChunk],
    ) -> list[dict[str, Any]]:
        """Internal-only payload; callers must expose only safe_diagnostics."""
        return [
            {
                "result_ref": item.result_ref,
                "turn_ref": item.turn_ref,
                "chunk_kind": item.chunk_kind,
                "content": item.content,
                "metric": item.metric,
                "periods": item.periods,
            }
            for item in chunks
        ]


def _resolved_plan(intent: AnalysisIntent) -> dict[str, Any]:
    expressions = []
    for operand in intent.operands:
        expression: dict[str, Any] = {"canonical_metric": operand.metric}
        for reference in operand.entities:
            expression[reference.role] = {
                "id": reference.entity.entity_id,
                "label": reference.entity.display_name,
            }
        expressions.append(expression)
    return {
        "operation": intent.operation.value,
        "periods": [
            [period.date_from.isoformat(), period.date_to.isoformat()]
            for period in intent.periods
        ],
        "expressions": expressions,
    }


def _deterministic_summary(result: NativeExecutionResult) -> dict[str, str]:
    if result.comparison is None:
        return {"title": "Сохранённый результат", "text": " ".join(
            f"{item.fact.label}: {item.fact.value} {item.fact.unit}"
            for item in result.task_results if item.fact is not None
        )}
    comparison = result.comparison
    return {
        "title": "Сохранённое сравнение",
        "text": (
            f"baseline={comparison.baseline_value} {comparison.unit}; "
            f"target={comparison.target_value} {comparison.unit}; "
            f"delta={comparison.delta} {comparison.unit}"
        ),
    }


def _single_metric(intent: AnalysisIntent | None) -> str | None:
    if intent is None:
        return None
    metrics = {operand.metric for operand in intent.operands}
    return next(iter(metrics)) if len(metrics) == 1 else None


def _period_dicts(periods: Sequence[PeriodRef]) -> list[dict[str, str]]:
    return [
        {"date_from": item.date_from.isoformat(), "date_to": item.date_to.isoformat()}
        for item in periods
    ]


def _sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()
