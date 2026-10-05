"""Seeding helpers for the #42 read tests (repo and API level).

Every row gets explicit timestamps, so no test depends on ``now()`` order.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def channel(conn: psycopg.Connection[Any], channel_id: str, title: str | None = None) -> None:
    conn.execute(
        "INSERT INTO channels (channel_id, title) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (channel_id, title),
    )


def video(
    conn: psycopg.Connection[Any],
    video_id: str,
    *,
    channel_id: str | None = None,
    title: str | None = None,
    published_at: datetime | None = None,
    discovered_at: datetime = T0,
    duration_sec: int | None = None,
    origin: str = "adhoc",
    unavailable: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, published_at, discovered_at,
                            duration_sec, origin, unavailable)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (video_id, channel_id, title, published_at, discovered_at, duration_sec, origin,
         unavailable),
    )


def job(
    conn: psycopg.Connection[Any],
    video_id: str,
    kind: str,
    state: str,
    *,
    created: int,
    dedupe_key: str = "default",
    error_class: str | None = None,
    last_error: str | None = None,
    finished_at: datetime | None = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO jobs (video_id, kind, state, dedupe_key, created_at, error_class,
                          last_error, finished_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (video_id, kind, state, dedupe_key, at(created), error_class, last_error, finished_at),
    ).fetchone()
    assert row is not None
    return int(row[0])


def transcript(
    conn: psycopg.Connection[Any],
    video_id: str,
    source: str = "whisper",
    *,
    segments: list[dict[str, Any]] | None = None,
    language: str | None = "en",
    speaker_source: str = "none",
) -> int:
    default: list[dict[str, Any]] = [{"start": 0.0, "end": 1.0, "text": "hi"}]
    segs = segments if segments is not None else default
    row = conn.execute(
        """
        INSERT INTO transcripts (video_id, source, language, speaker_source, segments, full_text)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s) RETURNING id
        """,
        (video_id, source, language, speaker_source, json.dumps(segs),
         " ".join(s["text"] for s in segs)),
    ).fetchone()
    assert row is not None
    return int(row[0])


def analysis(
    conn: psycopg.Connection[Any],
    video_id: str,
    transcript_id: int,
    *,
    created: int,
    model: str = "m1",
    prompt_version: str = "v1",
    tldr: str = "tldr",
    cost_usd: str | None = None,
    speaker_roster: Any = None,
    topics: list[tuple[int, str]] | None = None,
    claims: list[dict[str, Any]] | None = None,
    quotes: list[dict[str, Any]] | None = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO analyses (video_id, transcript_id, chunk_strategy, model, prompt_version,
                              tldr, speaker_roster, input_tokens, output_tokens, cost_usd,
                              duration_ms, created_at)
        VALUES (%s, %s, 'time:900:60', %s, %s, %s, %s::jsonb, 11, 22, %s, 333, %s)
        RETURNING id
        """,
        (video_id, transcript_id, model, prompt_version, tldr,
         None if speaker_roster is None else json.dumps(speaker_roster), cost_usd, at(created)),
    ).fetchone()
    assert row is not None
    analysis_id = int(row[0])
    for seq, title in topics or []:
        conn.execute(
            "INSERT INTO topics (analysis_id, seq, title) VALUES (%s, %s, %s)",
            (analysis_id, seq, title),
        )
    for c in claims or []:
        conn.execute(
            """
            INSERT INTO claims (analysis_id, text, speaker, start_sec, confidence,
                                source_chunk_seq)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (analysis_id, c["text"], c.get("speaker", "unknown"), c.get("start_sec"),
             c.get("confidence"), c.get("source_chunk_seq")),
        )
    for q in quotes or []:
        conn.execute(
            """
            INSERT INTO quotes (analysis_id, text, speaker, start_sec, source_chunk_seq)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (analysis_id, q["text"], q.get("speaker", "unknown"), q.get("start_sec"),
             q.get("source_chunk_seq")),
        )
    return analysis_id


def status_matrix(conn: psycopg.Connection[Any]) -> dict[str, str]:
    """One video per status rule and precedence case; returns ``video_id -> status``."""
    expected: dict[str, str] = {}

    video(conn, "st_done0001")
    t = transcript(conn, "st_done0001")
    analysis(conn, "st_done0001", t, created=1)
    expected["st_done0001"] = "done"

    video(conn, "st_proc0001")
    job(conn, "st_proc0001", "ingest", "pending", created=1)
    expected["st_proc0001"] = "processing"

    video(conn, "st_unav0001", unavailable="removed")
    expected["st_unav0001"] = "unavailable"

    video(conn, "st_fail0001")
    job(conn, "st_fail0001", "ingest", "done", created=1)
    job(conn, "st_fail0001", "transcribe", "dead", created=2, error_class="TOOL_FAILURE")
    expected["st_fail0001"] = "failed"

    video(conn, "st_idle0001")
    expected["st_idle0001"] = "idle"

    video(conn, "st_idle0002")
    job(conn, "st_idle0002", "analyze", "dead", created=1)
    job(conn, "st_idle0002", "analyze", "done", created=2)
    expected["st_idle0002"] = "idle"

    video(conn, "pr_donepend")
    t = transcript(conn, "pr_donepend")
    analysis(conn, "pr_donepend", t, created=1)
    job(conn, "pr_donepend", "analyze", "pending", created=5, dedupe_key="v2")
    expected["pr_donepend"] = "done"

    video(conn, "pr_unavdead", unavailable="private")
    job(conn, "pr_unavdead", "ingest", "dead", created=1)
    expected["pr_unavdead"] = "unavailable"

    video(conn, "pr_deadretr")
    job(conn, "pr_deadretr", "ingest", "dead", created=1)
    job(conn, "pr_deadretr", "ingest", "pending", created=2)
    expected["pr_deadretr"] = "processing"

    video(conn, "pr_unavjob1", unavailable="removed")
    job(conn, "pr_unavjob1", "ingest", "pending", created=1)
    expected["pr_unavjob1"] = "processing"

    video(conn, "tie_deaddon")
    job(conn, "tie_deaddon", "ingest", "dead", created=1)
    job(conn, "tie_deaddon", "ingest", "done", created=1, dedupe_key="b")
    expected["tie_deaddon"] = "idle"

    video(conn, "tie_donedea")
    job(conn, "tie_donedea", "ingest", "done", created=1)
    job(conn, "tie_donedea", "ingest", "dead", created=1, dedupe_key="b")
    expected["tie_donedea"] = "failed"

    return expected
