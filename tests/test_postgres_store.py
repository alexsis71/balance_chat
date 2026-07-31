from __future__ import annotations

from uuid import uuid4

import pytest

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    ContextContractV2,
    ContextMutation,
    Operation,
    PeriodRef,
    TransitionOutcome,
)
from balance_chat.store import PostgresContextStore, RevisionConflict


class FakeCursor:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, object]] = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql: str, params=None) -> None:
        self.executed.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0)


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self) -> FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True


def _mutation() -> ContextMutation:
    return ContextMutation(
        turn_id=str(uuid4()),
        user_message="Покажи поставки за май 2025",
        replace_intent=AnalysisIntent(
            operation=Operation.SHOW,
            operands=[
                AnalysisOperand(operand_id="supply", metric="distribution")
            ],
            periods=[PeriodRef(date_from="2025-05-01", date_to="2025-06-01")],
        ),
    )


def test_postgres_commit_writes_session_and_append_only_journal() -> None:
    session_id = str(uuid4())
    initial = ContextContractV2(session_id=session_id)
    cursor = FakeCursor([(0, initial.model_dump(mode="json"))])
    connection = FakeConnection(cursor)
    store = PostgresContextStore(
        "postgresql://example",
        connect=lambda *_args, **_kwargs: connection,
    )

    updated = store.commit(
        session_id,
        0,
        _mutation(),
        TransitionOutcome.SUCCESS,
    )

    assert updated.revision == 1
    assert connection.committed
    sql = "\n".join(statement for statement, _ in cursor.executed)
    assert "FOR UPDATE" in sql
    assert "UPDATE chat_rag.context_sessions_v2" in sql
    assert "INSERT INTO chat_rag.context_mutations_v2" in sql


def test_postgres_commit_rejects_stale_revision_before_mutation() -> None:
    session_id = str(uuid4())
    initial = ContextContractV2(session_id=session_id)
    cursor = FakeCursor([(2, initial.model_dump(mode="json"))])
    store = PostgresContextStore(
        "postgresql://example",
        connect=lambda *_args, **_kwargs: FakeConnection(cursor),
    )

    with pytest.raises(RevisionConflict):
        store.commit(
            session_id,
            1,
            _mutation(),
            TransitionOutcome.SUCCESS,
        )


def test_postgres_store_requires_uuid_identifiers() -> None:
    store = PostgresContextStore(
        "postgresql://example",
        connect=lambda *_args, **_kwargs: None,
    )
    with pytest.raises(ValueError, match="session_id"):
        store.get("not-a-uuid")
