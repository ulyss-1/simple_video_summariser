"""Advisory-lock behaviour against a real Postgres (issue #38).

Only Postgres itself can show that a session-level advisory lock excludes a
second connection and disappears with its connection, so these are
``integration`` tests. No migrations are needed; advisory locks touch no table.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import psycopg
import pytest

from services.planner.main import LOCK_KEYS, PostgresAdvisoryLocks
from services.planner.scheduler import Scheduler, Task

pytestmark = pytest.mark.integration

MakeLocks = Callable[[], PostgresAdvisoryLocks]


@pytest.fixture
def make_locks(postgres_dsn: str) -> Iterator[Callable[[], PostgresAdvisoryLocks]]:
    created: list[PostgresAdvisoryLocks] = []

    def make() -> PostgresAdvisoryLocks:
        locks = PostgresAdvisoryLocks(lambda: psycopg.connect(postgres_dsn))
        created.append(locks)
        return locks

    yield make
    for locks in created:
        locks.close()


def _can_take(dsn: str, name: str) -> bool:
    namespace, task_id = LOCK_KEYS[name]
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute("SELECT pg_try_advisory_lock(%s, %s)", (namespace, task_id)).fetchone()
        assert row is not None
        return bool(row[0])


def _scheduler(locks: PostgresAdvisoryLocks, tasks: list[Task]) -> Scheduler:
    return Scheduler(
        tasks, locks, install_signal_handlers=False, sleep=lambda _s: None
    )


def test_a_second_scheduler_skips_a_task_whose_lock_the_first_holds(make_locks: MakeLocks) -> None:
    first_locks, second_locks = make_locks(), make_locks()
    second_calls: list[str] = []
    second = _scheduler(second_locks, [Task("reap", 60, lambda: second_calls.append("reap"))])
    second_outcome: list[bool] = []

    def first_task() -> None:
        second_outcome.append(second.run_once())  # the first still holds "reap"

    first = _scheduler(first_locks, [Task("reap", 60, first_task)])

    assert first.run_once() is True

    assert second_calls == []
    assert second_outcome == [True]  # skipped_locked is not a failure


def test_the_second_scheduler_runs_the_task_once_the_first_is_done(make_locks: MakeLocks) -> None:
    first_locks, second_locks = make_locks(), make_locks()
    calls: list[str] = []
    _scheduler(first_locks, [Task("reap", 60, lambda: None)]).run_once()

    _scheduler(second_locks, [Task("reap", 60, lambda: calls.append("second"))]).run_once()

    assert calls == ["second"]


def test_the_lock_is_free_again_after_a_task_raises(make_locks: MakeLocks, postgres_dsn: str) -> None:
    def boom() -> None:
        raise RuntimeError("boom")

    sched = _scheduler(make_locks(), [Task("reap", 60, boom)])

    assert sched.run_once() is False

    assert _can_take(postgres_dsn, "reap")


def test_the_lock_is_freed_when_the_holding_connection_closes(make_locks: MakeLocks) -> None:
    holder, other = make_locks(), make_locks()
    assert holder.try_lock("reap") is True
    assert other.try_lock("reap") is False

    holder.close()  # what a dying process does to its connection

    assert other.try_lock("reap") is True


def test_distinct_tasks_do_not_block_each_other(make_locks: MakeLocks) -> None:
    first, second = make_locks(), make_locks()

    assert first.try_lock("reap") is True
    assert second.try_lock("poll_channels") is True
