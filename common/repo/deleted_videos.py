from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from common.repo.jobs import (
    VideoCancelEntry,
    VideoNotFound,
    apply_video_cancel,
    log_cancelled_entries,
)

__all__ = [
    "BulkDeleted",
    "DeletedVideo",
    "VideoNotFound",
    "delete_video",
    "delete_videos",
]


@dataclass(frozen=True, slots=True)
class DeletedVideo:
    media_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BulkDeleted:
    """What ``delete_videos`` did: the IDs in request order, and the media to unlink."""

    deleted: tuple[str, ...]
    not_found: tuple[str, ...]
    media: tuple[tuple[str, str], ...]  # (video_id, relative path)


def delete_videos(
    conn: psycopg.Connection[Any],
    video_ids: Sequence[str],
    *,
    before_commit: Callable[[], None] | None = None,
) -> BulkDeleted:
    """Delete several videos at once, all or nothing (#238, #231, #303).

    One transaction. Lock order is the one every multi-video writer uses: the jobs
    of every target video (by ``id``), then the target ``videos`` rows (by
    ``video_id``). Then, per existing video, exactly what #303 fixed: the jobs are
    cancelled with the #237 rule (``pending`` ends ``CANCELLED``, ``running`` gets
    ``cancel_requested_at``); every job row that is not ``running`` is deleted (their
    ``llm_batches`` go to ``job_id`` NULL for the orphan pass), each ``running`` row
    stays with its request; the tombstone is written and the video's data cascades.
    An ID with no ``videos`` row (unknown or already deleted) is ``not_found`` and gets
    no tombstone. A repeated ID counts once.
    """
    ids = list(dict.fromkeys(video_ids))
    entries_by_video: dict[str, list[VideoCancelEntry]] = {}
    media: list[tuple[str, str]] = []
    with conn.transaction():
        conn.execute(
            "SELECT id FROM jobs WHERE video_id = ANY(%s) ORDER BY id FOR UPDATE", (ids,)
        ).fetchall()
        existing = {
            str(row[0])
            for row in conn.execute(
                "SELECT video_id FROM videos WHERE video_id = ANY(%s)"
                " ORDER BY video_id FOR UPDATE",
                (ids,),
            ).fetchall()
        }
        for video_id in (v for v in ids if v in existing):
            entries_by_video[video_id] = apply_video_cancel(conn, video_id)
            media.extend(
                (video_id, str(row[0]))
                for row in conn.execute(
                    "SELECT path FROM media WHERE video_id = %s ORDER BY id", (video_id,)
                ).fetchall()
            )
            conn.execute(
                "INSERT INTO deleted_videos (video_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (video_id,),
            )
            conn.execute(
                "DELETE FROM jobs WHERE video_id = %s AND state <> 'running'", (video_id,)
            )
            conn.execute("DELETE FROM videos WHERE video_id = %s", (video_id,))
        if before_commit is not None:
            before_commit()
    for video_id, entries in entries_by_video.items():
        log_cancelled_entries(video_id, entries)
    return BulkDeleted(
        deleted=tuple(v for v in ids if v in existing),
        not_found=tuple(v for v in ids if v not in existing),
        media=tuple(media),
    )


def delete_video(
    conn: psycopg.Connection[Any],
    video_id: str,
    *,
    before_commit: Callable[[], None] | None = None,
) -> DeletedVideo:
    """Delete one video at once, whatever its jobs are doing (#231, #303).

    The one-ID form of ``delete_videos``. Raises ``VideoNotFound`` for an unknown or
    deleted video (nothing is written then).
    """
    result = delete_videos(conn, [video_id], before_commit=before_commit)
    if not result.deleted:
        raise VideoNotFound(video_id)
    return DeletedVideo(tuple(path for _, path in result.media))
