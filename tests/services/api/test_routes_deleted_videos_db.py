"""``GET /deleted-videos`` and the restore path against a real database (issue #239)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from common.config import get_settings
from services.api.main import create_app
from tests.services.api.conftest import LogSink

pytestmark = pytest.mark.integration

A = "dQw4w9WgXcQ"
B = "abcdefghijk"
C = "ABCDEFGHIJK"


@pytest.fixture
def dsn(head_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    monkeypatch.setenv("DATABASE_URL", head_dsn)
    monkeypatch.setenv("AUDIO_DIR", str(tmp_path))
    get_settings.cache_clear()
    return head_dsn


@pytest.fixture
def db(dsn: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def api(dsn: str, logs: LogSink) -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


def seed(db: psycopg.Connection[Any], video_id: str, title: str = "Title") -> None:
    db.execute("INSERT INTO channels (channel_id, title) VALUES ('UCkeep', 'Keep') ON CONFLICT DO NOTHING")
    db.execute(
        "INSERT INTO videos (video_id, channel_id, title, origin) VALUES (%s, 'UCkeep', %s, 'adhoc')",
        (video_id, title),
    )


def ids(api: TestClient, query: str = "") -> list[str]:
    return [i["video_id"] for i in api.get(f"/deleted-videos{query}").json()["items"]]


def test_delete_copies_title_and_channel_and_the_list_shows_them(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed(db, A, "Hello")

    assert api.delete(f"/videos/{A}").status_code == 204

    body = api.get("/deleted-videos").json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["video_id"] == A
    assert item["title"] == "Hello"
    assert item["channel_id"] == "UCkeep"
    assert item["channel_title"] == "Keep"
    assert item["restorable"] is True
    assert item["deleted_at"]


def test_bulk_delete_fills_the_columns_too(api: TestClient, db: psycopg.Connection[Any]) -> None:
    seed(db, A, "One")
    seed(db, B, "Two")

    assert api.post("/videos/delete", json={"video_ids": [A, B]}).status_code == 200

    rows = db.execute("SELECT video_id, title, channel_id FROM deleted_videos ORDER BY video_id").fetchall()
    assert rows == [(A, "One", "UCkeep"), (B, "Two", "UCkeep")]


def test_channel_delete_fills_the_columns_too(api: TestClient, db: psycopg.Connection[Any]) -> None:
    seed(db, A, "One")

    assert api.delete("/channels/UCkeep").status_code == 200

    assert db.execute("SELECT video_id, title, channel_id FROM deleted_videos").fetchall() == [
        (A, "One", "UCkeep")
    ]
    item = api.get("/deleted-videos").json()["items"][0]
    assert item["channel_title"] is None  # the channel is gone


def test_a_legacy_tombstone_lists_null_title_and_a_gone_channel_has_no_title(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    db.execute("INSERT INTO deleted_videos (video_id) VALUES (%s)", (A,))
    db.execute("INSERT INTO deleted_videos (video_id, title, channel_id) VALUES (%s, 'T', 'UCgone')", (B,))

    items = {i["video_id"]: i for i in api.get("/deleted-videos").json()["items"]}

    assert items[A]["title"] is None
    assert items[A]["channel_id"] is None
    assert items[A]["channel_title"] is None
    assert items[B]["title"] == "T"
    assert items[B]["channel_title"] is None


def test_restored_then_deleted_again_has_one_tombstone_with_the_second_title(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed(db, A, "First")
    api.delete(f"/videos/{A}")
    assert api.post("/videos", json={"url": A}).status_code == 202
    db.execute("UPDATE videos SET title = 'Second' WHERE video_id = %s", (A,))

    api.delete(f"/videos/{A}")

    assert db.execute("SELECT video_id, title FROM deleted_videos").fetchall() == [(A, "Second")]


def test_order_ties_and_pagination_one_past_the_boundary(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    db.execute("INSERT INTO deleted_videos (video_id, deleted_at) VALUES (%s, '2026-01-02')", (C,))
    db.execute("INSERT INTO deleted_videos (video_id, deleted_at) VALUES (%s, '2026-01-01')", (B,))
    db.execute("INSERT INTO deleted_videos (video_id, deleted_at) VALUES (%s, '2026-01-01')", (A,))

    assert ids(api) == [C, A, B]  # newest first, the tie by video_id
    page1 = api.get("/deleted-videos?limit=2").json()
    assert [i["video_id"] for i in page1["items"]] == [C, A]
    assert page1["total"] == 3
    assert ids(api, "?limit=2&offset=2") == [B]
    assert ids(api, "?limit=2&offset=3") == []


@pytest.mark.parametrize("query", ["offset=-1", "offset=1000001", "limit=0", "limit=201", "limit=x"])
def test_invalid_paging_is_422(api: TestClient, query: str) -> None:
    assert api.get(f"/deleted-videos?{query}").status_code == 422


def test_restore_is_post_videos_and_clears_the_tombstone(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed(db, A)
    api.delete(f"/videos/{A}")
    assert ids(api) == [A]

    response = api.post("/videos", json={"url": A})

    assert response.status_code == 202
    assert response.json()["created"] is True
    assert ids(api) == []
    assert [i["video_id"] for i in api.get("/videos").json()["items"]] == [A]
    job = db.execute("SELECT kind, state, priority FROM jobs WHERE video_id = %s", (A,)).fetchall()
    assert job == [("ingest", "pending", 10)]


def test_a_winding_down_cancel_is_not_restorable_and_post_videos_answers_409(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed(db, A)
    db.execute("INSERT INTO jobs (video_id, kind, state) VALUES (%s, 'ingest', 'running')", (A,))
    api.delete(f"/videos/{A}")

    item = api.get("/deleted-videos").json()["items"][0]
    assert item["restorable"] is False
    assert api.post("/videos", json={"url": A}).status_code == 409
