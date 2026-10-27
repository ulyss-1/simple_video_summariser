"""Channel delete against everything that could bring it back, forced both ways (issue #339).

Each pair of orders is made to happen every time, not hoped for. One side is held
open (the delete parks in its ``before_commit`` hook, a planner insert parks inside
``enqueue``), the competitor is started and observed blocked on it through
``pg_blocking_pids`` (a bounded wait, no repeat loops), and only then is the
holder released. Fakes for the feed and the catalog; no sleeps.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr

from common.backfill import ChannelNotRegistered, backfill
from common.channel_catalog import process_catalog_selection
from common.config import Settings
from common.errors import Cancelled
from common.models import CatalogEntry, ChannelCatalog, FeedEntry, VideoMeta
from common.queue import PostgresQueue
from common.repo.backfill_requests import create_request
from common.repo.deleted_videos import BulkDeleted, delete_videos
from common.repo.videos import JobGuard, guard_video_write, upsert_video
from services.api.backfill import DeleteChannelResult, delete_channel
from services.planner.backfill import run_channel_backfill
from services.planner.poll import poll_channels
from services.planner.reconcile import reconcile_channels

pytestmark = pytest.mark.integration

CHANNEL = "UC" + "a" * 22
CUTOFF = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
LATER = CUTOFF + timedelta(days=1)
DEADLINE_SEC = 30.0
KNOWN = "known000001"
SECOND = "known000002"
NEW = "newvideo001"


def _wait_until(predicate: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + DEADLINE_SEC
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"never happened within {DEADLINE_SEC}s: {what}")


def _blocked_by(observer: psycopg.Connection[Any], waiter: int, holder: int) -> Callable[[], bool]:
    def check() -> bool:
        row = observer.execute(
            "SELECT %s = ANY(pg_blocking_pids(%s))", (holder, waiter)
        ).fetchone()
        return bool(row and row[0])

    return check


class Held[T]:
    """Runs ``fn(park)`` on a thread, where ``park`` waits for ``release()`` (the ``before_commit``)."""

    def __init__(self, fn: Callable[[Callable[[], None]], T], pool: ThreadPoolExecutor) -> None:
        self.reached = threading.Event()
        self._release = threading.Event()
        self._future = pool.submit(fn, self._park)

    def _park(self) -> None:
        self.reached.set()
        if not self._release.wait(DEADLINE_SEC):
            raise AssertionError("the held transaction was never released")

    def wait_reached(self) -> None:
        assert self.reached.wait(DEADLINE_SEC), "the held transaction never reached its hook"

    def release(self) -> None:
        self._release.set()

    def result(self) -> T:
        return self._future.result(DEADLINE_SEC)


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=4) as executor:
        yield executor


@pytest.fixture
def dsn(head_dsn: str) -> str:
    with psycopg.connect(head_dsn) as setup:
        setup.execute(
            "INSERT INTO channels (channel_id, active, monitor_from) VALUES (%s, true, %s)",
            (CHANNEL, CUTOFF),
        )
        setup.execute(
            "INSERT INTO videos (video_id, channel_id, published_at, origin)"
            " VALUES (%s, %s, %s, 'rss'), (%s, %s, %s, 'adhoc')",
            (KNOWN, CHANNEL, LATER, SECOND, CHANNEL, LATER),
        )
    return head_dsn


@pytest.fixture
def conns(dsn: str) -> Iterator[tuple[psycopg.Connection[Any], ...]]:
    """other side, deleter, observer."""
    with (
        psycopg.connect(dsn) as other,
        psycopg.connect(dsn) as deleter,
        psycopg.connect(dsn, autocommit=True) as observer,
    ):
        yield other, deleter, observer


def _held_delete(
    pool: ThreadPoolExecutor, deleter: psycopg.Connection[Any]
) -> Held[DeleteChannelResult]:
    return Held(lambda park: delete_channel(deleter, CHANNEL, before_commit=park), pool)


def _state(observer: psycopg.Connection[Any]) -> dict[str, Any]:
    def col(sql: str) -> list[str]:
        return [str(r[0]) for r in observer.execute(sql)]

    return {
        "channels": col("SELECT channel_id FROM channels ORDER BY 1"),
        "videos": col("SELECT video_id FROM videos ORDER BY 1"),
        "jobs": col("SELECT video_id FROM jobs ORDER BY 1"),
        "tombstones": col("SELECT video_id FROM deleted_videos ORDER BY 1"),
        "requests": col("SELECT id FROM backfill_requests"),
    }


# --- RSS poll and gap reconciliation ----------------------------------------------------


class _Feed:
    def __init__(self, on_fetch: Callable[[], None]) -> None:
        self.on_fetch = on_fetch

    def fetch(self, channel_id: str) -> list[FeedEntry]:
        self.on_fetch()
        return [FeedEntry(NEW, channel_id, "New", LATER, False)]


class _Catalog:
    """Lists the new video above an already known one (the anchor)."""

    def __init__(self, on_list: Callable[[], None] = lambda: None) -> None:
        self.on_list = on_list

    def list_uploads(self, channel_id: str, *, limit: int) -> ChannelCatalog:
        self.on_list()
        entries = (CatalogEntry(NEW, "New", 60), CatalogEntry(KNOWN, "Known", 60))
        return ChannelCatalog(channel_id, entries, len(entries))


class _HeldQueue(PostgresQueue):
    """Waits inside ``enqueue``, i.e. inside the planner's insert transaction."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        super().__init__(conn)
        self.reached = threading.Event()
        self.release = threading.Event()

    def enqueue(self, *args: Any, **kwargs: Any) -> int | None:
        self.reached.set()
        if not self.release.wait(DEADLINE_SEC):
            raise AssertionError("the planner insert was never released")
        return super().enqueue(*args, **kwargs)


