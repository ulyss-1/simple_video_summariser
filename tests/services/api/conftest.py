"""Shared fixtures for the API tests (issue #39).

Fast tests never touch Postgres: ``DATABASE_URL`` points at a closed local
port, and any test that needs a database overrides ``deps.get_conn``. Log
output goes to an in-memory stream so tests can assert on the JSON lines.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.api.main as api_main
from common.config import Settings, get_settings
from common.logging import configure_logging
from tests.common.repo.conftest import head_dsn

__all__ = ["head_dsn"]

UNREACHABLE_DSN = "postgresql://user:SECRETPW@127.0.0.1:1/db"


class LogSink:
    def __init__(self) -> None:
        self.stream = io.StringIO()
        self.configure_calls = 0

    def records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]

    def text(self) -> str:
        return self.stream.getvalue()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DSN)
    monkeypatch.setenv("LOG_FORMAT", "json")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
    root.setLevel(level)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> LogSink:
    sink = LogSink()

    def capture(settings: Settings, *, stream: Any = None) -> None:
        sink.configure_calls += 1
        configure_logging(settings, stream=sink.stream)

    monkeypatch.setattr(api_main, "configure_logging", capture)
    return sink


@pytest.fixture
def app(logs: LogSink) -> FastAPI:
    return api_main.create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
