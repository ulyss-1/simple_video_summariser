"""Unfollow and ``GET /channels/{id}`` end to end on Postgres (issue #235).

Real ``get_conn`` and real SQL. Counts stay small: one or two channels, up to
three videos.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from common.config import get_settings
from common.models import FeedEntry
from common.queue import PRIORITY_BACKFILL, PostgresQueue
from common.repo.backfill_requests import create_request, request_backfill_id
from services.api.main import create_app
from services.planner.poll import poll_channels

pytestmark = pytest.mark.integration

CHANNEL = "UCuAXFkgsw1L7xaCfnd5JJOw"
OTHER = "UCxxxxxxxxxxxxxxxxxxxxxx"
CUTOFF = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
LATER = CUTOFF + timedelta(days=1)


@pytest.fixture
def db(head_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[psycopg.Connection[Any]]:
    monkeypatch.setenv("DATABASE_URL", head_dsn)
    get_settings.cache_clear()
    with psycopg.connect(head_dsn, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO channels (channel_id, title, active, monitor_from, last_polled,
                                  last_poll_err, poll_failing_since, poll_failures,
                                  feed_gap_detected_at, feed_gap_reason)
            VALUES (%(c)s, 'Followed', true, %(m)s, %(m)s, 'boom', %(m)s, 3, %(m)s, 'suspected'),
                   (%(o)s, 'Other', true, %(m)s, %(m)s, 'boom', %(m)s, 2, %(m)s, 'no_anchor')
            """,
            {"c": CHANNEL, "o": OTHER, "m": CUTOFF},
        )
        yield conn


@pytest.fixture
def api(db: psycopg.Connection[Any]) -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


def _video(db: psycopg.Connection[Any], video_id: str, channel: str = CHANNEL) -> None:
    db.execute(
        "INSERT INTO videos (video_id, channel_id, origin, published_at) VALUES (%s, %s, 'rss', %s)",
        (video_id, channel, LATER),
    )


def _job(
    db: psycopg.Connection[Any],
    video_id: str,
    *,
    kind: str = "ingest",
    state: str = "pending",
    priority: int = 0,
    backfill_id: str | None = None,
) -> int:
    payload = {} if backfill_id is None else {"backfill_id": backfill_id}
    row = db.execute(
        """
        INSERT INTO jobs (video_id, kind, state, priority, payload, run_after,
                          locked_by, locked_at, heartbeat_at)
        VALUES (%(v)s, %(k)s, %(s)s, %(p)s, %(pl)s, now(),
                CASE WHEN %(s)s = 'running' THEN 'w1' END,
                CASE WHEN %(s)s = 'running' THEN now() END,
                CASE WHEN %(s)s = 'running' THEN now() END)
        RETURNING id
        """,
        {"v": video_id, "k": kind, "s": state, "p": priority, "pl": Jsonb(payload)},
    ).fetchone()
    assert row is not None
    return int(row[0])


def _snapshot(db: psycopg.Connection[Any], channel: str) -> dict[str, list[tuple[Any, ...]]]:
    return {
        "channel": db.execute("SELECT * FROM channels WHERE channel_id = %s", (channel,)).fetchall(),
        "videos": db.execute("SELECT * FROM videos WHERE channel_id = %s ORDER BY 1", (channel,)).fetchall(),
        "jobs": db.execute(
            "SELECT j.* FROM jobs j JOIN videos v USING (video_id) WHERE v.channel_id = %s"
            " ORDER BY j.id",
            (channel,),
        ).fetchall(),
        "requests": db.execute(
            "SELECT * FROM backfill_requests WHERE channel_id = %s ORDER BY id", (channel,)
        ).fetchall(),
    }


def _unfollow(api: TestClient, channel: str = CHANNEL) -> Any:
    return api.post(f"/channels/{channel}/unfollow", json={})


