"""Tests for ``get_video_meta`` in common/repo/videos.py (issue #30)."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from common.models import VideoMeta
from common.repo import videos as videos_repo
from common.repo.videos import record_unavailable, upsert_video

pytestmark = pytest.mark.integration

PUBLISHED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_get_video_meta_returns_the_stored_fields_and_none_for_the_rest(
    conn: psycopg.Connection,
) -> None:
    meta = VideoMeta(
        video_id="abcdefghijk",
        channel_id="UC" + "a" * 22,
        title="A title",
        description="A description",
        duration_sec=321,
        published_at=PUBLISHED,
        language="en",
        live_status="not_live",
        manual_subtitle_langs=("en",),
        auto_caption_langs=("en", "de"),
    )
    upsert_video(conn, meta, "adhoc")

    got = videos_repo.get_video_meta(conn, "abcdefghijk")

    assert got == VideoMeta(
        video_id="abcdefghijk",
        channel_id="UC" + "a" * 22,
        title="A title",
        description="A description",
        duration_sec=321,
        published_at=PUBLISHED,
        language=None,
        live_status=None,
        manual_subtitle_langs=(),
        auto_caption_langs=(),
    )


def test_get_video_meta_is_none_for_an_unknown_video(conn: psycopg.Connection) -> None:
    assert videos_repo.get_video_meta(conn, "nosuchvideo") is None


def test_get_video_meta_turns_null_columns_into_empty_strings(
    conn: psycopg.Connection,
) -> None:
    record_unavailable(conn, "stubvideo01", "removed", origin="adhoc")

    got = videos_repo.get_video_meta(conn, "stubvideo01")

    assert got is not None
    assert (got.channel_id, got.title, got.description) == ("", "", "")
    assert got.duration_sec is None
    assert got.published_at is None
