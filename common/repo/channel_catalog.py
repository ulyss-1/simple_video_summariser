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
                    title = EXCLUDED.title,
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
               v.video_id IS NOT NULL AS known,
               CASE WHEN v.video_id IS NULL THEN NULL ELSE {_VIDEO_STATUS_SQL} END AS status,
               c.snapshot_limit, c.total_count
        FROM channel_catalog_entries c
        LEFT JOIN videos v ON v.video_id = c.video_id
        {_VIDEO_STATUS_JOINS}
        WHERE c.channel_id = %s
        ORDER BY c.position ASC, c.video_id ASC
        LIMIT %s
        """,
        (channel_id, limit),
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
    conn: psycopg.Connection[Any], *, batch_size: int
) -> list[CatalogClaim]:
    rows = conn.execute(
        """
        WITH picked AS (
            SELECT channel_id, video_id
            FROM channel_catalog_entries
            WHERE enrichment_state = 'pending'
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
        {"batch_size": batch_size},
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
