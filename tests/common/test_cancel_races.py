"""Forced interleavings of a user's cancel request against everything else (issue #237).

Each race is forced, never looped (``_docs/testing-guidelines.md``, "Flaky or racy
behaviour"): one side holds its transaction open, the competing real call is started
on another connection, the test confirms through ``pg_blocking_pids`` (bounded
deadline) that it is blocked, and only then lets the holder commit or roll back.

The holder is either a real function paused by ``PausingConn`` after a chosen statement
(``cancel_video``, ``delete_video``), or a plain open transaction that does exactly the
write the competing party would do (a claim, a reap, an outcome). The call under test is
always the real one.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from common.config import Settings, get_settings
from common.errors import Cancelled, TransientNetworkError
from common.queue import _CLAIM_SQL, HeartbeatResult, Job, PostgresQueue
from common.repo.deleted_videos import (
    VideoCancelling,
    VideoNotFound,
    VideoRunning,
    delete_video,
)
from common.repo.jobs import cancel_video
from common.worker import EXIT_HUNG_HANDLER, JobContext, Worker
from services.api.main import create_app

pytestmark = pytest.mark.integration

VID = "dQw4w9WgXcQ"
DEADLINE_SEC = 8.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(DATABASE_URL="postgresql://u:p@h/db")  # type: ignore[arg-type]


@pytest.fixture
def conn(head_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(head_dsn, autocommit=True) as connection:
        yield connection


@pytest.fixture
def connect(head_dsn: str) -> Iterator[Callable[[], psycopg.Connection[Any]]]:
    opened: list[psycopg.Connection[Any]] = []

    def _connect() -> psycopg.Connection[Any]:
        connection = psycopg.connect(head_dsn, autocommit=True)
        opened.append(connection)
        return connection

    yield _connect
    for connection in opened:
        connection.close()


@pytest.fixture
def holder(head_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """A connection with an explicit transaction: statements stay open until commit()."""
    with psycopg.connect(head_dsn, autocommit=False) as connection:
        yield connection


@pytest.fixture
def queue(conn: psycopg.Connection[Any]) -> PostgresQueue:
    return PostgresQueue(conn, settings=_settings(), rng=random.Random(1))


@pytest.fixture
def api(
    head_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> Iterator[TestClient]:
    """The FastAPI app over the test database."""
    monkeypatch.setenv("DATABASE_URL", head_dsn)
    monkeypatch.setenv("AUDIO_DIR", str(tmp_path))
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client


def seed_video(conn: psycopg.Connection[Any], video_id: str = VID) -> None:
    conn.execute("INSERT INTO videos (video_id) VALUES (%s)", (video_id,))


def row(conn: psycopg.Connection[Any], job_id: int) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        found = cur.fetchone()
    assert found is not None
    return found


def active(conn: psycopg.Connection[Any], video_id: str = VID) -> list[tuple[int, str]]:
    return [
        (int(r[0]), str(r[1]))
        for r in conn.execute(
            "SELECT id, state FROM jobs WHERE video_id = %s AND state IN ('pending', 'running')"
            " ORDER BY id",
            (video_id,),
        ).fetchall()
    ]


def wait_blocked(admin: psycopg.Connection[Any], blocked_pid: int, what: str) -> None:
    """Fail unless backend ``blocked_pid`` is waiting on another transaction, within a deadline."""
    deadline = time.monotonic() + DEADLINE_SEC
    while time.monotonic() < deadline:
        found = admin.execute(
            "SELECT cardinality(pg_blocking_pids(%s))", (blocked_pid,)
        ).fetchone()
        if found is not None and found[0] > 0:
            return
        time.sleep(0.02)
    pytest.fail(f"{what}: backend {blocked_pid} never became blocked on a lock")


class Call[T]:
    """Runs ``fn`` on a thread; ``result()`` joins with a deadline and returns or re-raises."""

    def __init__(self, fn: Callable[[], T]) -> None:
        self._value: T | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self._thread.start()

    def _run(self, fn: Callable[[], T]) -> None:
        try:
            self._value = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            self._error = exc

    def result(self, what: str) -> T:
        self._thread.join(DEADLINE_SEC)
        assert not self._thread.is_alive(), f"{what} never finished"
        if self._error is not None:
            raise self._error
        return self._value  # type: ignore[return-value]


class PausingConn:
    """Delegates to a real connection and pauses once, right after a statement matching ``when``.

    It exposes only ``execute`` and ``transaction``, which is all ``cancel_video`` and
    ``delete_video`` use. While paused the open transaction keeps every lock it took.
    """

    def __init__(
        self, inner: psycopg.Connection[Any], when: Callable[[str], bool]
    ) -> None:
        self._inner = inner
        self._when = when
        self.paused = threading.Event()
        self.resume = threading.Event()
        self._done = False

    def transaction(self) -> Any:
        return self._inner.transaction()

    def execute(self, query: Any, params: Any = None) -> Any:
        result = self._inner.execute(query, params)
        if not self._done and self._when(str(query)):
            self._done = True
            if isinstance(result, psycopg.Cursor) and result.description is not None:
                result = _Buffered(result.fetchall())  # type: ignore[assignment]
            self.paused.set()
            assert self.resume.wait(DEADLINE_SEC), "the paused call was never resumed"
        return result

    def wait_paused(self, what: str) -> None:
        assert self.paused.wait(DEADLINE_SEC), f"{what} never reached its pause point"


class _Buffered:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return self._rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


def after_job_locks(sql: str) -> bool:
    return "FROM jobs" in sql and "FOR UPDATE" in sql


def claim_job(
    queue: PostgresQueue, worker: str = "w1"
) -> tuple[AbstractContextManager[Job | None], Job]:
    manager = queue.claim(["ingest", "transcribe", "analyze"], worker=worker)
    job = manager.__enter__()
    assert job is not None
    return manager, job


# ---------------------------------------------------------------------------
# cancel against a claim of a pending job
# ---------------------------------------------------------------------------


def test_claim_while_the_cancel_holds_the_pending_job_gets_nothing_and_the_job_ends_cancelled(
    conn: psycopg.Connection[Any],
    connect: Callable[[], psycopg.Connection[Any]],
    queue: PostgresQueue,
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None
    cancelling = PausingConn(connect(), after_job_locks)
    cancel = Call(lambda: cancel_video(cancelling, VID))  # type: ignore[arg-type]
    cancelling.wait_paused("cancel")

    with PostgresQueue(connect(), settings=_settings()).claim(
        ["ingest"], worker="w2"
    ) as claimed:
        assert (
            claimed is None
        )  # the row is locked by the cancel: SKIP LOCKED passes it by

    cancelling.resume.set()
    entries = cancel.result("cancel")

    assert [(e.job_id, e.outcome) for e in entries] == [(job_id, "cancelled")]
    stored = row(conn, job_id)
    assert (stored["state"], stored["error_class"], stored["locked_by"]) == (
        "dead",
        "CANCELLED",
        None,
    )
    assert stored["attempts"] == 0
    with PostgresQueue(connect(), settings=_settings()).claim(
        ["ingest"], worker="w2"
    ) as late:
        assert late is None


def test_cancel_after_the_claim_committed_asks_the_running_job_and_nothing_is_both(
    holder: psycopg.Connection[Any],
    conn: psycopg.Connection[Any],
    connect: Callable[[], psycopg.Connection[Any]],
    queue: PostgresQueue,
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None
    holder.execute(
        _CLAIM_SQL, {"kinds": ["ingest"], "worker": "w1"}
    )  # claim not yet committed
    cancelling = connect()
    cancel = Call(lambda: cancel_video(cancelling, VID))
    wait_blocked(conn, cancelling.info.backend_pid, "cancel behind the open claim")
    holder.commit()

    entries = cancel.result("cancel")

    assert [(e.job_id, e.outcome) for e in entries] == [(job_id, "cancel_requested")]
    stored = row(conn, job_id)
    assert stored["state"] == "running"
    assert stored["cancel_requested_at"] is not None
    assert stored["error_class"] is None


# ---------------------------------------------------------------------------
# cancel against handoff
# ---------------------------------------------------------------------------


def test_handoff_after_the_cancel_committed_inserts_nothing_and_raises_cancelled(
    holder: psycopg.Connection[Any],
    conn: psycopg.Connection[Any],
    connect: Callable[[], psycopg.Connection[Any]],
    queue: PostgresQueue,
) -> None:
    seed_video(conn)
    assert queue.enqueue("ingest", VID) is not None
    manager, parent = claim_job(queue)
    # What a cancel request does first: lock the video's jobs, then store the request.
    holder.execute(
        "SELECT id FROM jobs WHERE video_id = %s ORDER BY id FOR UPDATE", (VID,)
    )
    holder.execute(
        "UPDATE jobs SET cancel_requested_at = now() WHERE id = %s", (parent.id,)
    )
    handing = PostgresQueue(connect(), settings=_settings())
    call = Call(lambda: handing.handoff(parent.id, "transcribe", VID))
    wait_blocked(conn, handing._conn.info.backend_pid, "handoff behind the open cancel")
    holder.commit()

    with pytest.raises(Cancelled):
        call.result("handoff")

    assert active(conn) == [(parent.id, "running")]  # no successor
    queue.fail_job(parent, "w1", RuntimeError("the handler stopped"))
    manager.__exit__(None, None, None)
    assert active(conn) == []
    assert row(conn, parent.id)["error_class"] == "CANCELLED"


def test_cancel_after_the_handoff_committed_cancels_the_successor_in_the_same_request(
    holder: psycopg.Connection[Any],
    conn: psycopg.Connection[Any],
    connect: Callable[[], psycopg.Connection[Any]],
    queue: PostgresQueue,
) -> None:
    seed_video(conn)
    assert queue.enqueue("ingest", VID) is not None
    manager, parent = claim_job(queue)
    # What handoff does: the parent row, then the video, then the successor insert.
    holder.execute("SELECT priority FROM jobs WHERE id = %s FOR UPDATE", (parent.id,))
    holder.execute("SELECT 1 FROM videos WHERE video_id = %s FOR UPDATE", (VID,))
    successor = holder.execute(
        "INSERT INTO jobs (video_id, kind) VALUES (%s, 'transcribe') RETURNING id",
        (VID,),
    ).fetchone()
    assert successor is not None
    cancelling = connect()
    call = Call(lambda: cancel_video(cancelling, VID))
    wait_blocked(conn, cancelling.info.backend_pid, "cancel behind the open handoff")
    holder.commit()

    entries = call.result("cancel")

    assert [(e.job_id, e.outcome) for e in entries] == [
        (parent.id, "cancel_requested"),
        (successor[0], "cancelled"),
    ]
    assert active(conn) == [(parent.id, "running")]
    queue.fail_job(parent, "w1", RuntimeError("the handler stopped"))
    manager.__exit__(None, None, None)
    assert active(conn) == []


# ---------------------------------------------------------------------------
# cancel against the success outcome
# ---------------------------------------------------------------------------


def test_a_request_landing_before_the_success_outcome_leaves_the_job_done_with_the_request(
    holder: psycopg.Connection[Any],
    conn: psycopg.Connection[Any],
    connect: Callable[[], psycopg.Connection[Any]],
    queue: PostgresQueue,
) -> None:
    seed_video(conn)
    assert queue.enqueue("analyze", VID) is not None
    manager, job = claim_job(queue)
    holder.execute(
        "SELECT id FROM jobs WHERE video_id = %s ORDER BY id FOR UPDATE", (VID,)
    )
    holder.execute(
        "UPDATE jobs SET cancel_requested_at = now() WHERE id = %s", (job.id,)
    )
    finishing = Call(lambda: manager.__exit__(None, None, None))
    admin = connect()  # the queue's own connection is the one that waits
    wait_blocked(admin, _only_other_backend(admin, holder), "the success outcome")
    holder.commit()
    finishing.result("the success outcome")

    stored = row(conn, job.id)
    assert stored["state"] == "done"
    assert stored["cancel_requested_at"] is not None
    assert stored["error_class"] is None
    assert active(conn) == []


def _only_other_backend(
    admin: psycopg.Connection[Any], holder: psycopg.Connection[Any]
) -> int:
    """The backend that waits on ``holder``; there is exactly one in these tests."""
    deadline = time.monotonic() + DEADLINE_SEC
    while time.monotonic() < deadline:
        waiting = admin.execute(
            "SELECT pid FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
            (holder.info.backend_pid,),
        ).fetchall()
        if waiting:
            return int(waiting[0][0])
        time.sleep(0.02)
    pytest.fail("no backend ever waited on the holder")


def test_cancel_of_a_video_whose_job_is_already_done_answers_with_no_jobs_and_changes_nothing(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    assert queue.enqueue("analyze", VID) is not None
    manager, job = claim_job(queue)
    manager.__exit__(None, None, None)
    before = row(conn, job.id)

    assert cancel_video(conn, VID) == []

    assert row(conn, job.id) == before


# ---------------------------------------------------------------------------
# a finished job is never re-opened
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("finish_sql", "expected"),
    [
        (
            "UPDATE jobs SET state='done', finished_at=now(), locked_by=NULL WHERE id=%s",
            ("done", None),
        ),
        (
            (
                "UPDATE jobs SET state='dead', finished_at=now(), locked_by=NULL, error_class='BUG'"
                " WHERE id=%s"
            ),
            ("dead", "BUG"),
        ),
    ],
    ids=["done", "dead-BUG"],
)
def test_a_job_that_finishes_while_the_request_waits_on_its_row_is_unchanged_by_the_request(
    holder: psycopg.Connection[Any],
    conn: psycopg.Connection[Any],
    connect: Callable[[], psycopg.Connection[Any]],
    queue: PostgresQueue,
    finish_sql: str,
    expected: tuple[str, str | None],
) -> None:
    seed_video(conn)
    assert queue.enqueue("ingest", VID) is not None
    _manager, job = claim_job(queue)
    holder.execute(finish_sql, (job.id,))
    cancelling = connect()
    call = Call(lambda: cancel_video(cancelling, VID))
    wait_blocked(
        conn, cancelling.info.backend_pid, "cancel behind the finishing outcome"
    )
    holder.commit()

    assert call.result("cancel") == []

    stored = row(conn, job.id)
    assert (stored["state"], stored["error_class"]) == expected
    assert stored["cancel_requested_at"] is None


# ---------------------------------------------------------------------------
# cancel against the reaper
# ---------------------------------------------------------------------------


def _make_stale(conn: psycopg.Connection[Any], job_id: int, attempts: int = 1) -> None:
    conn.execute(
        "UPDATE jobs SET heartbeat_at = now() - interval '1 hour', attempts = %s WHERE id = %s",
        (attempts, job_id),
    )


@pytest.mark.parametrize("attempts", [1, 4], ids=["first-attempt", "last-attempt"])
def test_a_stale_running_job_with_a_request_is_reaped_to_cancelled_never_to_pending(
    conn: psycopg.Connection[Any], queue: PostgresQueue, attempts: int
) -> None:
    seed_video(conn)
    assert queue.enqueue("ingest", VID) is not None
    _manager, job = claim_job(queue)
    _make_stale(conn, job.id, attempts)
    cancel_video(conn, VID)

    assert queue.reap_stale(60) == 1

    stored = row(conn, job.id)
    assert (stored["state"], stored["error_class"]) == ("dead", "CANCELLED")
    assert stored["last_error"] == "cancelled on request"
    assert stored["attempts"] == attempts
    assert stored["locked_by"] is None


def test_a_request_stored_while_the_reaper_looks_makes_it_skip_the_job_which_a_later_reap_cancels(
    conn: psycopg.Connection[Any],
    connect: Callable[[], psycopg.Connection[Any]],
    queue: PostgresQueue,
) -> None:
    seed_video(conn)
    assert queue.enqueue("ingest", VID) is not None
    _manager, job = claim_job(queue)
    _make_stale(conn, job.id)
    cancelling = PausingConn(connect(), after_job_locks)
    cancel = Call(lambda: cancel_video(cancelling, VID))  # type: ignore[arg-type]
    cancelling.wait_paused("cancel")

    assert (
        PostgresQueue(connect(), settings=_settings()).reap_stale(60) == 0
    )  # row is locked

    cancelling.resume.set()
    assert [e.outcome for e in cancel.result("cancel")] == ["cancel_requested"]
    assert queue.reap_stale(60) == 1
    stored = row(conn, job.id)
    assert (stored["state"], stored["error_class"]) == ("dead", "CANCELLED")


def test_a_request_behind_the_reaper_finds_the_job_pending_and_cancels_it_outright(
    holder: psycopg.Connection[Any],
    conn: psycopg.Connection[Any],
    connect: Callable[[], psycopg.Connection[Any]],
    queue: PostgresQueue,
) -> None:
    seed_video(conn)
    assert queue.enqueue("ingest", VID) is not None
    _manager, job = claim_job(queue)
    holder.execute(  # what the reaper does to a job without a request
        "UPDATE jobs SET state='pending', locked_by=NULL, locked_at=NULL,"
        " run_after = now() + interval '5 minutes' WHERE id = %s",
        (job.id,),
    )
    cancelling = connect()
    call = Call(lambda: cancel_video(cancelling, VID))
    wait_blocked(conn, cancelling.info.backend_pid, "cancel behind the reaper")
    holder.commit()

    assert [(e.job_id, e.outcome) for e in call.result("cancel")] == [
        (job.id, "cancelled")
    ]
    stored = row(conn, job.id)
    assert (stored["state"], stored["error_class"]) == ("dead", "CANCELLED")


# ---------------------------------------------------------------------------
# shutdown grace period and the #66 watchdog, through a real Worker
# ---------------------------------------------------------------------------


class ManualTimers:
    """A ``timer_factory`` that never fires by itself; the test fires a timer by hand."""

    class _Timer:
        def __init__(self, interval: float, function: Callable[[], None]) -> None:
            self.interval = interval
            self.function = function
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    def __init__(self) -> None:
        self.timers: list[ManualTimers._Timer] = []

    def __call__(
        self, interval: float, function: Callable[[], None]
    ) -> ManualTimers._Timer:
        timer = ManualTimers._Timer(interval, function)
        self.timers.append(timer)
        return timer

    def fire(self, interval: float) -> None:
        [timer] = [t for t in self.timers if t.interval == interval and not t.cancelled]
        timer.function()


def test_a_request_during_the_shutdown_grace_period_ends_the_job_cancelled_not_pending(
    conn: psycopg.Connection[Any],
    connect: Callable[[], psycopg.Connection[Any]],
    queue: PostgresQueue,
    tmp_path: Any,
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None
    timers = ManualTimers()

    def handler(job: Job, ctx: JobContext) -> None:
        worker.request_shutdown()  # arms the grace timer (17 s)
        cancel_video(conn, VID)
        timers.fire(17.0)  # the grace period expires: ctx.cancelled
        assert ctx.cancelled
        ctx.check_cancelled()

    worker = Worker(
        "w1",
        ["ingest"],
        handler,
        queue,
        liveness_path=tmp_path / "hb",
        heartbeat_sec=60,
        shutdown_grace_sec=17,
        heartbeat_queue_factory=lambda: PostgresQueue(connect(), settings=_settings()),
        runtime_limit_sec=lambda job: None,
        timer_factory=timers,
        install_signal_handlers=False,
    )

    worker.run(max_iterations=1)

    stored = row(conn, job_id)
    assert (stored["state"], stored["error_class"]) == ("dead", "CANCELLED")
    assert stored["attempts"] == 1  # not refunded
    assert active(conn) == []


def test_the_watchdog_firing_on_a_job_with_a_request_ends_it_cancelled_and_still_exits_70(
    conn: psycopg.Connection[Any],
    connect: Callable[[], psycopg.Connection[Any]],
    queue: PostgresQueue,
    tmp_path: Any,
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None
    timers = ManualTimers()
    exits: list[int] = []

    def handler(job: Job, ctx: JobContext) -> None:
        cancel_video(conn, VID)
        timers.fire(99.0)  # the runtime limit expires while the handler is "wedged"
        assert row(conn, job.id)["error_class"] == "CANCELLED"

    worker = Worker(
        "w1",
        ["ingest"],
        handler,
        queue,
        liveness_path=tmp_path / "hb",
        heartbeat_sec=60,
        heartbeat_queue_factory=lambda: PostgresQueue(connect(), settings=_settings()),
        runtime_limit_sec=lambda job: 99.0,
        exit_process=exits.append,
        timer_factory=timers,
        install_signal_handlers=False,
    )

    worker.run(max_iterations=1)

    assert exits == [EXIT_HUNG_HANDLER]
    stored = row(conn, job_id)
    assert (stored["state"], stored["error_class"]) == ("dead", "CANCELLED")
    assert (
        stored["last_error"] == "cancelled on request"
    )  # not a TOOL_FAILURE, no retry
    assert stored["attempts"] == 1


# ---------------------------------------------------------------------------
# cancel against DELETE /videos/{id}
# ---------------------------------------------------------------------------


def test_delete_that_wins_with_only_pending_jobs_makes_the_cancel_answer_not_found(
    conn: psycopg.Connection[Any],
    connect: Callable[[], psycopg.Connection[Any]],
    queue: PostgresQueue,
) -> None:
    seed_video(conn)
    assert queue.enqueue("ingest", VID) is not None
    deleting = PausingConn(
        connect(), lambda sql: "FROM videos" in sql and "FOR UPDATE" in sql
    )
    delete = Call(lambda: delete_video(deleting, VID))  # type: ignore[arg-type]
    deleting.wait_paused("delete")
    cancelling = connect()
    cancel = Call(lambda: cancel_video(cancelling, VID))
    wait_blocked(conn, cancelling.info.backend_pid, "cancel behind the delete")

    deleting.resume.set()

    delete.result("delete")
    with pytest.raises(VideoNotFound):
        cancel.result("cancel")
    assert conn.execute(
        "SELECT count(*) FROM jobs WHERE video_id = %s", (VID,)
    ).fetchone() == (0,)


def test_delete_that_goes_first_with_a_running_job_is_refused_and_the_cancel_then_asks_the_job(
    conn: psycopg.Connection[Any],
    connect: Callable[[], psycopg.Connection[Any]],
    queue: PostgresQueue,
) -> None:
    seed_video(conn)
    assert queue.enqueue("ingest", VID) is not None
    _manager, job = claim_job(queue)
    deleting = PausingConn(
        connect(), lambda sql: "FROM videos" in sql and "FOR UPDATE" in sql
    )
    delete = Call(lambda: delete_video(deleting, VID))  # type: ignore[arg-type]
    deleting.wait_paused("delete")
    cancelling = connect()
    cancel = Call(lambda: cancel_video(cancelling, VID))
    wait_blocked(conn, cancelling.info.backend_pid, "cancel behind the delete")

    deleting.resume.set()

    with pytest.raises(VideoRunning) as refused:
        delete.result("delete")
    assert not isinstance(refused.value, VideoCancelling)  # no request was stored yet
    assert [(e.job_id, e.outcome) for e in cancel.result("cancel")] == [
        (job.id, "cancel_requested")
    ]


def test_cancel_that_wins_with_a_running_job_makes_the_delete_say_cancellation_in_progress(
    conn: psycopg.Connection[Any],
    connect: Callable[[], psycopg.Connection[Any]],
    queue: PostgresQueue,
) -> None:
    seed_video(conn)
    assert queue.enqueue("ingest", VID) is not None
    _manager, job = claim_job(queue)
    cancelling = PausingConn(connect(), after_job_locks)
    cancel = Call(lambda: cancel_video(cancelling, VID))  # type: ignore[arg-type]
    cancelling.wait_paused("cancel")
    deleting = connect()
    delete = Call(lambda: delete_video(deleting, VID))
    wait_blocked(conn, deleting.info.backend_pid, "delete behind the cancel")

    cancelling.resume.set()

    assert [(e.job_id, e.outcome) for e in cancel.result("cancel")] == [
        (job.id, "cancel_requested")
    ]
    with pytest.raises(VideoCancelling):
        delete.result("delete")
    assert row(conn, job.id)["state"] == "running"


def test_cancel_that_wins_with_only_pending_jobs_lets_the_delete_through(
    conn: psycopg.Connection[Any],
    connect: Callable[[], psycopg.Connection[Any]],
    queue: PostgresQueue,
) -> None:
    seed_video(conn)
    job_id = queue.enqueue("ingest", VID)
    assert job_id is not None
    cancelling = PausingConn(connect(), after_job_locks)
    cancel = Call(lambda: cancel_video(cancelling, VID))  # type: ignore[arg-type]
    cancelling.wait_paused("cancel")
    deleting = connect()
    delete = Call(lambda: delete_video(deleting, VID))
    wait_blocked(conn, deleting.info.backend_pid, "delete behind the cancel")

    cancelling.resume.set()

    assert [(e.job_id, e.outcome) for e in cancel.result("cancel")] == [
        (job_id, "cancelled")
    ]
    delete.result("delete")
    assert conn.execute(
        "SELECT count(*) FROM videos WHERE video_id = %s", (VID,)
    ).fetchone() == (0,)


# ---------------------------------------------------------------------------
# the heartbeat tick, end to end
# ---------------------------------------------------------------------------


def test_a_heartbeat_tick_delivers_the_request_the_handler_stops_and_the_worker_claims_the_next_job(
    conn: psycopg.Connection[Any],
    connect: Callable[[], psycopg.Connection[Any]],
    queue: PostgresQueue,
    api: TestClient,
    tmp_path: Any,
) -> None:
    seed_video(conn)
    seed_video(conn, "abcdefghijk")
    first = queue.enqueue("ingest", VID)
    assert first is not None
    second_ids: list[int] = []
    timers = ManualTimers()
    at_checkpoint = threading.Event()
    go_on = threading.Event()
    seen: list[str] = []
    first_ctx: list[JobContext] = []

    def handler(job: Job, ctx: JobContext) -> None:
        if job.id != first:
            seen.append("second")
            return
        first_ctx.append(ctx)
        at_checkpoint.set()
        assert go_on.wait(DEADLINE_SEC), "the handler was never released"
        ctx.check_cancelled()
        seen.append("not cancelled")  # pragma: no cover - the test would fail below

    worker = Worker(
        "w1",
        ["ingest"],
        handler,
        queue,
        liveness_path=tmp_path / "hb",
        heartbeat_sec=60,
        heartbeat_queue_factory=lambda: PostgresQueue(connect(), settings=_settings()),
        runtime_limit_sec=lambda job: None,
        timer_factory=timers,
        install_signal_handlers=False,
        sleep=lambda seconds: None,
    )
    runner = Call(lambda: worker.run(max_iterations=2))
    assert at_checkpoint.wait(DEADLINE_SEC), "the handler never reached its checkpoint"

    response = api.post(f"/videos/{VID}/cancel", json={})
    assert response.status_code == 202
    assert response.json()["jobs"] == [
        {"job_id": first, "kind": "ingest", "outcome": "cancel_requested"}
    ]
    [ctx] = first_ctx
    assert not ctx.cancelled  # the POST only stored the request; the tick delivers it
    second = queue.enqueue("ingest", "abcdefghijk")
    assert second is not None
    second_ids.append(second)
    timers.fire(60.0)  # one heartbeat tick, no sleeping
    # The flag the handler holds, observed before the handler is released.
    assert ctx._cancel_event.wait(DEADLINE_SEC), "the heartbeat tick never set ctx.cancelled"
    assert ctx.cancelled
    go_on.set()
    runner.result("the worker")

    stored = row(conn, first)
    assert (stored["state"], stored["error_class"]) == ("dead", "CANCELLED")
    assert seen == ["second"]
    assert row(conn, second_ids[0])["state"] == "done"
    assert queue.heartbeat(first, "w1") is HeartbeatResult.LOST


def test_a_retryable_failure_after_the_request_ends_cancelled_with_no_retry_scheduled(
    conn: psycopg.Connection[Any], queue: PostgresQueue
) -> None:
    seed_video(conn)
    assert queue.enqueue("ingest", VID) is not None
    _manager, job = claim_job(queue)
    cancel_video(conn, VID)

    queue.fail_job(job, "w1", TransientNetworkError("blip"))

    stored = row(conn, job.id)
    assert (stored["state"], stored["error_class"]) == ("dead", "CANCELLED")
    assert active(conn) == []
