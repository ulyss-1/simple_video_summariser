"""Shared pytest fixtures (architecture.md 13; task #7).

Only integration tests touch Postgres. Pytest fixtures are lazy — a fixture
only runs if a test actually requests it (directly or through another
fixture) — so with `pytest -m "not integration"` no test asks for
`postgres_dsn`, `postgres_container` is never instantiated, and Docker is
never touched. This is what makes the fast path safe to run with Docker
Desktop stopped.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from testcontainers.community.postgres import PostgresContainer

# Same image tag as compose.yml (architecture.md 11.4, 16.1: >=18.6).
_POSTGRES_IMAGE = "postgres:18-alpine"


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """One Postgres container for the whole test session."""
    with PostgresContainer(_POSTGRES_IMAGE, driver=None) as container:
        yield container


@pytest.fixture
def postgres_dsn(postgres_container: PostgresContainer) -> Iterator[str]:
    """A ``postgresql://`` DSN for a fresh, empty database.

    Each test gets its own database on the shared container, created before
    and dropped after, so no test can see another test's data.
    """
    admin_dsn = postgres_container.get_connection_url()
    db_name = f"test_{uuid.uuid4().hex}"

    with psycopg.connect(admin_dsn, autocommit=True) as admin_conn:
        admin_conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
    try:
        yield _with_dbname(admin_dsn, db_name)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin_conn:
            admin_conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db_name))
            )


def _with_dbname(dsn: str, db_name: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{db_name}"))
