"""FastAPI dependencies shared by every router (issue #39; D13).

``require_auth`` is attached once at app level, so every route passes through
it; in v1 it does nothing. ``rate_limit`` is attached to the write router
only. Both are seams: real auth or throttling is one change here, not an
audit of every endpoint.

``get_conn`` is the only way a route reaches Postgres. It opens a short-lived,
time-bounded connection through ``common.db.connect()`` and always closes it.
Tests override it rather than patching internals.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import psycopg

from common.db import connect

#: Both stay well under the compose healthcheck's 5 s timeout (§11.4).
CONNECT_TIMEOUT_SEC = 2
STATEMENT_TIMEOUT_MS = 2000

#: The whole 503 body: no exception text, DSN or host ever reaches a client.
UNAVAILABLE_BODY = {"status": "unavailable"}


class DatabaseUnavailable(Exception):
    """``get_conn`` could not open a connection; the app answers 503."""


def require_auth() -> None:
    """No-op in v1 (D13). The one place real authentication will go."""


def rate_limit() -> None:
    """No-op in v1. The one place write throttling will go."""


def get_conn() -> Generator[psycopg.Connection[Any]]:
    try:
        conn = connect(
            connect_timeout=CONNECT_TIMEOUT_SEC,
            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
        )
    except psycopg.Error as exc:
        raise DatabaseUnavailable from exc
    try:
        yield conn
    finally:
        conn.close()
