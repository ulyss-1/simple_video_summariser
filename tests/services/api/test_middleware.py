"""Request id, request logging, error sanitising and CORS (issue #39)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator

import pytest
import structlog
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

import services.api.main as api_main
from services.api import routes_read
from services.api.main import RequestContextMiddleware
from tests.services.api.conftest import LogSink

MARKER = "ZZMARKER42"
_log = structlog.get_logger("tests.api")


@pytest.fixture
def probe_routes() -> Iterator[None]:
    """Test-only read routes, removed again afterwards."""
    router: APIRouter = routes_read.router
    before = list(router.routes)

    @router.get("/__test_log")
    def log_something() -> dict[str, str]:
        _log.info("inside handler")
        return {"ok": "yes"}

    @router.get("/__test_boom")
    def boom() -> dict[str, str]:
        raise RuntimeError(f"secret detail {MARKER}")

    @router.get("/__test_typed")
    def typed(n: int) -> dict[str, int]:
        return {"n": n}

    yield
    router.routes[:] = before


@pytest.fixture
def probe_client(probe_routes: None, logs: LogSink) -> Iterator[TestClient]:
    app: FastAPI = api_main.create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _request_lines(logs: LogSink) -> list[dict[str, object]]:
    return [r for r in logs.records() if r.get("event") == "request"]


# --- X-Request-Id --------------------------------------------------------------


@pytest.mark.parametrize(
    "incoming",
    ["0123456789abcdef0123456789abcdef", "a", "A.b_c-9", "x" * 128],
)
def test_a_valid_incoming_request_id_is_echoed_and_bound(
    probe_client: TestClient, logs: LogSink, incoming: str
) -> None:
    response = probe_client.get("/__test_log", headers={"X-Request-Id": incoming})

    assert response.headers["X-Request-Id"] == incoming
    inside = [r for r in logs.records() if r.get("event") == "inside handler"]
    assert [r["request_id"] for r in inside] == [incoming]


@pytest.mark.parametrize(
    "incoming",
    [
        "",
        "x" * 129,
        "has space",
        "quote'",
        'dq"',
        "semi;colon",
        "ünï",
        f"{MARKER}/slash",
    ],
)
def test_an_invalid_request_id_is_replaced_and_never_logged(
    probe_client: TestClient, logs: LogSink, incoming: str
) -> None:
    response = probe_client.get(
        "/__test_log", headers={"X-Request-Id": incoming.encode()}
    )

    effective = response.headers["X-Request-Id"]
    assert effective != incoming
    assert re.fullmatch(r"[0-9a-f]{32}", effective)
    inside = [r for r in logs.records() if r.get("event") == "inside handler"]
    assert [r["request_id"] for r in inside] == [effective]
    if incoming:
        assert incoming not in logs.text()


def test_a_missing_request_id_gets_a_generated_one(probe_client: TestClient) -> None:
    first = probe_client.get("/__test_log").headers["X-Request-Id"]
    second = probe_client.get("/__test_log").headers["X-Request-Id"]

    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert first != second


def test_request_id_does_not_leak_past_the_request(
    probe_client: TestClient, logs: LogSink
) -> None:
    probe_client.get("/__test_log", headers={"X-Request-Id": "first-req"})
    probe_client.get("/__test_boom", headers={"X-Request-Id": "boom-req"})
    _log.info("after requests")
    probe_client.get("/__test_log", headers={"X-Request-Id": "second-req"})

    records = logs.records()
    after = [r for r in records if r.get("event") == "after requests"]
    assert after and "request_id" not in after[0]
    inside = [r["request_id"] for r in records if r.get("event") == "inside handler"]
    assert inside == ["first-req", "second-req"]


def _http_scope(headers: list[tuple[bytes, bytes]]) -> Scope:
    return {"type": "http", "method": "GET", "path": "/x", "headers": headers}


async def _ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _raising_app(scope: Scope, receive: Receive, send: Send) -> None:
    raise RuntimeError(MARKER)


@pytest.mark.parametrize("inner", [_ok_app, _raising_app])
def test_the_middleware_unbinds_request_id_in_the_same_context(
    logs: LogSink, inner: object
) -> None:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    async def run() -> dict[str, object]:
        middleware = RequestContextMiddleware(inner)  # type: ignore[arg-type]
        await middleware(_http_scope([(b"x-request-id", b"ctx-1")]), receive, send)
        return structlog.contextvars.get_contextvars()

    assert asyncio.run(run()) == {}
    assert sent[0]["status"] in (204, 500)
    assert (b"x-request-id", b"ctx-1") in sent[0]["headers"]


@pytest.mark.parametrize(
    "headers",
    [
        [(b"x-request-id", b"a\r\nb")],
        [(b"x-request-id", b"\xff\xfe")],
        [(b"x-request-id", b"a\x00b")],
        [(b"x-request-id", b"x" * 129)],
    ],
)
def test_control_and_non_ascii_request_ids_are_replaced(
    headers: list[tuple[bytes, bytes]],
) -> None:
    assert re.fullmatch(r"[0-9a-f]{32}", api_main._request_id(_http_scope(headers)))


def test_a_128_character_request_id_is_kept() -> None:
    value = "y" * 128
    scope = _http_scope([(b"x-request-id", value.encode())])
    assert api_main._request_id(scope) == value


# --- one log line per request ---------------------------------------------------


def test_one_request_line_with_method_path_status_and_duration(
    probe_client: TestClient, logs: LogSink
) -> None:
    probe_client.get(f"/__test_log?token={MARKER}", headers={"X-Request-Id": "rid-1"})

    [line] = _request_lines(logs)
    assert line["method"] == "GET"
    assert line["path"] == "/__test_log"
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], (int, float))
    assert line["request_id"] == "rid-1"
    ours = [r for r in logs.records() if r.get("logger") != "httpx"]
    assert MARKER not in str(ours)


def test_a_failing_request_still_logs_one_request_line_with_status_500(
    probe_client: TestClient, logs: LogSink
) -> None:
    probe_client.get("/__test_boom", headers={"X-Request-Id": "rid-boom"})

    [line] = _request_lines(logs)
    assert line["status"] == 500
    assert line["request_id"] == "rid-boom"


# --- errors never reflect input ------------------------------------------------


def test_an_unhandled_exception_returns_a_generic_500_and_logs_the_traceback(
    probe_client: TestClient, logs: LogSink
) -> None:
    response = probe_client.get("/__test_boom", headers={"X-Request-Id": "rid-500"})

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert MARKER not in response.text
    assert response.headers["X-Request-Id"] == "rid-500"
    errors = [r for r in logs.records() if r.get("level") == "error"]
    assert errors
    assert errors[0]["request_id"] == "rid-500"
    assert "RuntimeError" in str(errors[0].get("exception"))


def test_a_validation_error_echoes_no_input(probe_client: TestClient) -> None:
    response = probe_client.get(f"/__test_typed?n={MARKER}")

    assert response.status_code == 422
    assert MARKER not in response.text
    for item in response.json()["detail"]:
        assert "input" not in item
        assert "ctx" not in item
        assert item["loc"] == ["query", "n"]


# --- CORS ------------------------------------------------------------------------


def test_cors_is_default_deny(probe_client: TestClient) -> None:
    response = probe_client.get(
        "/__test_log", headers={"Origin": "https://evil.example"}
    )
    preflight = probe_client.options(
        "/__test_log",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-origin" not in preflight.headers
