"""Unfollow against RSS polling and gap reconciliation, forced both ways (issue #235).

Each pair of orders is made to happen every time, not hoped for. The unfollow
side is held open before its commit, the planner pass is started and observed
blocked on it via ``pg_blocking_pids`` (a bounded wait, no repeat loops), and
only then is the holder committed. The reverse order holds the planner's
insert transaction open inside ``enqueue`` while the unfollow is started and
observed blocked on it.
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

from common.models import CatalogEntry, ChannelCatalog, FeedEntry
from common.queue import PostgresQueue
from common.repo.channels import deactivate_channel
from services.api.backfill import unfollow_channel
from services.planner.poll import poll_channels
from services.planner.reconcile import reconcile_channels

pytestmark = pytest.mark.integration

CHANNEL = "UC" + "a" * 22
CUTOFF = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
LATER = CUTOFF + timedelta(days=1)
DEADLINE_SEC = 30.0
KNOWN = "known000001"
NEW = "newvideo001"


def _wait_until(predicate: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + DEADLINE_SEC
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"never happened within {DEADLINE_SEC}s: {what}")


class _Feed:
    """Returns one new entry; ``on_fetch`` runs after the channel list was read."""

    def __init__(self, on_fetch: Callable[[], None]) -> None:
        self.on_fetch = on_fetch

    def fetch(self, channel_id: str) -> list[FeedEntry]:
        self.on_fetch()
        return [FeedEntry(NEW, channel_id, "New", LATER, False)]


class _Catalog:
    """Lists the new video above an already known one (the anchor)."""

    def __init__(self, on_list: Callable[[], None]) -> None:
        self.on_list = on_list

    def list_uploads(self, channel_id: str, *, limit: int) -> ChannelCatalog:
        self.on_list()
        entries = (CatalogEntry(NEW, "New", 60), CatalogEntry(KNOWN, "Known", 60))
        return ChannelCatalog(channel_id, entries, len(entries))


class _HeldQueue(PostgresQueue):
    """A queue that waits inside ``enqueue``, i.e. inside the insert transaction."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        super().__init__(conn)
        self.reached = threading.Event()
        self.release = threading.Event()

    def enqueue(self, *args: Any, **kwargs: Any) -> int | None:
        self.reached.set()
        if not self.release.wait(DEADLINE_SEC):
            raise AssertionError("the planner insert was never released")
        return super().enqueue(*args, **kwargs)


@pytest.fixture
def dsn(head_dsn: str) -> str:
    with psycopg.connect(head_dsn) as setup:
        setup.execute(
            "INSERT INTO channels (channel_id, active, monitor_from) VALUES (%s, true, %s)",
            (CHANNEL, CUTOFF),
        )
        setup.execute(
            "INSERT INTO videos (video_id, channel_id, published_at, origin)"
            " VALUES (%s, %s, %s, 'rss')",
            (KNOWN, CHANNEL, LATER),
        )
    return head_dsn


@pytest.fixture
def conns(dsn: str) -> Iterator[tuple[psycopg.Connection[Any], ...]]:
    """planner, unfollower, observer."""
    with (
        psycopg.connect(dsn) as planner,
        psycopg.connect(dsn) as unfollower,
        psycopg.connect(dsn, autocommit=True) as observer,
    ):
        yield planner, unfollower, observer


def _blocked_by(observer: psycopg.Connection[Any], waiter: int, holder: int) -> Callable[[], bool]:
    def check() -> bool:
        row = observer.execute(
            "SELECT %s = ANY(pg_blocking_pids(%s))", (holder, waiter)
        ).fetchone()
        return bool(row and row[0])

    return check


def _state(observer: psycopg.Connection[Any]) -> tuple[bool, list[str], list[str]]:
    active = observer.execute("SELECT active FROM channels").fetchone()
    assert active is not None
    videos = [r[0] for r in observer.execute("SELECT video_id FROM videos ORDER BY 1")]
    jobs = [r[0] for r in observer.execute("SELECT video_id FROM jobs ORDER BY 1")]
    return bool(active[0]), videos, jobs


Run = Callable[[psycopg.Connection[Any], Callable[[], None], PostgresQueue], object]


def _poll(conn: psycopg.Connection[Any], hook: Callable[[], None], queue: PostgresQueue) -> object:
    return poll_channels(conn, _Feed(hook), queue)


def _reconcile(
    conn: psycopg.Connection[Any], hook: Callable[[], None], queue: PostgresQueue
) -> object:
    return reconcile_channels(conn, _Catalog(hook), queue, clock=lambda: 0.0)