Run = Callable[[psycopg.Connection[Any], Callable[[], None], PostgresQueue], object]


def _poll(conn: psycopg.Connection[Any], hook: Callable[[], None], queue: PostgresQueue) -> object:
    return poll_channels(conn, _Feed(hook), queue)


def _reconcile(
    conn: psycopg.Connection[Any], hook: Callable[[], None], queue: PostgresQueue
) -> object:
    return reconcile_channels(conn, _Catalog(hook), queue, clock=lambda: 0.0)


@pytest.mark.parametrize("run", [_poll, _reconcile], ids=["poll", "reconcile"])
def test_a_pass_whose_list_was_read_before_the_delete_inserts_nothing_after_it(
    run: Run, conns: tuple[psycopg.Connection[Any], ...], pool: ThreadPoolExecutor
) -> None:
    planner, deleter, observer = conns
    held = _held_delete(pool, deleter)
    # The hook runs after the pass read its channel list and before it writes.
    future = pool.submit(
        run, planner, lambda: held.wait_reached(), PostgresQueue(planner)
    )
    _wait_until(
        _blocked_by(observer, planner.info.backend_pid, deleter.info.backend_pid),
        "the planner write blocked behind the open delete",
    )
    assert not future.done()

    held.release()
    result = held.result()
    future.result(DEADLINE_SEC)

    assert result.deleted_videos == 2
    assert _state(observer) == {
        "channels": [],
        "videos": [],
        "jobs": [],
        "tombstones": [KNOWN, SECOND],
        "requests": [],
    }


@pytest.mark.parametrize("run", [_poll, _reconcile], ids=["poll", "reconcile"])
def test_an_insert_that_commits_first_is_deleted_and_tombstoned_by_the_delete(
    run: Run, conns: tuple[psycopg.Connection[Any], ...], pool: ThreadPoolExecutor
) -> None:
    planner, deleter, observer = conns
    held_queue = _HeldQueue(planner)
    planner_future = pool.submit(run, planner, lambda: None, held_queue)
    assert held_queue.reached.wait(DEADLINE_SEC), "the planner insert never reached enqueue"
    delete_future = pool.submit(delete_channel, deleter, CHANNEL)
    _wait_until(
        _blocked_by(observer, deleter.info.backend_pid, planner.info.backend_pid),
        "the delete blocked behind the planner insert",
    )
    assert not delete_future.done()

    held_queue.release.set()
    planner_future.result(DEADLINE_SEC)
    result = delete_future.result(DEADLINE_SEC)

    assert result.deleted_videos == 3  # the planner's new video is read after the channel lock
    assert _state(observer) == {
        "channels": [],
        "videos": [],
        "jobs": [],
        "tombstones": sorted([KNOWN, SECOND, NEW]),
        "requests": [],
    }


