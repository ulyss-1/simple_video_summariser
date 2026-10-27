"""``POST /videos/delete``: HTTP layer (issue #238).

No Postgres: ``get_conn`` yields a sentinel and ``routes_write.delete_videos`` is
replaced, so validation, the cap, auth and the response shape are tested without a
database. The behaviour against a real database is in ``test_routes_bulk_delete_db.py``.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus

from common.repo.deleted_videos import BulkDeleted
from services.api import deps, routes_write
from tests.services.api.conftest import LogSink

A = "dQw4w9WgXcQ"
B = "abcdefghijk"
C = "-wNyEUrxzFU"
MARKER = "ZZMARKER42"


class _Info:
    transaction_status = TransactionStatus.IDLE


class _Conn:
    info = _Info()


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, conn: Any, video_ids: Sequence[str]) -> BulkDeleted:
        self.calls.append(list(video_ids))
        deleted = tuple(v for v in video_ids if v != B)
        return BulkDeleted(
            deleted=deleted,
            not_found=tuple(v for v in video_ids if v == B),
            media=(),
        )


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(routes_write, "delete_videos", rec)
    return rec


@pytest.fixture
def api(app: FastAPI, recorder: Recorder) -> Iterator[TestClient]:
    def fake_conn() -> Iterator[_Conn]:
        yield _Conn()

    app.dependency_overrides[deps.get_conn] = fake_conn
    with TestClient(app) as client:
        yield client


def test_a_valid_request_answers_results_in_request_order_and_counts(
    api: TestClient, recorder: Recorder
) -> None:
    response = api.post("/videos/delete", json={"video_ids": [C, B, A]})

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {"video_id": C, "outcome": "deleted"},
            {"video_id": B, "outcome": "not_found"},
            {"video_id": A, "outcome": "deleted"},
        ],
        "counts": {"deleted": 2, "not_found": 1},
    }
    assert recorder.calls == [[C, B, A]]


@pytest.mark.parametrize(
    "body",
    [
        {"video_ids": []},
        {"video_ids": [A, A]},
        {"video_ids": [A, MARKER]},  # 10 characters
        {"video_ids": [A + "x" + MARKER]},
        {"video_ids": [A[:-1] + "!"]},
        {"video_ids": [A, 123456]},
        {"video_ids": [None]},
        {"video_ids": MARKER},
        {},
        {"ids": [A]},
        {"video_ids": [A], "extra": 1},
        {"video_ids": [A], MARKER: 1},
    ],
)
def test_an_invalid_body_is_422_without_echo_and_deletes_nothing(
    api: TestClient, recorder: Recorder, body: dict[str, Any]
) -> None:
    response = api.post("/videos/delete", json=body)

    assert response.status_code == 422
    assert MARKER not in response.text
    assert A + "x" not in response.text
    assert recorder.calls == []


def test_the_cap_is_read_at_request_time_one_past_it_is_422_exactly_it_is_accepted(
    api: TestClient, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_write, "MAX_BULK_DELETE", 2)

    over = api.post("/videos/delete", json={"video_ids": [A, B, C]})
    at = api.post("/videos/delete", json={"video_ids": [A, B]})

    assert over.status_code == 422
    assert not any(v in over.text for v in (A, B, C))
    assert at.status_code == 200
    assert recorder.calls == [[A, B]]


def test_the_default_cap_is_the_largest_library_page() -> None:
    assert routes_write.MAX_BULK_DELETE == 200


@pytest.mark.parametrize(
    "content_type", [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"]
)
def test_a_non_json_content_type_is_415_and_deletes_nothing(
    api: TestClient, recorder: Recorder, content_type: str | None
) -> None:
    headers = {"Content-Type": content_type} if content_type else {}
    response = api.post("/videos/delete", content=f'{{"video_ids": ["{A}"]}}', headers=headers)

    assert response.status_code == 415
    assert recorder.calls == []


def _deny() -> None:
    raise HTTPException(status_code=401, detail="denied")


def _too_many() -> None:
    raise HTTPException(status_code=429, detail="slow down")


def test_bulk_delete_inherits_auth_and_write_rate_limit(
    app: FastAPI, logs: LogSink, recorder: Recorder
) -> None:
    app.dependency_overrides[deps.require_auth] = _deny
    with TestClient(app) as client:
        assert client.post("/videos/delete", json={"video_ids": [A]}).status_code == 401

    app.dependency_overrides.clear()
    app.dependency_overrides[deps.rate_limit] = _too_many
    with TestClient(app) as client:
        assert client.post("/videos/delete", json={"video_ids": [A]}).status_code == 429
    assert recorder.calls == []
