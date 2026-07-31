from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import Iterator
from uuid import UUID

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


class ContextStoreError(RuntimeError):
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

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._states.pop(session_id, None) is not None

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


class SQLiteContextStore:
    """Durable V2 store adapted from pipeline's optimistic SQLite store."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path).expanduser().resolve()
        self.busy_timeout_ms = max(100, int(busy_timeout_ms))
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create(self, session_id: str) -> ContextContractV2:
        state = ContextContractV2(session_id=session_id)
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO context_sessions "
                    "(session_id, revision, updated_at, payload_json) VALUES (?, ?, ?, ?)",
                    (
                        state.session_id,
                        state.revision,
                        state.updated_at.isoformat(),
                        state.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"session already exists: {session_id}") from exc
        except sqlite3.Error as exc:
            raise ContextStoreError("could not create context session") from exc
        return state

    def get(self, session_id: str) -> ContextContractV2:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT payload_json FROM context_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ContextStoreError("could not load context session") from exc
        if row is None:
            raise SessionNotFound(session_id)
        return ContextContractV2.model_validate_json(row[0])

    def delete(self, session_id: str) -> bool:
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    "DELETE FROM context_sessions WHERE session_id = ?",
                    (session_id,),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise ContextStoreError("could not delete context session") from exc

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
        try:
            with self._lock, self._transaction() as connection:
                row = connection.execute(
                    "SELECT revision, payload_json FROM context_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise SessionNotFound(session_id)
                actual_revision = int(row[0])
                if actual_revision != expected_revision:
                    raise RevisionConflict(session_id, expected_revision, actual_revision)
                current = ContextContractV2.model_validate_json(row[1])
                updated = apply_context_transition(
                    current,
                    mutation,
                    outcome,
                    result=result,
                    clarification_questions=clarification_questions,
                )
                cursor = connection.execute(
                    "UPDATE context_sessions "
                    "SET revision = ?, updated_at = ?, payload_json = ? "
                    "WHERE session_id = ? AND revision = ?",
                    (
                        updated.revision,
                        updated.updated_at.isoformat(),
                        updated.model_dump_json(),
                        session_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RevisionConflict(session_id, expected_revision, actual_revision)
                return updated
        except (SessionNotFound, RevisionConflict):
            raise
        except sqlite3.Error as exc:
            raise ContextStoreError("could not commit context transition") from exc

    def diagnostics(self) -> dict[str, object]:
        try:
            with closing(self._connect()) as connection:
                count = int(
                    connection.execute("SELECT COUNT(*) FROM context_sessions").fetchone()[0]
                )
        except sqlite3.Error as exc:
            raise ContextStoreError("could not inspect context store") from exc
        return {
            "type": "sqlite",
            "durable": True,
            "path": str(self.path),
            "active_sessions": count,
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000.0,
        )
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        try:
            with self._transaction() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS context_sessions ("
                    "session_id TEXT PRIMARY KEY, "
                    "revision INTEGER NOT NULL, "
                    "updated_at TEXT NOT NULL, "
                    "payload_json TEXT NOT NULL"
                    ")"
                )
        except sqlite3.Error as exc:
            raise ContextStoreError("could not initialize context store") from exc


_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


class PostgresContextStore:
    """Authoritative V2 session store with an append-only mutation journal."""

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "chat_rag",
        connect_timeout_s: int = 5,
        connect=None,
    ) -> None:
        self.dsn = str(dsn).strip()
        self.schema = str(schema).strip()
        if not self.dsn:
            raise ValueError("context store DSN is required")
        if not _SCHEMA_NAME.fullmatch(self.schema):
            raise ValueError("context store schema name is invalid")
        self.connect_timeout_s = max(1, int(connect_timeout_s))
        if connect is None:
            import psycopg

            connect = psycopg.connect
        self._connect = connect

    def validate(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regclass(%s), to_regclass(%s)",
                (
                    f"{self.schema}.context_sessions_v2",
                    f"{self.schema}.context_mutations_v2",
                ),
            )
            sessions, mutations = cursor.fetchone()
        if sessions is None or mutations is None:
            raise ContextStoreError("context contract V2 schema is not ready")

    def create(self, session_id: str) -> ContextContractV2:
        session_id = _uuid_text(session_id, "session_id")
        state = ContextContractV2(session_id=session_id)
        try:
            from psycopg.types.json import Jsonb

            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {self.schema}.context_sessions_v2 (
                        session_id, contract_version, revision,
                        metadata_bundle_id, metadata_bundle_version,
                        metadata_schema_version, payload, created_at, updated_at
                    ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        session_id,
                        state.contract_version,
                        state.revision,
                        None,
                        None,
                        None,
                        Jsonb(state.model_dump(mode="json")),
                        state.created_at,
                        state.updated_at,
                    ),
                )
                connection.commit()
        except Exception as exc:
            raise ContextStoreError("could not create PostgreSQL context session") from exc
        return state

    def get(self, session_id: str) -> ContextContractV2:
        session_id = _uuid_text(session_id, "session_id")
        try:
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT payload FROM {self.schema}.context_sessions_v2 "
                    "WHERE session_id = %s::uuid",
                    (session_id,),
                )
                row = cursor.fetchone()
        except Exception as exc:
            raise ContextStoreError("could not load PostgreSQL context session") from exc
        if row is None:
            raise SessionNotFound(session_id)
        return ContextContractV2.model_validate(row[0])

    def delete(self, session_id: str) -> bool:
        session_id = _uuid_text(session_id, "session_id")
        try:
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    f"DELETE FROM {self.schema}.context_sessions_v2 "
                    "WHERE session_id = %s::uuid",
                    (session_id,),
                )
                deleted = cursor.rowcount == 1
                connection.commit()
                return deleted
        except Exception as exc:
            raise ContextStoreError("could not delete PostgreSQL context session") from exc

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
        session_id = _uuid_text(session_id, "session_id")
        _uuid_text(mutation.turn_id, "turn_id")
        try:
            from psycopg.types.json import Jsonb

            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT revision, payload FROM {self.schema}.context_sessions_v2 "
                    "WHERE session_id = %s::uuid FOR UPDATE",
                    (session_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise SessionNotFound(session_id)
                actual_revision = int(row[0])
                if actual_revision != expected_revision:
                    raise RevisionConflict(session_id, expected_revision, actual_revision)
                current = ContextContractV2.model_validate(row[1])
                updated = apply_context_transition(
                    current,
                    mutation,
                    outcome,
                    result=result,
                    clarification_questions=clarification_questions,
                )
                metadata = updated.metadata
                cursor.execute(
                    f"""
                    UPDATE {self.schema}.context_sessions_v2
                    SET revision = %s, metadata_bundle_id = %s,
                        metadata_bundle_version = %s, metadata_schema_version = %s,
                        payload = %s, updated_at = %s
                    WHERE session_id = %s::uuid AND revision = %s
                    """,
                    (
                        updated.revision,
                        metadata.bundle_id if metadata else None,
                        metadata.bundle_version if metadata else None,
                        metadata.schema_version if metadata else None,
                        Jsonb(updated.model_dump(mode="json")),
                        updated.updated_at,
                        session_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RevisionConflict(session_id, expected_revision, actual_revision)
                cursor.execute(
                    f"""
                    INSERT INTO {self.schema}.context_mutations_v2 (
                        session_id, revision, turn_id, outcome, mutation,
                        contract_sha256
                    ) VALUES (%s::uuid, %s, %s::uuid, %s, %s, %s)
                    """,
                    (
                        session_id,
                        updated.revision,
                        mutation.turn_id,
                        outcome.value,
                        Jsonb(mutation.model_dump(mode="json")),
                        _contract_hash(updated),
                    ),
                )
                connection.commit()
                return updated
        except (SessionNotFound, RevisionConflict):
            raise
        except Exception as exc:
            raise ContextStoreError("could not commit PostgreSQL context transition") from exc

    def diagnostics(self) -> dict[str, object]:
        return {
            "type": "postgresql",
            "durable": True,
            "schema": self.schema,
        }

    def _connection(self):
        return self._connect(self.dsn, connect_timeout=self.connect_timeout_s)


def _uuid_text(value: str, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc


def _contract_hash(state: ContextContractV2) -> str:
    canonical = json.dumps(
        state.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()
