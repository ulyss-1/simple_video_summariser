"""The ingest handler against a ``DELETE`` that lands while it runs (issue #303).

Real Postgres, the real queue claim and the real ``delete_video``. The handler is
parked after its metadata fetch (an event, not a sleep), the video is deleted, and the
handler is released: the guard around ``upsert_video`` / ``record_unavailable`` must
refuse the write.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest

from common.errors import Cancelled, PermanentSourceError, UnavailableReason
from common.models import VideoMeta
from common.queue import PostgresQueue
from common.repo.deleted_videos import delete_video
from common.repo.videos import JobGuard, record_unavailable, upsert_video
from services.transcriber.ingest import make_ingest_handler
from tests.services.transcriber.fakes import (
    MANUAL_ID,
    FakeMetadata,
    FakeSubtitles,
    fixture_meta,
    make_ctx,
    make_settings,
    manual_segments,
)

pytestmark = pytest.mark.integration

DEADLINE_SEC = 10.0


class ParkedMetadata(FakeMetadata):
    """Signals when the fetch is reached and returns only after ``release`` is set."""

    def __init__(self, result: VideoMeta | BaseException) -> None:
        super().__init__(result)
        self.reached = threading.Event()
        self.release = threading.Event()

    def fetch(self, video_id: str) -> VideoMeta:
        self.reached.set()
        assert self.release.wait(DEADLINE_SEC), "the parked fetch was never released"
        return super().fetch(video_id)


@pytest.fixture
def connect(head_dsn: str) -> Iterator[Callable[[], psycopg.Connection[Any]]]:
    opened: list[psycopg.Connection[Any]] = []

    def _connect() -> psycopg.Connection[Any]:
        connection = psycopg.connect(head_dsn)
        opened.append(connection)
        return connection

    yield _connect
    for connection in opened:
        connection.close()


def run_parked_ingest(
    connect: Callable[[], psycopg.Connection[Any]], metadata: ParkedMetadata
) -> tuple[threading.Thread, list[BaseException]]:
    """Claim the one pending ingest job on a thread and run the real handler on it."""
    settings = make_settings()
    handler_conn = connect()
    queue = PostgresQueue(connect(), settings=settings)
    handler = make_ingest_handler(
        conn=handler_conn,
        queue=queue,
        metadata=metadata,
        subtitles=FakeSubtitles(manual=manual_segments()),
        settings=settings,
    )
    ctx, _logs = make_ctx()
    errors: list[BaseException] = []

    def work() -> None:
        try:
            with queue.claim(["ingest"], worker="w1") as job:
                assert job is not None
                handler(job, ctx)
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            errors.append(exc)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, errors


def seed(conn: psycopg.Connection[Any], video_id: str) -> int:
    conn.execute("INSERT INTO videos (video_id, origin) VALUES (%s, 'rss')", (video_id,))
    row = conn.execute(
        "INSERT INTO jobs (video_id, kind, payload) VALUES (%s, 'ingest', %s::jsonb) RETURNING id",
        (video_id, '{"origin": "rss"}'),
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def test_a_delete_during_the_metadata_fetch_leaves_no_video_no_channel_no_successor_and_a_cancelled_job(
    connect: Callable[[], psycopg.Connection[Any]],
) -> None:
    admin = connect()
    job_id = seed(admin, MANUAL_ID)
    metadata = ParkedMetadata(fixture_meta("manual_en", MANUAL_ID))
    thread, errors = run_parked_ingest(connect, metadata)
    assert metadata.reached.wait(DEADLINE_SEC), "the handler never reached its metadata fetch"

    delete_video(admin, MANUAL_ID)  # no lock to wait for: the handler holds none while it fetches
    metadata.release.set()
    thread.join(DEADLINE_SEC)

    assert not thread.is_alive(), "the ingest handler never finished"
    assert errors == []  # a cancelled job ends inside claim(); the worker carries on
    assert admin.execute("SELECT count(*) FROM videos").fetchone() == (0,)
    assert admin.execute("SELECT count(*) FROM channels").fetchone() == (0,)  # no stub channel
    assert admin.execute("SELECT video_id FROM deleted_videos").fetchall() == [(MANUAL_ID,)]
    assert admin.execute("SELECT id, state, error_class, last_error FROM jobs").fetchall() == [
        (job_id, "dead", "CANCELLED", "cancelled on request")
    ]  # and no successor job


def test_a_delete_before_a_permanent_error_is_recorded_does_not_bring_the_video_back(
    connect: Callable[[], psycopg.Connection[Any]],
) -> None:
    admin = connect()
    job_id = seed(admin, MANUAL_ID)
    metadata = ParkedMetadata(PermanentSourceError(UnavailableReason.REMOVED, "gone"))
    thread, errors = run_parked_ingest(connect, metadata)
    assert metadata.reached.wait(DEADLINE_SEC), "the handler never reached its metadata fetch"

    delete_video(admin, MANUAL_ID)
    metadata.release.set()
    thread.join(DEADLINE_SEC)

    assert not thread.is_alive(), "the ingest handler never finished"
    assert errors == []
    assert admin.execute("SELECT count(*) FROM videos").fetchone() == (0,)
    assert admin.execute("SELECT video_id FROM deleted_videos").fetchall() == [(MANUAL_ID,)]
    assert admin.execute(
        "SELECT state, error_class FROM jobs WHERE id = %s", (job_id,)
    ).fetchone() == ("dead", "CANCELLED")


def test_a_pending_job_enqueued_after_the_delete_ends_non_retryable_without_a_video_row(
    connect: Callable[[], psycopg.Connection[Any]],
) -> None:
    """A planner task that selected the video just before the delete enqueues for a gone video."""
    admin = connect()
    admin.execute("INSERT INTO deleted_videos (video_id) VALUES (%s)", (MANUAL_ID,))
    admin.execute("INSERT INTO jobs (video_id, kind) VALUES (%s, 'ingest')", (MANUAL_ID,))
    admin.commit()
    metadata = ParkedMetadata(fixture_meta("manual_en", MANUAL_ID))
    metadata.release.set()
    thread, errors = run_parked_ingest(connect, metadata)
    thread.join(DEADLINE_SEC)

    assert not thread.is_alive(), "the ingest handler never finished"
    assert errors == []
    assert admin.execute("SELECT count(*) FROM videos").fetchone() == (0,)
    # Dead on the first attempt: no retry, and PERMANENT_SOURCE raises no alert (8.4).
    assert admin.execute("SELECT state, error_class, attempts FROM jobs").fetchone() == (
        "dead",
        "PERMANENT_SOURCE",
        1,
    )
    assert admin.execute("SELECT count(*) FROM deleted_videos").fetchone() == (1,)


def test_the_unguarded_callers_keep_working_without_a_job(
    connect: Callable[[], psycopg.Connection[Any]],
) -> None:
    """The CLI path passes no guard: ``upsert_video`` and ``record_unavailable`` write as before."""
    conn = connect()

    upsert_video(conn, fixture_meta("manual_en", MANUAL_ID), "adhoc")
    record_unavailable(conn, "abcdefghijk", "removed", origin="adhoc")
    conn.commit()

    assert conn.execute("SELECT count(*) FROM videos").fetchone() == (2,)


def test_a_guard_for_a_job_the_worker_no_longer_holds_raises_cancelled_and_leaves_no_lock(
    connect: Callable[[], psycopg.Connection[Any]],
) -> None:
    admin = connect()
    job_id = seed(admin, MANUAL_ID)  # still pending: not held by anyone
    conn = connect()

    with pytest.raises(Cancelled):
        upsert_video(
            conn, fixture_meta("manual_en", MANUAL_ID), "adhoc", guard=JobGuard(job_id, "w1")
        )

    assert conn.info.transaction_status.name == "IDLE"  # rolled back: the lock is released
