"""Channel catalog routes: HTTP contract and error mapping (issue #242). No Postgres.

``get_conn`` yields a sentinel, ``get_catalog`` a fake, and the service functions
the routes call are replaced, so validation, status codes, fixed error messages
and the response shapes are tested without a database or a subprocess.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus

from common.backfill import ChannelNotRegistered
from common.channel_catalog import BulkCatalogResult
from common.errors import RateLimitedError, ToolFailureError, TransientNetworkError
from common.models import CatalogPage, CatalogVideo, ChannelCatalog
from services.api import deps, routes_read, routes_write

CHANNEL = "UC" + "c" * 22
NEWEST, OLDER = "newest00001", "-older00001"
MARKER = "ZZMARKER42"
SECRET = "yt-dlp stderr https://www.youtube.com/playlist?list=UUsecret"


class _Info:
    transaction_status = TransactionStatus.IDLE


class _Conn:
    info = _Info()
    read_only = False

    def transaction(self) -> Any:
        return nullcontext()


class _Catalog:
    def list_uploads(self, channel_id: str, *, limit: int) -> ChannelCatalog:
        raise AssertionError("the route must not call the catalog itself")


def _video(video_id: str, position: int, **overrides: Any) -> CatalogVideo:
    values: dict[str, Any] = {
        "channel_id": CHANNEL,
        "video_id": video_id,
        "position": position,
        "title": f"Title {position}",
        "description": None,
        "published_at": None,
        "enrichment_state": "pending",
        "enrichment_error": None,
        "known": False,
        "processing_status": None,
    }
    values.update(overrides)
    return CatalogVideo(**values)


def _page(*items: CatalogVideo, limit: int = 50) -> CatalogPage:
    return CatalogPage(CHANNEL, limit, 120, items)


class Calls:
    def __init__(self) -> None:
        self.discover: list[dict[str, Any]] = []
        self.process: list[dict[str, Any]] = []
        self.read: list[dict[str, Any]] = []
        self.retry: list[tuple[str, str]] = []
        self.page: CatalogPage | None = _page(_video(NEWEST, 0), _video(OLDER, 1))
        self.error: Exception | None = None
        self.results: tuple[BulkCatalogResult, ...] = ()
        self.retry_ok = True
        self.row_state: str | None = "failed"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    rec = Calls()

    def discover(conn: Any, catalog: Any, channel_id: str, *, limit: int) -> CatalogPage:
        rec.discover.append({"channel_id": channel_id, "limit": limit})
        if rec.error is not None:
            raise rec.error
        assert rec.page is not None
        return rec.page

    def process(
        conn: Any, queue: Any, channel_id: str, video_ids: list[str]
    ) -> tuple[BulkCatalogResult, ...]:
        rec.process.append({"channel_id": channel_id, "video_ids": list(video_ids)})
        if rec.error is not None:
            raise rec.error
        return rec.results

    def read(conn: Any, channel_id: str, *, limit: int) -> CatalogPage | None:
        rec.read.append({"channel_id": channel_id, "limit": limit})
        return rec.page

    def retry(conn: Any, channel_id: str, video_id: str) -> bool:
        rec.retry.append((channel_id, video_id))
        return rec.retry_ok

    monkeypatch.setattr(routes_write, "discover_catalog", discover)
    monkeypatch.setattr(routes_write, "process_catalog_selection", process)
    monkeypatch.setattr(routes_read, "get_catalog_page", read)
    monkeypatch.setattr(routes_write, "retry_enrichment", retry)
    monkeypatch.setattr(routes_write, "get_enrichment_state", lambda *a: rec.row_state)
    return rec


@pytest.fixture
def api(app: FastAPI, calls: Calls) -> Iterator[TestClient]:
    conn = _Conn()

    def fake_conn() -> Iterator[_Conn]:
        yield conn

    app.dependency_overrides[deps.get_conn] = fake_conn
    app.dependency_overrides[deps.get_catalog] = lambda: _Catalog()
    with TestClient(app) as client:
        yield client


def _url(tail: str, channel: str = CHANNEL) -> str:
    return f"/channels/{channel}/catalog{tail}"


# --- GET ----------------------------------------------------------------------


def test_get_returns_the_persisted_rows_newest_first_without_listing(
    api: TestClient, calls: Calls
) -> None:
    response = api.get(_url(""), params={"limit": 200})

    assert response.status_code == 200
    assert calls.read == [{"channel_id": CHANNEL, "limit": 200}]
    body = response.json()
    assert [i["video_id"] for i in body["items"]] == [NEWEST, OLDER]
    assert body["items"][0] == {
        "video_id": NEWEST,
        "title": "Title 0",
        "url": f"https://www.youtube.com/watch?v={NEWEST}",
        "description": None,
        "published_at": None,
        "enrichment_state": "pending",
        "enrichment_error": None,
        "known": False,
        "processing_status": None,
    }
    assert body["items"][1]["url"] == f"https://www.youtube.com/watch?v={OLDER}"
    assert (body["channel_id"], body["limit"], body["total_count"]) == (CHANNEL, 50, 120)


def test_get_defaults_to_the_first_range(api: TestClient, calls: Calls) -> None:
    assert api.get(_url("")).status_code == 200
    assert calls.read[0]["limit"] == 50


def test_get_shows_progressive_enrichment_per_row(api: TestClient, calls: Calls) -> None:
    calls.page = _page(
        _video(
            NEWEST,
            0,
            enrichment_state="done",
            description="Hello",
            published_at=datetime(2026, 9, 1, 2, 0, tzinfo=UTC),
        ),
        _video(OLDER, 1, enrichment_state="failed", enrichment_error="ToolFailureError"),
        _video("known000001", 2, known=True, processing_status="done"),
    )

    items = api.get(_url("")).json()["items"]

    assert [(i["enrichment_state"], i["description"], i["published_at"]) for i in items] == [
        ("done", "Hello", "2026-09-01T02:00:00Z"),
        ("failed", None, None),
        ("pending", None, None),
    ]
    assert (items[2]["known"], items[2]["processing_status"]) == (True, "done")


def test_get_without_a_snapshot_is_a_fixed_404(api: TestClient, calls: Calls) -> None:
    calls.page = None

    response = api.get(_url(""))

    assert response.status_code == 404
    assert response.json() == {"detail": routes_read.CATALOG_NOT_FOUND}


@pytest.mark.parametrize("limit", ["0", "49", "51", "5001", "abc", "1e2", "-50", MARKER, ""])
def test_get_rejects_other_ranges_before_reading(
    api: TestClient, calls: Calls, limit: str
) -> None:
    response = api.get(_url(""), params={"limit": limit})

    assert response.status_code == 422
    assert MARKER not in response.text
    assert calls.read == []


@pytest.mark.parametrize("channel", ["UC" + "a" * 21, "@" + MARKER, "UC" + MARKER + "a" * 13])
def test_get_rejects_a_non_canonical_channel_without_echo(
    api: TestClient, calls: Calls, channel: str
) -> None:
    response = api.get(_url("", channel))

    assert response.status_code == 422
    assert MARKER not in response.text
    assert calls.read == []


# --- discover -----------------------------------------------------------------


def test_discover_returns_flat_rows_before_metadata(api: TestClient, calls: Calls) -> None:
    response = api.post(_url("/discover"), json={"limit": 200})

    assert response.status_code == 200
    assert calls.discover == [{"channel_id": CHANNEL, "limit": 200}]
    assert response.json()["items"][0]["enrichment_state"] == "pending"
    assert response.json()["items"][0]["url"] == f"https://www.youtube.com/watch?v={NEWEST}"


def test_discover_defaults_to_the_first_range(api: TestClient, calls: Calls) -> None:
    assert api.post(_url("/discover"), json={}).status_code == 200
    assert calls.discover[0]["limit"] == 50


@pytest.mark.parametrize("limit", [0, 49, 51, 5001, "50", True, None, 50.5])
def test_discover_rejects_a_limit_outside_the_closed_set(
    api: TestClient, calls: Calls, limit: Any
) -> None:
    response = api.post(_url("/discover"), json={"limit": limit})

    assert response.status_code == 422
    assert calls.discover == []


def test_discover_rejects_unknown_fields_without_echo(api: TestClient, calls: Calls) -> None:
    response = api.post(_url("/discover"), json={"limit": 50, MARKER: 1})

    assert response.status_code == 422
    assert MARKER not in response.text
    assert calls.discover == []


def test_discover_requires_a_json_content_type(api: TestClient, calls: Calls) -> None:
    response = api.post(_url("/discover"), content='{"limit": 50}', headers={"content-type": "text/plain"})

    assert response.status_code == 415
    assert calls.discover == []


@pytest.mark.parametrize(
    "channel",
    ["UC" + "a" * 21, "@" + MARKER, CHANNEL + "%0A", "UC" + "a" * 10 + "%2F" + "a" * 11],
)
def test_discover_rejects_a_non_canonical_channel_without_echo(
    api: TestClient, calls: Calls, channel: str
) -> None:
    response = api.post(_url("/discover", channel), json={"limit": 50})

    assert response.status_code == 422
    assert response.json() == {"detail": routes_write.BAD_BACKFILL_CHANNEL}
    assert calls.discover == []


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (ChannelNotRegistered(CHANNEL), 404, routes_write.CHANNEL_NOT_REGISTERED),
        (ToolFailureError(SECRET), 502, routes_write.LISTING_FAILED),
        (TransientNetworkError(SECRET), 503, routes_write.LISTING_UNAVAILABLE),
        (RateLimitedError(SECRET), 503, routes_write.LISTING_UNAVAILABLE),
    ],
)
def test_discover_failures_map_to_fixed_messages(
    api: TestClient, calls: Calls, error: Exception, status_code: int, detail: str
) -> None:
    calls.error = error

    response = api.post(_url("/discover"), json={"limit": 50})

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    assert "yt-dlp" not in response.text and "UUsecret" not in response.text


# --- process ------------------------------------------------------------------


def test_process_returns_one_result_per_requested_id_in_order(
    api: TestClient, calls: Calls
) -> None:
    calls.results = (
        BulkCatalogResult(OLDER, "queued", 41, None),
        BulkCatalogResult(NEWEST, "already_known", None, None),
        BulkCatalogResult("zzzzzzzzzzz", "rejected", None, "not_in_catalog"),
    )

    response = api.post(_url("/process"), json={"video_ids": [OLDER, NEWEST, "zzzzzzzzzzz"]})

    assert response.status_code == 202
    assert calls.process == [
        {"channel_id": CHANNEL, "video_ids": [OLDER, NEWEST, "zzzzzzzzzzz"]}
    ]
    assert response.json() == {
        "channel_id": CHANNEL,
        "results": [
            {"video_id": OLDER, "outcome": "queued", "job_id": 41, "reason": None},
            {"video_id": NEWEST, "outcome": "already_known", "job_id": None, "reason": None},
            {
                "video_id": "zzzzzzzzzzz",
                "outcome": "rejected",
                "job_id": None,
                "reason": "not_in_catalog",
            },
        ],
    }


@pytest.mark.parametrize(
    "body",
    [
        {"video_ids": []},
        {"video_ids": [NEWEST, NEWEST]},
        {"video_ids": [f"v{n:010d}" for n in range(5001)]},
        {"video_ids": ["short"]},
        {"video_ids": [NEWEST, MARKER + "!!"]},
        {"video_ids": NEWEST},
        {"video_ids": [NEWEST], MARKER: 1},
        {"video_ids": [1]},
        {},
    ],
    ids=["empty", "duplicate", "5001", "malformed", "marker", "string", "extra", "int", "missing"],
)
def test_process_rejects_a_bad_selection_before_the_database(
    api: TestClient, calls: Calls, body: dict[str, Any]
) -> None:
    response = api.post(_url("/process"), json=body)

    assert response.status_code == 422
    assert MARKER not in response.text
    assert calls.process == []


def test_process_unregistered_channel_is_a_fixed_404(api: TestClient, calls: Calls) -> None:
    calls.error = ChannelNotRegistered(CHANNEL)

    response = api.post(_url("/process"), json={"video_ids": [NEWEST]})

    assert response.status_code == 404
    assert response.json() == {"detail": routes_write.CHANNEL_NOT_REGISTERED}


def test_process_requires_a_json_content_type(api: TestClient, calls: Calls) -> None:
    response = api.post(_url("/process"), content="x", headers={"content-type": "text/plain"})

    assert response.status_code == 415
    assert calls.process == []


# --- metadata retry -------------------------------------------------------------


def test_retry_changes_a_failed_row_to_pending(api: TestClient, calls: Calls) -> None:
    response = api.post(_url(f"/{NEWEST}/metadata/retry"))

    assert response.status_code == 202
    assert response.json() == {
        "channel_id": CHANNEL,
        "video_id": NEWEST,
        "enrichment_state": "pending",
    }
    assert calls.retry == [(CHANNEL, NEWEST)]


def test_retry_accepts_an_id_that_starts_with_a_dash(api: TestClient, calls: Calls) -> None:
    assert api.post(_url(f"/{OLDER}/metadata/retry")).status_code == 202
    assert calls.retry == [(CHANNEL, OLDER)]


def test_retry_on_a_row_that_has_not_failed_is_a_fixed_409(api: TestClient, calls: Calls) -> None:
    calls.retry_ok = False
    calls.row_state = "done"

    response = api.post(_url(f"/{NEWEST}/metadata/retry"))

    assert response.status_code == 409
    assert response.json() == {"detail": routes_write.CATALOG_RETRY_NOT_FAILED}


def test_retry_on_a_row_outside_this_catalog_is_a_fixed_404(
    api: TestClient, calls: Calls
) -> None:
    calls.retry_ok = False
    calls.row_state = None

    response = api.post(_url(f"/{NEWEST}/metadata/retry"))

    assert response.status_code == 404
    assert response.json() == {"detail": routes_write.CATALOG_ROW_NOT_FOUND}


@pytest.mark.parametrize(
    "video", ["short", MARKER + "!!!!", "aaaaaaaaaa%0A", "%61aaaaaaaaaa", "a" * 10 + "%2F"]
)
def test_retry_rejects_a_malformed_video_id_before_the_database(
    api: TestClient, calls: Calls, video: str
) -> None:
    response = api.post(_url(f"/{video}/metadata/retry"))

    assert response.status_code == 422
    assert MARKER not in response.text
    assert calls.retry == []


def test_retry_rejects_a_malformed_channel_without_echo(api: TestClient, calls: Calls) -> None:
    response = api.post(_url(f"/{NEWEST}/metadata/retry", "@" + MARKER))

    assert response.status_code == 422
    assert MARKER not in response.text
    assert calls.retry == []


# --- routing ------------------------------------------------------------------


def test_the_catalog_routes_do_not_shadow_the_backfill_routes(
    api: TestClient, calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def backfill(*args: Any, **kwargs: Any) -> Any:
        seen.append("backfill")
        raise ChannelNotRegistered(CHANNEL)

    monkeypatch.setattr(routes_write, "backfill", backfill)
    api.app.dependency_overrides[routes_write.get_subtitle_probe] = lambda: None  # type: ignore[attr-defined]

    assert api.post(f"/channels/{CHANNEL}/backfill", json={"limit": 5}).status_code == 404
    assert seen == ["backfill"]
    assert calls.discover == [] and calls.process == []
