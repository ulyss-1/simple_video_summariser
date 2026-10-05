"""Response models for the read routes (issue #42).

They are the public contract #46 generates TypeScript types from, so every
field is declared explicitly. Datetimes are always serialised in UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from pydantic import AfterValidator, BaseModel, JsonValue

from common.models import (
    Analysis,
    AnalysisRun,
    TranscriptInfo,
    TranscriptPage,
    VideoSummary,
)

UtcDatetime = Annotated[datetime, AfterValidator(lambda value: value.astimezone(UTC))]
VideoStatus = Literal["done", "processing", "unavailable", "failed", "idle"]


class ActiveJobOut(BaseModel):
    id: int
    kind: str
    state: str


class LastFailureOut(BaseModel):
    job_id: int
    kind: str
    error_class: str | None
    finished_at: UtcDatetime | None


class VideoItem(BaseModel):
    video_id: str
    title: str | None
    channel_id: str | None
    channel_title: str | None
    published_at: UtcDatetime | None
    duration_sec: int | None
    origin: str
    unavailable: str | None
    status: VideoStatus
    active_job: ActiveJobOut | None
    last_failure: LastFailureOut | None
    latest_analysis_at: UtcDatetime | None


class VideoList(BaseModel):
    items: list[VideoItem]
    total: int
    offset: int
    limit: int


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


def video_item(video: VideoSummary) -> VideoItem:
    return VideoItem(**_video_fields(video))


def video_detail(
    video: VideoSummary, transcript: TranscriptInfo | None, run: AnalysisRun | None
) -> VideoDetail:
    return VideoDetail(
        **_video_fields(video),
        transcript=None if transcript is None else transcript_summary(transcript),
        analysis=None if run is None else analysis_out(run),
    )


def _video_fields(video: VideoSummary) -> dict[str, Any]:
    active = video.active_job
    failure = video.last_failure
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
            else ActiveJobOut(id=active.id, kind=active.kind, state=active.state)
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
