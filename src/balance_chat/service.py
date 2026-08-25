from __future__ import annotations

from collections import defaultdict
import hashlib
import logging
from threading import RLock
from time import perf_counter
from typing import Any, Callable, Protocol
from uuid import uuid4

from pydantic import Field

from .contracts import (
    AnalysisIntent,
    ClarificationAnswer,
    ContextContractV2,
    ContextMutation,
    ContractModel,
    ResultReference,
    ResultMemoryWrite,
    TransitionOutcome,
)
from .runtime_consistency import (
    CommittedReloadedConsistencyError,
    ExecutedCommittedConsistencyError,
    assert_committed_reloaded_consistent,
    assert_executed_committed_consistent,
)
from .store import RevisionConflict
from .observability import log_event


LOGGER = logging.getLogger("balance_chat.service")


class TurnProcessingError(RuntimeError):
    def __init__(self, message: str, *, code: str = "turn_processing_error") -> None:
        super().__init__(message)
        self.code = code


class MetadataSessionMismatch(RuntimeError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"session {session_id} belongs to another metadata bundle")
        self.session_id = session_id


class TurnProcessResult(ContractModel):
    mutation: ContextMutation
    outcome: TransitionOutcome
    executed_intent: AnalysisIntent | None = None
    response: dict[str, Any] = Field(default_factory=dict)
    result_reference: ResultReference | None = None
    memory_write: ResultMemoryWrite | None = None
    clarification_questions: list[dict[str, Any]] | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class TurnProcessor(Protocol):
    def process(
        self,
        state: ContextContractV2,
        *,
        message: str,
        execute_db: bool,
        clarification: ClarificationAnswer | None,
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
        persist_result_memory: Callable[..., dict[str, Any]] | None = None,
        semantic_shadow: Any | None = None,
    ) -> None:
        self.store = store
        self.processor = processor
        self.metadata = metadata
        self.delete_result_memory = delete_result_memory
        self.persist_result_memory = persist_result_memory
        self.semantic_shadow = semantic_shadow
        self._locks: defaultdict[str, RLock] = defaultdict(RLock)
        self._locks_guard = RLock()

    def create_session(self) -> ContextContractV2:
        state = self.store.create(str(uuid4()), self.metadata)
        log_event(
            LOGGER,
            logging.INFO,
            "session_created",
            session_id=state.session_id,
            revision=state.revision,
            metadata_bundle_version=(state.metadata.bundle_version if state.metadata else None),
        )
        return state

    def get_session(self, session_id: str) -> ContextContractV2:
        state = self.store.get(session_id)
        log_event(
            LOGGER,
            logging.INFO,
            "session_loaded",
            session_id=session_id,
            revision=state.revision,
        )
        return state

    def record_feedback(
        self,
        *,
        session_id: str,
        request_id: str,
        rating: str,
        comment: str | None = None,
    ) -> dict[str, Any]:
        state = self.store.get(session_id)
        if rating not in {"helpful", "unhelpful"}:
            raise ValueError("unsupported feedback rating")
        log_event(
            LOGGER,
            logging.INFO,
            "turn_feedback_recorded",
            session_id=session_id,
            request_id=request_id,
            revision=state.revision,
            rating=rating,
            comment=(str(comment).strip()[:1000] if comment else None),
        )
        return {"status": "ok", "accepted": True}

    def delete_session(self, session_id: str) -> dict[str, Any]:
        with self._session_lock(session_id):
            deleted = bool(self.store.delete(session_id))
            memories = (
                int(self.delete_result_memory(session_id))
                if deleted and self.delete_result_memory is not None
                else 0
            )
        log_event(
            LOGGER,
            logging.INFO,
            "session_deleted",
            session_id=session_id,
            deleted=deleted,
            result_memories_deleted=memories,
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
        clarification: ClarificationAnswer | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        trace_id = request_id or str(uuid4())
        get_cached = getattr(self.store, "get_request_result", None)
        if request_id is not None and callable(get_cached):
            cached = get_cached(session_id, trace_id)
            if cached is not None:
                return cached
        started = perf_counter()
        reserved = False
        log_event(
            LOGGER,
            logging.INFO,
            "turn_started",
            request_id=trace_id,
            session_id=session_id,
            expected_revision=expected_revision,
            execute_db=execute_db,
            user_message=message,
            user_message_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
            user_message_length=len(message),
            clarification=(
                {
                    "source_turn_id": clarification.source_turn_id,
                    "clarification_id": clarification.clarification_id,
                }
                if clarification else None
            ),
        )
        try:
            reserve = getattr(self.store, "reserve", None)
            if callable(reserve):
                reserve(session_id, expected_revision, trace_id)
                reserved = True
            with self._session_lock(session_id):
                state = self.store.get(session_id)
                self._validate_metadata(state)
                if state.revision != expected_revision:
                    raise RevisionConflict(session_id, expected_revision, state.revision)
                shadow_state = (
                    state.model_copy(deep=True)
                    if self.semantic_shadow is not None
                    else None
                )
                processed = self.processor.process(
                    state,
                    message=message,
                    execute_db=execute_db,
                    clarification=clarification,
                    request_id=trace_id,
                )
                processed.mutation.assistant_summary = _compact_assistant_summary(
                    processed.response
                )
                committed = self.store.commit(
                    session_id,
                    expected_revision,
                    processed.mutation,
                    processed.outcome,
                    result=processed.result_reference,
                    clarification_questions=processed.clarification_questions,
                    memory_write=processed.memory_write,
                )
                if processed.executed_intent is not None:
                    try:
                        assert_executed_committed_consistent(
                            processed.executed_intent,
                            committed,
                            outcome=processed.outcome,
                        )
                    except ExecutedCommittedConsistencyError as exc:
                        log_event(
                            LOGGER,
                            logging.ERROR,
                            exc.event_code,
                            request_id=trace_id,
                            session_id=session_id,
                            revision=committed.revision,
                            dimension=exc.dimension,
                            expected=exc.expected,
                            actual=exc.actual,
                        )
                        raise
                reloaded = self.store.get(session_id)
                try:
                    assert_committed_reloaded_consistent(committed, reloaded)
                except CommittedReloadedConsistencyError as exc:
                    log_event(
                        LOGGER,
                        logging.ERROR,
                        exc.event_code,
                        request_id=trace_id,
                        session_id=session_id,
                        revision=committed.revision,
                        dimension=exc.dimension,
                        expected=exc.expected,
                        actual=exc.actual,
                    )
                    raise
                memory_status: dict[str, Any] = {}
                if (
                    processed.memory_write is not None
                    and self.persist_result_memory is not None
                    and committed.metadata is not None
                ):
                    try:
                        memory_status = self.persist_result_memory(
                            session_id=session_id,
                            revision=committed.revision,
                            metadata=committed.metadata,
                            write=processed.memory_write,
                        )
                        if memory_status.get("persisted"):
                            complete_memory = getattr(
                                self.store, "complete_memory_write", None
                            )
                            if callable(complete_memory):
                                complete_memory(session_id, committed.revision)
                    except Exception as memory_exc:
                        memory_status = {
                            "persisted": False,
                            "error": type(memory_exc).__name__,
                        }
                        log_event(
                            LOGGER,
                            logging.ERROR,
                            "result_memory_post_commit_failed",
                            request_id=trace_id,
                            session_id=session_id,
                            revision=committed.revision,
                            error_type=type(memory_exc).__name__,
                        )
                if memory_status:
                    processed.diagnostics.setdefault("result_memory", {}).update(
                        persisted=bool(memory_status.get("persisted")),
                        stored_chunks=int(memory_status.get("chunks") or 0),
                    )
                response = {
                    "status": _public_status(processed.outcome),
                    "session": _session_view(committed),
                    "context": _context_view(committed),
                    "result": processed.response,
                    "diagnostics": _safe_diagnostics(processed.diagnostics),
                    "request_id": trace_id,
                }
                save_cached = getattr(self.store, "save_request_result", None)
                if callable(save_cached):
                    save_cached(session_id, trace_id, response)
            production_elapsed_ms = int((perf_counter() - started) * 1000)
            shadow_allowed = True
            if reserved:
                reserved = False
                shadow_allowed = self._release_turn_reservation(
                    session_id, trace_id
                )
            if shadow_allowed:
                self._run_semantic_shadow(
                    state=shadow_state,
                    message=message,
                    processed=processed,
                    request_id=trace_id,
                    production_elapsed_ms=production_elapsed_ms,
                )
            intent = (
                committed.active_dialog_scope.intent
                if committed.active_dialog_scope is not None
                else None
            )
            log_event(
                LOGGER,
                logging.INFO,
                "turn_completed",
                request_id=trace_id,
                session_id=session_id,
                revision=committed.revision,
                outcome=processed.outcome.value,
                normalized_message=processed.mutation.normalized_message,
                operation=(intent.operation.value if intent else None),
                periods=(
                    [item.model_dump(mode="json") for item in intent.periods]
                    if intent else []
                ),
                operands=(
                    [
                        {
                            "operand_id": item.operand_id,
                            "metric": item.metric,
                            "aggregate_type": item.aggregate_type,
                            "entities": [
                                {
                                    "role": entity.role,
                                    "entity_id": entity.entity.entity_id,
                                    "entity_type": entity.entity.entity_type,
                                    "display_name": entity.entity.display_name,
                                }
                                for entity in item.entities
                            ],
                        }
                        for item in intent.operands
                    ] if intent else []
                ),
                interpretation=processed.diagnostics.get("interpretation"),
                execution=processed.diagnostics.get("execution"),
                summary=processed.diagnostics.get("summary"),
                result_memory=processed.diagnostics.get("result_memory"),
                result_fingerprint=(
                    processed.result_reference.resolved_plan_hash
                    if processed.result_reference is not None
                    else None
                ),
                elapsed_ms=production_elapsed_ms,
            )
            frame = (
                committed.conversation_window[-1]
                if committed.conversation_window else None
            )
            if frame is not None:
                log_event(
                    LOGGER,
                    logging.INFO,
                    "conversation_turn_committed",
                    request_id=trace_id,
                    session_id=session_id,
                    revision=committed.revision,
                    turn_handle=frame.turn_handle,
                    turn_id=frame.turn_id,
                    outcome=frame.outcome.value,
                    user_message=frame.user_message,
                    normalized_message=frame.normalized_message,
                    operation=frame.intent.operation.value,
                    operand_handles=[item.handle for item in frame.operands],
                    entity_tags=[
                        {
                            "handle": item.handle,
                            "operand_handle": item.operand_handle,
                            "type": item.entity.entity_type,
                            "role": item.role,
                            "canonical_id": item.entity.entity_id,
                            "label": item.entity.display_name,
                        }
                        for item in frame.entities
                    ],
                    period_tags=[
                        {
                            "handle": item.handle,
                            "owner_handle": item.owner_handle,
                            **item.period.model_dump(mode="json"),
                        }
                        for item in frame.periods
                    ],
                    result=(
                        {
                            "handle": frame.result.handle,
                            "row_count": frame.result.row_count,
                            "fact_count": len(frame.result.facts),
                        }
                        if frame.result else None
                    ),
                    conversation_window_size=len(committed.conversation_window),
                )
            return response
        except RevisionConflict as exc:
            log_event(
                LOGGER,
                logging.WARNING,
                "turn_revision_conflict",
                request_id=trace_id,
                session_id=session_id,
                expected_revision=exc.expected,
                actual_revision=exc.actual,
                elapsed_ms=int((perf_counter() - started) * 1000),
            )
            raise
        except Exception as exc:
            log_event(
                LOGGER,
                logging.ERROR,
                "turn_failed",
                request_id=trace_id,
                session_id=session_id,
                expected_revision=expected_revision,
                error_type=type(exc).__name__,
                error_code=getattr(exc, "code", None),
                elapsed_ms=int((perf_counter() - started) * 1000),
                exc_info=True,
            )
            raise
        finally:
            if reserved:
                reserved = False
                self._release_turn_reservation(session_id, trace_id)

    def _release_turn_reservation(self, session_id: str, request_id: str) -> bool:
        release = getattr(self.store, "release", None)
        if not callable(release):
            return False
        try:
            release(session_id, request_id)
            return True
        except Exception:
            log_event(
                LOGGER,
                logging.ERROR,
                "turn_reservation_release_failed",
                request_id=request_id,
                session_id=session_id,
            )
            return False

    def _run_semantic_shadow(
        self,
        *,
        state: ContextContractV2 | None,
        message: str,
        processed: TurnProcessResult,
        request_id: str,
        production_elapsed_ms: int,
    ) -> None:
        if self.semantic_shadow is None or state is None:
            return
        interpretation = processed.diagnostics.get("interpretation")
        interpretation_mode = (
            str(interpretation.get("mode") or "")
            if isinstance(interpretation, dict)
            else None
        )
        try:
            shadow = self.semantic_shadow.run(
                state=state.model_copy(deep=True),
                message=message,
                interpretation_mode=interpretation_mode,
                request_id=request_id,
            )
            proposal = (
                shadow.proposal.model_dump(mode="json")
                if shadow.validation_status.value == "valid"
                and shadow.proposal is not None
                else None
            )
            log_event(
                LOGGER,
                logging.INFO,
                "semantic_transition_shadow_completed",
                request_id=request_id,
                eligible=shadow.eligible,
                invoked=shadow.invoked,
                validation_status=shadow.validation_status.value,
                validation_errors=list(shadow.validation_errors),
                proposal=proposal,
                shadow_latency_ms=shadow.latency_ms,
                production_latency_ms=production_elapsed_ms,
                model=shadow.model,
                context_turn_count=shadow.context_turn_count,
                context_result_count=shadow.context_result_count,
                context_size_chars=shadow.context_size_chars,
            )
        except Exception as exc:
            log_event(
                LOGGER,
                logging.WARNING,
                "semantic_transition_shadow_failed",
                request_id=request_id,
                production_latency_ms=production_elapsed_ms,
                error_type=type(exc).__name__,
            )

    def _session_lock(self, session_id: str) -> RLock:
        with self._locks_guard:
            return self._locks[session_id]

    def _validate_metadata(self, state: ContextContractV2) -> None:
        if self.metadata is None:
            return
        if state.metadata is None or state.metadata != self.metadata:
            raise MetadataSessionMismatch(state.session_id)


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


def _compact_assistant_summary(response: dict[str, Any]) -> str | None:
    parts: list[str] = []
    title = response.get("title")
    if title:
        parts.append(str(title).strip())
    summary = response.get("summary")
    if isinstance(summary, dict):
        for key in ("title", "text"):
            value = str(summary.get(key) or "").strip()
            if value and value not in parts:
                parts.append(value)
    elif summary:
        parts.append(str(summary).strip())
    if not parts:
        status = str(response.get("status") or "").strip()
        operation = str(response.get("operation") or "").strip()
        if status or operation:
            parts.append(" ".join(item for item in (operation, status) if item))
    compact = "\n".join(item for item in parts if item).strip()
    return compact[:4000] or None