def test_unfollowing_an_active_channel_deactivates_it_and_resets_its_health_only(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    _video(db, "v0000000001")
    other_before = _snapshot(db, OTHER)

    response = _unfollow(api)

    assert response.status_code == 200
    assert response.json() == {
        "channel_id": CHANNEL,
        "active": False,
        "changed": True,
        "backfill": None,
    }
    row = db.execute(
        """
        SELECT title, active, monitor_from, last_polled, added_at, last_poll_err,
               poll_failing_since, poll_failures, feed_gap_detected_at, feed_gap_reason
        FROM channels WHERE channel_id = %s
        """,
        (CHANNEL,),
    ).fetchone()
    assert row is not None
    assert row[:4] == ("Followed", False, CUTOFF, CUTOFF)
    assert row[5:] == (None, None, 0, None, None)
    assert _snapshot(db, OTHER) == other_before


def test_an_unfollowed_channel_leaves_the_attention_lists(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    before = api.get("/ops/channels", params={"attention": "true"}).json()
    assert CHANNEL in str(before)

    _unfollow(api)

    after = api.get("/ops/channels", params={"attention": "true"}).json()
    assert CHANNEL not in str(after)
    assert OTHER in str(after)


def test_unfollow_ends_the_active_request_like_a_cancel_and_keeps_everything_else(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    outcome = create_request(db, CHANNEL, limit_n=5000, dry_run=False)
    bid = request_backfill_id(outcome.request_id)
    for video in ("v0000000001", "v0000000002", "v0000000003"):
        _video(db, video)
    pending = _job(db, "v0000000001", priority=PRIORITY_BACKFILL, backfill_id=bid)
    running = _job(db, "v0000000002", state="running", priority=PRIORITY_BACKFILL, backfill_id=bid)
    rss = _job(db, "v0000000003", priority=0)
    analyze = _job(db, "v0000000003", kind="analyze", priority=PRIORITY_BACKFILL, backfill_id=bid)

    response = _unfollow(api)

    assert response.status_code == 200
    assert response.json()["backfill"] == {"backfill_id": bid, "cancelled_jobs": 1}
    jobs = {r[0]: r[1] for r in db.execute("SELECT id, state FROM jobs")}
    assert jobs == {running: "running", rss: "pending", analyze: "pending"}
    assert pending not in jobs
    state = db.execute(
        "SELECT state, error_class FROM backfill_requests WHERE id = %s", (outcome.request_id,)
    ).fetchone()
    assert state == ("failed", "CANCELLED")
    assert {r[0] for r in db.execute("SELECT video_id FROM videos")} >= {
        "v0000000002",
        "v0000000003",
    }


def test_unfollow_reports_the_same_numbers_as_the_cancel_route(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    bids = []
    for channel, videos in ((CHANNEL, ("v0000000001", "v0000000002")), (OTHER, ("w0000000001", "w0000000002"))):
        outcome = create_request(db, channel, limit_n=50, dry_run=False)
        bid = request_backfill_id(outcome.request_id)
        bids.append(bid)
        for video in videos:
            _video(db, video, channel)
            _job(db, video, priority=PRIORITY_BACKFILL, backfill_id=bid)

    unfollowed = _unfollow(api, CHANNEL).json()["backfill"]
    cancelled = api.post(f"/channels/{OTHER}/backfill/cancel", json={"backfill_id": bids[1]}).json()

    assert unfollowed["cancelled_jobs"] == cancelled["cancelled"] == 2


def test_unfollowing_does_not_touch_videos_transcripts_or_other_jobs(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    for video in ("v0000000001", "v0000000002"):
        _video(db, video)
    _job(db, "v0000000001")
    _job(db, "v0000000001", kind="transcribe", state="running")
    _job(db, "v0000000002", kind="analyze", state="dead")
    db.execute("INSERT INTO deleted_videos (video_id) VALUES ('d0000000001')")
    before = _snapshot(db, CHANNEL)

    _unfollow(api)

    after = _snapshot(db, CHANNEL)
    assert after["videos"] == before["videos"]
    assert after["jobs"] == before["jobs"]
    assert db.execute("SELECT video_id FROM deleted_videos").fetchall() == [("d0000000001",)]


def test_repeating_the_unfollow_changes_nothing_and_still_succeeds(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    _unfollow(api)
    before = _snapshot(db, CHANNEL)

    response = _unfollow(api)

    assert response.status_code == 200
    assert response.json() == {
        "channel_id": CHANNEL,
        "active": False,
        "changed": False,
        "backfill": None,
    }
    assert _snapshot(db, CHANNEL) == before


def test_an_inactive_stub_with_a_request_has_the_request_ended_and_changed_false(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    db.execute("UPDATE channels SET active = false WHERE channel_id = %s", (CHANNEL,))
    outcome = create_request(db, CHANNEL, limit_n=50, dry_run=False)

    body = _unfollow(api).json()

    assert body["changed"] is False
    assert body["backfill"] == {
        "backfill_id": request_backfill_id(outcome.request_id),
        "cancelled_jobs": 0,
    }
    assert db.execute("SELECT state FROM backfill_requests").fetchall() == [("failed",)]


def test_an_unknown_channel_is_404_and_writes_nothing(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    unknown = "UC" + "z" * 22

    response = _unfollow(api, unknown)

    assert response.status_code == 404
    assert db.execute("SELECT count(*) FROM channels").fetchone() == (2,)


def test_unfollow_then_refollow_then_poll_is_forward_only(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    _unfollow(api)
    gap = FeedEntry("gap00000001", CHANNEL, "Gap", LATER, False)  # published while unfollowed
    fresh = FeedEntry(
        "new00000001", CHANNEL, "New", datetime.now(UTC) + timedelta(days=1), False
    )

    class Feed:
        def fetch(self, channel_id: str) -> list[FeedEntry]:
            return [fresh, gap] if channel_id == CHANNEL else []

    with psycopg.connect(get_settings().DATABASE_URL.get_secret_value()) as planner:
        assert poll_channels(planner, Feed(), PostgresQueue(planner)).videos_discovered == 0

        refollow = api.post("/channels", json={"url": CHANNEL})
        assert refollow.status_code == 200  # the row exists, so it is not created again
        row = db.execute(
            "SELECT active, monitor_from > %s FROM channels WHERE channel_id = %s",
            (CUTOFF, CHANNEL),
        ).fetchone()
        assert row == (True, True)

        result = poll_channels(planner, Feed(), PostgresQueue(planner))

    assert result.videos_discovered == 1  # only the entry after the new monitor_from
    assert db.execute("SELECT video_id FROM videos").fetchall() == [("new00000001",)]


def test_get_channel_returns_state_and_video_count_for_an_unfollowed_channel(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    _video(db, "v0000000001")
    _video(db, "v0000000002")
    _unfollow(api)

    response = api.get(f"/channels/{CHANNEL}")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "channel_id": CHANNEL,
        "title": "Followed",
        "active": False,
        "monitor_from": "2026-03-01T12:00:00Z",
        "added_at": body["added_at"],
        "video_count": 2,
    }
    assert api.get(f"/channels/{OTHER}").json()["video_count"] == 0


def test_get_channel_unknown_is_404_and_malformed_is_422_without_echo(api: TestClient) -> None:
    assert api.get("/channels/UC" + "z" * 22).status_code == 404
    bad = api.get("/channels/UC-ZZMARKER42")
    assert bad.status_code == 422
    assert "ZZMARKER42" not in bad.text
