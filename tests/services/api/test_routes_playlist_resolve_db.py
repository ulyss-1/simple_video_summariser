"""``POST /playlists/resolve`` against a real database (issue #342).

A fake ``PlaylistSource`` stands in for yt-dlp. The pool has one connection, so a
route that held it across the listing could not give it to the source's own borrow.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from common.config import get_settings
from common.models import CatalogEntry, PlaylistListing
from services.api import deps
from services.api.main import create_app
from tests.services.api.conftest import LogSink

pytestmark = pytest.mark.integration

PL = "PLAqhIrjkxbuWI23v9cThsA9GvCAUhRvKZ"
NEW, KNOWN, TOMB, BUSY = "vid00000001", "vid00000002", "vid00000003", "vid00000004"
TABLES = ("videos", "jobs", "deleted_videos", "analyses", "channels")


class Source:
    def __init__(self) -> None:
        self.borrowed_while_listing = False
        self.client: TestClient | None = None

    def list_playlist(self, playlist_id: str, *, limit: int) -> PlaylistListing:
        assert self.client is not None
        pool = self.client.app.state.pool  # type: ignore[attr-defined]
        conn = pool.getconn(timeout=1)  # PoolTimeout if the route still held the only one
        pool.putconn(conn)
        self.borrowed_while_listing = True
        ids = [NEW, KNOWN, TOMB, BUSY]
        return PlaylistListing(
            playlist_id, "List", tuple(CatalogEntry(v, f"t {v}", 5) for v in ids), 4
        )


@pytest.fixture
def db(head_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(head_dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO videos (video_id, origin) VALUES (%s, 'adhoc')", (KNOWN,))
        conn.execute("INSERT INTO videos (video_id, origin) VALUES (%s, 'adhoc')", (BUSY,))
        conn.execute(
            "INSERT INTO jobs (video_id, kind, dedupe_key, state) VALUES (%s, 'ingest', 'default', 'pending')",
            (BUSY,),
        )
        conn.execute("INSERT INTO deleted_videos (video_id) VALUES (%s)", (TOMB,))
        yield conn


@pytest.fixture
def api(
    head_dsn: str, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch, logs: LogSink
) -> Iterator[tuple[TestClient, Source]]:
    monkeypatch.setenv("DATABASE_URL", head_dsn)
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "1")
    monkeypatch.setenv("DB_POOL_TIMEOUT_SEC", "2")
    get_settings.cache_clear()
    source = Source()
    app = create_app()
    app.dependency_overrides[deps.get_playlist_source] = lambda: source
    with TestClient(app) as client:
        source.client = client
        yield client, source


def counts(db: psycopg.Connection[Any]) -> dict[str, int]:
    out = {}
    for table in TABLES:
        row = db.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert row is not None
        out[table] = int(row[0])
    return out


def test_new_known_tombstoned_and_busy_entries_are_marked(
    api: tuple[TestClient, Source],
) -> None:
    client, _source = api

    response = client.post("/playlists/resolve", json={"url": PL})

    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is False
    assert [(e["video_id"], e["known"], e["processing_status"]) for e in body["entries"]] == [
        (NEW, False, None),
        (KNOWN, True, "idle"),
        (TOMB, True, "deleted"),
        (BUSY, True, "processing"),
    ]


def test_a_video_with_both_a_row_and_a_tombstone_reports_its_row(
    api: tuple[TestClient, Source], db: psycopg.Connection[Any]
) -> None:
    client, _source = api
    db.execute("INSERT INTO deleted_videos (video_id) VALUES (%s)", (KNOWN,))

    entries = client.post("/playlists/resolve", json={"url": PL}).json()["entries"]

    assert entries[1]["processing_status"] == "idle"


def test_the_route_writes_nothing_and_holds_no_connection_while_listing(
    api: tuple[TestClient, Source], db: psycopg.Connection[Any]
) -> None:
    client, source = api
    before = counts(db)

    assert client.post("/playlists/resolve", json={"url": PL}).status_code == 200

    assert source.borrowed_while_listing  # the only pooled connection was free during the listing
    assert counts(db) == before
