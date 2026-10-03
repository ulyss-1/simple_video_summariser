"""Tests for ``common.db.connect`` (issue #11, architecture.md §2).

``connect()`` is the only function in scope here: it must hand back a working
psycopg connection built from ``get_settings().DATABASE_URL``, and nothing
else. The queue and repo layers receive a connection; they never open their
own (AGENTS.md -> Rules, architecture.md §2).
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from common.config import get_settings
from common.db import connect

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_connect_returns_a_working_connection_built_from_database_url(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    get_settings.cache_clear()

    conn = connect()
    try:
        assert isinstance(conn, psycopg.Connection)
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 1
    finally:
        conn.close()


def test_connect_uses_the_postgresql_scheme_dsn_directly(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # postgres_dsn from the fixture is already postgresql://; psycopg accepts
    # that scheme natively, unlike SQLAlchemy (migrations/env.py rewrites it
    # to postgresql+psycopg:// for that reason). No rewriting should happen
    # here.
    assert postgres_dsn.startswith("postgresql://")
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    get_settings.cache_clear()

    conn = connect()
    conn.close()


def test_connect_passes_connection_parameters_through(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    get_settings.cache_clear()

    with connect(connect_timeout=2, options="-c statement_timeout=1234") as conn:
        row = conn.execute("SHOW statement_timeout").fetchone()

    assert row == ("1234ms",)
