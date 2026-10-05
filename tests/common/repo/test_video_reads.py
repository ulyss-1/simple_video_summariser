"""``list_videos`` / ``get_video``: the library read model (issue #42)."""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from common.models import ActiveJob, JobFailure
from common.repo.videos import get_video, list_videos
from tests.common.repo import read_seed as seed

pytestmark = pytest.mark.integration

CH = "UC" + "c" * 22


def test_every_status_rule_and_precedence_case(conn: psycopg.Connection[Any]) -> None:
    expected = seed.status_matrix(conn)

    page = list_videos(conn, limit=200)

    assert {v.video_id: v.status for v in page.items} == expected
    for video_id, status in expected.items():
        detail = get_video(conn, video_id)
        assert detail is not None
        assert detail.status == status


def test_a_done_video_with_a_pending_reanalysis_shows_its_active_job(
    conn: psycopg.Connection[Any],
) -> None:
    seed.status_matrix(conn)

    detail = get_video(conn, "pr_donepend")

    assert detail is not None
    assert detail.active_job is not None
    assert (detail.active_job.kind, detail.active_job.state) == ("analyze", "pending")
    assert detail.latest_analysis_at == seed.at(1)


def test_the_active_job_earliest_in_the_pipeline_wins_then_the_oldest(
    conn: psycopg.Connection[Any],
) -> None:
    seed.video(conn, "aj000000001")
    seed.job(conn, "aj000000001", "zzz_other", "pending", created=0)
    seed.job(conn, "aj000000001", "analyze", "running", created=1)
    seed.job(conn, "aj000000001", "transcribe", "pending", created=3, dedupe_key="b")
    oldest = seed.job(conn, "aj000000001", "transcribe", "pending", created=2)
    seed.job(conn, "aj000000001", "transcribe", "pending", created=2, dedupe_key="a")

    detail = get_video(conn, "aj000000001")

    assert detail is not None
    assert detail.active_job == ActiveJob(oldest, "transcribe", "pending")


def test_ingest_beats_every_other_kind(conn: psycopg.Connection[Any]) -> None:
    seed.video(conn, "aj000000002")
    seed.job(conn, "aj000000002", "analyze", "running", created=0)
    ingest = seed.job(conn, "aj000000002", "ingest", "pending", created=9)

    detail = get_video(conn, "aj000000002")

    assert detail is not None
    assert detail.active_job == ActiveJob(ingest, "ingest", "pending")


def test_last_failure_is_the_newest_dead_job_without_last_error(
    conn: psycopg.Connection[Any],
) -> None:
    seed.video(conn, "lf000000001")
    seed.job(conn, "lf000000001", "ingest", "dead", created=1, error_class="RATE_LIMITED")
    newest = seed.job(
        conn, "lf000000001", "ingest", "dead", created=2, error_class="BUG",
        last_error="Traceback: secret", finished_at=seed.at(3),
    )
    seed.job(conn, "lf000000001", "ingest", "pending", created=4)

    detail = get_video(conn, "lf000000001")

    assert detail is not None
    assert detail.status == "processing"
    assert detail.last_failure == JobFailure(newest, "ingest", "BUG", seed.at(3))
    assert not hasattr(detail.last_failure, "last_error")


def test_two_dead_jobs_at_the_same_instant_report_the_higher_id(
    conn: psycopg.Connection[Any],
) -> None:
    seed.video(conn, "lf000000002")
    seed.job(conn, "lf000000002", "ingest", "dead", created=1, error_class="A")
    later = seed.job(conn, "lf000000002", "ingest", "dead", created=1, error_class="B")

    detail = get_video(conn, "lf000000002")

    assert detail is not None
    assert detail.last_failure is not None
    assert detail.last_failure.job_id == later


def test_no_jobs_means_no_active_job_and_no_failure(conn: psycopg.Connection[Any]) -> None:
    seed.video(conn, "nj000000001")

    detail = get_video(conn, "nj000000001")

    assert detail is not None
    assert (detail.active_job, detail.last_failure, detail.latest_analysis_at) == (None, None, None)


def test_jobs_without_a_video_row_are_not_listed(conn: psycopg.Connection[Any]) -> None:
    seed.job(conn, "orphan00001", "ingest", "dead", created=1)

    assert list_videos(conn).total == 0
    assert get_video(conn, "orphan00001") is None


def test_a_stub_row_returns_nulls(conn: psycopg.Connection[Any]) -> None:
    seed.video(conn, "stub0000001")

    [item] = list_videos(conn).items

    assert item.video_id == "stub0000001"
    assert (item.title, item.channel_id, item.channel_title, item.published_at,
            item.duration_sec, item.unavailable) == (None, None, None, None, None, None)
    assert item.origin == "adhoc"


def test_channel_title_comes_from_the_channels_row(conn: psycopg.Connection[Any]) -> None:
    seed.channel(conn, CH, "Chan")
    seed.video(conn, "ct000000001", channel_id=CH, title="T", duration_sec=5, origin="rss")

    [item] = list_videos(conn).items

    assert (item.channel_id, item.channel_title, item.title, item.duration_sec, item.origin) == (
        CH, "Chan", "T", 5, "rss",
    )