# --- synchronous backfill, planner backfill slice, catalog process -----------------------


def test_a_synchronous_backfill_listed_before_the_delete_inserts_nothing_and_is_not_registered(
    conns: tuple[psycopg.Connection[Any], ...], pool: ThreadPoolExecutor
) -> None:
    other, deleter, observer = conns
    held = _held_delete(pool, deleter)

    def run() -> object:
        return backfill(
            other,
            PostgresQueue(other),
            _Catalog(lambda: held.wait_reached()),
            CHANNEL,
            limit=50,
            dry_run=False,
        )

    future = pool.submit(run)
    _wait_until(
        _blocked_by(observer, other.info.backend_pid, deleter.info.backend_pid),
        "the backfill insert blocked behind the open delete",
    )
    held.release()
    held.result()

    with pytest.raises(ChannelNotRegistered):
        future.result(DEADLINE_SEC)
    assert _state(observer)["channels"] == []
    assert _state(observer)["videos"] == []
    assert _state(observer)["jobs"] == []
    assert _state(observer)["tombstones"] == [KNOWN, SECOND]


def test_a_planner_backfill_slice_listed_before_the_delete_ends_quietly_and_records_nothing(
    dsn: str, conns: tuple[psycopg.Connection[Any], ...], pool: ThreadPoolExecutor
) -> None:
    planner, deleter, observer = conns
    create_request(planner, CHANNEL, limit_n=50, dry_run=False)
    planner.commit()
    held = _held_delete(pool, deleter)
    settings = Settings(DATABASE_URL=SecretStr(dsn))
    future = pool.submit(
        run_channel_backfill,
        planner,
        PostgresQueue(planner),
        _Catalog(lambda: held.wait_reached()),
        settings=settings,
    )
    _wait_until(
        _blocked_by(observer, planner.info.backend_pid, deleter.info.backend_pid),
        "the slice insert blocked behind the open delete",
    )
    held.release()
    held.result()

    assert future.result(DEADLINE_SEC) == 0
    assert _state(observer) == {
        "channels": [],
        "videos": [],
        "jobs": [],
        "tombstones": [KNOWN, SECOND],
        "requests": [],
    }


def test_a_catalog_selection_behind_the_delete_answers_not_registered_and_queues_nothing(
    conns: tuple[psycopg.Connection[Any], ...], pool: ThreadPoolExecutor
) -> None:
    other, deleter, observer = conns
    observer.execute(
        "INSERT INTO channel_catalog_entries (channel_id, video_id, position, snapshot_limit,"
        " total_count, title) VALUES (%s, %s, 0, 50, 1, 'New')",
        (CHANNEL, NEW),
    )
    held = _held_delete(pool, deleter)
    held.wait_reached()
    future = pool.submit(process_catalog_selection, other, PostgresQueue(other), CHANNEL, [NEW])
    _wait_until(
        _blocked_by(observer, other.info.backend_pid, deleter.info.backend_pid),
        "the catalog selection blocked behind the open delete",
    )

    held.release()
    held.result()

    with pytest.raises(ChannelNotRegistered):
        future.result(DEADLINE_SEC)
    state = _state(observer)
    assert (state["channels"], state["videos"], state["jobs"]) == ([], [], [])
    assert NEW not in state["tombstones"]


# --- an ingest of one of the channel's videos ---------------------------------------------


def _meta(video_id: str) -> VideoMeta:
    return VideoMeta(
        video_id=video_id,
        channel_id=CHANNEL,
        title="t",
        duration_sec=1,
        published_at=None,
        description="",
        language=None,
        live_status="not_live",
        manual_subtitle_langs=(),
        auto_caption_langs=(),
    )


def _running_ingest(observer: psycopg.Connection[Any]) -> tuple[int, str]:
    """A claimed ``ingest`` job of ``KNOWN`` held by worker ``w1``."""
    job_id = PostgresQueue(observer).enqueue("ingest", KNOWN)
    assert job_id is not None
    observer.execute(
        "UPDATE jobs SET state = 'running', locked_by = 'w1', locked_at = now(),"
        " heartbeat_at = now() WHERE id = %s",
        (job_id,),
    )
    return job_id, "w1"


