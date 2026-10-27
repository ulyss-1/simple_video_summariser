"""Channel backfill, API side (issues #41, #116, #115; architecture.md 8.3).

The listing, selection, estimate and per-video writes live in
``common.backfill`` since #115, so the planner's ``channel_backfill`` task can
share them; this module re-exports the names the routes and tests use and keeps
``cancel_backfill``, which only the API calls.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.pq import TransactionStatus

from common.backfill import (
    BACKFILL_DRY_RUN_DEADLINE_SEC,
    BACKFILL_PROBE_CONCURRENCY,
    BACKFILL_PROBE_TIMEOUT_SEC,
    ESTIMATE_ASSUMES,
    LLM_BASIS,
    MAX_BACKFILL_LIMIT,
    MAX_BACKFILL_REQUEST_LIMIT,
    SUBTITLES_MANUAL,
    SUBTITLES_TRANSCRIBE,
    SUBTITLES_UNCHECKED,
    BackfillResult,
    ChannelNotRegistered,
    Estimate,
    LlmEstimate,
    SubtitleProbe,
    VideoEstimate,
    backfill,
)
from common.queue import PostgresQueue
from common.repo.backfill_requests import (
    cancel_active,
    delete_for_channel,
    lock_active_backfill_id,
)
from common.repo.channels import (
    channel_exists,
    deactivate_channel,
    delete_channel_row,
    lock_channel_for_delete,
)
from common.repo.deleted_videos import delete_videos
from common.repo.videos import delete_unused_backfill_video

__all__ = [
    "BACKFILL_DRY_RUN_DEADLINE_SEC",
    "BACKFILL_PROBE_CONCURRENCY",
    "BACKFILL_PROBE_TIMEOUT_SEC",
    "ESTIMATE_ASSUMES",
    "LLM_BASIS",
    "MAX_BACKFILL_LIMIT",
    "MAX_BACKFILL_REQUEST_LIMIT",
    "SUBTITLES_MANUAL",
    "SUBTITLES_TRANSCRIBE",
    "SUBTITLES_UNCHECKED",
    "BackfillResult",
    "CancelResult",
    "ChannelNotRegistered",
    "DeleteChannelResult",
    "Estimate",
    "LlmEstimate",
    "SubtitleProbe",
    "UnfollowResult",
    "VideoEstimate",
    "backfill",
    "cancel_backfill",
    "delete_channel",
    "unfollow_channel",
]


@dataclass(frozen=True, slots=True)
class CancelResult:
    channel_id: str
    backfill_id: str
    cancelled: int
    videos_removed: int
    videos_kept: int
    video_ids: tuple[str, ...]


def cancel_backfill(
    conn: psycopg.Connection[Any], channel_id: str, backfill_id: str
) -> CancelResult:
    """Undo one real backfill's still-pending ``ingest`` jobs (#116).

    Deletes the jobs (``PostgresQueue.cancel_backfill``) and, for each, its
    ``videos`` row when nothing else depends on it, all in one transaction:
    either the job and its row go together or neither does. A video whose row
    is kept (a transcript, or another job, exists) is counted in
    ``videos_kept``. If ``backfill_id`` is the id of the channel's active
    backfill request (#115), that request is ended too, in the same
    transaction, so the planner stops enqueueing more (it ends ``failed`` with
    ``error_class = 'CANCELLED'``). ``conn`` must have no open transaction.
    Raises ``ChannelNotRegistered`` if the channel has no row. An unknown
    ``backfill_id`` is not an error: it cancels nothing.
    """
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("cancel_backfill expects a connection with no open transaction")
    with conn.transaction():
        if not channel_exists(conn, channel_id):
            raise ChannelNotRegistered(channel_id)
        return _cancel_in_transaction(conn, channel_id, backfill_id)


def _cancel_in_transaction(
    conn: psycopg.Connection[Any], channel_id: str, backfill_id: str
) -> CancelResult:
    """The cancel itself; the caller owns the transaction (shared with unfollow, #235)."""
    cancel_active(conn, channel_id, backfill_id)
    jobs = PostgresQueue(conn).cancel_backfill(channel_id, backfill_id)
    removed = sum(delete_unused_backfill_video(conn, job.video_id) for job in jobs)
    return CancelResult(
        channel_id=channel_id,
        backfill_id=backfill_id,
        cancelled=len(jobs),
        videos_removed=removed,
        videos_kept=len(jobs) - removed,
        video_ids=tuple(job.video_id for job in jobs),
    )


@dataclass(frozen=True, slots=True)
class UnfollowResult:
    channel_id: str
    changed: bool
    cancel: CancelResult | None  # None when the channel had no active request


def unfollow_channel(conn: psycopg.Connection[Any], channel_id: str) -> UnfollowResult:
    """Stop following ``channel_id`` and end its active backfill request (#235).

    One transaction. The channel row is locked first (``deactivate_channel``),
    then the active request, then the jobs, as ``cancel_backfill`` does. A
    pending or running request is ended exactly as ``cancel_backfill`` with its
    ``backfill_id`` does, also for a channel that was already inactive. Videos,
    transcripts, analyses and every other job are left alone. ``conn`` must
    have no open transaction. Raises ``ChannelNotRegistered`` for an unknown
    channel.
    """
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("unfollow_channel expects a connection with no open transaction")
    with conn.transaction():
        changed = deactivate_channel(conn, channel_id)
        if changed is None:
            raise ChannelNotRegistered(channel_id)
        backfill_id = lock_active_backfill_id(conn, channel_id)
        cancel = (
            None if backfill_id is None else _cancel_in_transaction(conn, channel_id, backfill_id)
        )
    return UnfollowResult(channel_id=channel_id, changed=changed, cancel=cancel)


@dataclass(frozen=True, slots=True)
class DeleteChannelResult:
    channel_id: str
    deleted_videos: int
    cancel: CancelResult | None  # the active request that was ended; ``videos_*`` are 0
    media: tuple[tuple[str, str], ...]  # (video_id, relative path) to unlink after the commit


def delete_channel(
    conn: psycopg.Connection[Any],
    channel_id: str,
    *,
    before_commit: Callable[[], None] | None = None,
) -> DeleteChannelResult:
    """Delete ``channel_id`` and every video of it, all or nothing (#339, #231, #303).

    One transaction. Lock order: the ``channels`` row ``FOR NO KEY UPDATE`` first (see
    ``lock_channel_for_delete``), then the active backfill request, then what
    ``delete_videos`` locks (the channel's jobs by ``id``, its ``videos`` rows by
    ``video_id``), then the row delete of ``channels``. The request goes before the jobs
    because unfollow and the backfill cancel take them in that order. An active
    request is ended with the #116 rule and its pending jobs are deleted (counted in
    ``cancel.cancelled``); the videos are then deleted by ``delete_videos`` exactly as
    ``POST /videos/delete`` does, whatever their ``origin``, each with a tombstone
    (so the #116 sweep of unused backfill videos is not used here). The channel's backfill
    request rows go explicitly and its catalog snapshot by cascade. ``conn`` must have
    no open transaction. Raises ``ChannelNotRegistered`` for an unknown channel.
    """
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("delete_channel expects a connection with no open transaction")
    with conn.transaction():
        if not lock_channel_for_delete(conn, channel_id):
            raise ChannelNotRegistered(channel_id)
        backfill_id = lock_active_backfill_id(conn, channel_id)
        cancel: CancelResult | None = None
        if backfill_id is not None:
            cancel_active(conn, channel_id, backfill_id)
            jobs = PostgresQueue(conn).cancel_backfill(channel_id, backfill_id)
            cancel = CancelResult(
                channel_id=channel_id,
                backfill_id=backfill_id,
                cancelled=len(jobs),
                videos_removed=0,
                videos_kept=len(jobs),
                video_ids=tuple(job.video_id for job in jobs),
            )
        video_ids = [
            str(row[0])
            for row in conn.execute(
                "SELECT video_id FROM videos WHERE channel_id = %s ORDER BY video_id",
                (channel_id,),
            ).fetchall()
        ]
        deleted = delete_videos(conn, video_ids)
        delete_for_channel(conn, channel_id)
        delete_channel_row(conn, channel_id)
        if before_commit is not None:
            before_commit()
    return DeleteChannelResult(
        channel_id=channel_id,
        deleted_videos=len(deleted.deleted),
        cancel=cancel,
        media=deleted.media,
    )
