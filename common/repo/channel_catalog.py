"""Persisted channel catalog snapshots and their metadata enrichment (#242).

A catalog row is not a ``videos`` row: browsing an upload never makes it known.
Nothing here writes ``videos``, ``jobs`` or ``deleted_videos``. A tombstoned
(deleted) video is reported ``known`` so it is shown but not selectable.
"""

from __future__ import annotations

from typing import Any, cast

import psycopg

from common.models import (
    CatalogClaim,
    CatalogEnrichmentState,
    CatalogPage,
    CatalogVideo,
    ChannelCatalog,
    VideoMeta,
    VideoProcessingStatus,
)
from common.repo._hygiene import clean_text
from common.repo.videos import _VIDEO_STATUS_JOINS, _VIDEO_STATUS_SQL

CATALOG_LIMITS: tuple[int, ...] = (50, 200, 500, 5000)

#: A ``running`` row older than this was abandoned by a crashed planner.
RUNNING_STALE_SEC = 900


def replace_catalog_snapshot(
    conn: psycopg.Connection[Any], channel_id: str, listing: ChannelCatalog, *, limit: int
) -> None:
    if limit not in CATALOG_LIMITS:
        raise ValueError("catalog limit must be one of 50, 200, 500, 5000")
    if listing.channel_id != channel_id:
        raise ValueError("catalog channel does not match requested channel")
    video_ids = [entry.video_id for entry in listing.entries]
    if len(set(video_ids)) != len(video_ids):
        raise ValueError("catalog contains duplicate video IDs")
    with conn.transaction():
        for position, entry in enumerate(listing.entries):
            conn.execute(
                """
                INSERT INTO channel_catalog_entries
                    (channel_id, video_id, position, snapshot_limit, total_count, title)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (channel_id, video_id) DO UPDATE SET
                    position = EXCLUDED.position,
                    snapshot_limit = EXCLUDED.snapshot_limit,
                    total_count = EXCLUDED.total_count,
                    title = COALESCE(EXCLUDED.title, channel_catalog_entries.title),
                    updated_at = now()
                """,
                (
                    channel_id,
                    entry.video_id,
                    position,
                    limit,
                    listing.total_count,
                    clean_text(entry.title),
                ),
            )
        conn.execute(
            """
            DELETE FROM channel_catalog_entries
            WHERE channel_id = %(channel_id)s
              AND (position >= %(length)s OR NOT (video_id = ANY(%(video_ids)s)))
            """,
            {"channel_id": channel_id, "length": len(video_ids), "video_ids": video_ids},
        )


def get_catalog_page(
    conn: psycopg.Connection[Any], channel_id: str, *, limit: int
) -> CatalogPage | None:
    rows = conn.execute(
        f"""
        SELECT c.channel_id, c.video_id, c.position, c.title, c.description,
               c.published_at, c.enrichment_state, c.enrichment_error,
               (v.video_id IS NOT NULL
                OR EXISTS (SELECT 1 FROM deleted_videos d WHERE d.video_id = c.video_id)) AS known,
               CASE WHEN v.video_id IS NULL THEN NULL ELSE {_VIDEO_STATUS_SQL} END AS status,
               c.snapshot_limit, c.total_count
        FROM channel_catalog_entries c
        LEFT JOIN videos v ON v.video_id = c.video_id
        {_VIDEO_STATUS_JOINS}
        WHERE c.channel_id = %(channel_id)s
        ORDER BY c.position ASC, c.video_id ASC
        LIMIT %(limit)s
        """,
        # The shared status joins take the display preference (#146); a catalog row
        # needs only its status, which does not depend on which analysis is shown.
        {
            "channel_id": channel_id,
            "limit": limit,
            "prefer_model": None,
            "prefer_prompt_version": None,
        },
    ).fetchall()
    if not rows:
        return None
    items = tuple(
        CatalogVideo(
            channel_id=row[0],
            video_id=row[1],
            position=int(row[2]),
            title=row[3],
            description=row[4],
            published_at=row[5],
            enrichment_state=cast(CatalogEnrichmentState, row[6]),
            enrichment_error=row[7],
            known=bool(row[8]),
            processing_status=cast(VideoProcessingStatus | None, row[9]),
        )
        for row in rows
    )
    return CatalogPage(
        channel_id=channel_id,
        limit=limit,
        total_count=rows[0][11],
        items=items,
    )


