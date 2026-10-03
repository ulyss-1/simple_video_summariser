"""``GET /healthz`` (issue #39)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api import deps
from tests.services.api.conftest import UNREACHABLE_DSN, LogSink

KINDS = ("ingest", "transcribe", "analyze")
STATES = ("pending", "running", "done", "dead")


def _zeros() -> dict[str, dict[str, int]]:
    return {kind: dict.fromkeys(STATES, 0) for kind in KINDS}


class FakeConn:
    def __init__(
        self,
        rows: list[tuple[str, str, int]] | None = None,
        error: Exception | None = None,
    ):
        self.rows = rows or []
        self.error = error
        self.read_only = False
        self.queries: list[str] = []

    def execute(self, query: str, *args: Any, **kwargs: Any) -> FakeConn:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self

    def fetchall(self) -> list[tuple[str, str, int]]:
        return self.rows


def _use(app: FastAPI, conn: FakeConn) -> None:
    def override() -> Iterator[FakeConn]:
        yield conn

    app.dependency_overrides[deps.get_conn] = override


def test_empty_jobs_table_returns_a_zero_filled_shape(app: FastAPI) -> None:
    _use(app, FakeConn([]))

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "queue": _zeros()}


def test_counts_are_placed_under_their_kind_and_state(app: FastAPI) -> None:
    _use(
        app,
        FakeConn(
            [
                ("ingest", "done", 3),
                ("transcribe", "dead", 2),
                ("analyze", "running", 1),
            ]
        ),
    )

    with TestClient(app) as client:
        body = client.get("/healthz").json()

    expected = _zeros()
    expected["ingest"]["done"] = 3
    expected["transcribe"]["dead"] = 2
    expected["analyze"]["running"] = 1
    assert body["queue"] == expected


def test_unknown_kinds_and_states_are_kept_under_their_own_keys(app: FastAPI) -> None:
    _use(app, FakeConn([("notify", "pending", 4), ("analyze", "paused", 1)]))

    with TestClient(app) as client:
        queue = client.get("/healthz").json()["queue"]

    assert queue["notify"] == {"pending": 4}
    assert queue["analyze"] == {
        "pending": 0,
        "running": 0,
        "done": 0,
        "dead": 0,
        "paused": 1,
    }
    assert queue["ingest"] == dict.fromkeys(STATES, 0)


@pytest.mark.parametrize(
    "error",
    [
        psycopg.OperationalError(f"connection to {UNREACHABLE_DSN} failed"),
        psycopg.errors.QueryCanceled("canceling statement due to statement timeout"),
        RuntimeError(f"boom {UNREACHABLE_DSN}"),
    ],
)
def test_a_database_error_returns_503_without_details_and_is_logged(
    app: FastAPI, logs: LogSink, error: Exception
) -> None:
    _use(app, FakeConn(error=error))

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    for secret in ("SECRETPW", "127.0.0.1", "postgresql://", "Traceback", "boom"):
        assert secret not in response.text
    failures = [r for r in logs.records() if r.get("level") == "error"]
    assert failures and "exception" in failures[0]


def test_a_failing_connect_returns_503_and_logs_the_traceback(
    app: FastAPI, logs: LogSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(**kwargs: Any) -> Any:
        raise psycopg.OperationalError(f"could not connect to {UNREACHABLE_DSN}")

    monkeypatch.setattr(deps, "connect", refuse)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    failures = [r for r in logs.records() if r.get("level") == "error"]
    assert failures and "OperationalError" in str(failures[0].get("exception"))


def test_get_conn_closes_the_connection_after_a_normal_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []

    class Conn:
        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(deps, "connect", lambda **kwargs: Conn())

    gen = deps.get_conn()
    next(gen)
    gen.close()

    assert closed == [True]


def test_an_unreachable_database_answers_503_quickly(app: FastAPI) -> None:
    with TestClient(app) as client:
        started = time.monotonic()
        response = client.get("/healthz")
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert elapsed < 5


def test_get_conn_bounds_connect_and_statement_time_and_always_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[dict[str, Any]] = []
    closed: list[bool] = []

    class Conn:
        def close(self) -> None:
            closed.append(True)

    def fake_connect(**kwargs: Any) -> Conn:
        opened.append(kwargs)
        return Conn()

    monkeypatch.setattr(deps, "connect", fake_connect)

    gen = deps.get_conn()
    next(gen)
    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("handler failed"))

    assert closed == [True]
    [kwargs] = opened
    assert 0 < int(kwargs["connect_timeout"]) <= 2
    options = str(kwargs["options"])
    assert "statement_timeout=" in options
    timeout_ms = int(options.split("statement_timeout=")[1].split()[0])
    assert 0 < timeout_ms <= 2000


@pytest.mark.integration
def test_healthz_counts_seeded_jobs_in_real_postgres(
    head_dsn: str, monkeypatch: pytest.MonkeyPatch, logs: LogSink
) -> None:
    from common.config import get_settings
    from services.api.main import create_app

    with psycopg.connect(head_dsn) as conn:
        for i, (kind, state) in enumerate(
            [
                ("ingest", "done"),
                ("ingest", "done"),
                ("ingest", "pending"),
                ("transcribe", "dead"),
                ("analyze", "running"),
                ("notify", "pending"),
            ]
        ):
            conn.execute(
                "INSERT INTO jobs (video_id, kind, dedupe_key, state) VALUES (%s, %s, %s, %s)",
                (f"vid{i:08d}", kind, "k", state),
            )

    monkeypatch.setenv("DATABASE_URL", head_dsn)
    get_settings.cache_clear()

    with TestClient(create_app()) as client:
        response = client.get("/healthz")

    expected = _zeros()
    expected["ingest"]["done"] = 2
    expected["ingest"]["pending"] = 1
    expected["transcribe"]["dead"] = 1
    expected["analyze"]["running"] = 1
    expected["notify"] = {"pending": 1}
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "queue": expected}
