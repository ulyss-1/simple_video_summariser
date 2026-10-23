from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from common.models import CatalogEntry, ChannelCatalog, VideoMeta
from tests.common.repo import read_seed as seed

pytestmark = pytest.mark.integration

CHANNEL = "UC" + "c" * 22
OTHER_CHANNEL = "UC" + "d" * 22
PUBLISHED = datetime(2026, 10, 20, 12, 30, tzinfo=UTC)


def _repo() -> Any:
    from common.repo import channel_catalog

    return channel_catalog


def _channel(conn: psycopg.Connection[Any], channel_id: str = CHANNEL) -> None:
    conn.execute("INSERT INTO channels (channel_id) VALUES (%s)", (channel_id,))


def _listing(*entries: CatalogEntry, total: int | None = 77) -> ChannelCatalog:
    return ChannelCatalog(CHANNEL, entries, total)


def _meta(
    video_id: str,
    *,
    title: str = "Clean title",
    description: str = "Clean description",
    published_at: datetime | None = PUBLISHED,
) -> VideoMeta:
    return VideoMeta(
        video_id=video_id,
        channel_id=CHANNEL,
        title=title,
        description=description,
        duration_sec=10,
        published_at=published_at,
        language=None,
        live_status=None,
        manual_subtitle_langs=(),
        auto_caption_langs=(),
    )


def test_snapshot_is_newest_first_and_browsing_does_not_make_videos_known(
    conn: psycopg.Connection[Any],
) -> None:
    repo = _repo()
    _channel(conn)
    listing = _listing(
        CatalogEntry("newest00001", "Newest", 10),
        CatalogEntry("older000001", "Older", 20),
    )

    repo.replace_catalog_snapshot(conn, CHANNEL, listing, limit=50)
    page = repo.get_catalog_page(conn, CHANNEL, limit=50)

    assert page is not None
    assert page.channel_id == CHANNEL
    assert page.limit == 50
    assert page.total_count == 77
    assert [(row.video_id, row.position, row.known) for row in page.items] == [
        ("newest00001", 0, False),
        ("older000001", 1, False),
    ]
    assert conn.execute("SELECT count(*) FROM videos").fetchone() == (0,)


def test_rediscovery_updates_flat_fields_but_preserves_done_metadata(
    conn: psycopg.Connection[Any],
) -> None:
    repo = _repo()
    _channel(conn)
    repo.replace_catalog_snapshot(
        conn,
        CHANNEL,
        _listing(CatalogEntry("keep0000001", "Old", 10), CatalogEntry("move0000001", "Move", 20)),
        limit=50,
    )
    assert repo.claim_enrichment(conn, batch_size=1)[0].video_id == "keep0000001"
    assert repo.complete_enrichment(
        conn,
        _meta(
            "keep0000001",
            title="Enriched",
            description="Details",
            published_at=PUBLISHED,
        ),
    )

    repo.replace_catalog_snapshot(
        conn,
        CHANNEL,
        _listing(CatalogEntry("move0000001", "Moved", 20), CatalogEntry("keep0000001", "Flat", 10)),
        limit=50,
    )
    page = repo.get_catalog_page(conn, CHANNEL, limit=50)

    assert page is not None
    assert [(row.video_id, row.position, row.title) for row in page.items] == [
        ("move0000001", 0, "Moved"),
        ("keep0000001", 1, "Flat"),
    ]
    kept = page.items[1]
    assert (kept.description, kept.published_at, kept.enrichment_state) == (
        "Details",
        PUBLISHED,
        "done",
    )


def test_expanding_snapshot_keeps_identity_and_shorter_refresh_removes_stale_rows(
    conn: psycopg.Connection[Any],
) -> None:
    repo = _repo()
    _channel(conn)
    repo.replace_catalog_snapshot(
        conn,
        CHANNEL,
        _listing(CatalogEntry("same0000001", "Same", 1)),
        limit=50,
    )
    discovered_at = conn.execute(
        "SELECT discovered_at FROM channel_catalog_entries WHERE video_id = 'same0000001'"
    ).fetchone()

    repo.replace_catalog_snapshot(
        conn,
        CHANNEL,
        _listing(CatalogEntry("same0000001", "Same again", 1), CatalogEntry("extra000001", "Extra", 2)),
        limit=200,
    )
    assert conn.execute(
        "SELECT discovered_at FROM channel_catalog_entries WHERE video_id = 'same0000001'"
    ).fetchone() == discovered_at

    repo.replace_catalog_snapshot(
        conn,
        CHANNEL,
        _listing(CatalogEntry("extra000001", "Only", 2)),
        limit=50,
    )
    assert conn.execute(
        "SELECT video_id, position, snapshot_limit FROM channel_catalog_entries"
    ).fetchall() == [("extra000001", 0, 50)]


