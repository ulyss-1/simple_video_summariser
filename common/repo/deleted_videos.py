from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg


class VideoNotFound(Exception):
    pass


class VideoRunning(Exception):
    pass


class VideoCancelling(VideoRunning):
    """Every running job has a cancel request (#237): delete once they have stopped."""


@dataclass(frozen=True, slots=True)
class DeletedVideo:
    media_paths: tuple[str, ...]


def delete_video(
    conn: psycopg.Connection[Any],
    video_id: str,
    *,
    before_commit: Callable[[], None] | None = None,
) -> DeletedVideo:
    with conn.transaction():
        states = conn.execute(
            "SELECT state, cancel_requested_at FROM jobs WHERE video_id = %s ORDER BY id"
            " FOR UPDATE",
            (video_id,),
        ).fetchall()
        video = conn.execute(
            "SELECT 1 FROM videos WHERE video_id = %s FOR UPDATE", (video_id,)
        ).fetchone()
        if video is None:
            raise VideoNotFound(video_id)
        running = [row for row in states if row[0] == "running"]
        if running:
            if all(row[1] is not None for row in running):
                raise VideoCancelling(video_id)
            raise VideoRunning(video_id)
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
        conn.execute("DELETE FROM jobs WHERE video_id = %s", (video_id,))
        conn.execute("DELETE FROM videos WHERE video_id = %s", (video_id,))
        if before_commit is not None:
            before_commit()
    return DeletedVideo(paths)
