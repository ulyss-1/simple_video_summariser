"""Database connections (architecture.md §2; issue #11).

``connect()`` is the only place that opens a psycopg connection from
``DATABASE_URL``. Callers (``PostgresQueue``, the repo layer, ...) receive a
connection; per architecture.md §5's "Database connection" acceptance
criterion, none of them create their own.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from common.config import get_settings
from common.safe_url import safe_dsn


class _SafeConnection(psycopg.Connection[Any]):
    """A connection whose connect failures never carry the DSN (#207).

    libpq echoes the whole DSN, password included, in the error for a
    malformed URI, and ``psycopg_pool``'s reconnect worker logs that text on
    every retry. The failure is replaced here, at the source, by one built from
    ``safe_dsn``, with no chained original, so neither the pool's logger nor a
    traceback can reach the secret.
    """

    @classmethod
    def connect(cls, conninfo: str = "", *args: Any, **kwargs: Any) -> Any:
        failure: psycopg.Error | None = None
        try:
            return super().connect(conninfo, *args, **kwargs)
        except psycopg.Error as exc:
            failure = exc
        # Raised outside the ``except`` block so the original is not chained.
        # Without the scheme: the target is useful, a URI-shaped string is not.
        shown = safe_dsn(conninfo)
        target = shown.partition("://")[2] or shown
        message = f"could not connect to {target} ({type(failure).__name__})"
        del failure
        raise psycopg.OperationalError(message)


def connect(**kwargs: Any) -> psycopg.Connection[Any]:
    """Open a new psycopg connection to ``get_settings().DATABASE_URL``.

    psycopg accepts the ``postgresql://`` scheme directly - unlike
    SQLAlchemy, which ``migrations/env.py`` must rewrite to
    ``postgresql+psycopg://`` - so the DSN is passed through unchanged.
    ``kwargs`` (e.g. ``connect_timeout``, ``options``) go to
    ``psycopg.connect`` as extra connection parameters (#39's ``/healthz``);
    a keyword overrides the same parameter in the DSN, so ``options=``
    replaces any ``options`` set in ``DATABASE_URL``.
    """
    conn: psycopg.Connection[Any] = _SafeConnection.connect(
        get_settings().DATABASE_URL.get_secret_value(), **kwargs
    )
    return conn


def _reset_connection(conn: psycopg.Connection[Any]) -> None:
    """Undo per-request session state before a connection goes back to the pool.

    ``read_only``, ``isolation_level`` and ``autocommit`` live on the
    connection object, not in the server session, so a read route's
    ``conn.read_only = True`` would otherwise make the next write route fail.
    """
    conn.read_only = None
    conn.isolation_level = None
    conn.deferrable = None
    conn.autocommit = False


def create_pool(*, connect_timeout: int, statement_timeout_ms: int) -> ConnectionPool[Any]:
    """A bounded, *unopened* pool of connections to ``DATABASE_URL`` (#95).

    Only the API uses it; CLI, workers, planner and migrate keep ``connect()``.
    Sizes and the wait timeout come from settings at call time. The caller
    opens it (``open(wait=False)``, so Postgres being down does not stop the
    app starting) and closes it. ``statement_timeout`` is a startup option,
    so it is set once when the pool creates a connection, not per request.
    ``check`` replaces a connection that went bad while idle before it is
    handed out; ``reset`` clears per-request session state on return.
    """
    settings = get_settings()
    return ConnectionPool(
        settings.DATABASE_URL.get_secret_value(),
        kwargs={
            "connect_timeout": connect_timeout,
            "options": f"-c statement_timeout={statement_timeout_ms}",
        },
        min_size=settings.DB_POOL_MIN_SIZE,
        max_size=settings.DB_POOL_MAX_SIZE,
        timeout=settings.DB_POOL_TIMEOUT_SEC,
        connection_class=_SafeConnection,
        check=ConnectionPool.check_connection,
        reset=_reset_connection,
        name="api",
        open=False,
    )
