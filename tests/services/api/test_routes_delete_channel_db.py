"""``DELETE /channels/{id}`` end to end on Postgres (issue #339).

Real ``get_conn`` and real SQL. Counts stay small: two channels, up to four videos.
The forced interleavings are in ``tests/services/planner/test_delete_channel_races_db.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from common.backfill import backfill
from common.config import get_settings
from common.models import CatalogEntry, ChannelCatalog
from common.queue import PRIORITY_BACKFILL, PostgresQueue
from common.repo import deleted_videos
from common.repo.backfill_requests import create_request, request_backfill_id
from services.api import backfill as api_backfill
from services.api import routes_write
from services.api.main import create_app
from tests.services.api.conftest import LogSink

pytestmark = pytest.mark.integration

CHANNEL = "UCuAXFkgsw1L7xaCfnd5JJOw"
OTHER = "UCxxxxxxxxxxxxxxxxxxxxxx"
MONITOR_FROM = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
RSS, BACKFILL, ADHOC, SPARE = "rss00000001", "bf000000001", "ad000000001", "sp000000001"
OTHER_VIDEO = "ot000000001"


@pytest.fixture
def dsn(head_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    monkeypatch.setenv("DATABASE_URL", head_dsn)
    monkeypatch.setenv("AUDIO_DIR", str(tmp_path))
    get_settings.cache_clear()
    return head_dsn


@pytest.fixture
def db(dsn: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO channels (channel_id, title, active, monitor_from)"
            " VALUES (%(c)s, 'Followed', true, %(m)s), (%(o)s, 'Other', true, %(m)s)",
            {"c": CHANNEL, "o": OTHER, "m": MONITOR_FROM},
        )
        yield conn


@pytest.fixture
def api(dsn: str, logs: LogSink) -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


def _video(db: psycopg.Connection[Any], video_id: str, origin: str, channel: str = CHANNEL) -> None:
    db.execute(
        "INSERT INTO videos (video_id, channel_id, title, origin) VALUES (%s, %s, 'T', %s)",
        (video_id, channel, origin),
    )


def _job(
    db: psycopg.Connection[Any],
    video_id: str,
    state: str,
    *,
    kind: str = "ingest",
    priority: int = 0,
    backfill_id: str | None = None,
) -> int:
    payload = {} if backfill_id is None else {"backfill_id": backfill_id}
    row = db.execute(
        """
        INSERT INTO jobs (video_id, kind, state, priority, payload, locked_by, locked_at, heartbeat_at)
        VALUES (%(v)s, %(k)s, %(s)s, %(p)s, %(pl)s,
                CASE WHEN %(s)s = 'running' THEN 'w1' END,
                CASE WHEN %(s)s = 'running' THEN now() END,
                CASE WHEN %(s)s = 'running' THEN now() END)
        RETURNING id
        """,
        {"v": video_id, "k": kind, "s": state, "p": priority, "pl": Jsonb(payload)},
    ).fetchone()
    assert row is not None
    return int(row[0])


def _derived(db: psycopg.Connection[Any], video_id: str) -> None:
    transcript = db.execute(
        "INSERT INTO transcripts (video_id, source, segments, full_text)"
        " VALUES (%s, 'whisper', '[]', 'x') RETURNING id",
        (video_id,),
    ).fetchone()
    assert transcript is not None
    db.execute(
        "INSERT INTO analyses (video_id, transcript_id, chunk_strategy, model, prompt_version, tldr)"
        " VALUES (%s, %s, 'time:1:0', 'm', 'v1', 'x')",
        (video_id, transcript[0]),
    )


def _catalog_snapshot(db: psycopg.Connection[Any], channel: str, video_id: str) -> None:
    db.execute(
        "INSERT INTO channel_catalog_entries (channel_id, video_id, position, snapshot_limit,"
        " total_count, title) VALUES (%s, %s, 0, 50, 1, 'C')",
        (channel, video_id),
    )


def _state(db: psycopg.Connection[Any]) -> dict[str, list[tuple[Any, ...]]]:
    tables = (
        "channels",
        "videos",
        "jobs",
        "transcripts",
        "analyses",
        "channel_catalog_entries",
        "backfill_requests",
        "deleted_videos",
        "media",
    )
    return {t: db.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables}


def _other_channel(db: psycopg.Connection[Any]) -> None:
    _video(db, OTHER_VIDEO, "rss", OTHER)
    _job(db, OTHER_VIDEO, "pending")
    _derived(db, OTHER_VIDEO)
    _catalog_snapshot(db, OTHER, OTHER_VIDEO)


def _other_rows(db: psycopg.Connection[Any]) -> list[Any]:
    return [
        db.execute("SELECT * FROM channels WHERE channel_id = %s", (OTHER,)).fetchall(),
        db.execute("SELECT * FROM videos WHERE channel_id = %s", (OTHER,)).fetchall(),
        db.execute("SELECT * FROM jobs WHERE video_id = %s", (OTHER_VIDEO,)).fetchall(),
        db.execute("SELECT * FROM analyses WHERE video_id = %s", (OTHER_VIDEO,)).fetchall(),
        db.execute("SELECT * FROM channel_catalog_entries WHERE channel_id = %s", (OTHER,)).fetchall(),
    ]


def test_deleting_a_channel_removes_it_and_every_video_whatever_the_origin(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    for video_id, origin in ((RSS, "rss"), (BACKFILL, "backfill"), (ADHOC, "adhoc")):
        _video(db, video_id, origin)
    running = _job(db, RSS, "running")
    pending = _job(db, BACKFILL, "pending")
    _derived(db, ADHOC)
    _catalog_snapshot(db, CHANNEL, SPARE)
    done_request = create_request(db, CHANNEL, limit_n=50, dry_run=False).request_id
    db.execute("UPDATE backfill_requests SET state = 'done' WHERE id = %s", (done_request,))
    _other_channel(db)
    other_before = _other_rows(db)

    response = api.delete(f"/channels/{CHANNEL}")

    assert response.status_code == 200
    assert response.json() == {"channel_id": CHANNEL, "deleted_videos": 3, "cancelled_backfill": None}
    state = _state(db)
    assert [r[0] for r in state["channels"]] == [OTHER]
    assert [r[0] for r in state["videos"]] == [OTHER_VIDEO]
    assert {r[0] for r in state["deleted_videos"]} == {RSS, BACKFILL, ADHOC}
    assert [r[0] for r in state["jobs"] if r[0] in (running, pending)] == [running]
    cancel = db.execute(
        "SELECT cancel_requested_at IS NOT NULL FROM jobs WHERE id = %s", (running,)
    ).fetchone()
    assert cancel == (True,)
    assert db.execute("SELECT count(*) FROM backfill_requests").fetchone() == (0,)
    assert [r[0] for r in state["channel_catalog_entries"]] == [OTHER]
    assert db.execute("SELECT count(*) FROM analyses WHERE video_id = %s", (ADHOC,)).fetchone() == (0,)
    assert _other_rows(db) == other_before
    assert api.get(f"/channels/{CHANNEL}").status_code == 404
    assert api.get(f"/channels/{CHANNEL}/catalog").status_code == 404
    assert CHANNEL not in str(api.get("/channels").json())
    assert CHANNEL not in str(api.get("/ops/channels").json())


def test_an_active_backfill_request_is_ended_first_and_reported(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    outcome = create_request(db, CHANNEL, limit_n=50, dry_run=False)
    bid = request_backfill_id(outcome.request_id)
    _video(db, BACKFILL, "backfill")
    _video(db, RSS, "rss")
    _job(db, BACKFILL, "pending", priority=PRIORITY_BACKFILL, backfill_id=bid)
    _job(db, RSS, "pending")

    response = api.delete(f"/channels/{CHANNEL}")

    assert response.json() == {
        "channel_id": CHANNEL,
        "deleted_videos": 2,
        "cancelled_backfill": {"backfill_id": bid, "cancelled_jobs": 1},
    }
    assert db.execute("SELECT count(*) FROM backfill_requests").fetchone() == (0,)
    assert {r[0] for r in db.execute("SELECT video_id FROM deleted_videos")} == {BACKFILL, RSS}


def test_a_channel_with_no_videos_is_deleted_with_a_zero_count(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    response = api.delete(f"/channels/{CHANNEL}")

    assert response.status_code == 200
    assert response.json() == {"channel_id": CHANNEL, "deleted_videos": 0, "cancelled_backfill": None}
    assert db.execute("SELECT channel_id FROM channels").fetchall() == [(OTHER,)]


def test_an_unknown_channel_is_404_and_writes_nothing(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    _video(db, RSS, "rss")
    before = _state(db)

    response = api.delete("/channels/UC" + "z" * 22)

    assert response.status_code == 404
    assert _state(db) == before


def test_media_files_of_the_deleted_videos_are_unlinked_and_a_bad_path_still_answers_200(
    api: TestClient, db: psycopg.Connection[Any], tmp_path: Path, logs: LogSink
) -> None:
    _video(db, RSS, "rss")
    _video(db, ADHOC, "adhoc")
    audio = tmp_path / "a.opus"
    audio.write_bytes(b"audio")
    for video_id, rel in ((RSS, "a.opus"), (ADHOC, "../outside.opus")):
        db.execute(
            "INSERT INTO media (video_id, path, bytes, expires_at) VALUES (%s, %s, 5, now())",
            (video_id, rel),
        )
    outside = tmp_path.parent / "outside.opus"
    outside.write_bytes(b"keep")

    response = api.delete(f"/channels/{CHANNEL}")

    assert response.status_code == 200
    assert not audio.exists()
    assert outside.read_bytes() == b"keep"
    [line] = [r for r in logs.records() if r.get("event") == "video.delete.unlink_failed"]
    assert (line["video_id"], line["error_class"]) == (ADHOC, "UnsafeMediaPath")


def test_a_database_failure_before_commit_is_503_and_changes_nothing(
    api: TestClient, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = create_request(db, CHANNEL, limit_n=50, dry_run=False)
    bid = request_backfill_id(outcome.request_id)
    _video(db, RSS, "rss")
    _video(db, BACKFILL, "backfill")
    _job(db, RSS, "running")
    _job(db, BACKFILL, "pending", priority=PRIORITY_BACKFILL, backfill_id=bid)
    _derived(db, RSS)
    _catalog_snapshot(db, CHANNEL, SPARE)
    _other_channel(db)
    before = _state(db)
    real = api_backfill.delete_channel

    def failing(conn: Any, channel_id: str) -> Any:
        def forced() -> None:
            raise psycopg.OperationalError("forced")

        return real(conn, channel_id, before_commit=forced)

    monkeypatch.setattr(routes_write, "delete_channel", failing)

    response = api.delete(f"/channels/{CHANNEL}")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert _state(db) == before


def test_the_videos_are_deleted_by_the_shared_function_without_the_bulk_cap(
    api: TestClient, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # delete_channel passes every id of the channel to delete_videos; no HTTP cap applies.
    monkeypatch.setattr(routes_write, "MAX_BULK_DELETE", 1)
    for video_id in (RSS, BACKFILL, ADHOC):
        _video(db, video_id, "rss")
    seen: list[list[str]] = []
    real = deleted_videos.delete_videos

    def spy(conn: Any, ids: Any, **kwargs: Any) -> Any:
        seen.append(list(ids))
        return real(conn, ids, **kwargs)

    monkeypatch.setattr(api_backfill, "delete_videos", spy)

    response = api.delete(f"/channels/{CHANNEL}")

    assert response.json()["deleted_videos"] == 3
    assert seen == [sorted([RSS, BACKFILL, ADHOC])]


class _Catalog:
    def __init__(self, entries: tuple[CatalogEntry, ...]) -> None:
        self.entries = entries

    def list_uploads(self, channel_id: str, *, limit: int) -> ChannelCatalog:
        return ChannelCatalog(channel_id, self.entries, len(self.entries))


def test_following_the_channel_again_registers_it_new_and_a_backfill_inserts_no_deleted_video(
    api: TestClient, db: psycopg.Connection[Any], dsn: str
) -> None:
    _video(db, RSS, "rss")
    _video(db, BACKFILL, "backfill")
    assert api.delete(f"/channels/{CHANNEL}").status_code == 200

    registered = api.post("/channels", json={"url": CHANNEL})

    assert registered.status_code == 201
    assert registered.json()["created"] is True
    assert datetime.fromisoformat(registered.json()["monitor_from"]) > MONITOR_FROM
    entries = (CatalogEntry(RSS, "a", 60), CatalogEntry(BACKFILL, "b", 60))
    with psycopg.connect(dsn) as conn:
        result = backfill(
            conn,
            PostgresQueue(conn),
            _Catalog(entries),
            CHANNEL,
            limit=50,
            dry_run=False,
        )
    assert (result.already_known, result.new_videos, result.enqueued) == (2, 0, 0)
    assert db.execute("SELECT count(*) FROM videos WHERE channel_id = %s", (CHANNEL,)).fetchone() == (0,)