def test_order_is_total_and_pages_cover_every_video_once(conn: psycopg.Connection[Any]) -> None:
    same = datetime(2026, 5, 1, tzinfo=UTC)
    seed.video(conn, "o_newest001", published_at=datetime(2026, 6, 1, tzinfo=UTC))
    seed.video(conn, "o_same_c01x", published_at=same, discovered_at=seed.at(1))
    seed.video(conn, "o_same_b01x", published_at=same, discovered_at=seed.at(0))
    seed.video(conn, "o_same_a01x", published_at=same, discovered_at=seed.at(0))
    seed.video(conn, "o_nullpub01", published_at=None, discovered_at=seed.at(5))
    seed.video(conn, "o_nullpub02", published_at=None, discovered_at=seed.at(9))

    walked: list[str] = []
    offset = 0
    while True:
        page = list_videos(conn, offset=offset, limit=2)
        assert page.total == 6
        if not page.items:
            break
        walked.extend(v.video_id for v in page.items)
        offset += 2

    assert walked == [
        "o_newest001",
        "o_same_c01x",
        "o_same_a01x",
        "o_same_b01x",
        "o_nullpub02",
        "o_nullpub01",
    ]


def test_filters_combine_with_and(conn: psycopg.Connection[Any]) -> None:
    other = "UC" + "o" * 22
    seed.channel(conn, CH)
    seed.channel(conn, other)
    seed.video(conn, "f_in_000001", channel_id=CH, published_at=datetime(2026, 9, 1, tzinfo=UTC))
    seed.job(conn, "f_in_000001", "ingest", "pending", created=1)
    seed.video(conn, "f_idle00001", channel_id=CH, published_at=datetime(2026, 9, 1, tzinfo=UTC))
    seed.video(conn, "f_other0001", channel_id=other,
               published_at=datetime(2026, 9, 1, tzinfo=UTC))
    seed.job(conn, "f_other0001", "ingest", "pending", created=1)
    seed.video(conn, "f_early0001", channel_id=CH, published_at=datetime(2026, 8, 31, 23, 59,
                                                                          tzinfo=UTC))
    seed.job(conn, "f_early0001", "ingest", "pending", created=1)

    page = list_videos(
        conn,
        channel_id=CH,
        status="processing",
        published_after=datetime(2026, 9, 1, tzinfo=UTC),
        published_before=datetime(2026, 9, 2, tzinfo=UTC),
    )

    assert [v.video_id for v in page.items] == ["f_in_000001"]
    assert page.total == 1


def test_published_after_is_inclusive_and_before_is_exclusive(
    conn: psycopg.Connection[Any],
) -> None:
    edge = datetime(2026, 9, 1, tzinfo=UTC)
    seed.video(conn, "d_edge00001", published_at=edge)
    seed.video(conn, "d_null00001", published_at=None)

    assert [v.video_id for v in list_videos(conn, published_after=edge).items] == ["d_edge00001"]
    assert list_videos(conn, published_before=edge).total == 0
    assert list_videos(conn, published_after=edge, published_before=edge).total == 0


def test_total_counts_every_match_and_offset_beyond_total_is_empty(
    conn: psycopg.Connection[Any],
) -> None:
    for i in range(5):
        seed.video(conn, f"tot{i:08d}", discovered_at=seed.at(i))

    last = list_videos(conn, offset=4, limit=2)
    beyond = list_videos(conn, offset=5, limit=2)
    far = list_videos(conn, offset=1_000_000, limit=2)

    assert (len(last.items), last.total) == (1, 5)
    assert (beyond.items, beyond.total) == ((), 5)
    assert (far.items, far.total) == ((), 5)


def test_an_empty_database_and_an_unknown_channel_return_nothing(
    conn: psycopg.Connection[Any],
) -> None:
    assert list_videos(conn).items == ()
    assert list_videos(conn).total == 0
    seed.video(conn, "x0000000001")
    assert list_videos(conn, channel_id="UC" + "z" * 22).total == 0


def test_a_page_uses_a_fixed_number_of_statements(conn: psycopg.Connection[Any]) -> None:
    def statements(n: int) -> int:
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM videos")
        for i in range(n):
            seed.video(conn, f"n{i:010d}")
            seed.job(conn, f"n{i:010d}", "ingest", "dead", created=i)
        queries: list[str] = []

        class Spy:
            def execute(self, query: Any, params: Any = None) -> Any:
                queries.append(str(query))
                return conn.execute(query, params)

        page = list_videos(Spy(), limit=200)  # type: ignore[arg-type]
        assert len(page.items) == n
        return len(queries)

    assert statements(1) == statements(40)


def test_date_filters_do_not_depend_on_the_session_time_zone(
    conn: psycopg.Connection[Any],
) -> None:
    conn.execute("SET TimeZone = 'Asia/Tokyo'")
    seed.video(conn, "tz000000001", published_at=datetime(2026, 9, 1, tzinfo=UTC))

    tokyo_midnight = datetime(2026, 9, 1, tzinfo=zoneinfo.ZoneInfo("Asia/Tokyo"))

    assert list_videos(conn, published_after=datetime(2026, 9, 1, tzinfo=UTC)).total == 1
    assert list_videos(conn, published_before=tokyo_midnight).total == 0
