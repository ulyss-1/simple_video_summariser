"""Write routes (issues #39, #40; architecture.md 8.3; D13). Rate-limited as a group.

- ``POST /videos`` turns a pasted video URL or bare ID into a ``videos`` row
  and one ``ingest`` job at ``PRIORITY_INTERACTIVE``.
- ``POST /channels`` starts forward-only monitoring from ``monitor_from = now()``.
- ``POST /channels/{channel_id}/backfill`` lists a registered channel's newest
  uploads and, unless it is a dry run, enqueues the unknown ones at
  ``PRIORITY_BACKFILL`` (#41; logic in ``services.api.backfill``).

Input is untrusted. It is parsed by ``common.youtube_refs`` before anything
reaches the database, and a rejection returns a fixed message that never
echoes the submitted text. Both routes are idempotent: repeating a submission
never creates a second job or row, and never moves ``monitor_from``.
Route bodies hold no SQL; they call ``common.repo`` and ``JobQueue.enqueue``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import psycopg
import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from psycopg.pq import TransactionStatus
from pydantic import BaseModel, ConfigDict, Field
from starlette.convertors import Convertor, register_url_convertor

from common.errors import RateLimitedError, ToolFailureError, TransientNetworkError
from common.models import CatalogSource
from common.queue import PRIORITY_INTERACTIVE, JobQueue, PostgresQueue
from common.repo.channels import register_channel
from common.repo.jobs import latest_job
from common.repo.videos import insert_submitted_video
from common.youtube_refs import (
    UnsupportedChannelRef,
    is_channel_id,
    parse_channel_ref,
    parse_video_ref,
)
from services.api.backfill import (
    MAX_BACKFILL_LIMIT,
    BackfillResult,
    ChannelNotRegistered,
    backfill,
)
from services.api.deps import get_catalog, get_conn, rate_limit

_log = structlog.get_logger(__name__)


class _UntrustedSegment(Convertor[str]):
    """Matches anything, newlines and decoded ``/`` included.

    The default ``str`` converter stops at ``/`` and ``path`` stops at a
    newline, so a malformed ID would get a bare 404 instead of the route's
    own 422. The route validates the value itself. Only routes that name
    ``:untrusted`` use it; any other ``/channels/.../backfill``-suffixed route
    added later would be shadowed by this one.
    """

    regex = "(?s:.+?)"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("untrusted", _UntrustedSegment())

router = APIRouter(dependencies=[Depends(rate_limit)])

BAD_VIDEO_REF = "url must be a YouTube video ID or video URL"
BAD_CHANNEL_REF = "url must be a YouTube channel ID or youtube.com/channel/ URL"
BAD_BACKFILL_CHANNEL = "channel_id must be a canonical YouTube channel ID (UC + 22 characters)"
CHANNEL_NOT_REGISTERED = "channel is not registered; register it with POST /channels first"
LISTING_UNAVAILABLE = "YouTube listing unavailable, try again later"
LISTING_FAILED = "YouTube listing failed"


class SubmitRef(BaseModel):
    model_config = ConfigDict(strict=True)

    url: str


class BackfillRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    limit: int = Field(ge=1, le=MAX_BACKFILL_LIMIT)
    dry_run: bool = True


class BackfillEstimate(BaseModel):
    audio_sec: int
    unknown_duration: int
    rtf: float | None
    rtf_samples: int
    transcription_sec: int | None
    assumes: str


class BackfillResponse(BaseModel):
    channel_id: str
    dry_run: bool
    limit: int
    listed: int
    total_count: int | None
    already_known: int
    new_videos: int
    enqueued: int
    video_ids: list[str]
    estimate: BackfillEstimate
    backfill_id: str | None


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


def _backfill_channel_id(channel_id: str) -> str:
    """Validate the path before ``get_conn`` or ``get_catalog`` resolve."""
    if not is_channel_id(channel_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_BACKFILL_CHANNEL)
    return channel_id


@router.post(
    "/channels/{channel_id:untrusted}/backfill",
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"model": BackfillResponse, "description": "Dry run, or nothing new"}},
)
def post_channel_backfill(
    channel_id: Annotated[str, Depends(_backfill_channel_id)],
    body: BackfillRequest,
    response: Response,
    conn: Annotated[psycopg.Connection[Any], Depends(get_conn)],
    catalog: Annotated[CatalogSource, Depends(get_catalog)],
) -> BackfillResponse:
    """Backfill a registered channel's newest ``limit`` uploads (#41, D9b).

    This is the one API route that runs a subprocess: the catalog is a
    yt-dlp flat listing, bounded by ``deps.BACKFILL_LIST_TIMEOUT_SEC`` and
    ``MAX_BACKFILL_LIMIT``. It departs from "the API performs no processing"
    because architecture.md 8.3 wants a synchronous dry-run answer, and a
    flat listing is network-bound, not CPU work (owner decision; moving it
    to a worker is #115). A dry run (the default) writes nothing; a real run
    answers 202 when it enqueued at least one job, else 200.
    """
    _require_idle(conn)
    started = time.perf_counter()
    try:
        result = backfill(
            conn,
            PostgresQueue(conn),
            catalog,
            channel_id,
            limit=body.limit,
            dry_run=body.dry_run,
        )
    except ChannelNotRegistered:
        raise HTTPException(status.HTTP_404_NOT_FOUND, CHANNEL_NOT_REGISTERED) from None
    except (TransientNetworkError, RateLimitedError, ToolFailureError) as exc:
        _log.warning(
            "backfill listing failed",
            channel_id=channel_id,
            error_class=type(exc).__name__,
        )
        if isinstance(exc, ToolFailureError):
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, LISTING_FAILED) from None
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, LISTING_UNAVAILABLE
        ) from None
    _log.info(
        "backfill",
        channel_id=channel_id,
        limit=result.limit,
        dry_run=result.dry_run,
        listed=result.listed,
        new_videos=result.new_videos,
        enqueued=result.enqueued,
        backfill_id=result.backfill_id,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    if result.enqueued == 0:
        response.status_code = status.HTTP_200_OK
    return _backfill_response(result)


def _backfill_response(result: BackfillResult) -> BackfillResponse:
    estimate = result.estimate
    return BackfillResponse(
        channel_id=result.channel_id,
        dry_run=result.dry_run,
        limit=result.limit,
        listed=result.listed,
        total_count=result.total_count,
        already_known=result.already_known,
        new_videos=result.new_videos,
        enqueued=result.enqueued,
        video_ids=list(result.video_ids),
        estimate=BackfillEstimate(
            audio_sec=estimate.audio_sec,
            unknown_duration=estimate.unknown_duration,
            rtf=estimate.rtf,
            rtf_samples=estimate.rtf_samples,
            transcription_sec=estimate.transcription_sec,
            assumes=estimate.assumes,
        ),
        backfill_id=result.backfill_id,
    )
