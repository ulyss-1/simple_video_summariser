"""Analysis repository functions (issue #14, architecture.md §6, §7.4).

Unlike the rest of ``common/repo/``, this module's contract is called out
explicitly by issue #14: ``save_analysis`` never commits. The caller's
transaction decides, so #30 can persist a whole analysis - plus everything
else in that job - atomically.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from common.models import Analysis, Claim, Quote, Topic
from common.repo._hygiene import clean_json, clean_text


def save_analysis(conn: psycopg.Connection[Any], analysis: Analysis) -> int:
    """Insert ``analysis`` plus its topics, claims and quotes; return the id.

    Does not commit (see module docstring).
    """
    row = conn.execute(
        """
        INSERT INTO analyses
            (video_id, transcript_id, chunk_strategy, model, prompt_version,
             tldr, speaker_roster, input_tokens, output_tokens, cost_usd,
             duration_ms)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            analysis.video_id,
            analysis.transcript_id,
            analysis.chunk_strategy,
            analysis.model,
            analysis.prompt_version,
            clean_text(analysis.tldr),
            _to_jsonb(analysis.speaker_roster),
            analysis.input_tokens,
            analysis.output_tokens,
            analysis.cost_usd,
            analysis.duration_ms,
        ),
    ).fetchone()
    assert row is not None
    analysis_id = int(row[0])

    for topic in analysis.topics:
        conn.execute(
            """
            INSERT INTO topics (analysis_id, seq, title, summary, start_sec)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                analysis_id,
                topic.seq,
                clean_text(topic.title),
                clean_text(topic.summary),
                topic.start_sec,
            ),
        )

    for claim in analysis.claims:
        conn.execute(
            """
            INSERT INTO claims
                (analysis_id, text, speaker, start_sec, confidence, source_chunk_seq)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                analysis_id,
                clean_text(claim.text),
                clean_text(claim.speaker),
                claim.start_sec,
                claim.confidence,
                claim.source_chunk_seq,
            ),
        )

    for quote in analysis.quotes:
        conn.execute(
            """
            INSERT INTO quotes (analysis_id, text, speaker, start_sec, source_chunk_seq)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                analysis_id,
                clean_text(quote.text),
                clean_text(quote.speaker),
                quote.start_sec,
                quote.source_chunk_seq,
            ),
        )

    return analysis_id


def analysis_exists(
    conn: psycopg.Connection[Any],
    video_id: str,
    transcript_id: int,
    chunk_strategy: str,
    model: str,
    prompt_version: str,
) -> bool:
    """Whether an analysis with exactly this five-part key is already stored."""
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM analyses
            WHERE video_id = %s AND transcript_id = %s AND chunk_strategy = %s
              AND model = %s AND prompt_version = %s
        )
        """,
        (video_id, transcript_id, chunk_strategy, model, prompt_version),
    ).fetchone()
    assert row is not None
    return bool(row[0])


def latest_analysis(conn: psycopg.Connection[Any], video_id: str) -> Analysis | None:
    """The newest analysis for ``video_id``, with its children populated.

    Topics ordered by ``seq``, claims and quotes by ``start_sec``; ties on
    ``created_at`` broken by ``id DESC``. ``None`` when the video has no
    analysis.
    """
    row = conn.execute(
        """
        SELECT id, video_id, transcript_id, chunk_strategy, model,
               prompt_version, tldr, speaker_roster, input_tokens,
               output_tokens, cost_usd, duration_ms, created_at
        FROM analyses
        WHERE video_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (video_id,),
    ).fetchone()
    if row is None:
        return None

    (
        analysis_id,
        video_id,
        transcript_id,
        chunk_strategy,
        model,
        prompt_version,
        tldr,
        speaker_roster,
        input_tokens,
        output_tokens,
        cost_usd,
        duration_ms,
        created_at,
    ) = row

    topics = conn.execute(
        """
        SELECT id, seq, title, summary, start_sec FROM topics
        WHERE analysis_id = %s ORDER BY seq
        """,
        (analysis_id,),
    ).fetchall()
    claims = conn.execute(
        """
        SELECT id, text, speaker, start_sec, confidence, source_chunk_seq FROM claims
        WHERE analysis_id = %s ORDER BY start_sec
        """,
        (analysis_id,),
    ).fetchall()
    quotes = conn.execute(
        """
        SELECT id, text, speaker, start_sec, source_chunk_seq FROM quotes
        WHERE analysis_id = %s ORDER BY start_sec
        """,
        (analysis_id,),
    ).fetchall()

    return Analysis(
        id=int(analysis_id),
        video_id=video_id,
        transcript_id=int(transcript_id),
        chunk_strategy=chunk_strategy,
        model=model,
        prompt_version=prompt_version,
        tldr=tldr,
        speaker_roster=speaker_roster,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=float(cost_usd) if cost_usd is not None else None,
        duration_ms=duration_ms,
        created_at=created_at,
        topics=tuple(_topic(item) for item in topics),
        claims=tuple(_claim(item) for item in claims),
        quotes=tuple(_quote(item) for item in quotes),
    )


def _to_jsonb(value: dict[str, object] | None) -> Jsonb | None:
    return None if value is None else Jsonb(clean_json(value))


def _topic(row: tuple[Any, ...]) -> Topic:
    topic_id, seq, title, summary, start_sec = row
    return Topic(
        seq=seq,
        title=title,
        summary=summary,
        start_sec=float(start_sec) if start_sec is not None else None,
        id=int(topic_id),
    )


def _claim(row: tuple[Any, ...]) -> Claim:
    claim_id, text, speaker, start_sec, confidence, source_chunk_seq = row
    return Claim(
        text=text,
        speaker=speaker,
        start_sec=float(start_sec) if start_sec is not None else None,
        confidence=confidence,
        source_chunk_seq=source_chunk_seq,
        id=int(claim_id),
    )


def _quote(row: tuple[Any, ...]) -> Quote:
    quote_id, text, speaker, start_sec, source_chunk_seq = row
    return Quote(
        text=text,
        speaker=speaker,
        start_sec=float(start_sec) if start_sec is not None else None,
        source_chunk_seq=source_chunk_seq,
        id=int(quote_id),
    )
