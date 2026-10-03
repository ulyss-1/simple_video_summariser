"""FastAPI dependencies shared by every router (issue #39; D13).

``require_auth`` is attached once at app level, so every route passes through
it; in v1 it does nothing. ``rate_limit`` is attached to the write router
only. Both are seams: real auth or throttling is one change here, not an
audit of every endpoint.

``get_conn`` is the only way a route reaches Postgres. It opens a short-lived,
time-bounded connection through ``common.db.connect()`` and always closes it.
Tests override it rather than patching internals.

``get_catalog`` hands the backfill route its ``CatalogSource`` (#41).
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import psycopg

from common.db import connect
from common.models import CatalogSource

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


#: Below nginx's ``proxy_read_timeout 120s`` (architecture.md 11.3).
BACKFILL_LIST_TIMEOUT_SEC = 90


def get_catalog() -> CatalogSource:
    """The channel catalog for ``POST /channels/{id}/backfill`` (#41).

    The adapter is imported here, not at module level, so importing the app
    loads no ``adapters.*`` or yt-dlp module. Tests override this dependency.
    """
    from adapters.youtube.catalog import YouTubeCatalog

    return YouTubeCatalog(timeout=BACKFILL_LIST_TIMEOUT_SEC)