def claim_enrichment(
    conn: psycopg.Connection[Any], *, batch_size: int, stale_sec: int = RUNNING_STALE_SEC
) -> list[CatalogClaim]:
    """Claim up to ``batch_size`` rows to enrich (``pending``, or ``running`` for
    over ``stale_sec`` seconds: the planner died mid-batch), oldest first."""
    rows = conn.execute(
        """
        WITH picked AS (
            SELECT channel_id, video_id
            FROM channel_catalog_entries
            WHERE enrichment_state = 'pending'
               OR (enrichment_state = 'running'
                   AND updated_at < now() - make_interval(secs => %(stale_sec)s))
            ORDER BY updated_at, channel_id, position, video_id
            FOR UPDATE SKIP LOCKED
            LIMIT %(batch_size)s
        )
        UPDATE channel_catalog_entries AS c
        SET enrichment_state = 'running', enrichment_error = NULL,
            attempts = attempts + 1, updated_at = now()
        FROM picked
        WHERE (c.channel_id, c.video_id) = (picked.channel_id, picked.video_id)
        RETURNING c.channel_id, c.video_id, c.attempts
        """,
        {"batch_size": batch_size, "stale_sec": stale_sec},
    ).fetchall()
    return [CatalogClaim(row[0], row[1], int(row[2])) for row in rows]


def complete_enrichment(conn: psycopg.Connection[Any], meta: VideoMeta) -> bool:
    row = conn.execute(
        """
        UPDATE channel_catalog_entries
        SET title = %s, description = %s, published_at = %s,
            enrichment_state = 'done', enrichment_error = NULL, updated_at = now()
        WHERE channel_id = %s AND video_id = %s AND enrichment_state = 'running'
        RETURNING 1
        """,
        (
            clean_text(meta.title),
            clean_text(meta.description),
            meta.published_at,
            meta.channel_id,
            meta.video_id,
        ),
    ).fetchone()
    return row is not None


def fail_enrichment(
    conn: psycopg.Connection[Any], channel_id: str, video_id: str, *, error_class: str
) -> bool:
    row = conn.execute(
        """
        UPDATE channel_catalog_entries
        SET enrichment_state = 'failed', enrichment_error = %s, updated_at = now()
        WHERE channel_id = %s AND video_id = %s AND enrichment_state = 'running'
        RETURNING 1
        """,
        (clean_text(error_class), channel_id, video_id),
    ).fetchone()
    return row is not None


def retry_enrichment(
    conn: psycopg.Connection[Any], channel_id: str, video_id: str
) -> bool:
    row = conn.execute(
        """
        UPDATE channel_catalog_entries
        SET enrichment_state = 'pending', enrichment_error = NULL, updated_at = now()
        WHERE channel_id = %s AND video_id = %s AND enrichment_state = 'failed'
        RETURNING 1
        """,
        (channel_id, video_id),
    ).fetchone()
    return row is not None


def get_enrichment_state(
    conn: psycopg.Connection[Any], channel_id: str, video_id: str
) -> CatalogEnrichmentState | None:
    """The enrichment state of one catalog row, or ``None`` if the channel's catalog lacks it."""
    row = conn.execute(
        """
        SELECT enrichment_state FROM channel_catalog_entries
        WHERE channel_id = %s AND video_id = %s
        """,
        (channel_id, video_id),
    ).fetchone()
    return None if row is None else cast(CatalogEnrichmentState, row[0])
