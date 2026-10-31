"""Response models for the read and ops routes (issues #42, #44).

They are the public contract #46 generates TypeScript types from, so every
field is declared explicitly. Datetimes are always serialised in UTC.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from pydantic import AfterValidator, BaseModel, Field, JsonValue, ValidationError

from common.models import (
    Analysis,
    AnalysisRun,
    CatalogPage,
    CatalogVideo,
    ChannelPage,
    TranscriptInfo,
    TranscriptPage,
    VideoSummary,
)
from common.progress import estimate_effort
from common.repo.backfill_requests import BackfillRequestRow
from common.repo.jobs import JobRow
from common.repo.planner_runs import PlannerTaskRun
from common.repo.search import SearchPage as RepoSearchPage
from common.youtube_refs import is_video_id

UtcDatetime = Annotated[datetime, AfterValidator(lambda value: value.astimezone(UTC))]
VideoStatus = Literal["done", "processing", "unavailable", "failed", "idle"]


class HealthOut(BaseModel):
    """``GET /healthz`` 200 body: liveness plus queue depth by kind and state."""

    status: str
    queue: dict[str, dict[str, int]]


class ActiveJobOut(BaseModel):
    id: int
    kind: str
    state: str
    cancel_requested_at: UtcDatetime | None = None


class LastFailureOut(BaseModel):
    job_id: int
    kind: str
    error_class: str | None
    finished_at: UtcDatetime | None


class LastCancelledOut(BaseModel):
    """The newest job of a video, when it ended ``CANCELLED`` (#300)."""

    job_id: int
    kind: str
    finished_at: UtcDatetime | None


class ProgressOut(BaseModel):
    """Progress of a transcribe job (#245).

    A running job carries every figure. A pending one carries only the estimate
    (``estimated_sec``, ``eta_sec``, ``duration_sec``, ``estimate_source``); the
    rest are ``None``. ``estimate_source`` is ``"default"`` when there was no RTF
    history, so the UI can call the figure rough.
    """

    percent: int | None
    done_sec: float | None
    duration_sec: float | None
    elapsed_sec: float | None
    eta_sec: float | None
    estimated_sec: float | None
    estimate_source: Literal["history", "default"] | None
    updated_at: UtcDatetime | None


def progress_out(row: JobRow, history: Sequence[float] | None = None) -> ProgressOut | None:
    """The progress to show for ``row``, or ``None``.

    Only ``transcribe`` jobs have any. A ``running`` one returns what the worker
    stored (``None`` before its first write, or when the stored JSON is not
    the expected shape). A ``pending`` one is estimated from its payload duration
    and ``history`` (the recent RTFs); without ``history`` or a duration there is
    nothing to show.
    """
    if row.kind != "transcribe":
        return None
    if row.state == "running":
        if row.progress is None:
            return None
        try:
            return ProgressOut.model_validate(row.progress)
        except ValidationError:
            return None
    if row.state == "pending" and history is not None:
        estimate = estimate_effort(row.payload_duration_sec, history)
        if estimate is None:
            return None
        return ProgressOut(
            percent=None,
            done_sec=None,
            duration_sec=row.payload_duration_sec,
            elapsed_sec=None,
            eta_sec=round(estimate.estimated_sec, 1),
            estimated_sec=round(estimate.estimated_sec, 1),
            estimate_source=estimate.source,
            updated_at=None,
        )
    return None


class VideoFields(BaseModel):
    """Fields shared by a library item and a search result (#42, #43)."""

    video_id: str
    title: str | None
    channel_id: str | None
    channel_title: str | None
    published_at: UtcDatetime | None
    duration_sec: int | None
    unavailable: str | None


class VideoItem(VideoFields):
    origin: str
    status: VideoStatus
    active_job: ActiveJobOut | None
    last_failure: LastFailureOut | None
    latest_analysis_at: UtcDatetime | None
    has_transcript: bool
    #: Non-null exactly when the newest job ended CANCELLED (#300, #325).
    last_cancelled: LastCancelledOut | None


class VideoList(BaseModel):
    items: list[VideoItem]
    total: int
    offset: int
    limit: int


class DeletedVideoItem(BaseModel):
    video_id: str
    title: str | None
    channel_id: str | None
    channel_title: str | None
    deleted_at: datetime
    restorable: bool


class DeletedVideoList(BaseModel):
    items: list[DeletedVideoItem]
    total: int


class ChannelItem(BaseModel):
    channel_id: str
    title: str | None
    active: bool
    video_count: int


class ChannelDetailOut(BaseModel):
    channel_id: str
    title: str | None
    active: bool
    monitor_from: UtcDatetime
    added_at: UtcDatetime
    video_count: int


class ChannelList(BaseModel):
    items: list[ChannelItem]
    total: int
    offset: int
    limit: int


class PlannerTaskRunItem(BaseModel):
    task_name: str
    interval_sec: float
    last_started_at: UtcDatetime | None
    last_finished_at: UtcDatetime | None
    last_outcome: Literal["ok", "failed"] | None
    last_error: str | None
    last_count: int | None
    last_ok_at: UtcDatetime | None
    last_skipped_locked_at: UtcDatetime | None


class PlannerRunsOut(BaseModel):
    items: list[PlannerTaskRunItem]


def ops_planner_runs(runs: list[PlannerTaskRun]) -> PlannerRunsOut:
    return PlannerRunsOut(
        items=[
            PlannerTaskRunItem(
                task_name=r.task_name,
                interval_sec=r.interval_sec,
                last_started_at=r.last_started_at,
                last_finished_at=r.last_finished_at,
                last_outcome=r.last_outcome,
                last_error=r.last_error,
                last_count=r.last_count,
                last_ok_at=r.last_ok_at,
                last_skipped_locked_at=r.last_skipped_locked_at,
            )
            for r in runs
        ]
    )


def channel_list(page: ChannelPage, *, offset: int, limit: int) -> ChannelList:
    return ChannelList(
        items=[
            ChannelItem(
                channel_id=c.channel_id,
                title=c.title,
                active=c.active,
                video_count=c.video_count,
            )
            for c in page.items
        ],
        total=page.total,
        offset=offset,
        limit=limit,
    )


class TopicOut(BaseModel):
    seq: int
    title: str
    summary: str | None
    start_sec: float | None


class ClaimOut(BaseModel):
    text: str
    speaker: str
    start_sec: float | None
    confidence: str | None
    source_chunk_seq: int | None


class QuoteOut(BaseModel):
    text: str
    speaker: str
    start_sec: float | None
    source_chunk_seq: int | None


class AnalysisOut(BaseModel):
    id: int
    model: str
    prompt_version: str
    chunk_strategy: str
    transcript_id: int
    transcript_source: str
    created_at: UtcDatetime
    tldr: str
    speaker_roster: JsonValue | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    calls: int | None
    cost_usd: float | None
    duration_ms: int | None
    topics: list[TopicOut]
    claims: list[ClaimOut]
    quotes: list[QuoteOut]


class TranscriptSummary(BaseModel):
    id: int
    source: str
    language: str | None
    speaker_source: str
    segment_count: int


class VideoDetail(VideoItem):
    transcript: TranscriptSummary | None
    analysis: AnalysisOut | None
    #: The active transcribe job's figures, else ``None`` (#245).
    progress: ProgressOut | None


class AnalysisList(BaseModel):
    items: list[AnalysisOut]
    total: int
    offset: int
    limit: int


class SegmentOut(BaseModel):
    index: int
    start: float
    end: float
    text: str
    speaker: str | None


class TranscriptOut(BaseModel):
    video_id: str
    transcript_id: int
    source: str
    language: str | None
    speaker_source: str
    total_segments: int
    offset: int
    limit: int
    segments: list[SegmentOut]


class SpanOut(BaseModel):
    text: str
    match: bool


class ExcerptOut(BaseModel):
    spans: list[SpanOut]
    #: Seconds from the start of the video of the transcript segment the match
    #: is in; ``None`` for a ``full_text`` fallback excerpt (#110).
    start_sec: Annotated[float, Field(ge=0)] | None


class SearchResult(VideoFields):
    #: ``None`` when the video matched on its title or analysis and has no transcript.
    transcript_source: str | None
    matched_in: list[Literal["title", "analysis", "transcript"]]
    excerpts: list[ExcerptOut]


class SearchPage(BaseModel):
    results: list[SearchResult]
    limit: int
    offset: int
    has_more: bool


def search_page(page: RepoSearchPage, *, limit: int, offset: int) -> SearchPage:
    return SearchPage(
        results=[
            SearchResult(
                video_id=hit.video_id,
                title=hit.title,
                channel_id=hit.channel_id,
                channel_title=hit.channel_title,
                published_at=hit.published_at,
                duration_sec=hit.duration_sec,
                unavailable=hit.unavailable,
                transcript_source=hit.transcript_source,
                matched_in=list(hit.matched_in),
                excerpts=[
                    ExcerptOut(
                        spans=[SpanOut(text=span.text, match=span.match) for span in e.spans],
                        start_sec=e.start_sec,
                    )
                    for e in hit.excerpts
                ],
            )
            for hit in page.results
        ],
        limit=limit,
        offset=offset,
        has_more=page.has_more,
    )


def video_item(video: VideoSummary) -> VideoItem:
    return VideoItem(**_video_fields(video))


def video_detail(
    video: VideoSummary,
    transcript: TranscriptInfo | None,
    run: AnalysisRun | None,
    progress: ProgressOut | None = None,
) -> VideoDetail:
    return VideoDetail(
        **_video_fields(video),
        progress=progress,
        transcript=None if transcript is None else transcript_summary(transcript),
        analysis=None if run is None else analysis_out(run),
    )


def _video_fields(video: VideoSummary) -> dict[str, Any]:
    active = video.active_job
    failure = video.last_failure
    cancelled = video.last_cancelled
    return {
        "video_id": video.video_id,
        "title": video.title,
        "channel_id": video.channel_id,
        "channel_title": video.channel_title,
        "published_at": video.published_at,
        "duration_sec": video.duration_sec,
        "origin": video.origin,
        "unavailable": video.unavailable,
        "status": video.status,
        "active_job": (
            None if active is None
            else ActiveJobOut(
                id=active.id,
                kind=active.kind,
                state=active.state,
                cancel_requested_at=active.cancel_requested_at,
            )
        ),
        "last_failure": (
            None if failure is None
            else LastFailureOut(
                job_id=failure.job_id,
                kind=failure.kind,
                error_class=failure.error_class,
                finished_at=failure.finished_at,
            )
        ),
        "latest_analysis_at": video.latest_analysis_at,
        "has_transcript": video.has_transcript,
        "last_cancelled": (
            None if cancelled is None
            else LastCancelledOut(
                job_id=cancelled.job_id, kind=cancelled.kind, finished_at=cancelled.finished_at
            )
        ),
    }


def transcript_summary(info: TranscriptInfo) -> TranscriptSummary:
    return TranscriptSummary(
        id=info.id,
        source=info.source,
        language=info.language,
        speaker_source=info.speaker_source,
        segment_count=info.segment_count,
    )


def analysis_out(run: AnalysisRun) -> AnalysisOut:
    a: Analysis = run.analysis
    assert a.id is not None and a.created_at is not None
    return AnalysisOut(
        id=a.id,
        model=a.model,
        prompt_version=a.prompt_version,
        chunk_strategy=a.chunk_strategy,
        transcript_id=a.transcript_id,
        transcript_source=run.transcript_source,
        created_at=a.created_at,
        tldr=a.tldr,
        speaker_roster=cast(JsonValue, a.speaker_roster),
        input_tokens=a.input_tokens,
        output_tokens=a.output_tokens,
        cache_read_tokens=a.cache_read_tokens,
        cache_write_tokens=a.cache_write_tokens,
        calls=a.calls,
        cost_usd=a.cost_usd,
        duration_ms=a.duration_ms,
        topics=[
            TopicOut(seq=t.seq, title=t.title, summary=t.summary, start_sec=t.start_sec)
            for t in a.topics
        ],
        claims=[
            ClaimOut(
                text=c.text,
                speaker=c.speaker,
                start_sec=c.start_sec,
                confidence=c.confidence,
                source_chunk_seq=c.source_chunk_seq,
            )
            for c in a.claims
        ],
        quotes=[
            QuoteOut(
                text=q.text,
                speaker=q.speaker,
                start_sec=q.start_sec,
                source_chunk_seq=q.source_chunk_seq,
            )
            for q in a.quotes
        ],
    )


class OpsChannelOut(BaseModel):
    channel_id: str
    title: str | None
    poll_failing_since: UtcDatetime | None
    poll_failures: int
    last_poll_err: str | None
    feed_gap_detected_at: UtcDatetime | None
    feed_gap_reason: Literal["suspected", "no_anchor", "listing_window_exhausted"] | None


class OpsChannelListOut(BaseModel):
    items: list[OpsChannelOut]


class JobOut(BaseModel):
    """One ``jobs`` row for the Ops view (#44). ``payload`` is never exposed."""

    id: int
    video_id: str
    video_title: str | None
    kind: str
    dedupe_key: str
    state: str
    priority: int
    attempts: int
    error_class: str | None
    last_error: str | None
    run_after: UtcDatetime
    locked_by: str | None
    heartbeat_at: UtcDatetime | None
    finished_at: UtcDatetime | None
    created_at: UtcDatetime
    progress: ProgressOut | None
    cancel_requested_at: UtcDatetime | None = None


class JobListOut(BaseModel):
    items: list[JobOut]
    next_before_id: int | None


def job_out(row: JobRow, history: Sequence[float] | None = None) -> JobOut:
    return JobOut(
        id=row.id,
        video_id=row.video_id,
        video_title=row.video_title,
        kind=row.kind,
        dedupe_key=row.dedupe_key,
        state=row.state,
        priority=row.priority,
        attempts=row.attempts,
        error_class=row.error_class,
        last_error=row.last_error,
        run_after=row.run_after,
        locked_by=row.locked_by,
        heartbeat_at=row.heartbeat_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
        progress=progress_out(row, history),
        cancel_requested_at=row.cancel_requested_at,
    )


def transcript_out(page: TranscriptPage, *, offset: int, limit: int) -> TranscriptOut:
    info = page.info
    return TranscriptOut(
        video_id=page.video_id,
        transcript_id=info.id,
        source=info.source,
        language=info.language,
        speaker_source=info.speaker_source,
        total_segments=info.segment_count,
        offset=offset,
        limit=limit,
        segments=[
            SegmentOut(index=s.index, start=s.start, end=s.end, text=s.text, speaker=s.speaker)
            for s in page.segments
        ],
    )


class BackfillRequestOut(BaseModel):
    """``GET /channels/{id}/backfill-requests/{request_id}`` (#115).

    ``result`` is ``null`` until the first listing succeeds; then it has the
    keys of ``POST /channels/{id}/backfill``'s response.
    """

    request_id: int
    channel_id: str
    limit: int
    dry_run: bool
    state: Literal["pending", "running", "done", "failed"]
    attempts: int
    enqueued: int
    error_class: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    finished_at: UtcDatetime | None
    result: dict[str, JsonValue] | None


def backfill_request_out(row: BackfillRequestRow) -> BackfillRequestOut:
    return BackfillRequestOut(
        request_id=row.id,
        channel_id=row.channel_id,
        limit=row.limit_n,
        dry_run=row.dry_run,
        state=cast(Any, row.state),
        attempts=row.attempts,
        enqueued=row.enqueued,
        error_class=row.error_class,
        created_at=row.created_at,
        updated_at=row.updated_at,
        finished_at=row.finished_at,
        result=cast(Any, row.result),
    )


CatalogEnrichmentState = Literal["pending", "running", "done", "failed"]
CatalogLimit = Literal[50, 200, 500, 5000]


class CatalogItemOut(BaseModel):
    """One upload in a channel catalog (#242).

    ``url`` is derived from the validated ``video_id``; no source URL is stored.
    ``title`` comes with the flat listing, ``description`` and ``published_at``
    are null until ``enrichment_state`` is ``done``. ``known`` is true when the
    video already has a library row or was deleted; ``processing_status`` is its
    library status, null when not in the library.
    """

    video_id: str
    title: str | None
    url: str
    description: str | None
    published_at: UtcDatetime | None
    enrichment_state: CatalogEnrichmentState
    enrichment_error: str | None
    known: bool
    processing_status: VideoStatus | None


class CatalogPageOut(BaseModel):
    """A channel's persisted catalog, newest first (#242)."""

    channel_id: str
    limit: CatalogLimit
    total_count: int | None
    items: list[CatalogItemOut]


def catalog_item_out(video: CatalogVideo) -> CatalogItemOut:
    if not is_video_id(video.video_id):  # the table's CHECK makes this unreachable
        raise ValueError("catalog row has a non-canonical video ID")
    return CatalogItemOut(
        video_id=video.video_id,
        title=video.title,
        url=f"https://www.youtube.com/watch?v={video.video_id}",
        description=video.description,
        published_at=video.published_at,
        enrichment_state=video.enrichment_state,
        enrichment_error=video.enrichment_error,
        known=video.known,
        processing_status=video.processing_status,
    )


def catalog_page_out(page: CatalogPage) -> CatalogPageOut:
    return CatalogPageOut(
        channel_id=page.channel_id,
        limit=cast(CatalogLimit, page.limit),
        total_count=page.total_count,
        items=[catalog_item_out(item) for item in page.items],
    )


class ModelEstimate(BaseModel):
    """One model's estimated analysis cost (#153). ``cost_usd`` is a number only when ``estimated``.

    ``unknown`` carries a ``reason``; ``free`` has neither number nor reason.
    """

    model: str
    state: Literal["estimated", "unknown", "free"]
    cost_usd: float | None
    reason: Literal["no_transcript", "no_cost_samples", "no_price"] | None
    samples: int


class AnalysisEstimate(BaseModel):
    video_id: str
    summarizer: Literal["ollama", "anthropic", "free"]
    transcript_chars: int | None
    batch: bool
    estimates: list[ModelEstimate]
