"""Tests for common/repo/videos.py (issue #14)."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from common.models import VideoMeta
from common.repo.channels import add_channel, list_active_channels
from common.repo.videos import mark_unavailable, upsert_video


def make_meta(**overrides: object) -> VideoMeta:
    defaults: dict[str, object] = {
        "video_id": "abc12345678",
        "channel_id": "UCabc",
        "title": "Title",
        "description": "Desc",
        "duration_sec": 120,
        "published_at": datetime(2026, 1, 1, tzinfo=UTC),
        "language": "en",
        "live_status": None,
        "manual_subtitle_langs": (),
        "auto_caption_langs": (),
    }
    defaults.update(overrides)
    return VideoMeta(**defaults)  # type: ignore[arg-type]


def test_upsert_video_inserts_a_new_video(conn: psycopg.Connection) -> None:
    upsert_video(conn, make_meta(), origin="adhoc")

    row = conn.execute(
        """
        SELECT video_id, channel_id, title, duration_sec, published_at,
               description, origin, unavailable
        FROM videos WHERE video_id = %s
        """,
        ("abc12345678",),
    ).fetchone()
    assert row == (
        "abc12345678",
        "UCabc",
        "Title",
        120,
        datetime(2026, 1, 1, tzinfo=UTC),
        "Desc",
        "adhoc",
        None,
    )


def test_upsert_video_updates_title_duration_published_and_description(
    conn: psycopg.Connection,
) -> None:
    upsert_video(conn, make_meta(), origin="adhoc")

    updated = make_meta(
        title="New title",
        duration_sec=999,
        published_at=datetime(2026, 2, 2, tzinfo=UTC),
        description="New desc",
    )
    upsert_video(conn, updated, origin="rss")

    row = conn.execute(
        "SELECT title, duration_sec, published_at, description FROM videos WHERE video_id = %s",
        ("abc12345678",),
    ).fetchone()
    assert row == (
        "New title",
        999,
        datetime(2026, 2, 2, tzinfo=UTC),
        "New desc",
    )


def test_upsert_video_never_changes_origin_or_discovered_at_on_update(
    conn: psycopg.Connection,
) -> None:
    upsert_video(conn, make_meta(), origin="adhoc")
    before = conn.execute(
        "SELECT discovered_at, origin FROM videos WHERE video_id = %s", ("abc12345678",)
    ).fetchone()

    upsert_video(conn, make_meta(title="Changed"), origin="rss")

    after = conn.execute(
        "SELECT discovered_at, origin FROM videos WHERE video_id = %s", ("abc12345678",)
    ).fetchone()
    assert after == before


def test_upsert_video_creates_an_inactive_channel_placeholder_when_missing(
    conn: psycopg.Connection,
) -> None:
    upsert_video(conn, make_meta(channel_id="UCnew"), origin="adhoc")

    row = conn.execute(
        "SELECT active FROM channels WHERE channel_id = %s", ("UCnew",)
    ).fetchone()
    assert row == (False,)
    assert list_active_channels(conn) == []  # ad-hoc submission does not start monitoring


def test_upsert_video_leaves_an_existing_channel_row_untouched(conn: psycopg.Connection) -> None:
    add_channel(conn, "UCexisting", "Existing")

    upsert_video(conn, make_meta(channel_id="UCexisting"), origin="adhoc")

    [channel] = list_active_channels(conn)
    assert channel.channel_id == "UCexisting"
    assert channel.active is True


def test_upsert_video_strips_nul_from_title_and_description(conn: psycopg.Connection) -> None:
    upsert_video(
        conn, make_meta(title="bad\x00title", description="bad\x00desc"), origin="adhoc"
    )

    row = conn.execute(
        "SELECT title, description FROM videos WHERE video_id = %s", ("abc12345678",)
    ).fetchone()
    assert row == ("badtitle", "baddesc")


@pytest.mark.parametrize("reason", ["removed", "private", "geoblocked", "agegated"])
def test_mark_unavailable_accepts_each_valid_reason(
    conn: psycopg.Connection, reason: str
) -> None:
    upsert_video(conn, make_meta(), origin="adhoc")

    mark_unavailable(conn, "abc12345678", reason)

    row = conn.execute(
        "SELECT unavailable FROM videos WHERE video_id = %s", ("abc12345678",)
    ).fetchone()
    assert row == (reason,)


def test_mark_unavailable_rejects_any_other_value(conn: psycopg.Connection) -> None:
    upsert_video(conn, make_meta(), origin="adhoc")

    with pytest.raises(ValueError):
        mark_unavailable(conn, "abc12345678", "banned")
