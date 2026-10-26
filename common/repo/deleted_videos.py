from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg

from common.repo.jobs import VideoNotFound as VideoNotFound
from common.repo.jobs import apply_video_cancel, log_cancelled_entries


@dataclass(frozen=True, slots=True)
class DeletedVideo:
    media_paths: tuple[str, ...]


def delete_video(
    conn: psycopg.Connection[Any],
    video_id: str,
    *,
    before_commit: Callable[[], None] | None = None,
) -> DeletedVideo:
    """Delete a video at once, whatever its jobs are doing (#231, #303).

    One transaction. Lock order is the one ``cancel_video``, ``handoff`` and
    ``raise_priority`` use: the video's jobs (by id), then the ``videos`` row. The
    jobs are cancelled with the #237 rule (``pending`` ends ``CANCELLED``, ``running``
    gets ``cancel_requested_at``); every job row that is not ``running`` is deleted
    (their ``llm_batches`` go to ``job_id`` NULL for the orphan pass), each ``running``
    row stays with its request, so its worker ends it ``CANCELLED`` at its next
    checkpoint and carries on. The tombstone is written and the video's data cascades.
    Raises ``VideoNotFound`` for an unknown or deleted video.
    """
    with conn.transaction():
        conn.execute(
            "SELECT id FROM jobs WHERE video_id = %s ORDER BY id FOR UPDATE", (video_id,)
        ).fetchall()
        video = conn.execute(
            "SELECT 1 FROM videos WHERE video_id = %s FOR UPDATE", (video_id,)
        ).fetchone()
        if video is None:
            raise VideoNotFound(video_id)
        entries = apply_video_cancel(conn, video_id)
        paths = tuple(
            str(row[0])
            for row in conn.execute(
                "SELECT path FROM media WHERE video_id = %s ORDER BY id", (video_id,)
            ).fetchall()
        )
        conn.execute(
            "INSERT INTO deleted_videos (video_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (video_id,),
        )
        conn.execute("DELETE FROM jobs WHERE video_id = %s AND state <> 'running'", (video_id,))
        conn.execute("DELETE FROM videos WHERE video_id = %s", (video_id,))
        if before_commit is not None:
            before_commit()
    log_cancelled_entries(video_id, entries)
    return DeletedVideo(paths)
