"""Channel repository functions (issue #14, architecture.md §6)."""

from __future__ import annotations

from typing import Any

import psycopg

from common.models import Channel
from common.repo._hygiene import clean_text


def add_channel(conn: psycopg.Connection[Any], channel_id: str, title: str | None) -> None:
    """Create an active channel with ``monitor_from = now()``.

    A no-op if ``channel_id`` already has a row, active or not. Calling
    this again for an already-active channel changes nothing, not even
    ``monitor_from``. Re-activating a channel that ``upsert_video``
    (``common/repo/videos.py``) created as inactive, or one that was later
    deactivated, is out of scope (moved to #40) - doing nothing on conflict
    is the conservative choice that avoids implementing that policy here by
    accident.
    """
    conn.execute(
        """
        INSERT INTO channels (channel_id, title, active, monitor_from)
        VALUES (%s, %s, true, now())
        ON CONFLICT (channel_id) DO NOTHING
        """,
        (channel_id, clean_text(title)),
    )


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
