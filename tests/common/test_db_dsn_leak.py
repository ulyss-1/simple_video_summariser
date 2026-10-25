"""A failing connection never writes the DATABASE_URL password anywhere (issue #207).

Every test uses a sentinel password and asserts its absence from the exception
text and from the captured log output. Nothing here prints the sentinel or the
captured text. The capture sits on the root handler that ``configure_logging``
installs, so it includes third-party loggers (``psycopg``, ``psycopg.pool``).
"""

from __future__ import annotations

import io
import logging
import threading
from collections.abc import Iterator
from urllib.parse import quote

import psycopg
import pytest

from common.config import get_settings
from common.db import connect, create_pool
from common.logging import configure_logging

SENTINEL = "SENTINEL-pw-7Qx"
SPECIAL = "p@ss/w:rd#?[]%" + SENTINEL
RECONNECT_DEADLINE_SEC = 10.0

MALFORMED = f"postgresql://user:{SENTINEL}@[bad/db"
UNREACHABLE = f"postgresql://user:{SENTINEL}@127.0.0.1:1/db"
SPECIAL_UNREACHABLE = f"postgresql://user:{quote(SPECIAL, safe='')}@127.0.0.1:1/db"
SPECIAL_MALFORMED = f"postgresql://user:{quote(SPECIAL, safe='')}@[bad/db"

CASES = {
    "malformed": MALFORMED,
    "unreachable": UNREACHABLE,
    "special-characters": SPECIAL_UNREACHABLE,
    "special-characters-malformed": SPECIAL_MALFORMED,
}


class _Sink:
    def __init__(self) -> None:
        self.stream = io.StringIO()

    def text(self) -> str:
        return self.stream.getvalue()


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Sink]:
    monkeypatch.setenv("LOG_FORMAT", "json")
    get_settings.cache_clear()
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    captured = _Sink()
    yield captured
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
    root.setLevel(level)
    get_settings.cache_clear()


def _use(monkeypatch: pytest.MonkeyPatch, dsn: str, captured: _Sink) -> None:
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    configure_logging(get_settings(), stream=captured.stream)


def _assert_clean(*texts: str) -> None:
    for text in texts:
        assert SENTINEL not in text
        assert "p@ss" not in text
        assert quote(SPECIAL, safe="") not in text
        assert "@[bad" not in text  # the raw malformed DSN


@pytest.mark.parametrize("dsn", CASES.values(), ids=CASES.keys())
def test_connect_failure_text_and_chain_hold_no_password(
    dsn: str, monkeypatch: pytest.MonkeyPatch, sink: _Sink
) -> None:
    _use(monkeypatch, dsn, sink)

    with pytest.raises(psycopg.OperationalError) as caught:
        connect(connect_timeout=2)

    exc: BaseException | None = caught.value
    while exc is not None:  # the whole chain, in case the original is kept
        _assert_clean(str(exc), repr(exc))
        exc = exc.__cause__ or exc.__context__
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ or caught.value.__context__ is None
    _assert_clean(sink.text())


@pytest.mark.parametrize("dsn", CASES.values(), ids=CASES.keys())
def test_pool_reconnect_worker_logs_no_password(
    dsn: str, monkeypatch: pytest.MonkeyPatch, sink: _Sink
) -> None:
    _use(monkeypatch, dsn, sink)
    attempted = threading.Event()

    class Watch(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                attempted.set()

    watch = Watch()
    pool_logger = logging.getLogger("psycopg.pool")
    pool_logger.addHandler(watch)
    pool = create_pool(connect_timeout=1, statement_timeout_ms=1000)
    try:
        pool.open(wait=False)
        # Force the condition: wait for one failed reconnect attempt to be logged.
        assert attempted.wait(RECONNECT_DEADLINE_SEC), (
            "the pool's reconnect worker logged no failed attempt within the deadline"
        )
    finally:
        pool_logger.removeHandler(watch)
        pool.close()

    assert "psycopg.pool" in sink.text()  # the third-party record was captured
    _assert_clean(sink.text())


@pytest.mark.integration
def test_bad_credentials_leak_no_password(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch, sink: _Sink
) -> None:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(postgres_dsn)
    host = parts.netloc.rsplit("@", 1)[-1]
    bad = urlunsplit(parts._replace(netloc=f"{parts.username}:{SENTINEL}@{host}"))
    _use(monkeypatch, bad, sink)

    with pytest.raises(psycopg.OperationalError) as caught:
        connect(connect_timeout=5)

    _assert_clean(str(caught.value), repr(caught.value), sink.text())
