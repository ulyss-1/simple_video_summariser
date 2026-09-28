"""Media repository: retention queries (issue #36; architecture.md §6 ``media``, §7.2).

``media.path`` is relative to ``AUDIO_DIR``. This module holds the queries
the planner's audio retention needs: what is expired, what is oldest, how many
bytes are recorded, which paths are referenced, and the lock-recheck-delete
step that lets retention delete one row safely while a transcriber may be
working on the same video. #29 adds ``register_media`` / ``delete_media``
to this module separately.

Like every ``common/repo/`` function these take a psycopg connection first,
use parameterized SQL and never commit or roll back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import psycopg

_COLUMNS = "id, video_id, path, bytes, created_at, expires_at"


@dataclass(frozen=True, slots=True)
class MediaRow:
    """One ``media`` row."""

    id: int
    video_id: str
    path: str
    bytes: int
    created_at: datetime
    expires_at: datetime


class LockOutcome(StrEnum):
    """Result of ``lock_for_deletion``."""

    #: Row is locked by this transaction and still eligible.
    LOCKED = "locked"
    #: Row is gone or locked by another connection; skip it.
    UNAVAILABLE = "unavailable"
    #: Row is eligible but its video has a ``running`` job; do not delete.
    RUNNING = "running"
    #: Row is no longer expired / the total is no longer over the cap.
    NOT_ELIGIBLE = "not_eligible"


def _row(values: tuple[Any, ...]) -> MediaRow:
    return MediaRow(*values)


def list_expired(conn: psycopg.Connection[Any], now: datetime) -> list[MediaRow]:
    """Rows with ``expires_at <= now`` (the boundary counts as expired), oldest expiry first."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM media WHERE expires_at <= %s ORDER BY expires_at, id",
        (now,),
    ).fetchall()
    return [_row(r) for r in rows]


def list_oldest_first(conn: psycopg.Connection[Any]) -> list[MediaRow]:
    """All rows by ``created_at`` ascending, ties broken by ``id`` ascending."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM media ORDER BY created_at, id"
    ).fetchall()
    return [_row(r) for r in rows]


def total_bytes(conn: psycopg.Connection[Any]) -> int:
    """``SUM(media.bytes)``; 0 for an empty table."""
    row = conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM media").fetchone()
    assert row is not None
    return int(row[0])


def list_referenced_paths(conn: psycopg.Connection[Any]) -> set[str]:
    """Every ``media.path`` (relative to ``AUDIO_DIR``)."""
    rows = conn.execute("SELECT path FROM media").fetchall()
    return {r[0] for r in rows}


def lock_for_deletion(
    conn: psycopg.Connection[Any],
    media_id: int,
    *,
    expired_at: datetime | None = None,
    max_bytes: int | None = None,
) -> tuple[LockOutcome, MediaRow | None]:
    """Lock one row and re-check that it may still be deleted.

    Exactly one criterion must be given: ``expired_at`` (the row must still
    have ``expires_at <= expired_at``) or ``max_bytes`` (the total recorded
    bytes must still exceed it). The row is taken with
    ``SELECT ... FOR UPDATE SKIP LOCKED``, so a row another connection holds
    is reported ``UNAVAILABLE`` instead of waited on. A row whose video has
    a ``running`` job is reported ``RUNNING``.

    Only ``LOCKED`` returns the row; the lock lasts until the caller commits
    or rolls back, after it has unlinked the file and called ``delete_row``.
    """
    if (expired_at is None) == (max_bytes is None):
        raise ValueError("give exactly one of expired_at or max_bytes")

    found = conn.execute(
        f"SELECT {_COLUMNS} FROM media WHERE id = %s FOR UPDATE SKIP LOCKED",
        (media_id,),
    ).fetchone()
    if found is None:
        return LockOutcome.UNAVAILABLE, None
    row = _row(found)

    if expired_at is not None:
        eligible = row.expires_at <= expired_at
    else:
        assert max_bytes is not None
        eligible = total_bytes(conn) > max_bytes
    if not eligible:
        return LockOutcome.NOT_ELIGIBLE, None

    running = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM jobs WHERE video_id = %s AND state = 'running')",
        (row.video_id,),
    ).fetchone()
    assert running is not None
    if running[0]:
        return LockOutcome.RUNNING, None
    return LockOutcome.LOCKED, row


def delete_row(conn: psycopg.Connection[Any], media_id: int) -> None:
    """Delete one ``media`` row by id. Does not commit."""
    conn.execute("DELETE FROM media WHERE id = %s", (media_id,))
