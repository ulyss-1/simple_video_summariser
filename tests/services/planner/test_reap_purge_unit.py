"""The planner ``reap`` task also purges old CANCELLED jobs (issue #327). No database."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg.pq import TransactionStatus

from common.config import Settings
from services.planner import main as planner_main


class FakeConn:
    """Tracks the transaction scope; ends idle only if the purge was committed."""

    def __init__(self) -> None:
        self.depth = 0
        self.purge_depth: int | None = None
        self.committed = 0
        self.closed = False

    @property
    def info(self) -> SimpleNamespace:
        status = TransactionStatus.INTRANS if self.depth else TransactionStatus.IDLE
        return SimpleNamespace(transaction_status=status)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1
            self.committed += 1

    def close(self) -> None:
        self.closed = True


def test_reap_returns_reaped_plus_purged_and_commits_the_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = FakeConn()
    retentions: list[timedelta] = []

    def fake_purge(c: FakeConn, retention: timedelta, **_: Any) -> int:
        conn.purge_depth = c.depth
        retentions.append(retention)
        return 3

    monkeypatch.setattr(planner_main, "reap_stale_jobs", lambda queue, settings: 2)
    monkeypatch.setattr(planner_main, "PostgresQueue", lambda c, settings: object())
    monkeypatch.setattr(planner_main, "purge_cancelled_jobs", fake_purge)
    settings = Settings(
        DATABASE_URL="postgresql://u:p@h/db",  # type: ignore[arg-type]
        CANCELLED_JOB_RETENTION_DAYS=2,
    )

    tasks = {t.name: t for t in planner_main.build_registry(settings, lambda: conn)}  # type: ignore[arg-type,return-value]

    # run_on_fresh_connection raises if the connection is not idle on return.
    assert tasks["reap"].fn() == 5
    assert retentions == [timedelta(days=2)]
    assert conn.purge_depth == 1
    assert conn.committed == 1 and conn.depth == 0 and conn.closed
