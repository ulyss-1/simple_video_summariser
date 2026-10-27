"""``POST /playlists/resolve``: HTTP contract, bounds and connection hygiene (issue #342).

No Postgres and no yt-dlp: ``get_playlist_source`` yields a fake, the pool is a
``FakePool`` and the one library read is replaced. Behaviour against a real
database is in ``test_routes_playlist_resolve_db.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import nullcontext
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus

from common.errors import (
    PermanentSourceError,
    RateLimitedError,
    ToolFailureError,
    TransientNetworkError,
    UnavailableReason,
)
from common.models import CatalogEntry, PlaylistListing
from services.api import deps, playlists
from tests.services.api.conftest import FakePool, request_with

PL = "PLAqhIrjkxbuWI23v9cThsA9GvCAUhRvKZ"
A, B, C = "vid00000001", "-vid0000002", "vid00000003"
SECRET = "yt-dlp stderr https://www.youtube.com/playlist?list=PLsecret ZZMARKER42"


class _Info:
    transaction_status = TransactionStatus.IDLE


class _Conn:
    info = _Info()

    def transaction(self) -> Any:
        return nullcontext()


def _listing(ids: list[str], *, total: int | None = None, skipped: int = 0) -> PlaylistListing:
    return PlaylistListing(
        PL,
        "My list",
        tuple(CatalogEntry(v, f"title {v}", 60) for v in ids),
        len(ids) if total is None else total,
        skipped,
    )


class FakeSource:
    def __init__(self, result: PlaylistListing | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[str, int]] = []

    def list_playlist(self, playlist_id: str, *, limit: int) -> PlaylistListing:
        self.calls.append((playlist_id, limit))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class Env:
    def __init__(self, client: TestClient, source: FakeSource, statuses: dict[str, str]) -> None:
        self.client = client
        self.source = source
        self.statuses = statuses
        self.reads: list[list[str]] = []

    def post(self, url: str = PL, **kw: Any) -> Any:
        return self.client.post("/playlists/resolve", json={"url": url}, **kw)


@pytest.fixture
def env(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    source = FakeSource(_listing([A, B, C]))
    statuses: dict[str, str] = {}
    holder: dict[str, Env] = {}

    def fake_statuses(conn: Any, ids: list[str]) -> dict[str, str]:
        holder["env"].reads.append(list(ids))
        return {i: statuses[i] for i in ids if i in statuses}

    monkeypatch.setattr(playlists, "library_statuses", fake_statuses)
    pool = FakePool([_Conn() for _ in range(5)])
    app.dependency_overrides[deps.get_playlist_source] = lambda: source
    app.dependency_overrides[deps.get_conn_provider] = lambda: deps.get_conn_provider(
        request_with(pool)
    )
    with TestClient(app) as client:
        holder["env"] = Env(client, source, statuses)
        yield holder["env"]


def test_a_playlist_is_listed_in_order_with_the_canonical_url_and_no_known_videos(
    env: Env,
) -> None:
    response = env.post()

    assert response.status_code == 200
    assert response.json() == {
        "playlist_id": PL,
        "title": "My list",
        "total_count": 3,
        "truncated": False,
        "entries": [
            {
                "position": i,
                "video_id": v,
                "title": f"title {v}",
                "url": f"https://www.youtube.com/watch?v={v}",
                "known": False,
                "processing_status": None,
            }
            for i, v in enumerate([A, B, C])
        ],
    }
    assert env.source.calls == [(PL, playlists.MAX_PLAYLIST_ENTRIES)]


def test_known_and_tombstoned_videos_are_marked_with_their_status(env: Env) -> None:
    env.statuses.update({B: "done", C: "deleted"})

    entries = env.post().json()["entries"]

    assert [(e["known"], e["processing_status"]) for e in entries] == [
        (False, None),
        (True, "done"),
        (True, "deleted"),
    ]
    assert env.reads == [[A, B, C]]


@pytest.mark.parametrize(
    "url",
    [
        PL,
        f"https://www.youtube.com/playlist?list={PL}",
        f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={PL}",
        f"m.youtube.com/playlist?list={PL}",
    ],
)
def test_every_accepted_reference_asks_the_source_for_the_same_id(env: Env, url: str) -> None:
    assert env.post(url).status_code == 200
    assert env.source.calls == [(PL, playlists.MAX_PLAYLIST_ENTRIES)]


@pytest.mark.parametrize(
    "url",
    [
        "RDdQw4w9WgXcQ",
        "RDCLAK5uy_" + "a" * 20,
        "LL",
        "WL",
        "https://www.youtube.com/playlist?list=LL",
        f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RD{'a' * 20}",
        "dQw4w9WgXcQ",
        "",
        "https://example.com/playlist?list=" + PL,
        "ZZMARKER42 not a playlist",
    ],
)
def test_a_refused_reference_is_a_fixed_422_and_never_reaches_the_source(
    env: Env, url: str
) -> None:
    response = env.post(url)

    assert response.status_code == 422
    assert response.json() == {"detail": playlists_bad_ref()}
    assert "ZZMARKER42" not in response.text
    assert env.source.calls == []
    assert env.reads == []


def playlists_bad_ref() -> str:
    from services.api import routes_write

    return routes_write.BAD_PLAYLIST_REF


def test_a_body_that_is_not_json_is_415(env: Env) -> None:
    response = env.client.post(
        "/playlists/resolve", content=f"url={PL}", headers={"content-type": "text/plain"}
    )
    assert response.status_code == 415
    assert env.source.calls == []


@pytest.mark.parametrize("body", [{}, {"url": 5}, {"url": PL, "extra": 1}, {"link": PL}])
def test_a_malformed_body_is_422_and_never_reaches_the_source(env: Env, body: Any) -> None:
    assert env.client.post("/playlists/resolve", json=body).status_code == 422
    assert env.source.calls == []


@pytest.mark.parametrize(
    ("error", "code", "detail"),
    [
        (PermanentSourceError(UnavailableReason.REMOVED, SECRET), 404, "playlist not found or not public"),
        (TransientNetworkError(SECRET), 503, "YouTube listing unavailable, try again later"),
        (RateLimitedError(SECRET), 503, "YouTube listing unavailable, try again later"),
        (ToolFailureError(SECRET), 502, "YouTube listing failed"),
    ],
)
def test_source_errors_map_to_fixed_statuses_without_yt_dlp_text(
    env: Env, error: BaseException, code: int, detail: str
) -> None:
    env.source.result = error

    response = env.post()

    assert response.status_code == code
    assert response.json() == {"detail": detail}
    assert "ZZMARKER42" not in response.text
    assert env.reads == []


def test_an_empty_playlist_is_an_empty_list_and_reads_nothing(env: Env) -> None:
    env.source.result = _listing([], total=0)

    body = env.post().json()

    assert body["entries"] == []
    assert body["truncated"] is False
    assert env.reads == []


# --- the bound ---------------------------------------------------------------------


def test_the_cap_is_the_injectable_module_constant(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(playlists, "MAX_PLAYLIST_ENTRIES", 2)

    body = env.post().json()  # the fake lists 3 although it was asked for 2

    assert env.source.calls == [(PL, 2)]
    assert [e["video_id"] for e in body["entries"]] == [A, B]
    assert body["truncated"] is True
    assert body["total_count"] == 3
    assert env.reads == [[A, B]]


def test_the_default_cap_is_200() -> None:
    assert playlists.MAX_PLAYLIST_ENTRIES == 200


def test_truncated_follows_the_reported_total(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(playlists, "MAX_PLAYLIST_ENTRIES", 3)
    env.source.result = _listing([A, B, C], total=7)

    body = env.post().json()

    assert len(body["entries"]) == 3
    assert (body["total_count"], body["truncated"]) == (7, True)


def test_a_playlist_exactly_at_the_cap_is_not_truncated(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(playlists, "MAX_PLAYLIST_ENTRIES", 3)
    env.source.result = _listing([A, B, C], total=3)

    assert env.post().json()["truncated"] is False


def test_skipped_repeats_do_not_make_a_short_list_look_truncated(env: Env) -> None:
    env.source.result = _listing([A, B], total=3, skipped=1)  # one repeat was skipped

    assert env.post().json()["truncated"] is False


def test_an_unknown_total_is_reported_as_null_and_not_truncated(
    env: Env,
) -> None:
    env.source.result = PlaylistListing(PL, None, (CatalogEntry(A, "a", None),), None)
    body = env.post().json()
    assert (body["total_count"], body["truncated"]) == (None, False)


def test_repeated_or_malformed_ids_in_a_listing_are_dropped(env: Env) -> None:
    env.source.result = PlaylistListing(
        PL,
        None,
        (
            CatalogEntry(A, "a", None),
            CatalogEntry("bad", "x", None),
            CatalogEntry(A, "again", None),
            CatalogEntry(B, None, None),
        ),
        4,
        0,
    )

    body = env.post().json()

    assert [(e["position"], e["video_id"]) for e in body["entries"]] == [(0, A), (1, B)]
    assert body["title"] is None
    assert body["entries"][1]["title"] is None


# --- connection hygiene (#219 pattern) ------------------------------------------------


def _checked_out(pool: FakePool) -> int:
    return pool.getconn_calls - len(pool.returned)


def test_no_connection_is_checked_out_while_the_source_blocks(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = FakePool([_Conn(), _Conn()])
    during: list[int] = []

    class BlockingSource:
        def list_playlist(self, playlist_id: str, *, limit: int) -> PlaylistListing:
            during.append(_checked_out(pool))
            return _listing([A])

    monkeypatch.setattr(playlists, "library_statuses", lambda conn, ids: {})
    app.dependency_overrides[deps.get_playlist_source] = lambda: BlockingSource()
    app.dependency_overrides[deps.get_conn_provider] = lambda: deps.get_conn_provider(
        request_with(pool)
    )
    with TestClient(app) as client:
        assert client.post("/playlists/resolve", json={"url": PL}).status_code == 200

    assert during == [0]
    assert pool.getconn_calls == 1  # one short read afterwards
    assert _checked_out(pool) == 0


def test_no_connection_is_borrowed_when_the_listing_fails_or_is_refused(
    app: FastAPI,
) -> None:
    pool = FakePool([_Conn()])
    app.dependency_overrides[deps.get_playlist_source] = lambda: FakeSource(
        TransientNetworkError("x")
    )
    app.dependency_overrides[deps.get_conn_provider] = lambda: deps.get_conn_provider(
        request_with(pool)
    )
    with TestClient(app) as client:
        assert client.post("/playlists/resolve", json={"url": PL}).status_code == 503
        assert client.post("/playlists/resolve", json={"url": "LL"}).status_code == 422

    assert pool.getconn_calls == 0
