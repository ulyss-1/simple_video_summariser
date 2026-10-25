"""Channel catalog routes end to end on Postgres (issue #242).

The real ``get_conn`` is used; only the catalog source is faked. Enrichment is
driven through the repository functions the planner task uses.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from common.config import get_settings
from common.models import CatalogEntry, ChannelCatalog, VideoMeta
from common.repo.channel_catalog import (
    claim_enrichment,
    complete_enrichment,
    fail_enrichment,
)
from services.api import deps
from services.api.main import create_app

pytestmark = pytest.mark.integration

CHANNEL = "UCuAXFkgsw1L7xaCfnd5JJOw"
A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"


class Catalog:
    def __init__(self) -> None:
        self.entries = (
            CatalogEntry(A, "Newest", 60),
            CatalogEntry(B, "Middle", 60),
            CatalogEntry(C, "Oldest", 60),
        )
        self.calls: list[int] = []

    def list_uploads(self, channel_id: str, *, limit: int) -> ChannelCatalog:
        self.calls.append(limit)
        return ChannelCatalog(channel_id, self.entries[:limit], 3)


@pytest.fixture
def db(head_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[psycopg.Connection[Any]]:
    monkeypatch.setenv("DATABASE_URL", head_dsn)
    get_settings.cache_clear()
    with psycopg.connect(head_dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO channels (channel_id) VALUES (%s)", (CHANNEL,))
        yield conn


@pytest.fixture
def catalog() -> Catalog:
    return Catalog()


@pytest.fixture
def api(db: psycopg.Connection[Any], catalog: Catalog) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[deps.get_catalog] = lambda: catalog
    with TestClient(app) as client:
        yield client


def _meta(video_id: str) -> VideoMeta:
    return VideoMeta(
        video_id=video_id,
        channel_id=CHANNEL,
        title=f"Full {video_id}",
        description="About it",
        duration_sec=10,
        published_at=datetime(2026, 9, 1, 2, 0, tzinfo=UTC),
        language=None,
        live_status=None,
        manual_subtitle_langs=(),
        auto_caption_langs=(),
    )


def _states(api: TestClient) -> dict[str, str]:
    items = api.get(f"/channels/{CHANNEL}/catalog").json()["items"]
    return {i["video_id"]: i["enrichment_state"] for i in items}


def test_get_is_404_until_discovery_and_discovery_adds_nothing_to_the_library(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    assert api.get(f"/channels/{CHANNEL}/catalog").status_code == 404

    response = api.post(f"/channels/{CHANNEL}/catalog/discover", json={"limit": 50})

    assert response.status_code == 200
    assert [i["video_id"] for i in response.json()["items"]] == [A, B, C]
    assert api.get(f"/channels/{CHANNEL}/catalog").json() == response.json()
    assert db.execute("SELECT (SELECT count(*) FROM videos), (SELECT count(*) FROM jobs)").fetchone() == (0, 0)


def test_unregistered_channel_is_404_for_discovery_and_process(
    api: TestClient, catalog: Catalog
) -> None:
    other = "UC" + "z" * 22

    assert api.post(f"/channels/{other}/catalog/discover", json={"limit": 50}).status_code == 404
    assert api.post(f"/channels/{other}/catalog/process", json={"video_ids": [A]}).status_code == 404
    assert catalog.calls == []


def test_rows_enrich_progressively_and_a_failed_row_is_retryable(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    api.post(f"/channels/{CHANNEL}/catalog/discover", json={"limit": 50})
    assert _states(api) == {A: "pending", B: "pending", C: "pending"}

    claims = claim_enrichment(db, batch_size=2)
    assert [c.video_id for c in claims] == [A, B]
    assert _states(api) == {A: "running", B: "running", C: "pending"}

    complete_enrichment(db, _meta(A))
    fail_enrichment(db, CHANNEL, B, error_class="ToolFailureError")
    items = {i["video_id"]: i for i in api.get(f"/channels/{CHANNEL}/catalog").json()["items"]}
    assert (items[A]["enrichment_state"], items[A]["description"], items[A]["published_at"]) == (
        "done",
        "About it",
        "2026-09-01T02:00:00Z",
    )
    assert (items[B]["enrichment_state"], items[B]["enrichment_error"]) == (
        "failed",
        "ToolFailureError",
    )
    assert items[C]["enrichment_state"] == "pending"

    retried = api.post(f"/channels/{CHANNEL}/catalog/{B}/metadata/retry")
    assert retried.status_code == 202
    assert _states(api)[B] == "pending"
    assert api.post(f"/channels/{CHANNEL}/catalog/{B}/metadata/retry").status_code == 409
    assert api.post(f"/channels/{CHANNEL}/catalog/{A}/metadata/retry").status_code == 409
    assert api.post(f"/channels/{CHANNEL}/catalog/{'q' * 11}/metadata/retry").status_code == 404
    assert _states(api)[A] == "done"


def test_process_queues_new_videos_once_and_reports_every_id(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    api.post(f"/channels/{CHANNEL}/catalog/discover", json={"limit": 50})
    outsider = "o" * 11

    first = api.post(f"/channels/{CHANNEL}/catalog/process", json={"video_ids": [B, outsider, A]})

    assert first.status_code == 202
    results = first.json()["results"]
    assert [(r["video_id"], r["outcome"], r["reason"]) for r in results] == [
        (B, "queued", None),
        (outsider, "rejected", "not_in_catalog"),
        (A, "queued", None),
    ]
    assert db.execute(
        "SELECT video_id, origin FROM videos ORDER BY video_id"
    ).fetchall() == [(A, "backfill"), (B, "backfill")]
    assert db.execute(
        "SELECT video_id, kind, priority FROM jobs ORDER BY id"
    ).fetchall() == [(B, "ingest", -10), (A, "ingest", -10)]

    again = api.post(f"/channels/{CHANNEL}/catalog/process", json={"video_ids": [A, C]})

    assert [(r["video_id"], r["outcome"]) for r in again.json()["results"]] == [
        (A, "already_known"),
        (C, "queued"),
    ]
    assert db.execute("SELECT count(*) FROM jobs").fetchone() == (3,)
    rows = {i["video_id"]: i for i in api.get(f"/channels/{CHANNEL}/catalog").json()["items"]}
    assert [(rows[v]["known"], rows[v]["processing_status"]) for v in (A, B, C)] == [
        (True, "processing"),
        (True, "processing"),
        (True, "processing"),
    ]


def test_a_deleted_video_stays_deleted_and_shows_as_known(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    db.execute("INSERT INTO deleted_videos (video_id) VALUES (%s)", (A,))
    api.post(f"/channels/{CHANNEL}/catalog/discover", json={"limit": 50})

    items = {i["video_id"]: i for i in api.get(f"/channels/{CHANNEL}/catalog").json()["items"]}
    assert (items[A]["known"], items[A]["processing_status"]) == (True, None)

    result = api.post(f"/channels/{CHANNEL}/catalog/process", json={"video_ids": [A]}).json()

    assert result["results"][0]["outcome"] == "already_known"
    assert db.execute("SELECT count(*) FROM videos").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM deleted_videos").fetchone() == (1,)


def test_a_narrower_rediscovery_shrinks_the_snapshot(
    api: TestClient, catalog: Catalog
) -> None:
    api.post(f"/channels/{CHANNEL}/catalog/discover", json={"limit": 500})
    catalog.entries = catalog.entries[:2]

    page = api.post(f"/channels/{CHANNEL}/catalog/discover", json={"limit": 50}).json()

    assert [i["video_id"] for i in page["items"]] == [A, B]
    assert catalog.calls == [500, 50]