def test_an_ingest_parked_after_its_metadata_fetch_writes_nothing_once_the_channel_is_deleted(
    conns: tuple[psycopg.Connection[Any], ...], pool: ThreadPoolExecutor
) -> None:
    worker, deleter, observer = conns
    job_id, locked_by = _running_ingest(observer)
    held = _held_delete(pool, deleter)
    held.wait_reached()
    write = pool.submit(
        upsert_video, worker, _meta(KNOWN), "adhoc", guard=JobGuard(job_id, locked_by)
    )
    _wait_until(
        _blocked_by(observer, worker.info.backend_pid, deleter.info.backend_pid),
        "the guarded ingest write blocked behind the open delete",
    )

    held.release()
    held.result()

    with pytest.raises(Cancelled):
        write.result(DEADLINE_SEC)
    state = _state(observer)
    assert (state["channels"], state["videos"]) == ([], [])  # no videos row, no channels stub
    request = observer.execute(
        "SELECT cancel_requested_at IS NOT NULL FROM jobs WHERE id = %s", (job_id,)
    ).fetchone()
    assert request == (True,)  # the worker ends the job CANCELLED at its next checkpoint


def test_an_ingest_write_that_commits_first_does_not_deadlock_and_the_delete_removes_it(
    conns: tuple[psycopg.Connection[Any], ...], pool: ThreadPoolExecutor
) -> None:
    worker, deleter, observer = conns
    job_id, locked_by = _running_ingest(observer)
    guard_video_write(worker, KNOWN, JobGuard(job_id, locked_by))  # holds the job lock
    delete_future = pool.submit(delete_channel, deleter, CHANNEL)
    _wait_until(
        _blocked_by(observer, deleter.info.backend_pid, worker.info.backend_pid),
        "the delete blocked behind the ingest's job lock",
    )

    # The delete now holds the channel row FOR NO KEY UPDATE. The ingest's write takes
    # KEY SHARE on it through the videos foreign key; FOR UPDATE here would deadlock.
    worker.execute("SELECT 1 FROM channels WHERE channel_id = %s FOR KEY SHARE", (CHANNEL,))
    upsert_video(worker, _meta(KNOWN), "adhoc")
    worker.commit()

    result = delete_future.result(DEADLINE_SEC)

    assert result.deleted_videos == 2
    state = _state(observer)
    assert (state["channels"], state["videos"]) == ([], [])
    assert KNOWN in state["tombstones"]


# --- against POST /videos/delete ------------------------------------------------------------


def test_a_channel_delete_held_open_makes_a_bulk_delete_wait_and_find_the_video_gone(
    conns: tuple[psycopg.Connection[Any], ...], pool: ThreadPoolExecutor
) -> None:
    other, deleter, observer = conns
    held = _held_delete(pool, deleter)
    held.wait_reached()
    bulk = pool.submit(delete_videos, other, [KNOWN])
    _wait_until(
        _blocked_by(observer, other.info.backend_pid, deleter.info.backend_pid),
        "the bulk delete blocked behind the channel delete",
    )

    held.release()
    result = held.result()
    bulk_result: BulkDeleted = bulk.result(DEADLINE_SEC)

    assert result.deleted_videos == 2
    assert (bulk_result.deleted, bulk_result.not_found) == ((), (KNOWN,))
    assert _state(observer)["tombstones"] == [KNOWN, SECOND]


def test_a_bulk_delete_held_open_makes_the_channel_delete_wait_and_skip_that_video(
    conns: tuple[psycopg.Connection[Any], ...], pool: ThreadPoolExecutor
) -> None:
    other, deleter, observer = conns
    held_bulk = Held(lambda park: delete_videos(other, [KNOWN], before_commit=park), pool)
    held_bulk.wait_reached()
    channel_future = pool.submit(delete_channel, deleter, CHANNEL)
    _wait_until(
        _blocked_by(observer, deleter.info.backend_pid, other.info.backend_pid),
        "the channel delete blocked behind the bulk delete",
    )

    held_bulk.release()
    bulk_result = held_bulk.result()
    result = channel_future.result(DEADLINE_SEC)

    assert bulk_result.deleted == (KNOWN,)
    assert result.deleted_videos == 1  # KNOWN was deleted once, by the bulk call
    state = _state(observer)
    assert (state["channels"], state["videos"]) == ([], [])
    assert state["tombstones"] == [KNOWN, SECOND]
