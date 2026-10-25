"""A user's cancel request in the queue and the repo (issue #237).

Every outcome write checks ``jobs.cancel_requested_at`` in its own ``UPDATE``: a
request turns any outcome other than success into the terminal ``dead`` /
``CANCELLED`` one. The interleavings against other writers are in
``test_cancel_races.py``; this module is the single-connection behaviour.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from common.config import Settings
from common.errors import (
    Cancelled,
    Defer,
    ErrorClass,
    PermanentSourceError,
    TransientNetworkError,
)
from common.queue import HeartbeatResult, Job, PostgresQueue
from common.repo.deleted_videos import VideoNotFound
from common.repo.jobs import cancel_video, get_job, list_jobs
from tests.common.repo.conftest import head_dsn

pytestmark = pytest.mark.integration

__all__ = ["head_dsn"]

VID = "dQw4w9WgXcQ"
OTHER = "abcdefghijk"


@pytest.fixture
def conn(head_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(head_dsn, autocommit=True) as connection:
        yield connection


@pytest.fixture
def queue(conn: psycopg.Connection[Any]) -> PostgresQueue:
    settings = Settings(DATABASE_URL="postgresql://u:p@h/db")  # type: ignore[arg-type]
    return PostgresQueue(conn, settings=settings, rng=random.Random(1))


def seed_video(conn: psycopg.Connection[Any], video_id: str = VID) -> None:
    conn.execute("INSERT INTO videos (video_id) VALUES (%s)", (video_id,))


def row(conn: psycopg.Connection[Any], job_id: int) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        found = cur.fetchone()
    assert found is not None
    return found


def request_cancel(conn: psycopg.Connection[Any], job_id: int) -> None:
    conn.execute("UPDATE jobs SET cancel_requested_at = now() WHERE id = %s", (job_id,))


def claim_one(queue: PostgresQueue, kind: str = "ingest") -> Any:
    manager = queue.claim([kind], worker="w1")
    job = manager.__enter__()
    assert job is not None
    return manager, job


def assert_cancelled_outcome(stored: dict[str, Any], *, attempts: int) -> None:
    assert stored["state"] == "dead"
    assert stored["error_class"] == "CANCELLED"
    assert stored["last_error"] == "cancelled on request"
    assert stored["finished_at"] is not None
    assert stored["locked_by"] is None
    assert stored["locked_at"] is None
    assert stored["progress"] is None
    assert stored["attempts"] == attempts
    assert stored["cancel_requested_at"] is not None


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_tells_held_from_cancel_requested_from_lost(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None
    manager, _job = claim_one(queue)

    assert queue.heartbeat(job_id, "w1") is HeartbeatResult.HELD
    request_cancel(conn, job_id)
    assert queue.heartbeat(job_id, "w1") is HeartbeatResult.CANCEL_REQUESTED
    assert queue.heartbeat(job_id, "w2") is HeartbeatResult.LOST  # another worker's job
    manager.__exit__(None, None, None)
    assert queue.heartbeat(job_id, "w1") is HeartbeatResult.LOST  # no longer running


# ---------------------------------------------------------------------------
# every outcome path with a request ends CANCELLED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raised",
    [
        Cancelled(),
        PermanentSourceError("removed", "gone"),
        TransientNetworkError("blip"),
        RuntimeError("a bug"),
        Defer(datetime(2030, 1, 1, tzinfo=UTC)),
    ],
    ids=["Cancelled", "PermanentSource", "TransientNetwork", "unclassified", "Defer"],
)
def test_a_request_turns_every_non_success_outcome_into_the_cancelled_one(
    conn: psycopg.Connection[Any], queue: PostgresQueue, raised: BaseException
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("transcribe", VID)
    assert job_id is not None
    conn.execute(
        "UPDATE jobs SET progress = '{\"percent\": 5}' WHERE id = %s", (job_id,)
    )

    with queue.claim(
        ["transcribe"], worker="w1"
    ) as job:  # the worker carries on: no exception
        assert job is not None
        request_cancel(conn, job_id)
        raise raised

    assert_cancelled_outcome(row(conn, job_id), attempts=1)


def test_a_shutdown_signal_with_a_request_still_ends_cancelled_and_propagates(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None

    with pytest.raises(KeyboardInterrupt), queue.claim(["ingest"], worker="w1"):
        request_cancel(conn, job_id)
        raise KeyboardInterrupt

    assert_cancelled_outcome(row(conn, job_id), attempts=1)


def test_a_shutdown_cancel_without_a_request_still_hands_the_job_back(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None

    with pytest.raises(Cancelled), queue.claim(["ingest"], worker="w1"):
        raise Cancelled

    stored = row(conn, job_id)
    assert (stored["state"], stored["attempts"], stored["error_class"]) == (
        "pending",
        0,
        None,
    )


def test_fail_job_with_a_request_ends_cancelled(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None
    _manager, job = claim_one(queue)
    request_cancel(conn, job_id)

    queue.fail_job(job, "w1", RuntimeError("hung"))

    assert_cancelled_outcome(row(conn, job_id), attempts=1)


def test_a_handler_that_returns_normally_after_the_request_ends_done_and_keeps_the_request(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("analyze", VID)
    assert job_id is not None

    with queue.claim(["analyze"], worker="w1"):
        request_cancel(conn, job_id)

    stored = row(conn, job_id)
    assert (stored["state"], stored["error_class"], stored["last_error"]) == (
        "done",
        None,
        None,
    )
    assert stored["cancel_requested_at"] is not None


def test_a_cancel_is_logged_once_at_info_and_never_as_a_dead_letter(
    conn: psycopg.Connection[Any],
    queue: PostgresQueue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None

    with caplog.at_level(logging.DEBUG), queue.claim(["ingest"], worker="w1"):
        request_cancel(conn, job_id)
        raise PermanentSourceError("removed", "gone")

    cancelled = [r for r in caplog.records if r.getMessage() == "job.cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0].levelno == logging.INFO
    assert (cancelled[0].job_id, cancelled[0].video_id, cancelled[0].kind) == (
        job_id,
        VID,
        "ingest",
    )
    assert not [r for r in caplog.records if r.getMessage() == "job dead-lettered"]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_classified_failure_keeps_the_videos_unavailable_mark_the_handler_committed(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None

    with queue.claim(["ingest"], worker="w1"):
        request_cancel(conn, job_id)
        conn.execute(
            "UPDATE videos SET unavailable = 'removed' WHERE video_id = %s", (VID,)
        )
        raise PermanentSourceError("removed", "gone")

    assert row(conn, job_id)["error_class"] == "CANCELLED"
    assert conn.execute(
        "SELECT unavailable FROM videos WHERE video_id = %s", (VID,)
    ).fetchone() == ("removed",)


# ---------------------------------------------------------------------------
# the reaper
# ---------------------------------------------------------------------------


def test_the_reaper_logs_a_cancelled_job_once_at_info_and_not_as_a_dead_letter(
    conn: psycopg.Connection[Any],
    queue: PostgresQueue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None
    _manager, _job = claim_one(queue)
    request_cancel(conn, job_id)
    conn.execute(
        "UPDATE jobs SET heartbeat_at = now() - interval '1 hour' WHERE id = %s",
        (job_id,),
    )

    with caplog.at_level(logging.DEBUG):
        assert queue.reap_stale(60) == 1

    assert_cancelled_outcome(row(conn, job_id), attempts=1)
    assert [r.getMessage() for r in caplog.records if r.name == "common.errors"] == [
        "job.cancelled"
    ]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# the successor guard
# ---------------------------------------------------------------------------


def test_handoff_without_a_request_still_enqueues_the_successor(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    assert queue.enqueue("ingest", VID) is not None
    _manager, parent = claim_one(queue)

    successor = queue.handoff(parent.id, "transcribe", VID, priority=3)

    assert successor is not None
    assert row(conn, successor)["state"] == "pending"


def test_handoff_with_a_request_raises_cancelled_and_inserts_nothing(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    assert queue.enqueue("ingest", VID) is not None
    _manager, parent = claim_one(queue)
    request_cancel(conn, parent.id)

    with pytest.raises(Cancelled):
        queue.handoff(parent.id, "transcribe", VID)

    assert conn.execute("SELECT count(*) FROM jobs").fetchone() == (1,)


def test_handoff_errors_for_a_missing_video_or_foreign_parent_are_unchanged(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    assert queue.enqueue("ingest", VID) is not None
    _manager, parent = claim_one(queue)

    with pytest.raises(ValueError, match="no video"):
        queue.handoff(parent.id, "transcribe", OTHER)
    seed_video(conn, OTHER)
    with pytest.raises(ValueError, match="does not belong"):
        queue.handoff(parent.id, "transcribe", OTHER)


# ---------------------------------------------------------------------------
# resuming a cancelled job
# ---------------------------------------------------------------------------


def _cancelled_job(conn: psycopg.Connection[Any], queue: PostgresQueue) -> int:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None
    with queue.claim(["ingest"], worker="w1"):
        request_cancel(conn, job_id)
        raise Cancelled
    return job_id


def test_retrying_a_cancelled_job_clears_the_request_so_it_does_not_end_again_at_once(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    job_id = _cancelled_job(conn, queue)

    outcome = queue.retry_dead(job_id)

    assert outcome.status == "retried"
    assert row(conn, job_id)["cancel_requested_at"] is None
    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        assert job.cancel_requested_at is None
    stored = row(conn, job_id)
    assert (stored["state"], stored["error_class"]) == ("done", None)


def test_the_bulk_retry_accepts_cancelled_jobs_by_class_and_clears_the_request(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    job_id = _cancelled_job(conn, queue)

    result = queue.retry_dead_matching(error_class=ErrorClass.CANCELLED.value, limit=5)

    assert result.job_ids == [job_id]
    stored = row(conn, job_id)
    assert (stored["state"], stored["cancel_requested_at"]) == ("pending", None)


# ---------------------------------------------------------------------------
# cancel_video
# ---------------------------------------------------------------------------


def test_cancel_video_handles_pending_and_running_jobs_and_leaves_finished_ones(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    running = queue.enqueue("ingest", VID)
    assert running is not None
    _manager, _job = claim_one(queue)
    pending = queue.enqueue("analyze", VID, dedupe_key="v1:fake")
    assert pending is not None
    done = conn.execute(
        "INSERT INTO jobs (video_id, kind, state, finished_at) VALUES (%s, 'analyze', 'done', now())"
        " RETURNING id",
        (VID,),
    ).fetchone()
    dead = conn.execute(
        "INSERT INTO jobs (video_id, kind, state, error_class, finished_at)"
        " VALUES (%s, 'transcribe', 'dead', 'BUG', now()) RETURNING id",
        (VID,),
    ).fetchone()
    assert done is not None and dead is not None
    untouched = {j: row(conn, j) for j in (done[0], dead[0])}

    entries = cancel_video(conn, VID)

    assert [(e.job_id, e.kind, e.outcome) for e in entries] == [
        (running, "ingest", "cancel_requested"),
        (pending, "analyze", "cancelled"),
    ]
    assert row(conn, running)["state"] == "running"
    assert row(conn, running)["cancel_requested_at"] is not None
    stored_pending = row(conn, pending)
    assert (
        stored_pending["state"],
        stored_pending["error_class"],
        stored_pending["last_error"],
        stored_pending["attempts"],
        stored_pending["finished_at"] is not None,
    ) == ("dead", "CANCELLED", "cancelled on request", 0, True)
    assert {j: row(conn, j) for j in untouched} == untouched


def test_repeating_the_request_reports_the_running_job_again_and_keeps_the_first_timestamp(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None
    _manager, _job = claim_one(queue)

    first = cancel_video(conn, VID)
    stamp = row(conn, job_id)["cancel_requested_at"]
    second = cancel_video(conn, VID)

    assert [e.outcome for e in first] == ["cancel_requested"]
    assert [e.outcome for e in second] == ["cancel_requested"]
    assert row(conn, job_id)["cancel_requested_at"] == stamp


def test_cancel_video_with_no_active_job_is_an_empty_list_and_an_unknown_video_raises(
    conn: psycopg.Connection[Any],
) -> None:
    seed_video(conn)

    assert cancel_video(conn, VID) == []
    with pytest.raises(VideoNotFound):
        cancel_video(conn, OTHER)


def test_cancel_video_leaves_other_videos_alone(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    seed_video(conn, OTHER)
    mine = queue.enqueue("ingest", VID)
    theirs = queue.enqueue("ingest", OTHER)
    assert mine is not None and theirs is not None

    cancel_video(conn, VID)

    assert row(conn, mine)["error_class"] == "CANCELLED"
    assert row(conn, theirs)["state"] == "pending"


def test_ops_job_rows_expose_the_request_and_filter_by_the_cancelled_class(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None
    _manager, _job = claim_one(queue)
    cancel_video(conn, VID)

    item = get_job(conn, job_id)
    assert item is not None
    assert (
        item.cancel_requested_at is not None
        and item.cancel_requested_at <= datetime.now(UTC) + timedelta(seconds=5)
    )
    assert list_jobs(conn, error_class="CANCELLED", limit=10) == []  # still running
    queue.fail_job(_job, "w1", RuntimeError("stopped"))
    assert [j.id for j in list_jobs(conn, error_class="CANCELLED", limit=10)] == [
        job_id
    ]


def test_a_job_object_carries_the_request_timestamp(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None
    request_cancel(conn, job_id)
    conn.execute("UPDATE jobs SET state = 'pending' WHERE id = %s", (job_id,))

    with queue.claim(["ingest"], worker="w1") as job:
        assert isinstance(job, Job)
        assert job.cancel_requested_at is not None
