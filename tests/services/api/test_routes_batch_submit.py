"""``POST /videos/batch``: HTTP layer (issue #341).

No Postgres: ``get_conn`` yields a sentinel and ``routes_write.submit_video`` is
replaced by a recorder. Behaviour against a real database is in
``test_routes_batch_submit_db.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus

from common.queue import PRIORITY_NORMAL
from services.api import deps, routes_write
from tests.services.api.conftest import LogSink

A = "dQw4w9WgXcQ"
B = "abcdefghijk"
MARKER = "ZZMARKER42"


class _Info:
    transaction_status = TransactionStatus.IDLE


class _Conn:
    info = _Info()


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, conn: Any, video_id: str, *, queue: Any, priority: int) -> Any:
        self.calls.append((video_id, priority))
        return routes_write.SubmitResult(video_id, 7, "pending", created=True)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(routes_write, "submit_video", rec)
    monkeypatch.setattr(routes_write, "PostgresQueue", lambda conn: None)
    return rec


@pytest.fixture
def api(app: FastAPI, recorder: Recorder) -> Iterator[TestClient]:
    def fake_conn() -> Iterator[_Conn]:
        yield _Conn()

    app.dependency_overrides[deps.get_conn] = fake_conn
    with TestClient(app) as client:
        yield client


def test_mixed_input_gets_one_outcome_per_item_in_order_without_echo(
    api: TestClient, recorder: Recorder
) -> None:
    response = api.post(
        "/videos/batch",
        json={
            "urls": [
                A,
                "not a url",
                f"https://youtu.be/{A}",
                "https://www.youtube.com/playlist?list=PLabcdefghijklmnop",
            ]
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {"index": 0, "outcome": "queued", "video_id": A, "job_id": 7},
            {"index": 1, "outcome": "invalid", "reason": "not_a_video_ref"},
            {"index": 2, "outcome": "duplicate", "video_id": A, "first_index": 0},
            {"index": 3, "outcome": "invalid", "reason": "not_a_video_ref"},
        ],
        "counts": {
            "queued": 1, "restarted": 0, "already_submitted": 0, "duplicate": 1,
            "invalid": 2, "cancellation_in_progress": 0, "failed": 0,
        },
    }
    assert "not a url" not in response.text
    assert "playlist" not in response.text
    assert recorder.calls == [(A, PRIORITY_NORMAL)]


@pytest.mark.parametrize(
    "body",
    [
        {"urls": []},
        {"urls": [A, 123]},
        {"urls": [None]},
        {"urls": [A + MARKER * 300]},  # over MAX_REF_CHARS
        {"urls": MARKER},
        {},
        {"url": [A]},
        {"urls": [A], "extra": 1},
        {"urls": [A], MARKER: 1},
    ],
)
def test_an_invalid_body_is_422_without_echo_and_submits_nothing(
    api: TestClient, recorder: Recorder, body: dict[str, Any]
) -> None:
    response = api.post("/videos/batch", json=body)

    assert response.status_code == 422
    assert MARKER not in response.text
    assert recorder.calls == []


def test_the_cap_is_read_at_request_time_one_past_it_is_422_exactly_it_is_accepted(
    api: TestClient, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_write, "MAX_BATCH_SUBMIT", 2)

    over = api.post("/videos/batch", json={"urls": [A, B, "ABCDEFGHIJK"]})
    at = api.post("/videos/batch", json={"urls": [A, B]})

    assert over.status_code == 422
    assert A not in over.text
    assert at.status_code == 200
    assert [c[0] for c in recorder.calls] == [A, B]


def test_the_default_cap_is_the_playlist_cap() -> None:
    assert routes_write.MAX_BATCH_SUBMIT == 200


@pytest.mark.parametrize(
    "content_type", [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"]
)
def test_a_non_json_content_type_is_415_and_submits_nothing(
    api: TestClient, recorder: Recorder, content_type: str | None
) -> None:
    headers = {"Content-Type": content_type} if content_type else {}
    response = api.post("/videos/batch", content=f'{{"urls": ["{A}"]}}', headers=headers)

    assert response.status_code == 415
    assert recorder.calls == []


def _deny() -> None:
    raise HTTPException(status_code=401, detail="denied")


def _too_many() -> None:
    raise HTTPException(status_code=429, detail="slow down")


def test_batch_submit_inherits_auth_and_write_rate_limit(
    app: FastAPI, logs: LogSink, recorder: Recorder
) -> None:
    app.dependency_overrides[deps.require_auth] = _deny
    with TestClient(app) as client:
        assert client.post("/videos/batch", json={"urls": [A]}).status_code == 401

    app.dependency_overrides.clear()
    app.dependency_overrides[deps.rate_limit] = _too_many
    with TestClient(app) as client:
        assert client.post("/videos/batch", json={"urls": [A]}).status_code == 429
    assert recorder.calls == []
