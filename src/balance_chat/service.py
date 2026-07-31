from __future__ import annotations

from collections import defaultdict
from threading import RLock
from typing import Any, Callable, Protocol
from uuid import uuid4

from pydantic import Field

from .contracts import (
    ContextContractV2,
    ContextMutation,
    ContractModel,
    ResultReference,
    TransitionOutcome,
)
from .store import RevisionConflict


class TurnProcessingError(RuntimeError):
    def __init__(self, message: str, *, code: str = "turn_processing_error") -> None:
        super().__init__(message)
        self.code = code


class TurnProcessResult(ContractModel):
    mutation: ContextMutation
    outcome: TransitionOutcome
    response: dict[str, Any] = Field(default_factory=dict)
    result_reference: ResultReference | None = None
    clarification_questions: list[dict[str, Any]] | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class TurnProcessor(Protocol):
    def process(
        self,
        state: ContextContractV2,
        *,
        message: str,
        execute_db: bool,
        clarification: dict[str, Any] | None,
        request_id: str,
    ) -> TurnProcessResult: ...


class BalanceChatService:
    """Revision-aware application boundary; processing remains dependency-injected."""

    def __init__(
        self,
        store: Any,
        processor: TurnProcessor,
        *,
        metadata=None,
        delete_result_memory: Callable[[str], int] | None = None,
    ) -> None:
        self.store = store
        self.processor = processor
        self.metadata = metadata
        self.delete_result_memory = delete_result_memory
        self._locks: defaultdict[str, RLock] = defaultdict(RLock)
        self._locks_guard = RLock()

    def create_session(self) -> ContextContractV2:
        return self.store.create(str(uuid4()), self.metadata)

    def get_session(self, session_id: str) -> ContextContractV2:
        return self.store.get(session_id)

    def delete_session(self, session_id: str) -> dict[str, Any]:
        with self._session_lock(session_id):
            deleted = bool(self.store.delete(session_id))
            memories = (
                int(self.delete_result_memory(session_id))
                if deleted and self.delete_result_memory is not None
                else 0
            )
        return {
            "session_id": session_id,
            "deleted": deleted,
            "result_memories_deleted": memories,
        }

    def execute_turn(
        self,
        session_id: str,
        *,
        expected_revision: int,
        message: str,
        execute_db: bool = False,
        clarification: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        with self._session_lock(session_id):
            state = self.store.get(session_id)
            if state.revision != expected_revision:
                raise RevisionConflict(session_id, expected_revision, state.revision)
            trace_id = request_id or str(uuid4())
            processed = self.processor.process(
                state,
                message=message,
                execute_db=execute_db,
                clarification=clarification,
                request_id=trace_id,
            )
            committed = self.store.commit(
                session_id,
                expected_revision,
                processed.mutation,
                processed.outcome,
                result=processed.result_reference,
                clarification_questions=processed.clarification_questions,
            )
            return {
                "status": _public_status(processed.outcome),
                "session": _session_view(committed),
                "context": _context_view(committed),
                "result": processed.response,
                "diagnostics": _safe_diagnostics(processed.diagnostics),
                "request_id": trace_id,
            }

    def _session_lock(self, session_id: str) -> RLock:
        with self._locks_guard:
            return self._locks[session_id]


def _public_status(outcome: TransitionOutcome) -> str:
    return {
        TransitionOutcome.SUCCESS: "ok",
        TransitionOutcome.NO_DATA: "no_data",
        TransitionOutcome.ERROR: "error",
        TransitionOutcome.CLARIFICATION: "needs_clarification",
    }[outcome]


def _session_view(state: ContextContractV2) -> dict[str, Any]:
    return {
        "session_id": state.session_id,
        "revision": state.revision,
        "contract_version": state.contract_version,
        "created_at": state.created_at.isoformat(),
        "updated_at": state.updated_at.isoformat(),
    }


def _context_view(state: ContextContractV2) -> dict[str, Any]:
    return {
        "active": (
            state.active_dialog_scope.model_dump(mode="json")
            if state.active_dialog_scope
            else None
        ),
        "last_successful": (
            state.last_successful_scope.model_dump(mode="json")
            if state.last_successful_scope
            else None
        ),
        "entities": [
            item.model_dump(mode="json") for item in state.entity_memory
        ],
        "pending_clarification": (
            state.pending_clarification.model_dump(mode="json")
            if state.pending_clarification
            else None
        ),
        "result_refs": [
            {
                "result_id": item.result_id,
                "turn_id": item.turn_id,
                "status": item.status,
                "row_count": item.row_count,
            }
            for item in state.result_references
        ],
    }


def _safe_diagnostics(value: dict[str, Any]) -> dict[str, Any]:
    allowed: dict[str, set[str]] = {
        "interpretation": {"mode", "source", "confidence", "trigger"},
        "result_memory": {
            "retrieved_chunks",
            "result_refs",
            "persisted",
            "stored_chunks",
        },
        "execution": {"status", "task_count", "elapsed_ms"},
    }
    safe: dict[str, Any] = {}
    for section, keys in allowed.items():
        raw = value.get(section)
        if isinstance(raw, dict):
            safe[section] = {key: raw[key] for key in keys if key in raw}
    return safe
