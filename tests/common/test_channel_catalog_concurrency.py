"""Two bulk submissions for the same new video queue it exactly once (issue #242).

The interleaving is forced, not hoped for: submission A is held open inside its
transaction (after the ``videos`` insert, before its ``enqueue`` returns),
submission B is started and observed blocked on A via ``pg_blocking_pids``, and
only then is A released.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest

from common.channel_catalog import (
    BulkCatalogResult,
    discover_catalog,
    process_catalog_selection,
)
from common.models import CatalogEntry, ChannelCatalog
from common.queue import PostgresQueue
from tests.common.repo.conftest import head_dsn

__all__ = ["head_dsn"]

pytestmark = pytest.mark.integration

CHANNEL = "UC" + "c" * 22
VIDEO = "aaaaaaaaaaa"
DEADLINE_SEC = 30.0


class _Catalog:
    def list_uploads(self, channel_id: str, *, limit: int) -> ChannelCatalog:
        return ChannelCatalog(channel_id, (CatalogEntry(VIDEO, "T", None),), 1)


class _HeldQueue(PostgresQueue):
    """A ``PostgresQueue`` that waits inside ``enqueue`` until released."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        super().__init__(conn)
        self.reached = threading.Event()
        self.release = threading.Event()

    def enqueue(self, *args: Any, **kwargs: Any) -> int | None:
        self.reached.set()
        if not self.release.wait(DEADLINE_SEC):
            raise AssertionError("submission A was never released")
        return super().enqueue(*args, **kwargs)


def _wait_until(predicate: Any, what: str) -> None:
    deadline = time.monotonic() + DEADLINE_SEC
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"never happened within {DEADLINE_SEC}s: {what}")


def test_concurrent_bulk_submission_queues_each_video_once(head_dsn: str) -> None:
    with psycopg.connect(head_dsn, autocommit=True) as setup:
        setup.execute("INSERT INTO channels (channel_id) VALUES (%s)", (CHANNEL,))
        discover_catalog(setup, _Catalog(), CHANNEL, limit=50)

    with (
        psycopg.connect(head_dsn, autocommit=True) as conn_a,
        psycopg.connect(head_dsn, autocommit=True) as conn_b,
        psycopg.connect(head_dsn, autocommit=True) as watcher,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        held = _HeldQueue(conn_a)
        first = pool.submit(process_catalog_selection, conn_a, held, CHANNEL, [VIDEO])
        _wait_until(held.reached.is_set, "submission A reached its enqueue")

        second = pool.submit(
            process_catalog_selection, conn_b, PostgresQueue(conn_b), CHANNEL, [VIDEO]
        )

        def b_blocked_by_a() -> bool:
            row = watcher.execute(
                "SELECT %s = ANY(pg_blocking_pids(%s))",
                (conn_a.info.backend_pid, conn_b.info.backend_pid),
            ).fetchone()
            return bool(row and row[0])

        _wait_until(b_blocked_by_a, "submission B blocked behind submission A")
        assert not second.done()
        held.release.set()
        results: list[BulkCatalogResult] = [first.result(DEADLINE_SEC)[0], second.result(DEADLINE_SEC)[0]]

    assert sorted(r.outcome for r in results) == ["already_known", "queued"]
    queued = next(r for r in results if r.outcome == "queued")
    with psycopg.connect(head_dsn) as check:
        assert check.execute(
            "SELECT count(*) FROM videos WHERE video_id = %s", (VIDEO,)
        ).fetchone() == (1,)
        assert check.execute(
            "SELECT id, priority FROM jobs WHERE video_id = %s AND kind = 'ingest'", (VIDEO,)
        ).fetchall() == [(queued.job_id, -10)]
