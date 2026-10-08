"""``GET /videos/{id}/render`` without a database (issue #45).

The repository functions are replaced by fakes, and ``get_conn`` is overridden
so no connection is opened. The 422 tests use a ``get_conn`` that fails the
test if it is ever resolved.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.models import Analysis, Claim, VideoMeta
from services.api import deps, routes_read

VID = "dQw4w9WgXcQ"
CH = "UC" + "a" * 22
MARKER = "ZZMARKER45"


def _no_db() -> Iterator[Any]:
    raise AssertionError("the database must not be touched")
    yield  # pragma: no cover


class _Conn:
    read_only = False


def _fake_conn() -> Iterator[Any]:
    yield _Conn()


class Fakes:
    def __init__(self) -> None:
        self.video: VideoMeta | None = VideoMeta(
            video_id=VID, channel_id=CH, title="Fake title", description="",
            duration_sec=61, published_at=datetime(2026, 1, 2, tzinfo=UTC), language=None,
            live_status=None, manual_subtitle_langs=(), auto_caption_langs=(),
        )  # fmt: skip
        self.analysis: Analysis | None = Analysis(
            video_id=VID, transcript_id=1, chunk_strategy="c", model="m", prompt_version="p",
            tldr="the tldr", claims=(Claim(text="a claim", start_sec=62),),
            created_at=datetime(2026, 1, 3, tzinfo=UTC),
        )  # fmt: skip
        self.title: str | None = "Fake channel"
        self.calls: list[tuple[str, str]] = []

    def get_video_meta(self, conn: Any, video_id: str) -> VideoMeta | None:
        self.calls.append(("meta", video_id))
        return self.video

    def latest_analysis(self, conn: Any, video_id: str) -> Analysis | None:
        self.calls.append(("analysis", video_id))
        return self.analysis

    def get_channel_title(self, conn: Any, channel_id: str) -> str | None:
        self.calls.append(("channel", channel_id))
        return self.title


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    fake = Fakes()
    monkeypatch.setattr(routes_read, "get_video_meta", fake.get_video_meta)
    monkeypatch.setattr(routes_read, "latest_analysis", fake.latest_analysis)
    monkeypatch.setattr(routes_read, "get_channel_title", fake.get_channel_title)
    return fake


@pytest.fixture
def api(app: FastAPI, fakes: Fakes) -> Iterator[TestClient]:
    app.dependency_overrides[deps.get_conn] = _fake_conn
    with TestClient(app) as client:
        yield client


@pytest.fixture
def no_db_api(app: FastAPI) -> Iterator[TestClient]:
    app.dependency_overrides[deps.get_conn] = _no_db
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    "video_id",
    [
        "a" * 10,
        "a" * 12,
        "abc%2Fdefghij",
        "abcdefghij.",
        "abcde fghij",
        "abcdefghijé",
        "%41bcdefghijk",
        "abcdefghij" + MARKER,
        "a" * 5 + "é" * 6,
    ],
)
def test_bad_ids_are_422_before_the_database_and_never_echoed(
    no_db_api: TestClient, video_id: str
) -> None:
    response = no_db_api.get(f"/videos/{video_id}/render")

    assert response.status_code == 422
    assert MARKER not in response.text
    assert video_id not in response.text


def test_eleven_character_ids_are_accepted_including_a_leading_dash(
    api: TestClient, fakes: Fakes
) -> None:
    fakes.video = None

    for video_id in ("-wNyEUrxzFU", "a" * 11):
        response = api.get(f"/videos/{video_id}/render")
        assert response.status_code == 404
        assert response.json() == {"detail": "video not found"}
        assert video_id not in response.text


def test_unknown_video_is_404_without_looking_for_an_analysis(
    api: TestClient, fakes: Fakes
) -> None:
    fakes.video = None

    response = api.get(f"/videos/{VID}/render")

    assert response.status_code == 404
    assert response.json() == {"detail": "video not found"}
    assert VID not in response.text
    assert [name for name, _ in fakes.calls] == ["meta"]


def test_video_without_analysis_is_404(api: TestClient, fakes: Fakes) -> None:
    fakes.analysis = None

    response = api.get(f"/videos/{VID}/render")

    assert response.status_code == 404
    assert response.json() == {"detail": "no analysis yet"}
    assert VID not in response.text


def test_200_returns_the_rendered_page_with_hardening_headers(
    api: TestClient, fakes: Fakes
) -> None:
    response = api.get(f"/videos/{VID}/render")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-cache"
    body = response.text
    assert body.startswith("<!doctype html>")
    for part in ("Fake title", "Fake channel", "the tldr", "a claim", "1:02"):
        assert part in body
    assert ("channel", CH) in fakes.calls


def test_unknown_channel_title_falls_back_to_the_channel_id(
    api: TestClient, fakes: Fakes
) -> None:
    fakes.title = None

    assert CH in api.get(f"/videos/{VID}/render").text


def test_openapi_declares_the_200_as_html(api: TestClient) -> None:
    schema = api.get("/openapi.json").json()

    ok = schema["paths"]["/videos/{video_id}/render"]["get"]["responses"]["200"]
    assert list(ok["content"]) == ["text/html"]


def test_the_route_handler_is_sync_and_has_no_sql() -> None:
    import inspect

    handler = routes_read.get_video_render
    assert not inspect.iscoroutinefunction(handler)
    assert "execute" not in inspect.getsource(handler)