def test_catalog_known_rows_share_all_five_library_status_values(
    conn: psycopg.Connection[Any],
) -> None:
    repo = _repo()
    expected = seed.status_matrix(conn)
    _channel(conn)
    entries = tuple(CatalogEntry(video_id, video_id, None) for video_id in expected)
    repo.replace_catalog_snapshot(conn, CHANNEL, ChannelCatalog(CHANNEL, entries, len(entries)), limit=200)

    page = repo.get_catalog_page(conn, CHANNEL, limit=200)

    assert page is not None
    actual = {row.video_id: row.processing_status for row in page.items}
    assert actual == expected
    assert set(actual.values()) == {"done", "processing", "unavailable", "failed", "idle"}
    assert all(row.known for row in page.items)


def test_claims_skip_locked_rows_increment_attempts_and_respect_batch_size(
    head_dsn: str,
) -> None:
    repo = _repo()
    with psycopg.connect(head_dsn) as setup:
        _channel(setup)
        repo.replace_catalog_snapshot(
            setup,
            CHANNEL,
            _listing(
                CatalogEntry("first000001", "First", 1),
                CatalogEntry("second00001", "Second", 2),
                CatalogEntry("third000001", "Third", 3),
            ),
            limit=50,
        )
        setup.commit()

    with psycopg.connect(head_dsn) as locker, psycopg.connect(head_dsn) as claimant:
        locker.execute(
            "SELECT 1 FROM channel_catalog_entries WHERE video_id = 'first000001' FOR UPDATE"
        )
        claims = repo.claim_enrichment(claimant, batch_size=2)
        claimant.commit()

    assert [(claim.video_id, claim.attempts) for claim in claims] == [
        ("second00001", 1),
        ("third000001", 1),
    ]
    with psycopg.connect(head_dsn) as check:
        assert check.execute(
            "SELECT video_id, enrichment_state, attempts FROM channel_catalog_entries "
            "ORDER BY position"
        ).fetchall() == [
            ("first000001", "pending", 0),
            ("second00001", "running", 1),
            ("third000001", "running", 1),
        ]


def test_completion_cleans_metadata_and_marks_only_a_running_row_done(
    conn: psycopg.Connection[Any],
) -> None:
    repo = _repo()
    _channel(conn)
    repo.replace_catalog_snapshot(
        conn, CHANNEL, _listing(CatalogEntry("clean000001", "Flat", 1)), limit=50
    )
    repo.claim_enrichment(conn, batch_size=1)

    assert repo.complete_enrichment(
        conn,
        _meta("clean000001", title="A\x00 title", description="D\x00etails"),
    )
    assert not repo.complete_enrichment(conn, _meta("clean000001", title="Again"))
    row = conn.execute(
        "SELECT title, description, published_at, enrichment_state, enrichment_error "
        "FROM channel_catalog_entries WHERE video_id = 'clean000001'"
    ).fetchone()
    assert row == ("A title", "Details", PUBLISHED, "done", None)


def test_failure_stores_only_the_supplied_exception_class_and_retry_is_failed_only(
    conn: psycopg.Connection[Any],
) -> None:
    repo = _repo()
    _channel(conn)
    repo.replace_catalog_snapshot(
        conn,
        CHANNEL,
        _listing(CatalogEntry("failed00001", "Failed", 1), CatalogEntry("pending0001", "Pending", 2)),
        limit=50,
    )
    repo.claim_enrichment(conn, batch_size=1)

    assert repo.fail_enrichment(
        conn, CHANNEL, "failed00001", error_class="ToolFailureError"
    )
    assert not repo.fail_enrichment(
        conn, CHANNEL, "failed00001", error_class="Secret upstream detail"
    )
    assert not repo.retry_enrichment(conn, CHANNEL, "pending0001")
    assert repo.retry_enrichment(conn, CHANNEL, "failed00001")
    assert not repo.retry_enrichment(conn, CHANNEL, "failed00001")
    assert conn.execute(
        "SELECT enrichment_state, enrichment_error, attempts FROM channel_catalog_entries "
        "WHERE video_id = 'failed00001'"
    ).fetchone() == ("pending", None, 1)


def test_missing_channel_or_snapshot_returns_none(conn: psycopg.Connection[Any]) -> None:
    repo = _repo()
    assert repo.get_catalog_page(conn, CHANNEL, limit=50) is None
    _channel(conn)
    assert repo.get_catalog_page(conn, CHANNEL, limit=50) is None


def test_catalog_snapshot_does_not_clear_tombstones_or_restore_deleted_videos(
    conn: psycopg.Connection[Any],
) -> None:
    repo = _repo()
    _channel(conn)
    conn.execute("INSERT INTO deleted_videos (video_id) VALUES ('deleted0001')")

    repo.replace_catalog_snapshot(
        conn, CHANNEL, _listing(CatalogEntry("deleted0001", "Deleted", 1)), limit=50
    )

    assert conn.execute(
        "SELECT video_id FROM deleted_videos WHERE video_id = 'deleted0001'"
    ).fetchone() == ("deleted0001",)
    assert conn.execute(
        "SELECT video_id FROM videos WHERE video_id = 'deleted0001'"
    ).fetchone() is None
