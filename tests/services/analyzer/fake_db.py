"""A database double for the analyzer entrypoint tests (issue #33).

It answers just what ``PostgresQueue`` asks a connection for: ``transaction()``,
``cursor()`` and ``execute()``. ``FakeDatabase.connect`` is the injectable
``connect``: it records every connection it hands out and can fail its first
``failures`` calls, which is how the "database down at startup" tests run
without a database or a real sleep.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict
from typing import Any, Self

from common.queue import Job

CLAIM_MARKER = "state='running', locked_at=now()"
HEARTBEAT_MARKER = "SET heartbeat_at"


class FakeCursor:
    rowcount = 1

    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn
        self._last_sql = ""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        self._last_sql = sql
        self._conn.executed.append((sql, params))
        return self

    def fetchone(self) -> dict[str, Any] | None:
        if CLAIM_MARKER in self._last_sql and self._conn.database.pending_rows:
            return self._conn.database.pending_rows.pop(0)
        return None


class FakeConn:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.executed: list[tuple[str, Any]] = []
        self.closed = False

    def transaction(self) -> Any:
        return nullcontext()

    def cursor(self, **kwargs: Any) -> FakeCursor:
        return FakeCursor(self)

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        return FakeCursor(self).execute(sql, params)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def statements(self, marker: str) -> list[Any]:
        return [params for sql, params in self.executed if marker in sql]


class FakeDatabase:
    def __init__(self, *, failures: int = 0, jobs: list[Job] | None = None) -> None:
        self.failures = failures
        self.connect_calls = 0
        self.connections: list[FakeConn] = []
        self.pending_rows = [asdict(job) for job in jobs or []]

    def connect(self) -> Any:
        self.connect_calls += 1
        if self.connect_calls <= self.failures:
            raise ConnectionError("connection refused (fake)")
        conn = FakeConn(self)
        self.connections.append(conn)
        return conn
