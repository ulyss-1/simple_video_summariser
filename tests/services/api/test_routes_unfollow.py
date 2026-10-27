"""``POST /channels/{channel_id}/unfollow``: HTTP layer (issue #235).

No Postgres: ``get_conn`` yields a sentinel and ``routes_write.unfollow_channel``
is replaced, so validation, status codes, error mapping and the response shape
are tested without a database.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus

from services.api import deps, routes_write
from services.api.backfill import CancelResult, ChannelNotRegistered, UnfollowResult

CHANNEL = "UCuAXFkgsw1L7xaCfnd5JJOw"
BID = "0f8fad5b-d9cb-469f-a165-70867728950e"
MARKER = "ZZMARKER42"


class _Info:
    transaction_status = TransactionStatus.IDLE


class _Conn:
    info = _Info()


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: Exception | None = None
        self.result = UnfollowResult(CHANNEL, True, CancelResult(CHANNEL, BID, 2, 2, 0, ("a", "b")))

    def __call__(self, conn: Any, channel_id: str) -> UnfollowResult:
        self.calls.append(channel_id)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(routes_write, "unfollow_channel", rec)
    return rec


@pytest.fixture
def api(app: FastAPI, recorder: Recorder) -> Iterator[TestClient]:
    def fake_conn() -> Iterator[_Conn]:
        yield _Conn()

    app.dependency_overrides[deps.get_conn] = fake_conn
    with TestClient(app) as client:
        yield client


def _url(channel: str = CHANNEL) -> str:
    return f"/channels/{channel}/unfollow"


def test_a_valid_request_answers_with_the_result_and_the_cancelled_backfill(
    api: TestClient, recorder: Recorder
) -> None:
    response = api.post(_url(), json={})

    assert response.status_code == 200
    assert response.json() == {
        "channel_id": CHANNEL,
        "active": False,
        "changed": True,
        "backfill": {"backfill_id": BID, "cancelled_jobs": 2},
    }
    assert recorder.calls == [CHANNEL]


def test_no_active_request_and_no_change_answers_backfill_null(
    api: TestClient, recorder: Recorder
) -> None:
    recorder.result = UnfollowResult(CHANNEL, False, None)

    body = api.post(_url(), json={}).json()

    assert (body["changed"], body["backfill"]) == (False, None)


def test_an_unknown_channel_is_404_with_a_fixed_message(
    api: TestClient, recorder: Recorder
) -> None:
    recorder.error = ChannelNotRegistered(CHANNEL)

    response = api.post(_url(), json={})

    assert response.status_code == 404
    assert response.json() == {"detail": routes_write.CHANNEL_NOT_REGISTERED}


@pytest.mark.parametrize(
    "channel",
    ["UC" + "a" * 21, "UC" + "a" * 23, "@" + MARKER, CHANNEL + "%0A", "UC" + MARKER + "a" * 11],
)
def test_a_non_canonical_channel_id_is_422_without_echo_and_before_any_call(
    api: TestClient, recorder: Recorder, channel: str
) -> None:
    response = api.post(_url(channel), json={})

    assert response.status_code == 422
    assert response.json() == {"detail": routes_write.BAD_BACKFILL_CHANNEL}
    assert recorder.calls == []


def test_a_percent_encoded_canonical_id_is_accepted(api: TestClient, recorder: Recorder) -> None:
    encoded = CHANNEL[:2] + "%" + format(ord(CHANNEL[2]), "02X") + CHANNEL[3:]

    assert api.post(_url(encoded), json={}).status_code == 200
    assert recorder.calls == [CHANNEL]


@pytest.mark.parametrize("body", [{"x": 1}, {MARKER: 1}, [], "s"])
def test_a_non_empty_or_non_object_body_is_422_without_echo(
    api: TestClient, recorder: Recorder, body: Any
) -> None:
    response = api.post(_url(), json=body)

    assert response.status_code == 422
    assert MARKER not in response.text
    assert recorder.calls == []


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
def test_a_non_json_content_type_is_415_and_unfollows_nothing(
    api: TestClient, recorder: Recorder, content_type: str | None
) -> None:
    headers = {} if content_type is None else {"Content-Type": content_type}

    response = api.post(_url(), content=b"{}", headers=headers)

    assert response.status_code == 415
    assert recorder.calls == []
