"""Write routes (issues #39, #40; architecture.md 8.3; D13). Rate-limited as a group.

- ``POST /videos`` turns a pasted video URL or bare ID into a ``videos`` row
  and one ``ingest`` job at ``PRIORITY_INTERACTIVE``.
- ``POST /channels`` starts forward-only monitoring from ``monitor_from = now()``.

Input is untrusted. It is parsed by ``common.youtube_refs`` before anything
reaches the database, and a rejection returns a fixed message that never
echoes the submitted text. Both routes are idempotent: repeating a submission
never creates a second job or row, and never moves ``monitor_from``.
Route bodies hold no SQL; they call ``common.repo`` and ``JobQueue.enqueue``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response, status
from psycopg.pq import TransactionStatus
from pydantic import BaseModel, ConfigDict

from common.queue import PRIORITY_INTERACTIVE, JobQueue, PostgresQueue
from common.repo.channels import register_channel
from common.repo.jobs import latest_job
from common.repo.videos import insert_submitted_video
from common.youtube_refs import (
    UnsupportedChannelRef,
    parse_channel_ref,
    parse_video_ref,
)
from services.api.deps import get_conn, rate_limit

router = APIRouter(dependencies=[Depends(rate_limit)])

BAD_VIDEO_REF = "url must be a YouTube video ID or video URL"
BAD_CHANNEL_REF = "url must be a YouTube channel ID or youtube.com/channel/ URL"


class SubmitRef(BaseModel):
    model_config = ConfigDict(strict=True)

    url: str


class VideoSubmission(BaseModel):
    video_id: str
    job_id: int
    state: str
    created: bool


class ChannelRegistration(BaseModel):
    channel_id: str
    active: bool
    monitor_from: datetime
    added_at: datetime
    created: bool


@dataclass(frozen=True, slots=True)
class SubmitResult:
    video_id: str
    job_id: int
    state: str
    created: bool


def _require_idle(conn: psycopg.Connection[Any]) -> None:
    """The write must be the outermost transaction, or it would only be a savepoint.

    ``get_conn`` closes without committing, so a savepoint inside a
    transaction some earlier statement opened would be silently lost.
    """
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("write route expects a connection with no open transaction")


def submit_video(
    conn: psycopg.Connection[Any], video_id: str, *, queue: JobQueue
) -> SubmitResult:
    """Record ``video_id`` and give it one ``ingest`` job, unless it already has one.

    In one transaction: a bare ``adhoc`` row (an existing row is kept as it
    is), then the latest ``ingest`` job is returned if there is one, whatever
    its state; a finished job is not resubmitted (retry is #44), and an active
    one keeps its priority (#103). Otherwise one job is enqueued at
    ``PRIORITY_INTERACTIVE``. A concurrent submission that loses the enqueue
    race returns the winner's job.
    """
    with conn.transaction():
        insert_submitted_video(conn, video_id)
        existing = latest_job(conn, video_id, "ingest")
        if existing is None:
            job_id = queue.enqueue("ingest", video_id, priority=PRIORITY_INTERACTIVE)
            if job_id is not None:
                return SubmitResult(video_id, job_id, "pending", created=True)
            existing = latest_job(conn, video_id, "ingest")
        if existing is None:
            raise RuntimeError("enqueue conflicted but no ingest job is visible")
        job_id, state = existing
        return SubmitResult(video_id, job_id, state, created=False)


@router.post(
    "/videos",
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"model": VideoSubmission, "description": "Already submitted"}},
)
def post_videos(
    body: SubmitRef,
    response: Response,
    conn: Annotated[psycopg.Connection[Any], Depends(get_conn)],
) -> VideoSubmission:
    try:
        video_id = parse_video_ref(body.url)
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_VIDEO_REF
        ) from None
    _require_idle(conn)
    result = submit_video(conn, video_id, queue=PostgresQueue(conn))
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return VideoSubmission(
        video_id=result.video_id,
        job_id=result.job_id,
        state=result.state,
        created=result.created,
    )


@router.post(
    "/channels",
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": ChannelRegistration, "description": "Already registered"}
    },
)
def post_channels(
    body: SubmitRef,
    response: Response,
    conn: Annotated[psycopg.Connection[Any], Depends(get_conn)],
) -> ChannelRegistration:
    try:
        channel_id = parse_channel_ref(body.url)
    except UnsupportedChannelRef as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_CHANNEL_REF
        ) from None
    _require_idle(conn)
    with conn.transaction():
        registered = register_channel(conn, channel_id)
    if not registered.created:
        response.status_code = status.HTTP_200_OK
    channel = registered.channel
    return ChannelRegistration(
        channel_id=channel.channel_id,
        active=channel.active,
        monitor_from=channel.monitor_from,
        added_at=channel.added_at,
        created=registered.created,
    )
