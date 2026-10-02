from __future__ import annotations

from collections.abc import Callable
from typing import cast

import psycopg
import pytest

from tests import conftest


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _wait() -> Callable[..., None]:
    wait = getattr(conftest, "_wait_for_postgres", None)
    assert callable(wait), "tests.conftest._wait_for_postgres is missing"
    return cast(Callable[..., None], wait)


def test_postgres_readiness_succeeds_without_sleeping() -> None:
    clock = FakeClock()
    calls: list[tuple[str, float]] = []

    def probe(dsn: str, connect_timeout_sec: int) -> None:
        calls.append((dsn, connect_timeout_sec))

    _wait()(
        "postgresql://user:secret@localhost:55012/db",
        "abc123",
        probe=probe,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert calls == [("postgresql://user:secret@localhost:55012/db", 5)]
    assert clock.sleeps == []


def test_postgres_readiness_retries_operational_errors_with_capped_backoff() -> None:
    clock = FakeClock()
    failures = [
        psycopg.OperationalError("refused"),
        psycopg.OperationalError("reset"),
        psycopg.OperationalError("starting"),
    ]
    calls = 0

    def probe(dsn: str, connect_timeout_sec: int) -> None:
        nonlocal calls
        calls += 1
        if failures:
            raise failures.pop(0)

    _wait()(
        "postgresql://user:secret@localhost:55012/db",
        "abc123",
        probe=probe,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert calls == 4
    assert clock.sleeps == [0.1, 0.2, 0.4]
    assert all(delay <= 1.0 for delay in clock.sleeps)


def test_postgres_readiness_deadline_reports_endpoint_without_password() -> None:
    clock = FakeClock()
    errors: list[psycopg.OperationalError] = []

    def probe(dsn: str, connect_timeout_sec: int) -> None:
        error = psycopg.OperationalError(f"cannot connect using {dsn}")
        errors.append(error)
        raise error

    with pytest.raises(RuntimeError) as exc_info:
        _wait()(
            "postgresql://user:secret@localhost:55012/db",
            "abc123",
            probe=probe,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            deadline_sec=0.31,
        )

    message = str(exc_info.value)
    assert "abc123" in message
    assert "localhost:55012" in message
    assert "0.3" in message
    assert "3 attempts" in message
    assert "secret" not in message
    assert exc_info.value.__cause__ is errors[-1]
    assert len(errors) == 3


def test_postgres_readiness_does_not_retry_other_exceptions() -> None:
    clock = FakeClock()
    calls = 0
    expected = ValueError("bad probe")

    def probe(dsn: str, connect_timeout_sec: int) -> None:
        nonlocal calls
        calls += 1
        raise expected

    with pytest.raises(ValueError) as exc_info:
        _wait()(
            "postgresql://user:secret@localhost:55012/db",
            "abc123",
            probe=probe,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert exc_info.value is expected
    assert calls == 1
    assert clock.sleeps == []
