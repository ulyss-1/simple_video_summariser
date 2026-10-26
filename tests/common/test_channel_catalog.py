"""Channel catalog discovery and selected processing (issue #242), on Postgres.

The catalog source is a fake and the queue is the real ``PostgresQueue``, so the
rows and jobs the bulk action writes are read back from the database. Counts
stay at a handful of entries; the named ranges are exercised through the limit
argument, not by seeding thousands of rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from common.backfill import ChannelNotRegistered
from common.channel_catalog import (
    BulkCatalogResult,
    discover_catalog,
    process_catalog_selection,
)
from common.errors import PermanentSourceError, ToolFailureError, UnavailableReason
from common.models import CatalogEntry, ChannelCatalog
from common.queue import PRIORITY_BACKFILL, PostgresQueue

pytestmark = pytest.mark.integration

CHANNEL = "UC" + "c" * 22
OTHER = "UC" + "d" * 22
A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"


class FakeCatalog:
    def __init__(self, *entries: CatalogEntry, error: Exception | None = None) -> None:
        self.entries = entries
        self.error = error
        self.calls: list[tuple[str, int]] = []

    def list_uploads(self, channel_id: str, *, limit: int) -> ChannelCatalog:
        self.calls.append((channel_id, limit))
        if self.error is not None:
            raise self.error
        return ChannelCatalog(channel_id, self.entries, 99)


@pytest.fixture
def db(head_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(head_dsn, autocommit=True) as connection:
        connection.execute("INSERT INTO channels (channel_id) VALUES (%s)", (CHANNEL,))
        yield connection


def _seed(db: psycopg.Connection[Any], *ids: str) -> None:
    entries = tuple(CatalogEntry(i, f"T {i}", None) for i in ids)
    discover_catalog(db, FakeCatalog(*entries), CHANNEL, limit=50)


def _count(db: psycopg.Connection[Any], table: str) -> int:
    row = db.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


# --- discovery ------------------------------------------------------------------


@pytest.mark.parametrize("limit", [50, 200, 500, 5000])
def test_discover_accepts_only_the_named_ranges_and_passes_the_limit_on(
    db: psycopg.Connection[Any], limit: int
) -> None:
    catalog = FakeCatalog(CatalogEntry(A, "Newest", 1), CatalogEntry(B, "Older", 2))

    page = discover_catalog(db, catalog, CHANNEL, limit=limit)

    assert catalog.calls == [(CHANNEL, limit)]
    assert (page.limit, page.total_count) == (limit, 99)
    assert [(i.video_id, i.position, i.known) for i in page.items] == [(A, 0, False), (B, 1, False)]
    assert all(i.description is None and i.enrichment_state == "pending" for i in page.items)
    assert _count(db, "videos") == 0 and _count(db, "jobs") == 0


@pytest.mark.parametrize("limit", [0, 49, 51, 4999, 5001, True, "50"])
def test_discover_rejects_other_limits_before_listing(
    db: psycopg.Connection[Any], limit: Any
) -> None:
    catalog = FakeCatalog(CatalogEntry(A, "x", None))

    with pytest.raises(ValueError, match="catalog limit"):
        discover_catalog(db, catalog, CHANNEL, limit=limit)

    assert catalog.calls == []


def test_discover_unregistered_channel_never_lists(db: psycopg.Connection[Any]) -> None:
    catalog = FakeCatalog(CatalogEntry(A, "x", None))

    with pytest.raises(ChannelNotRegistered):
        discover_catalog(db, catalog, OTHER, limit=50)

    assert catalog.calls == []


def test_discover_treats_a_removed_listing_as_empty_and_clears_the_snapshot(
    db: psycopg.Connection[Any],
) -> None:
    _seed(db, A)
    gone = FakeCatalog(error=PermanentSourceError(UnavailableReason.REMOVED))

    page = discover_catalog(db, gone, CHANNEL, limit=50)

    assert (page.items, page.total_count) == ((), None)
    assert _count(db, "channel_catalog_entries") == 0


def test_discover_propagates_a_tool_failure_and_keeps_the_old_snapshot(
    db: psycopg.Connection[Any],
) -> None:
    _seed(db, A)

    with pytest.raises(ToolFailureError):
        discover_catalog(db, FakeCatalog(error=ToolFailureError("boom")), CHANNEL, limit=50)

    assert _count(db, "channel_catalog_entries") == 1


def test_discover_drops_non_canonical_and_duplicate_ids(db: psycopg.Connection[Any]) -> None:
    catalog = FakeCatalog(
        CatalogEntry(A, "ok", None),
        CatalogEntry("bad id", "nope", None),
        CatalogEntry(A, "dup", None),
        CatalogEntry("-wNyEUrxzFU", "dash", None),
    )

    page = discover_catalog(db, catalog, CHANNEL, limit=50)

    assert [i.video_id for i in page.items] == [A, "-wNyEUrxzFU"]
    assert [i.position for i in page.items] == [0, 1]


def test_discover_cuts_an_overshooting_listing_to_the_range(
    db: psycopg.Connection[Any],
) -> None:
    """One past the smallest range: an adapter that ignores ``limit`` cannot grow the snapshot."""
    ids = [f"vid{n:08d}" for n in range(51)]
    catalog = FakeCatalog(*(CatalogEntry(i, i, None) for i in ids))

    page = discover_catalog(db, catalog, CHANNEL, limit=50)

    assert [i.video_id for i in page.items] == ids[:50]
    assert _count(db, "channel_catalog_entries") == 50


def test_discover_marks_library_videos_and_tombstones_known(db: psycopg.Connection[Any]) -> None:
    db.execute(
        "INSERT INTO videos (video_id, channel_id, origin) VALUES (%s, %s, 'rss')", (A, CHANNEL)
    )
    db.execute("INSERT INTO deleted_videos (video_id) VALUES (%s)", (B,))
    catalog = FakeCatalog(*(CatalogEntry(i, i, None) for i in (A, B, C)))

    page = discover_catalog(db, catalog, CHANNEL, limit=50)

    assert [(i.video_id, i.known, i.processing_status) for i in page.items] == [
        (A, True, "idle"),
        (B, True, None),
        (C, False, None),
    ]


def test_discover_requires_an_idle_connection(db: psycopg.Connection[Any]) -> None:
    with db.transaction(), pytest.raises(RuntimeError, match="no open transaction"):
        discover_catalog(db, FakeCatalog(), CHANNEL, limit=50)


# --- bulk -----------------------------------------------------------------------


def test_bulk_queues_unknown_rows_with_backfill_semantics_in_request_order(
    db: psycopg.Connection[Any],
) -> None:
    _seed(db, A, B, C)

    results = process_catalog_selection(db, PostgresQueue(db), CHANNEL, [C, A])

    assert [(r.video_id, r.outcome, r.reason) for r in results] == [
        (C, "queued", None),
        (A, "queued", None),
    ]
    assert db.execute(
        "SELECT video_id, origin, channel_id FROM videos ORDER BY video_id"
    ).fetchall() == [(A, "backfill", CHANNEL), (C, "backfill", CHANNEL)]
    rows = db.execute(
        "SELECT id, video_id, kind, priority, payload FROM jobs ORDER BY id"
    ).fetchall()
    assert [(r[1], r[2], r[3], r[4]) for r in rows] == [
        (C, "ingest", PRIORITY_BACKFILL, {"origin": "backfill"}),
        (A, "ingest", PRIORITY_BACKFILL, {"origin": "backfill"}),
    ]
    assert [r.job_id for r in results] == [rows[0][0], rows[1][0]]


def test_bulk_rejects_an_id_not_in_this_channel_catalog(db: psycopg.Connection[Any]) -> None:
    _seed(db, A)

    results = process_catalog_selection(db, PostgresQueue(db), CHANNEL, [B])

    assert results == (BulkCatalogResult(B, "rejected", None, "not_in_catalog"),)
    assert _count(db, "videos") == 0 and _count(db, "jobs") == 0


def test_bulk_rejects_an_id_that_only_another_channel_lists(db: psycopg.Connection[Any]) -> None:
    db.execute("INSERT INTO channels (channel_id) VALUES (%s)", (OTHER,))
    discover_catalog(db, FakeCatalog(CatalogEntry(B, "theirs", None)), OTHER, limit=50)
    _seed(db, A)

    [result] = process_catalog_selection(db, PostgresQueue(db), CHANNEL, [B])

    assert result.outcome == "rejected"
    assert _count(db, "videos") == 0


def test_bulk_second_submission_is_already_known_and_writes_nothing(
    db: psycopg.Connection[Any],
) -> None:
    _seed(db, A)
    process_catalog_selection(db, PostgresQueue(db), CHANNEL, [A])

    [again] = process_catalog_selection(db, PostgresQueue(db), CHANNEL, [A])

    assert again == BulkCatalogResult(A, "already_known", None, None)
    assert _count(db, "videos") == 1 and _count(db, "jobs") == 1


def test_bulk_reports_a_video_added_after_discovery_as_already_known(
    db: psycopg.Connection[Any],
) -> None:
    _seed(db, A, B)
    db.execute(
        "INSERT INTO videos (video_id, channel_id, origin) VALUES (%s, %s, 'rss')", (A, CHANNEL)
    )

    results = process_catalog_selection(db, PostgresQueue(db), CHANNEL, [A, B])

    assert [(r.video_id, r.outcome) for r in results] == [(A, "already_known"), (B, "queued")]
    assert _count(db, "jobs") == 1


def test_bulk_does_not_resurrect_a_deleted_video(db: psycopg.Connection[Any]) -> None:
    _seed(db, A)
    db.execute("INSERT INTO deleted_videos (video_id) VALUES (%s)", (A,))

    [result] = process_catalog_selection(db, PostgresQueue(db), CHANNEL, [A])

    assert result.outcome == "already_known"
    assert _count(db, "videos") == 0 and _count(db, "jobs") == 0
    assert _count(db, "deleted_videos") == 1


@pytest.mark.parametrize(
    "ids",
    [[], [A, A], ["short"], [A, "x" * 12], [A, B, C]],
    ids=["empty", "duplicate", "malformed", "long", "over-cap"],
)
def test_bulk_validates_the_whole_input_before_any_write(
    db: psycopg.Connection[Any], ids: list[str]
) -> None:
    _seed(db, A, B, C)

    with pytest.raises(ValueError):
        process_catalog_selection(db, PostgresQueue(db), CHANNEL, ids, max_selected=2)

    assert _count(db, "videos") == 0 and _count(db, "jobs") == 0


def test_bulk_unregistered_channel(db: psycopg.Connection[Any]) -> None:
    with pytest.raises(ChannelNotRegistered):
        process_catalog_selection(db, PostgresQueue(db), OTHER, [A])


def test_bulk_never_returns_queued_without_a_job_id(db: psycopg.Connection[Any]) -> None:
    """A no-op enqueue (an active ingest job already exists) reports that job's ID."""
    _seed(db, A)
    existing = PostgresQueue(db).enqueue("ingest", A, payload={}, priority=0)

    class Collides:
        def enqueue(self, *args: Any, **kwargs: Any) -> int | None:
            return None

    [result] = process_catalog_selection(db, Collides(), CHANNEL, [A])  # type: ignore[arg-type]

    assert (result.outcome, result.job_id) == ("queued", existing)
