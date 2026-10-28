"""Purge of the CANCELLED records #303 keeps for deleted videos (issue #327). Needs Postgres.

Retention is injected (seconds) and ``now`` is fixed; nothing waits on a clock.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from common.repo.jobs import purge_cancelled_jobs
from services.analyzer.orphans import PostgresOrphanStore, handle_orphans

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
RETENTION = timedelta(seconds=60)
OLD = NOW - timedelta(seconds=61)
FRESH = NOW - timedelta(seconds=59)
GONE = "goneVideo01"
LIVE = "liveVideo01"
NONE = "noneVideo01"


def _job(
    conn: psycopg.Connection[Any],
    video_id: str,
    *,
    state: str = "dead",
    error_class: str | None = "CANCELLED",
    finished_at: datetime | None = OLD,
    kind: str = "ingest",
) -> int:
    row = conn.execute(
        "INSERT INTO jobs (video_id, kind, state, error_class, finished_at, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (video_id, kind, state, error_class, finished_at, OLD if finished_at is None else NOW),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _ids(conn: psycopg.Connection[Any]) -> set[int]:
    return {int(r[0]) for r in conn.execute("SELECT id FROM jobs").fetchall()}


def _tombstone(conn: psycopg.Connection[Any], video_id: str) -> None:
    conn.execute("INSERT INTO deleted_videos (video_id) VALUES (%s)", (video_id,))


def _video(conn: psycopg.Connection[Any], video_id: str) -> None:
    conn.execute("INSERT INTO videos (video_id) VALUES (%s)", (video_id,))


def test_only_an_old_cancelled_job_of_a_tombstoned_video_without_a_row_is_purged(
    conn: psycopg.Connection[Any],
) -> None:
    _tombstone(conn, GONE)
    purged = _job(conn, GONE)
    just_inside = _job(conn, GONE, finished_at=FRESH, kind="transcribe")
    not_cancelled = _job(conn, GONE, error_class="PERMANENT", kind="analyze")
    done = _job(conn, GONE, state="done", error_class=None, kind="notify")
    # Both a live row and a tombstone: the video row wins.
    _tombstone(conn, LIVE)
    _video(conn, LIVE)
    live = _job(conn, LIVE)
    untracked = _job(conn, NONE)

    assert purge_cancelled_jobs(conn, RETENTION, now=NOW) == 1

    assert purged not in _ids(conn)
    assert _ids(conn) == {just_inside, not_cancelled, done, live, untracked}


def test_pending_and_running_jobs_are_never_purged(conn: psycopg.Connection[Any]) -> None:
    _tombstone(conn, GONE)
    pending = _job(conn, GONE, state="pending", kind="ingest")
    running = _job(conn, GONE, state="running", kind="transcribe")

    assert purge_cancelled_jobs(conn, RETENTION, now=NOW) == 0
    assert _ids(conn) == {pending, running}


def test_age_falls_back_to_created_at_when_finished_at_is_null(
    conn: psycopg.Connection[Any],
) -> None:
    _tombstone(conn, GONE)
    _job(conn, GONE, finished_at=None)

    assert purge_cancelled_jobs(conn, RETENTION, now=NOW) == 1
    assert _ids(conn) == set()


def test_exactly_at_the_retention_is_not_yet_purged(conn: psycopg.Connection[Any]) -> None:
    _tombstone(conn, GONE)
    job = _job(conn, GONE, finished_at=NOW - RETENTION)

    assert purge_cancelled_jobs(conn, RETENTION, now=NOW) == 0
    assert _ids(conn) == {job}


def test_a_purged_jobs_batch_stays_for_the_orphan_pass_which_handles_it_once(
    conn: psycopg.Connection[Any],
) -> None:
    _tombstone(conn, GONE)
    job = _job(conn, GONE, kind="analyze")
    conn.execute(
        "INSERT INTO llm_batches (job_id, step_key, batch_id, request_sha256)"
        " VALUES (%s, 'roster', 'b1', 'h')",
        (job,),
    )

    assert purge_cancelled_jobs(conn, RETENTION, now=NOW) == 1

    assert conn.execute("SELECT job_id, batch_id FROM llm_batches").fetchall() == [(None, "b1")]
    cancelled: list[str] = []

    class Canceller:
        def cancel_orphan_batch(self, batch_id: str) -> str:
            cancelled.append(batch_id)
            return "cancelled"

    for _ in range(2):
        handle_orphans(
            PostgresOrphanStore(conn),
            Canceller(),  # type: ignore[arg-type]
            limit=10,
            expired_after=timedelta(days=1),
            stop=lambda: False,
        )
    assert cancelled == ["b1"]
    assert conn.execute("SELECT count(*) FROM llm_batches").fetchone() == (0,)


def test_a_resubmitted_video_is_untouched_in_either_order(conn: psycopg.Connection[Any]) -> None:
    _tombstone(conn, GONE)
    _job(conn, GONE)
    # Purge first, then the resubmit (clears the tombstone, creates the video and a fresh ingest).
    assert purge_cancelled_jobs(conn, RETENTION, now=NOW) == 1
    conn.execute("DELETE FROM deleted_videos WHERE video_id = %s", (GONE,))
    _video(conn, GONE)
    fresh = _job(conn, GONE, state="pending", error_class=None, finished_at=None)
    assert _ids(conn) == {fresh}

    # Resubmit first (#304 deletes the old rows), then the purge finds nothing.
    assert purge_cancelled_jobs(conn, RETENTION, now=NOW) == 0
    assert _ids(conn) == {fresh}
    assert conn.execute("SELECT count(*) FROM videos WHERE video_id = %s", (GONE,)).fetchone() == (1,)


def test_a_row_locked_by_another_transaction_is_skipped_and_purged_on_a_later_run(
    head_dsn: str,
) -> None:
    with psycopg.connect(head_dsn, autocommit=True) as setup:
        _tombstone(setup, GONE)
        job = _job(setup, GONE)

    with (
        psycopg.connect(head_dsn) as holder,
        psycopg.connect(head_dsn, autocommit=True) as runner,
    ):
        holder.execute("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (job,))
        # On its own thread so a (wrong) wait shows as a bounded timeout, not a hang.
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(purge_cancelled_jobs, runner, RETENTION, now=NOW)
            assert future.result(timeout=30) == 0
        assert runner.execute("SELECT count(*) FROM jobs").fetchone() == (1,)
        holder.rollback()

        assert purge_cancelled_jobs(runner, RETENTION, now=NOW) == 1
        assert runner.execute("SELECT count(*) FROM jobs").fetchone() == (0,)


def test_a_pass_that_purged_nothing_logs_nothing_and_one_that_purged_logs_info(
    conn: psycopg.Connection[Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="common.repo.jobs")
    assert purge_cancelled_jobs(conn, RETENTION, now=NOW) == 0
    assert [r for r in caplog.records if r.message == "planner.cancelled_jobs_purged"] == []

    _tombstone(conn, GONE)
    _job(conn, GONE)
    assert purge_cancelled_jobs(conn, RETENTION, now=NOW) == 1
    records = [r for r in caplog.records if r.message == "planner.cancelled_jobs_purged"]
    assert len(records) == 1 and records[0].levelname == "INFO"
    assert records[0].purged == 1  # type: ignore[attr-defined]
