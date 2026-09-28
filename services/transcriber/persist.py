"""Persist helpers shared by the ``ingest`` and ``transcribe`` handlers (issues #28, #29)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg

from common.chunking import chunk_segments, chunk_strategy
from common.config import Settings
from common.models import Segment
from common.queue import JobQueue, analyze_dedupe_key
from common.repo.transcripts import save_chunks, save_transcript


def save_transcript_with_chunks(
    conn: psycopg.Connection[Any],
    settings: Settings,
    video_id: str,
    *,
    source: str,
    language: str | None,
    speaker_source: str,
    segments: Sequence[Segment],
    engine_meta: dict[str, Any] | None,
) -> int:
    """Save a transcript and its chunks in one transaction, returning the transcript id.

    Any read transaction the caller left open is committed first, so the write
    below is a real transaction: a failure while saving chunks rolls the
    transcript back too, and a transcript is never left without its chunks.
    """
    chunks = chunk_segments(
        segments, chunk_sec=settings.CHUNK_SEC, overlap_sec=settings.OVERLAP_SEC
    )
    conn.commit()
    with conn.transaction():
        transcript_id = save_transcript(
            conn, video_id, source, language, speaker_source, segments, engine_meta
        )
        save_chunks(
            conn,
            transcript_id,
            chunk_strategy(settings.CHUNK_SEC, settings.OVERLAP_SEC),
            chunks,
        )
    return transcript_id


def enqueue_analyze(
    queue: JobQueue, settings: Settings, video_id: str, *, priority: int
) -> int | None:
    """Enqueue the ``analyze`` job for ``video_id`` under the configured dedupe key."""
    return queue.enqueue(
        "analyze",
        video_id,
        dedupe_key=analyze_dedupe_key(settings.PROMPT_VERSION, settings.SUMMARIZER),
        priority=priority,
    )
