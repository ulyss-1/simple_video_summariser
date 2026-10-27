"""FastAPI dependencies shared by every router (issue #39; D13).

``require_auth`` is attached once at app level, so every route passes through
it; in v1 it does nothing. ``rate_limit`` is attached to the write router
only. Both are seams: real auth or throttling is one change here, not an
audit of every endpoint.

``get_conn`` is the only way a route reaches Postgres. It borrows a connection
from the pool the app's lifespan opened (``app.state.pool``, #95) and always
returns it; the pool rolls back an unfinished transaction and replaces a
connection that went bad while idle. Tests override it rather than patching
internals. ``get_read_conn`` wraps it
for the read routes (#42).

``get_catalog`` hands the backfill route its ``CatalogSource`` (#41).
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated, Any

import psycopg
import structlog
from fastapi import Depends, Request
from psycopg_pool import PoolTimeout

from common.backfill import ConnectionProvider
from common.config import get_settings
from common.models import CatalogSource, PlaylistSource
from common.progress import HISTORY_SIZE
from common.repo.transcripts import rtf_history

if TYPE_CHECKING:
    from adapters.youtube.channel_resolver import ResolvedChannel

ChannelResolver = Callable[[str, str], "ResolvedChannel"]

_log = structlog.get_logger(__name__)

#: Both stay well under the compose healthcheck's 5 s timeout (§11.4).
CONNECT_TIMEOUT_SEC = 2
STATEMENT_TIMEOUT_MS = 2000

#: The whole 503 body: no exception text, DSN or host ever reaches a client.
UNAVAILABLE_BODY = {"status": "unavailable"}


#: The read routes' 503 body (#42); ``/healthz`` keeps ``UNAVAILABLE_BODY``.
READ_UNAVAILABLE_BODY = {"detail": "database unavailable"}


class DatabaseUnavailable(Exception):
    """``get_conn`` could not open a connection; the app answers 503."""


class ReadDatabaseUnavailable(DatabaseUnavailable):
    """A read route's query failed with ``psycopg.OperationalError``; 503 (#42)."""


def require_auth() -> None:
    """No-op in v1 (D13). The one place real authentication will go."""


def rate_limit() -> None:
    """No-op in v1. The one place write throttling will go."""


def _checkout_reason(pool: Any, exc: psycopg.Error) -> dict[str, str]:
    """Why a ``PoolTimeout`` happened, for the operator's log line only (#218).

    The pool opens ``wait=False``, so connect failures happen in its background
    worker and ``getconn`` raises the same ``PoolTimeout`` whether the pool is
    exhausted or the database is down. ``pool.get_stats()`` tells them apart:

    - ``pool_size == 0``: nothing established or being established, so
      ``db_unreachable``.
    - ``connections_errors > 0`` and ``connections_ms == 0``: connects were
      tried, all failed, none ever completed, so ``db_unreachable``. Needed
      because ``pool_size`` also counts attempts still in progress (the real
      pool reports it as ``max_size`` against a closed port).
    - otherwise ``pool_size >= 1`` and ``pool_available == 0`` (connections
      exist, none free): ``pool_exhausted``. The ambiguous "database was up and
      is now dying" case lands here too: a connect did complete once, so it
      reads as a capacity problem until the pool drops its connections to zero
      (then ``db_unreachable``).

    Anything else (another error type, idle connections despite the timeout,
    ``get_stats`` missing or raising) yields no ``reason``: the plain 503 as
    before. The result carries no DSN, host or exception text, and the client
    response is unchanged (architecture.md section 1).
    """
    if not isinstance(exc, PoolTimeout):
        return {}
    try:
        stats = pool.get_stats()
        size, available = int(stats["pool_size"]), int(stats["pool_available"])
    except Exception:  # noqa: BLE001 - a failed diagnosis must not mask the 503
        return {}
    never_connected = stats.get("connections_errors", 0) > 0 and not stats.get("connections_ms", 0)
    if size == 0 or never_connected:
        return {"reason": "db_unreachable"}
    if available == 0:
        return {"reason": "pool_exhausted"}
    return {}


