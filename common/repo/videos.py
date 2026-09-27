"""Video repository functions (issue #14, architecture.md §6)."""

from __future__ import annotations

from typing import Any

import psycopg

from common.models import VideoMeta
from common.repo._hygiene import clean_text

_UNAVAILABLE_REASONS = {"removed", "private", "geoblocked", "agegated"}


def upsert_video(conn: psycopg.Connection[Any], meta: VideoMeta, origin: str) -> None:
    """Insert ``meta`` as a video, or update its mutable fields on a later call.

    ``title``, ``duration_sec``, ``published_at`` and ``description`` are
    refreshed on every call. ``origin`` and ``discovered_at`` are set only
    on the first insert - they record the video's *first* discovery, so a
    later call, even with a different ``origin``, never changes them.

    When ``meta.channel_id`` has no ``channels`` row yet, one is created
    with ``active = false`` so the foreign key holds without starting
    monitoring for an ad-hoc submission. An existing channel row, active or
    not, is left untouched.
    """
    conn.execute(
        """
        INSERT INTO channels (channel_id, active)
        VALUES (%s, false)
        ON CONFLICT (channel_id) DO NOTHING
        """,
        (meta.channel_id,),
    )
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, duration_sec,
                             published_at, description, origin)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (video_id) DO UPDATE SET
            title        = EXCLUDED.title,
            duration_sec = EXCLUDED.duration_sec,
            published_at = EXCLUDED.published_at,
            description  = EXCLUDED.description
        """,
        (
            meta.video_id,
            meta.channel_id,
            clean_text(meta.title),
            meta.duration_sec,
            meta.published_at,
            clean_text(meta.description),
            origin,
        ),
    )


def mark_unavailable(conn: psycopg.Connection[Any], video_id: str, reason: str) -> None:
    """Record why ``video_id`` can no longer be fetched.

    ``reason`` must be one of ``removed``, ``private``, ``geoblocked`` or
    ``agegated``; anything else raises ``ValueError``.
    """
    if reason not in _UNAVAILABLE_REASONS:
        raise ValueError(
            f"unavailable reason must be one of {sorted(_UNAVAILABLE_REASONS)}, got {reason!r}"
        )
    conn.execute(
        "UPDATE videos SET unavailable = %s WHERE video_id = %s",
        (reason, video_id),
    )
