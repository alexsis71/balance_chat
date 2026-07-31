from __future__ import annotations

from threading import RLock

from .contracts import (
    ContextContractV2,
    ContextMutation,
    ResultReference,
    TransitionOutcome,
)
from .reducer import apply_context_transition


class RevisionConflict(RuntimeError):
    def __init__(self, session_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"context revision conflict for {session_id}: expected {expected}, actual {actual}"
        )
        self.session_id = session_id
        self.expected = expected
        self.actual = actual


class SessionNotFound(KeyError):
    pass


class InMemoryContextStore:
    """Reference store with optimistic concurrency; production stores use this boundary."""

    def __init__(self) -> None:
        self._states: dict[str, ContextContractV2] = {}
        self._lock = RLock()

    def create(self, session_id: str) -> ContextContractV2:
        with self._lock:
            if session_id in self._states:
                raise ValueError(f"session already exists: {session_id}")
            state = ContextContractV2(session_id=session_id)
            self._states[session_id] = state
            return state.model_copy(deep=True)

    def get(self, session_id: str) -> ContextContractV2:
        with self._lock:
            state = self._states.get(session_id)
            if state is None:
                raise SessionNotFound(session_id)
            return state.model_copy(deep=True)

    def commit(
        self,
        session_id: str,
        expected_revision: int,
        mutation: ContextMutation,
        outcome: TransitionOutcome,
        *,
        result: ResultReference | None = None,
        clarification_questions: list[dict] | None = None,
    ) -> ContextContractV2:
        with self._lock:
            current = self._states.get(session_id)
            if current is None:
                raise SessionNotFound(session_id)
            if current.revision != expected_revision:
                raise RevisionConflict(session_id, expected_revision, current.revision)
            updated = apply_context_transition(
                current,
                mutation,
                outcome,
                result=result,
                clarification_questions=clarification_questions,
            )
            self._states[session_id] = updated
            return updated.model_copy(deep=True)

