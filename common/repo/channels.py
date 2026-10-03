"""Channel repository functions (issue #14, architecture.md §6)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from common.models import Channel
from common.repo._hygiene import clean_text


def add_channel(conn: psycopg.Connection[Any], channel_id: str, title: str | None) -> None:
    """Create an active channel with ``monitor_from = now()``.

    A no-op if ``channel_id`` already has a row, active or not. Calling
    this again for an already-active channel changes nothing, not even
    ``monitor_from``. To also re-activate an inactive channel (one that
    ``upsert_video`` created for an ad-hoc video, or one that was
    deactivated), use ``register_channel``.
    """
    conn.execute(
        """
        INSERT INTO channels (channel_id, title, active, monitor_from)
        VALUES (%s, %s, true, now())
        ON CONFLICT (channel_id) DO NOTHING
        """,
        (channel_id, clean_text(title)),
    )


@dataclass(frozen=True, slots=True)
class RegisteredChannel:
    channel: Channel
    created: bool


def register_channel(conn: psycopg.Connection[Any], channel_id: str) -> RegisteredChannel:
    """Start forward-only monitoring of ``channel_id`` (#40, D9b) in one statement.

    - Unknown channel: inserted with ``active = true`` and ``monitor_from = now()``.
    - Inactive channel: set ``active = true`` and ``monitor_from = now()``, so
      the gap is never backfilled; ``title`` and ``added_at`` are kept.
    - Active channel: nothing changes, not even ``monitor_from``.

    The state change is the single ``INSERT ... ON CONFLICT DO UPDATE ... WHERE``
    statement; an already-active channel is then read back. ``created`` is true
    only for the insert. Concurrent calls for the same new channel are safe:
    ``ON CONFLICT`` waits for the other insert, then takes the no-op path.
    """
    row = conn.execute(
        """
        INSERT INTO channels AS c (channel_id, active, monitor_from)
        VALUES (%(id)s, true, now())
        ON CONFLICT (channel_id) DO UPDATE
            SET active = true, monitor_from = now()
            WHERE c.active = false
        RETURNING c.channel_id, c.title, c.active, c.monitor_from, c.last_polled,
                  c.last_poll_err, c.added_at, (c.xmax = 0) AS created
        """,
        {"id": channel_id},
    ).fetchone()
    if row is not None:
        return RegisteredChannel(channel=_channel(row[:7]), created=bool(row[7]))
    # Already active: the upsert changed nothing and returned nothing. Read it
    # in a new statement, whose snapshot also sees a row a concurrent
    # registration has just committed.
    existing = conn.execute(
        """
        SELECT channel_id, title, active, monitor_from, last_polled, last_poll_err, added_at
        FROM channels WHERE channel_id = %(id)s
        """,
        {"id": channel_id},
    ).fetchone()
    assert existing is not None
    return RegisteredChannel(channel=_channel(existing), created=False)


def list_active_channels(conn: psycopg.Connection[Any]) -> list[Channel]:
    """Every channel with ``active = true``."""
    rows = conn.execute(
        """
        SELECT channel_id, title, active, monitor_from, last_polled,
               last_poll_err, added_at
        FROM channels
        WHERE active = true
        ORDER BY channel_id
        """
    ).fetchall()
    return [_channel(row) for row in rows]


def record_poll(conn: psycopg.Connection[Any], channel_id: str, error: str | None = None) -> None:
    """Set ``last_polled = now()`` and ``last_poll_err`` to ``error``.

    Passing no error clears whatever error the previous poll recorded.
    """
    conn.execute(
        """
        UPDATE channels
        SET last_polled = now(), last_poll_err = %s
        WHERE channel_id = %s
        """,
        (clean_text(error), channel_id),
    )


def _channel(row: tuple[Any, ...]) -> Channel:
    channel_id, title, active, monitor_from, last_polled, last_poll_err, added_at = row
    return Channel(
        channel_id=channel_id,
        title=title,
        active=active,
        monitor_from=monitor_from,
        last_polled=last_polled,
        last_poll_err=last_poll_err,
        added_at=added_at,
    )
