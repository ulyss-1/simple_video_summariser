"""Video repository functions (issue #14, architecture.md §6)."""

from __future__ import annotations

from typing import Any

import psycopg

from common.models import FeedEntry, VideoMeta
from common.repo._hygiene import clean_text

_UNAVAILABLE_REASONS = {"removed", "private", "geoblocked", "agegated"}


def upsert_video(conn: psycopg.Connection[Any], meta: VideoMeta, origin: str) -> None:
    """Insert ``meta`` as a video, or update its mutable fields on a later call.

    ``title``, ``duration_sec``, ``published_at`` and ``description`` are
    refreshed on every call, and a missing ``channel_id`` (a stub row from
    ``record_unavailable``) is filled in. ``origin`` and ``discovered_at`` are set only
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
            channel_id   = COALESCE(videos.channel_id, EXCLUDED.channel_id),
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


def _check_reason(reason: str) -> None:
    if reason not in _UNAVAILABLE_REASONS:
        raise ValueError(
            f"unavailable reason must be one of {sorted(_UNAVAILABLE_REASONS)}, got {reason!r}"
        )


def mark_unavailable(conn: psycopg.Connection[Any], video_id: str, reason: str) -> None:
    """Record why ``video_id`` can no longer be fetched.

    ``reason`` must be one of ``removed``, ``private``, ``geoblocked`` or
    ``agegated``; anything else raises ``ValueError``.
    """
    _check_reason(reason)
    conn.execute(
        "UPDATE videos SET unavailable = %s WHERE video_id = %s",
        (reason, video_id),
    )


def clear_unavailable(conn: psycopg.Connection[Any], video_id: str) -> None:
    """Reset ``videos.unavailable`` to NULL, e.g. a private video went public.

    A no-op when ``video_id`` has no row.
    """
    conn.execute("UPDATE videos SET unavailable = NULL WHERE video_id = %s", (video_id,))


def record_unavailable(
    conn: psycopg.Connection[Any], video_id: str, reason: str, *, origin: str
) -> None:
    """Mark ``video_id`` unavailable, creating a stub row if it has none.

    The stub has a NULL ``channel_id`` and the given ``origin`` (first
    ingest of an already-removed video). An existing row keeps its
    ``origin`` and metadata; only ``unavailable`` changes. ``reason`` is
    validated like ``mark_unavailable``.
    """
    _check_reason(reason)
    conn.execute(
        """
        INSERT INTO videos (video_id, origin, unavailable)
        VALUES (%s, %s, %s)
        ON CONFLICT (video_id) DO UPDATE SET unavailable = EXCLUDED.unavailable
        """,
        (video_id, origin, reason),
    )


def insert_discovered_video(conn: psycopg.Connection[Any], entry: FeedEntry, origin: str) -> bool:
    """Insert the video ``entry`` lists, unless it already has a row.

    Returns whether a row was inserted. An existing row is left exactly as it is
    (``origin``, ``title``, ``description``, ``discovered_at`` and the rest), which is
    what makes polling the same feed twice harmless. ``upsert_video`` is the wrong tool
    here: it needs full metadata and overwrites ``title`` and ``description``.

    A missing ``channels`` row is created inactive, as ``upsert_video`` does.
    """
    conn.execute(
        """
        INSERT INTO channels (channel_id, active)
        VALUES (%s, false)
        ON CONFLICT (channel_id) DO NOTHING
        """,
        (entry.channel_id,),
    )
    cur = conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, published_at, origin)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (video_id) DO NOTHING
        """,
        (entry.video_id, entry.channel_id, clean_text(entry.title), entry.published_at, origin),
    )
    return cur.rowcount == 1


def video_exists(conn: psycopg.Connection[Any], video_id: str) -> bool:
    """Whether ``videos`` has a row for ``video_id`` (issue #29)."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM videos WHERE video_id = %s)", (video_id,)
    ).fetchone()
    assert row is not None
    return bool(row[0])


def get_video_meta(conn: psycopg.Connection[Any], video_id: str) -> VideoMeta | None:
    """The stored fields of ``video_id`` as a ``VideoMeta``, or ``None`` without a row.

    Fields the ``videos`` table does not store (``language``, ``live_status``, the
    subtitle language lists) are ``None`` or ``()``. NULL text columns (a stub row
    from ``record_unavailable``) come back as empty strings.
    """
    row = conn.execute(
        """
        SELECT video_id, channel_id, title, description, duration_sec, published_at
        FROM videos
        WHERE video_id = %s
        """,
        (video_id,),
    ).fetchone()
    if row is None:
        return None
    found_id, channel_id, title, description, duration_sec, published_at = row
    return VideoMeta(
        video_id=found_id,
        channel_id=channel_id or "",
        title=title or "",
        description=description or "",
        duration_sec=duration_sec,
        published_at=published_at,
        language=None,
        live_status=None,
        manual_subtitle_langs=(),
        auto_caption_langs=(),
    )