def get_conn(request: Request) -> Generator[psycopg.Connection[Any]]:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        # The lifespan did not run (or has ended): nothing to borrow from.
        raise DatabaseUnavailable
    try:
        # ``DB_POOL_TIMEOUT_SEC`` is the pool's own wait bound; PoolTimeout,
        # TooManyRequests and PoolClosed are all psycopg errors. The caller
        # sees only the 503 body: the exception goes to the log, whose
        # message carries no DSN (the pool never formats one).
        conn = pool.getconn()
    except psycopg.Error as exc:
        _log.warning("db.pool_unavailable", error=type(exc).__name__, **_checkout_reason(pool, exc))
        raise DatabaseUnavailable from exc
    try:
        yield conn
    finally:
        # putconn rolls back an open or aborted transaction before the
        # connection is reused, and discards a broken one.
        pool.putconn(conn)


def get_conn_provider(request: Request) -> ConnectionProvider:
    """A per-phase connection provider for ``backfill`` (#204).

    Each ``with provider() as conn`` is one ``get_conn`` lifecycle: borrow on
    entry, ``putconn`` (rollback, reset) on exit. A route that does long
    non-DB work uses it instead of ``get_conn`` so no pooled connection sits
    idle through that work. A failed borrow raises ``DatabaseUnavailable``.
    """

    @contextmanager
    def provide() -> Iterator[psycopg.Connection[Any]]:
        gen = get_conn(request)
        conn = next(gen)
        try:
            yield conn
        finally:
            gen.close()  # runs get_conn's ``finally``: putconn

    return provide


def get_read_conn(
    conn: Annotated[psycopg.Connection[Any], Depends(get_conn)],
) -> Generator[psycopg.Connection[Any]]:
    """``get_conn``'s connection, read-only, for the read routes (#42).

    Every transaction on it is ``READ ONLY`` and bounded by ``get_conn``'s
    ``statement_timeout``. A ``psycopg.OperationalError`` (the database went
    away, or ``QueryCanceled`` from the timeout) becomes
    ``ReadDatabaseUnavailable``, which the app answers with 503.
    """
    try:
        conn.read_only = True
        yield conn
    except psycopg.OperationalError as exc:
        raise ReadDatabaseUnavailable from exc


#: Below nginx's ``proxy_read_timeout 120s`` (architecture.md 11.3).
BACKFILL_LIST_TIMEOUT_SEC = 90


def get_catalog() -> CatalogSource:
    """The channel catalog for ``POST /channels/{id}/backfill`` (#41).

    The adapter is imported here, not at module level, so importing the app
    loads no ``adapters.*`` or yt-dlp module. Tests override this dependency.
    """
    from adapters.youtube.catalog import YouTubeCatalog

    return YouTubeCatalog(
        timeout=BACKFILL_LIST_TIMEOUT_SEC,
        cookie_file=get_settings().YTDLP_COOKIE_FILE,
    )


def get_playlist_source() -> PlaylistSource:
    """The playlist lister for ``POST /playlists/resolve`` (#342).

    The same adapter and 90 s timeout as ``get_catalog``, imported here for the same
    reason. Tests override this dependency.
    """
    from adapters.youtube.catalog import YouTubeCatalog

    return YouTubeCatalog(
        timeout=BACKFILL_LIST_TIMEOUT_SEC,
        cookie_file=get_settings().YTDLP_COOKIE_FILE,
    )


def get_channel_resolver() -> ChannelResolver:
    """The ``@handle``/``/c/``/``/user/`` resolver for ``POST /channels`` (#81).

    Imported here, like ``get_catalog``, so importing the app loads no adapter.
    Tests override this dependency.
    """
    from adapters.youtube.channel_resolver import resolve_channel

    return resolve_channel


def transcribe_rtf_history(conn: psycopg.Connection[Any]) -> list[float]:
    """Recent ``rtf`` of finished transcripts made with the configured Whisper model (#245).

    The basis of the estimate shown for a transcribe job that has not started.
    The API reads the same ``WHISPER_*`` settings as the transcriber.
    """
    settings = get_settings()
    return rtf_history(conn, settings.WHISPER_MODEL, settings.WHISPER_COMPUTE, HISTORY_SIZE)
