"""The ``ingest`` job handler (issue #28, architecture.md §7.1).

Fetches metadata, upserts the ``videos`` row, then picks exactly one
transcript path (see ``choose_transcript_path``). Captions first (#416):
YouTube's English captions (manual, else auto) are used whenever they can be had;
the video goes to Whisper only when they cannot be acquired. The handler holds no state
between jobs and writes no SQL: the database is reached through
``common.repo`` and the ``JobQueue`` port.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

import psycopg
import structlog

from adapters.youtube.errors import UpcomingVideoError
from adapters.youtube.subtitles import SubtitleAvailability, speaker_source
from common.config import Settings
from common.errors import (
    Defer,
    PermanentSourceError,
    RateLimitedError,
    ToolFailureError,
    TransientNetworkError,
    UnavailableReason,
)
from common.models import MetadataSource, Segment, VideoMeta
from common.queue import Job, JobQueue
from common.repo.jobs import latest_job_outcome
from common.repo.transcripts import get_best_transcript
from common.repo.videos import (
    JobGuard,
    clear_unavailable,
    get_discovered_at,
    record_unavailable,
    upsert_video,
)
from common.shorts import should_skip_short
from common.worker import JobContext
from common.youtube_refs import VIDEO_ID_PATTERN
from services.transcriber.persist import enqueue_analyze, save_transcript_with_chunks

_VIDEO_ID = VIDEO_ID_PATTERN
_ORIGINS = frozenset({"adhoc", "rss", "backfill"})
_STRONG_SOURCES = frozenset({"youtube_manual", "whisper"})

#: Why a video went to Whisper (recorded in the transcribe payload, the transcript's
#: ``engine_meta`` and the job log).
WHISPER_NO_TRACKS = "none"
WHISPER_AUTO_DISABLED = "auto_disabled"
WHISPER_EMPTY_TRACK = "empty"
WHISPER_RATE_LIMITED = "rate_limited"
WHISPER_TIMEOUT = "timeout"
WHISPER_TOOL_FAILURE = "tool_failure"
WHISPER_PREFERRED = "prefer_whisper"

_log = structlog.get_logger(__name__)


def warn_if_prefer_whisper(settings: Settings) -> None:
    """One startup WARNING when the override that ignores captions is on (#416)."""
    if settings.PREFER_WHISPER:
        _log.warning(
            "PREFER_WHISPER is on: YouTube captions are ignored and every video goes to "
            "Whisper. This is an override for tests, not for production."
        )


#: Rescheduling of premieres and live streams (#65). A premiere often starts a
#: few minutes late, so the first look is a margin after the scheduled start.
PREMIERE_MARGIN = timedelta(minutes=10)
#: Start unknown, or past but the video still reports upcoming.
UNKNOWN_START_RECHECK = timedelta(hours=1)
#: The end of a running stream cannot be known; look again after a typical length.
LIVE_RECHECK = timedelta(hours=2)
#: Ended, but YouTube is still processing the recording.
POST_LIVE_RECHECK = timedelta(minutes=30)
_NOT_YET = {"is_upcoming", "is_live", "post_live"}


def _not_yet_until(meta_status: str, release_at: datetime | None, now: datetime) -> datetime:
    """When to look at a video that is not available yet again."""
    if meta_status == "is_live":
        return now + LIVE_RECHECK
    if meta_status == "post_live":
        return now + POST_LIVE_RECHECK
    if release_at is not None and release_at > now:
        return release_at + PREMIERE_MARGIN
    if release_at is not None:
        return now + PREMIERE_MARGIN  # scheduled start passed: it is just late
    return now + UNKNOWN_START_RECHECK


class SubtitleSource(Protocol):
    """The subtitle port (architecture.md §3)."""

    def available(self, meta: VideoMeta) -> SubtitleAvailability: ...

    def fetch(
        self,
        video_id: str,
        lang: str,
        kind: str,
        *,
        cancelled: Callable[[], bool] | None = None,
        timeout: float | None = None,
    ) -> list[Segment]: ...


class TranscriptPath(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"
    TRANSCRIBE = "transcribe"
    AUTO_FALLBACK = "auto_fallback"
    HAVE_TRANSCRIPT = "have_transcript"
    NONE = "none"


def choose_transcript_path(
    availability: SubtitleAvailability,
    *,
    prefer_whisper: bool,
    auto_caption_fallback: bool,
    existing_source: str | None,
    transcribe_state: str | None,
    transcribe_error_class: str | None = None,
) -> TranscriptPath:
    """The ingest decision table (issues #28, #416), as a pure function. First match wins.

    1. An existing ``youtube_manual`` or ``whisper`` transcript -> ``HAVE_TRANSCRIPT``; an
       existing ``youtube_auto`` one too, unless a manual track can be used now.
    2. ``PREFER_WHISPER`` off, manual EN available -> ``MANUAL``.
    3. ``PREFER_WHISPER`` off, ``AUTO_CAPTION_FALLBACK`` on, auto EN available -> ``AUTO``.
    4. Newest ``transcribe`` job not dead -> ``TRANSCRIBE``. A user-cancelled one
       (``CANCELLED``, #237) is not a dead-letter: the video transcribes again.
    5. Newest ``transcribe`` job dead -> auto EN available and enabled -> ``AUTO_FALLBACK``,
       else ``NONE`` (the #92 planner sweep keeps working).

    Row 4 is reached when YouTube lists no usable English track, a track could not be
    acquired (the handler removes it from ``availability`` and asks again), or
    ``PREFER_WHISPER`` is on. No origin appears here: every origin is treated alike.
    """
    if existing_source in _STRONG_SOURCES:
        return TranscriptPath.HAVE_TRANSCRIPT
    if existing_source == "youtube_auto" and not prefer_whisper:
        if availability.manual is not None:
            return TranscriptPath.MANUAL
        return TranscriptPath.HAVE_TRANSCRIPT
    if not prefer_whisper:
        if availability.manual is not None:
            return TranscriptPath.MANUAL
        if availability.auto is not None and auto_caption_fallback:
            return TranscriptPath.AUTO
    if transcribe_state != "dead" or transcribe_error_class == "CANCELLED":
        return TranscriptPath.TRANSCRIBE
    if availability.auto is not None and auto_caption_fallback:
        return TranscriptPath.AUTO_FALLBACK
    return TranscriptPath.NONE


def _validate(job: Job) -> str:
    video_id = job.video_id
    if not isinstance(video_id, str) or _VIDEO_ID.fullmatch(video_id) is None:
        raise ValueError(f"invalid video id: {video_id!r}")
    origin = job.payload.get("origin", "adhoc")
    if not isinstance(origin, str) or origin not in _ORIGINS:
        raise ValueError(f"invalid origin {origin!r}; expected one of {sorted(_ORIGINS)}")
    return origin


def make_ingest_handler(
    *,
    conn: psycopg.Connection[Any],
    queue: JobQueue,
    metadata: MetadataSource,
    subtitles: SubtitleSource,
    settings: Settings,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    guard_writes: bool = True,
    monotonic: Callable[[], float] = time.monotonic,
) -> Callable[[Job, JobContext], None]:
    """Build the handler. ``guard_writes`` puts its ``videos`` writes under the job-row
    guard (#303, architecture.md 7.1); only the CLI, whose in-process jobs have no
    ``jobs`` row, turns it off. ``now`` and ``monotonic`` (the caption cap's clock) are injectable."""
    def not_yet_available(
        job: Job, ctx: JobContext, status: str, until_of: Callable[[datetime], datetime]
    ) -> None:
        """Defer, or dead-letter once the video has waited too long (#65)."""
        current = now()
        discovered = get_discovered_at(conn, job.video_id)
        conn.commit()
        if discovered is None:
            discovered = job.created_at
        limit = timedelta(days=settings.UPCOMING_MAX_WAIT_DAYS)
        if current - discovered >= limit:
            raise PermanentSourceError(
                UnavailableReason.REMOVED,
                f"video {job.video_id} is still {status} {settings.UPCOMING_MAX_WAIT_DAYS} days "
                f"after it was discovered ({discovered.isoformat()}); giving up",
            )
        until = until_of(current)
        ctx.logger.info(
            "video not available yet, rescheduling", live_status=status, until=until.isoformat()
        )
        raise Defer(until)

    def handler(job: Job, ctx: JobContext) -> None:
        origin = _validate(job)
        video_id = job.video_id
        guard = JobGuard(job.id, job.locked_by) if guard_writes else None

        def permanent(exc: PermanentSourceError) -> None:
            record_unavailable(conn, video_id, exc.reason.value, origin=origin, guard=guard)
            conn.commit()

        try:
            meta = metadata.fetch(video_id, cancelled=lambda: ctx.cancelled)
        except PermanentSourceError as exc:
            permanent(exc)
            raise
        except UpcomingVideoError:
            # yt-dlp reported the premiere but could not dump it: start unknown.
            not_yet_available(
                job, ctx, "is_upcoming", lambda current: _not_yet_until("is_upcoming", None, current)
            )
        if meta.video_id != video_id:
            raise ToolFailureError(
                f"metadata is for {meta.video_id!r}, but the job is for {video_id!r}"
            )
        ctx.check_cancelled()

        if meta.live_status in _NOT_YET:
            status = meta.live_status
            not_yet_available(
                job, ctx, status, lambda current: _not_yet_until(status, meta.release_at, current)
            )

        upsert_video(conn, meta, origin, guard=guard)
        # A policy outcome, not a source failure: the job finishes done (#80).
        # Only automatic origins are skipped; a human's `adhoc` submission
        # clears an earlier skip and is processed.
        if should_skip_short(
            is_short=meta.is_short, origin=origin, skip_shorts=settings.SKIP_SHORTS
        ):
            record_unavailable(
                conn, video_id, UnavailableReason.SHORT.value, origin=origin, guard=guard
            )
            conn.commit()
            ctx.logger.info("ingest skipped: short", video_id=video_id, origin=origin)
            return
        clear_unavailable(conn, video_id)
        conn.commit()

        # The caption step (every download together) has a hard wall-clock cap, #416: the
        # yt-dlp child is killed at it. Anything short of captions sends the video to Whisper.
        cap = float(settings.CAPTIONS_CHECK_TIMEOUT_SEC)
        started = monotonic()

        def try_track(kind: str, lang: str) -> tuple[list[Segment], str | None]:
            """The segments of one track, or why it could not be acquired."""
            ctx.check_cancelled()
            remaining = cap - (monotonic() - started)
            if remaining <= 0:
                return [], WHISPER_TIMEOUT
            try:
                segments = subtitles.fetch(
                    video_id, lang, kind, cancelled=lambda: ctx.cancelled, timeout=remaining
                )
            except PermanentSourceError as exc:
                permanent(exc)
                raise
            except RateLimitedError as exc:
                ctx.logger.warning("captions rate limited", track=lang, kind=kind, error=str(exc))
                return [], WHISPER_RATE_LIMITED
            except TransientNetworkError as exc:
                ctx.logger.warning("caption download timed out or failed", kind=kind, error=str(exc))
                return [], WHISPER_TIMEOUT
            except ToolFailureError as exc:
                ctx.logger.warning("caption download failed", track=lang, kind=kind, error=str(exc))
                return [], WHISPER_TOOL_FAILURE
            return segments, (None if segments else WHISPER_EMPTY_TRACK)

        available = subtitles.available(meta)
        best = get_best_transcript(conn, video_id)
        existing_source = best.source if best is not None else None
        transcribe_state, transcribe_error_class = latest_job_outcome(conn, video_id, "transcribe")
        conn.commit()  # end the read transaction: enqueue() opens its own

        def choose(availability: SubtitleAvailability) -> TranscriptPath:
            return choose_transcript_path(
                availability,
                prefer_whisper=settings.PREFER_WHISPER,
                auto_caption_fallback=settings.AUTO_CAPTION_FALLBACK,
                existing_source=existing_source,
                transcribe_state=transcribe_state,
                transcribe_error_class=transcribe_error_class,
            )

        tracks = available
        path = choose(tracks)
        saved_source: str | None = None
        segments: list[Segment] = []
        kind = ""
        lang = ""
        whisper_reason: str | None = None

        # A track that cannot be acquired is removed and the table asked again:
        # manual -> auto -> Whisper. A 429 or a timeout skips every remaining track.
        while path in (TranscriptPath.MANUAL, TranscriptPath.AUTO, TranscriptPath.AUTO_FALLBACK):
            if path is TranscriptPath.MANUAL:
                assert tracks.manual is not None
                kind, lang = "manual", tracks.manual
            else:
                assert tracks.auto is not None
                kind, lang = "auto", tracks.auto
            segments, why = try_track(kind, lang)
            if segments:
                break
            if whisper_reason is None or why in (WHISPER_RATE_LIMITED, WHISPER_TIMEOUT):
                whisper_reason = why
            if why in (WHISPER_RATE_LIMITED, WHISPER_TIMEOUT):
                tracks = SubtitleAvailability(manual=None, auto=None)
            elif kind == "manual":
                tracks = SubtitleAvailability(manual=None, auto=tracks.auto)
            else:
                tracks = SubtitleAvailability(manual=tracks.manual, auto=None)
            path = choose(tracks)

        if segments:
            saved_source = "youtube_manual" if kind == "manual" else "youtube_auto"
            if kind == "auto":
                ctx.logger.info(
                    "using YouTube auto-generated captions: no manual track", track=lang
                )
            save_transcript_with_chunks(
                conn,
                settings,
                video_id,
                source=saved_source,
                language=lang,
                speaker_source=speaker_source(segments),
                segments=segments,
                engine_meta={"track": lang, "kind": kind},
            )
            enqueue_analyze(queue, settings, job.id, video_id, priority=job.priority)
        elif path is TranscriptPath.HAVE_TRANSCRIPT:
            enqueue_analyze(queue, settings, job.id, video_id, priority=job.priority)
        elif path is TranscriptPath.TRANSCRIBE:
            if whisper_reason is None:
                if settings.PREFER_WHISPER:
                    whisper_reason = WHISPER_PREFERRED
                elif available.auto is not None and not settings.AUTO_CAPTION_FALLBACK:
                    whisper_reason = WHISPER_AUTO_DISABLED
                else:
                    whisper_reason = WHISPER_NO_TRACKS
            log = (
                ctx.logger.warning
                if whisper_reason in (WHISPER_RATE_LIMITED, WHISPER_TOOL_FAILURE, WHISPER_TIMEOUT)
                else ctx.logger.info
            )
            log("whisper chosen: captions could not be acquired", reason=whisper_reason)
            # duration_sec lets the worker derive this job's runtime limit (#66).
            payload: dict[str, Any] = {"captions_unavailable": whisper_reason}
            if meta.language:
                payload["language"] = meta.language
            if meta.duration_sec:
                payload["duration_sec"] = meta.duration_sec
            job_id = queue.handoff(
                job.id,
                "transcribe",
                video_id,
                payload=payload,
                priority=job.priority,
            )
            if job_id is None:
                ctx.logger.info("transcribe job already pending or running")
        elif path is TranscriptPath.NONE:
            ctx.logger.warning(
                "no transcript source left: transcription dead-lettered and "
                "auto-captions are unavailable or disabled"
            )

        summary: dict[str, Any] = {"transcript_path": path.value}
        if saved_source is not None:
            summary["transcript_source"] = saved_source
        if path is TranscriptPath.TRANSCRIBE:
            summary["whisper_reason"] = whisper_reason
        ctx.logger.info("ingest finished", **summary)

    return handler
