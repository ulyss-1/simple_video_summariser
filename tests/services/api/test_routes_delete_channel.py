"""``DELETE /channels/{channel_id}``: HTTP layer (issue #339).

No Postgres: ``get_conn`` yields a sentinel and ``routes_write.delete_channel`` is
replaced, so validation, status codes, error mapping and the response shape are
tested without a database.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus

from services.api import deps, routes_write
from services.api.backfill import (
    CancelResult,
    ChannelNotRegistered,
    DeleteChannelResult,
)

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
        self.result = DeleteChannelResult(
            CHANNEL, 3, CancelResult(CHANNEL, BID, 2, 0, 2, ("a", "b")), ()
        )

    def __call__(self, conn: Any, channel_id: str) -> DeleteChannelResult:
        self.calls.append(channel_id)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(routes_write, "delete_channel", rec)
    return rec


@pytest.fixture
def api(app: FastAPI, recorder: Recorder) -> Iterator[TestClient]:
    def fake_conn() -> Iterator[_Conn]:
        yield _Conn()

    app.dependency_overrides[deps.get_conn] = fake_conn
    with TestClient(app) as client:
        yield client


def test_a_valid_request_answers_with_the_count_and_the_cancelled_backfill(
    api: TestClient, recorder: Recorder
) -> None:
    response = api.delete(f"/channels/{CHANNEL}")

    assert response.status_code == 200
    assert response.json() == {
        "channel_id": CHANNEL,
        "deleted_videos": 3,
        "cancelled_backfill": {"backfill_id": BID, "cancelled_jobs": 2},
    }
    assert recorder.calls == [CHANNEL]


def test_no_active_request_answers_cancelled_backfill_null(
    api: TestClient, recorder: Recorder
) -> None:
    recorder.result = DeleteChannelResult(CHANNEL, 0, None, ())

    body = api.delete(f"/channels/{CHANNEL}").json()

    assert body == {"channel_id": CHANNEL, "deleted_videos": 0, "cancelled_backfill": None}


def test_an_unknown_channel_is_404_with_a_fixed_message(
    api: TestClient, recorder: Recorder
) -> None:
    recorder.error = ChannelNotRegistered(CHANNEL)

    response = api.delete(f"/channels/{CHANNEL}")

    assert response.status_code == 404
    assert response.json() == {"detail": routes_write.CHANNEL_NOT_REGISTERED}


def test_a_database_failure_is_the_write_503(api: TestClient, recorder: Recorder) -> None:
    recorder.error = psycopg.OperationalError("forced")

    response = api.delete(f"/channels/{CHANNEL}")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


@pytest.mark.parametrize(
    "channel",
    ["UC" + "a" * 21, "UC" + "a" * 23, "@" + MARKER, CHANNEL + "%0A", "UC" + MARKER + "a" * 11],
)
def test_a_non_canonical_channel_id_is_422_without_echo_and_before_any_call(
    api: TestClient, recorder: Recorder, channel: str
) -> None:
    response = api.delete(f"/channels/{channel}")

    assert response.status_code == 422
    assert response.json() == {"detail": routes_write.BAD_BACKFILL_CHANNEL}
    assert recorder.calls == []


def test_a_percent_encoded_canonical_id_is_422_because_a_delete_wants_the_raw_id(
    api: TestClient, recorder: Recorder
) -> None:
    encoded = CHANNEL[:2] + "%" + format(ord(CHANNEL[2]), "02X") + CHANNEL[3:]

    response = api.delete(f"/channels/{encoded}")

    assert response.status_code == 422
    assert response.json() == {"detail": routes_write.BAD_BACKFILL_CHANNEL}
    assert recorder.calls == []
