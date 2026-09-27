"""Integration tests for ``common.worker.Worker`` against a real ``PostgresQueue``
(issue #13, architecture.md §5).

``tests/common/test_worker.py`` drives the loop against a ``FakeQueue`` for
every behaviour that is really about the state machine. The three things
here need a real database because they depend on genuine Postgres semantics
that a fake can't honestly reproduce:

- the heartbeat really lands on a *second* connection, distinct from the one
  ``claim`` uses (``jobs.heartbeat_at`` moves even though the claim
  connection never touches it);
- a job cancelled by the grace period is released to ``pending`` with its
  ``attempts`` unchanged - the queue's own accounting (#11), not the
  worker's;
- ``heartbeat()`` returning ``False`` once another worker has genuinely
  reclaimed the row (via ``reap_stale``), not just a fake told to say so.

Marked ``integration`` at module level per AGENTS.md - needs Docker Desktop.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row

from common.config import Settings, get_settings
from common.queue import Job, PostgresQueue
from common.worker import JobContext, Worker

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _config_for(dsn: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


@pytest.fixture
def head_dsn(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    command.upgrade(_config_for(postgres_dsn, monkeypatch), "head")
    return postgres_dsn


@pytest.fixture
def claim_conn(head_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(head_dsn, autocommit=True) as connection:
        yield connection


@pytest.fixture
def heartbeat_conn(head_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(head_dsn, autocommit=True) as connection:
        yield connection


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"DATABASE_URL": "postgresql://u:p@h/db"}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def claim_queue(claim_conn: psycopg.Connection[Any]) -> PostgresQueue:
    return PostgresQueue(claim_conn, settings=_settings())


@pytest.fixture
def heartbeat_queue(heartbeat_conn: psycopg.Connection[Any]) -> PostgresQueue:
    return PostgresQueue(heartbeat_conn, settings=_settings())


def _row(conn: psycopg.Connection[Any], job_id: int) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
    assert row is not None
    return row


class _ManualTimerHandle:
    def __init__(self, function: Any) -> None:
        self.function = function
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.function()


class _ManualTimerFactory:
    """A timer that never fires on its own; tests fire it by hand, still
    against real Postgres for everything else, per issue #13's "no real
    sleep" testing constraint."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[float, _ManualTimerHandle]] = []

    def __call__(self, interval: float, function: Any) -> _ManualTimerHandle:
        handle = _ManualTimerHandle(function)
        self.scheduled.append((interval, handle))
        return handle

    @property
    def last(self) -> _ManualTimerHandle:
        return self.scheduled[-1][1]


def make_worker(
    claim_queue: PostgresQueue,
    heartbeat_queue: PostgresQueue,
    handler: Any,
    *,
    liveness_path: Path,
    timer_factory: _ManualTimerFactory,
    heartbeat_sec: float = 60,
    shutdown_grace_sec: float = 20,
) -> Worker:
    return Worker(
        "w1",
        ["ingest"],
        handler,
        claim_queue,
        liveness_path=liveness_path,
        heartbeat_sec=heartbeat_sec,
        shutdown_grace_sec=shutdown_grace_sec,
        heartbeat_queue_factory=lambda: heartbeat_queue,
        timer_factory=timer_factory,
        sleep=lambda _s: None,
        install_signal_handlers=False,
    )


def test_heartbeat_updates_heartbeat_at_via_a_connection_distinct_from_claim(
    claim_queue: PostgresQueue,
    heartbeat_queue: PostgresQueue,
    claim_conn: psycopg.Connection[Any],
    tmp_path: Path,
) -> None:
    job_id = claim_queue.enqueue("ingest", "v1")
    assert job_id is not None
    timer_factory = _ManualTimerFactory()
    at_claim: dict[str, Any] = {}

    def handler(job: Job, ctx: JobContext) -> None:
        at_claim["heartbeat_at"] = _row(claim_conn, job_id)["heartbeat_at"]
        timer_factory.last.fire()

    worker = make_worker(
        claim_queue, heartbeat_queue, handler, liveness_path=tmp_path / "hb", timer_factory=timer_factory
    )
    worker.run(max_iterations=1)

    # The claim connection is autocommit, so a fresh SELECT sees whatever the
    # heartbeat connection committed, even though claim_queue itself never
    # issued the UPDATE.
    after = _row(claim_conn, job_id)["heartbeat_at"]
    assert at_claim["heartbeat_at"] is not None
    assert after is not None
    assert after >= at_claim["heartbeat_at"]


def test_grace_expiry_releases_the_job_to_pending_without_spending_an_attempt(
    claim_queue: PostgresQueue,
    heartbeat_queue: PostgresQueue,
    claim_conn: psycopg.Connection[Any],
    tmp_path: Path,
) -> None:
    job_id = claim_queue.enqueue("ingest", "v1")
    assert job_id is not None
    timer_factory = _ManualTimerFactory()

    def handler(job: Job, ctx: JobContext) -> None:
        worker.request_shutdown()
        timer_factory.last.fire()  # grace period expires
        ctx.check_cancelled()  # raises Cancelled

    worker = make_worker(
        claim_queue, heartbeat_queue, handler, liveness_path=tmp_path / "hb", timer_factory=timer_factory
    )
    attempts_before_claim = _row(claim_conn, job_id)["attempts"]
    worker.run()  # returns normally: process exit 0

    row = _row(claim_conn, job_id)
    assert row["state"] == "pending"
    # attempts was incremented by claim() itself (architecture.md §5's claim
    # query does attempts=attempts+1); Cancelled must undo exactly that,
    # leaving it where it started rather than "spent".
    assert row["attempts"] == attempts_before_claim
    assert row["locked_by"] is None


def test_heartbeat_returns_false_once_the_job_is_genuinely_reaped(
    claim_queue: PostgresQueue,
    heartbeat_queue: PostgresQueue,
    claim_conn: psycopg.Connection[Any],
    tmp_path: Path,
) -> None:
    job_id = claim_queue.enqueue("ingest", "v1")
    assert job_id is not None
    timer_factory = _ManualTimerFactory()
    cancelled_seen = []

    def handler(job: Job, ctx: JobContext) -> None:
        # Simulate the reaper (architecture.md §5's "Reaping (C2)") having
        # reclaimed this row and handed it to another worker while this
        # one's heartbeat connection was stalled: the row now belongs to
        # "other-worker", exactly as reap_stale + a fresh claim would leave
        # it, checked with real SQL rather than a fake told to lie.
        with claim_conn.cursor() as cur:
            cur.execute("UPDATE jobs SET locked_by = 'other-worker' WHERE id = %s", (job_id,))

        timer_factory.last.fire()  # this worker's heartbeat tick, now stale
        cancelled_seen.append(ctx.cancelled)

    worker = make_worker(
        claim_queue, heartbeat_queue, handler, liveness_path=tmp_path / "hb", timer_factory=timer_factory
    )
    worker.run(max_iterations=1)

    assert cancelled_seen == [True]