@pytest.mark.parametrize("run", [_poll, _reconcile], ids=["poll", "reconcile"])
def test_an_unfollow_held_open_blocks_the_insert_and_the_pass_adds_nothing(
    run: Run, conns: tuple[psycopg.Connection[Any], ...]
) -> None:
    planner, unfollower, observer = conns

    def unfollow_without_commit() -> None:
        # Runs after the planner read its channel list, before it writes.
        assert deactivate_channel(unfollower, CHANNEL) is True

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, planner, unfollow_without_commit, PostgresQueue(planner))
        _wait_until(
            _blocked_by(observer, planner.info.backend_pid, unfollower.info.backend_pid),
            "the planner insert blocked behind the open unfollow",
        )
        assert not future.done()
        unfollower.commit()
        future.result(DEADLINE_SEC)

    assert _state(observer) == (False, [KNOWN], [])


@pytest.mark.parametrize("run", [_poll, _reconcile], ids=["poll", "reconcile"])
def test_an_insert_that_commits_first_keeps_its_video_and_job_after_the_unfollow(
    run: Run, conns: tuple[psycopg.Connection[Any], ...]
) -> None:
    planner, unfollower, observer = conns
    held = _HeldQueue(planner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        planner_future = pool.submit(run, planner, lambda: None, held)
        _wait_until(held.reached.is_set, "the planner insert reached its enqueue")
        unfollow_future = pool.submit(unfollow_channel, unfollower, CHANNEL)
        _wait_until(
            _blocked_by(observer, unfollower.info.backend_pid, planner.info.backend_pid),
            "the unfollow blocked behind the planner insert",
        )
        assert not unfollow_future.done()
        held.release.set()
        planner_future.result(DEADLINE_SEC)
        assert unfollow_future.result(DEADLINE_SEC).changed is True

    assert _state(observer) == (False, [KNOWN, NEW], [NEW])


# --- the other guarded writes: poll health and the no_anchor marker -------------------


class _HookedFeed:
    def __init__(self, on_fetch: Callable[[], None], *, fail: bool) -> None:
        self.on_fetch = on_fetch
        self.fail = fail

    def fetch(self, channel_id: str) -> list[FeedEntry]:
        self.on_fetch()
        if self.fail:
            raise RuntimeError("feed down")
        return []


def _poll_failure(
    conn: psycopg.Connection[Any], hook: Callable[[], None], queue: PostgresQueue
) -> object:
    return poll_channels(conn, _HookedFeed(hook, fail=True), queue)


def _poll_success_without_entries(
    conn: psycopg.Connection[Any], hook: Callable[[], None], queue: PostgresQueue
) -> object:
    return poll_channels(conn, _HookedFeed(hook, fail=False), queue)


class _NoAnchorCatalog:
    def __init__(self, on_list: Callable[[], None]) -> None:
        self.on_list = on_list

    def list_uploads(self, channel_id: str, *, limit: int) -> ChannelCatalog:
        self.on_list()
        entries = (CatalogEntry("other000001", "Unknown", 60),)
        return ChannelCatalog(channel_id, entries, 1)


def _reconcile_no_anchor(
    conn: psycopg.Connection[Any], hook: Callable[[], None], queue: PostgresQueue
) -> object:
    return reconcile_channels(conn, _NoAnchorCatalog(hook), queue, clock=lambda: 0.0)


@pytest.mark.parametrize(
    "run",
    [_poll_failure, _poll_success_without_entries, _reconcile_no_anchor],
    ids=["poll_failure", "poll_success_no_entries", "reconcile_no_anchor"],
)
def test_an_unfollow_held_open_blocks_health_and_marker_writes_which_then_write_nothing(
    run: Run, conns: tuple[psycopg.Connection[Any], ...]
) -> None:
    planner, unfollower, observer = conns

    def unfollow_without_commit() -> None:
        assert deactivate_channel(unfollower, CHANNEL) is True

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, planner, unfollow_without_commit, PostgresQueue(planner))
        _wait_until(
            _blocked_by(observer, planner.info.backend_pid, unfollower.info.backend_pid),
            "the planner write blocked behind the open unfollow",
        )
        assert not future.done()
        unfollower.commit()
        future.result(DEADLINE_SEC)

    row = observer.execute(
        "SELECT active, last_polled, last_poll_err, poll_failing_since, poll_failures,"
        " feed_gap_reason FROM channels"
    ).fetchone()
    assert row == (False, None, None, None, 0, None)
