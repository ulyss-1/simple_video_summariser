"""Tests for common/repo/transcripts.py (issue #14, architecture.md §7.3, D2)."""

from __future__ import annotations

import psycopg
import pytest

from common.models import Chunk, Segment
from common.repo.transcripts import (
    get_best_transcript,
    get_chunks,
    save_chunks,
    save_transcript,
)


def test_save_transcript_returns_an_id_and_builds_full_text(
    conn: psycopg.Connection, video_id: str
) -> None:
    segments = (Segment(0.0, 1.0, "Hello"), Segment(1.0, 2.0, "world"))

    transcript_id = save_transcript(conn, video_id, "youtube_manual", "en", "none", segments, None)

    assert isinstance(transcript_id, int)
    row = conn.execute(
        "SELECT full_text, language, speaker_source FROM transcripts WHERE id = %s",
        (transcript_id,),
    ).fetchone()
    assert row == ("Hello world", "en", "none")


def test_save_transcript_retried_returns_existing_id_and_changes_nothing(
    conn: psycopg.Connection, video_id: str
) -> None:
    first_id = save_transcript(
        conn, video_id, "whisper", "en", "none", (Segment(0, 1, "a"),), {"m": 1}
    )

    second_id = save_transcript(
        conn, video_id, "whisper", "en", "none", (Segment(0, 1, "b"),), {"m": 2}
    )

    assert second_id == first_id
    row = conn.execute(
        "SELECT full_text, engine_meta FROM transcripts WHERE id = %s", (first_id,)
    ).fetchone()
    assert row == ("a", {"m": 1})


@pytest.mark.parametrize("source", ["bogus", "", "YOUTUBE_MANUAL"])
def test_save_transcript_rejects_an_invalid_source(
    conn: psycopg.Connection, video_id: str, source: str
) -> None:
    with pytest.raises(ValueError):
        save_transcript(conn, video_id, source, "en", "none", (), None)


@pytest.mark.parametrize("speaker_source", ["bogus", "", "labels"])
def test_save_transcript_rejects_an_invalid_speaker_source(
    conn: psycopg.Connection, video_id: str, speaker_source: str
) -> None:
    with pytest.raises(ValueError):
        save_transcript(conn, video_id, "whisper", "en", speaker_source, (), None)


def test_save_transcript_strips_nul_from_text_and_engine_meta(
    conn: psycopg.Connection, video_id: str
) -> None:
    segments = (Segment(0, 1, "bad\x00text"),)

    transcript_id = save_transcript(
        conn, video_id, "whisper", "en", "none", segments, {"note": "bad\x00note"}
    )

    row = conn.execute(
        "SELECT full_text, engine_meta, segments FROM transcripts WHERE id = %s",
        (transcript_id,),
    ).fetchone()
    assert row is not None
    full_text, engine_meta, segs = row
    assert full_text == "badtext"
    assert engine_meta == {"note": "badnote"}
    assert segs[0]["text"] == "badtext"


def test_get_best_transcript_prefers_manual_over_whisper_and_auto(
    conn: psycopg.Connection, video_id: str
) -> None:
    save_transcript(conn, video_id, "youtube_auto", "en", "none", (Segment(0, 1, "auto"),), None)
    save_transcript(
        conn, video_id, "whisper", "en", "none", (Segment(0, 1, "whisper"),), None
    )
    save_transcript(
        conn,
        video_id,
        "youtube_manual",
        "en",
        "subtitle_labels",
        (Segment(0, 1, "manual"),),
        None,
    )

    best = get_best_transcript(conn, video_id)

    assert best is not None
    assert best.source == "youtube_manual"
    assert best.full_text == "manual"
    assert best.segments == (Segment(0.0, 1.0, "manual"),)


def test_get_best_transcript_prefers_whisper_over_auto(
    conn: psycopg.Connection, video_id: str
) -> None:
    save_transcript(conn, video_id, "youtube_auto", "en", "none", (Segment(0, 1, "auto"),), None)
    save_transcript(
        conn, video_id, "whisper", "en", "none", (Segment(0, 1, "whisper"),), None
    )

    best = get_best_transcript(conn, video_id)

    assert best is not None
    assert best.source == "whisper"


def test_get_best_transcript_returns_none_when_no_transcript(
    conn: psycopg.Connection, video_id: str
) -> None:
    assert get_best_transcript(conn, video_id) is None


def test_save_chunks_inserts_and_get_chunks_orders_by_seq(
    conn: psycopg.Connection, video_id: str
) -> None:
    transcript_id = save_transcript(conn, video_id, "whisper", "en", "none", (), None)
    chunks = [
        Chunk(seq=1, start_sec=60.0, end_sec=120.0, text="second"),
        Chunk(seq=0, start_sec=0.0, end_sec=60.0, text="first"),
    ]

    save_chunks(conn, transcript_id, "time:900:60", chunks)

    result = get_chunks(conn, transcript_id, "time:900:60")
    assert [c.seq for c in result] == [0, 1]
    assert [c.text for c in result] == ["first", "second"]


def test_save_chunks_twice_does_not_fail(conn: psycopg.Connection, video_id: str) -> None:
    transcript_id = save_transcript(conn, video_id, "whisper", "en", "none", (), None)
    chunks = [Chunk(seq=0, start_sec=0.0, end_sec=1.0, text="x")]

    save_chunks(conn, transcript_id, "time:900:60", chunks)
    save_chunks(conn, transcript_id, "time:900:60", chunks)

    result = get_chunks(conn, transcript_id, "time:900:60")
    assert len(result) == 1


def test_get_chunks_returns_empty_list_when_strategy_has_none(
    conn: psycopg.Connection, video_id: str
) -> None:
    transcript_id = save_transcript(conn, video_id, "whisper", "en", "none", (), None)

    assert get_chunks(conn, transcript_id, "time:900:60") == []


def test_save_chunks_strips_nul_from_text(conn: psycopg.Connection, video_id: str) -> None:
    transcript_id = save_transcript(conn, video_id, "whisper", "en", "none", (), None)

    save_chunks(conn, transcript_id, "s", [Chunk(seq=0, start_sec=0.0, end_sec=1.0, text="bad\x00text")])

    [chunk] = get_chunks(conn, transcript_id, "s")
    assert chunk.text == "badtext"
