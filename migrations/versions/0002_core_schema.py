"""core schema

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-27

Creates the domain schema exactly as specified in architecture.md §6:
``channels``, ``videos``, ``transcripts`` (with full-text search and
``transcript_rank()``), ``transcript_chunks``, ``analyses``, ``topics``,
``claims`` and ``quotes`` (task #8). The ``jobs`` table (#9) and ``media``
table (#10) are out of scope and follow as later revisions.

Raw SQL through ``op.execute``, copied from §6 (convention set by #7). No
SQLAlchemy table objects, no CHECK constraints on the enumerated TEXT
columns (``source``, ``origin``, ``unavailable``, ``confidence``) — those
are validated in code by #14/#22 per the issue's "Out of scope".

``transcripts.fts`` stays a **STORED** generated column so the GIN index
can materialize it — PostgreSQL 18's virtual-by-default generated columns
would break that index (architecture.md §16.1).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ============ channels & videos ============
    op.execute(
        """
        CREATE TABLE channels (
            channel_id    TEXT PRIMARY KEY,
            title         TEXT,
            active        BOOLEAN     NOT NULL DEFAULT true,
            monitor_from  TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_polled   TIMESTAMPTZ,
            last_poll_err TEXT,
            added_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE videos (
            video_id      TEXT PRIMARY KEY,
            channel_id    TEXT REFERENCES channels(channel_id),
            title         TEXT,
            duration_sec  INTEGER,
            published_at  TIMESTAMPTZ,
            description   TEXT,
            origin        TEXT NOT NULL DEFAULT 'adhoc',
            unavailable   TEXT,
            discovered_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX videos_channel_pub_idx ON videos (channel_id, published_at DESC)")

    # ============ transcripts ============
    op.execute(
        """
        CREATE TABLE transcripts (
            id             BIGSERIAL PRIMARY KEY,
            video_id       TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
            source         TEXT NOT NULL,
            language       TEXT,
            speaker_source TEXT NOT NULL DEFAULT 'none',
            segments       JSONB NOT NULL,
            full_text      TEXT NOT NULL,
            engine_meta    JSONB,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (video_id, source)
        )
        """
    )

    op.execute(
        """
        ALTER TABLE transcripts ADD COLUMN fts tsvector
            GENERATED ALWAYS AS (to_tsvector('english', coalesce(full_text,''))) STORED
        """
    )
    op.execute("CREATE INDEX transcripts_fts_idx ON transcripts USING GIN (fts)")

    op.execute(
        """
        CREATE FUNCTION transcript_rank(src TEXT) RETURNS INT IMMUTABLE LANGUAGE sql AS
        $$ SELECT CASE src WHEN 'youtube_manual' THEN 0
                           WHEN 'whisper' THEN 1
                           WHEN 'youtube_auto' THEN 2 ELSE 3 END $$
        """
    )

    # ============ chunks (D9c) ============
    op.execute(
        """
        CREATE TABLE transcript_chunks (
            id             BIGSERIAL PRIMARY KEY,
            transcript_id  BIGINT NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
            seq            INTEGER NOT NULL,
            start_sec      NUMERIC(10,3) NOT NULL,
            end_sec        NUMERIC(10,3) NOT NULL,
            text           TEXT NOT NULL,
            chunk_strategy TEXT NOT NULL,
            UNIQUE (transcript_id, chunk_strategy, seq)
        )
        """
    )
    op.execute(
        "CREATE INDEX chunks_lookup_idx ON transcript_chunks (transcript_id, chunk_strategy, seq)"
    )

    # ============ analyses ============
    op.execute(
        """
        CREATE TABLE analyses (
            id             BIGSERIAL PRIMARY KEY,
            video_id       TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
            transcript_id  BIGINT NOT NULL REFERENCES transcripts(id),
            chunk_strategy TEXT NOT NULL,
            model          TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            tldr           TEXT NOT NULL,
            speaker_roster JSONB,
            input_tokens   INTEGER NOT NULL DEFAULT 0,
            output_tokens  INTEGER NOT NULL DEFAULT 0,
            cost_usd       NUMERIC(10,5),
            duration_ms    INTEGER,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX analyses_video_idx ON analyses (video_id, created_at DESC)")
    op.execute("CREATE INDEX analyses_version_idx ON analyses (prompt_version, model)")

    op.execute(
        """
        CREATE TABLE topics (
            id          BIGSERIAL PRIMARY KEY,
            analysis_id BIGINT NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
            seq         INTEGER NOT NULL,
            title       TEXT NOT NULL,
            summary     TEXT,
            start_sec   NUMERIC(10,3)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE claims (
            id          BIGSERIAL PRIMARY KEY,
            analysis_id BIGINT NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
            text        TEXT NOT NULL,
            speaker     TEXT NOT NULL DEFAULT 'unknown',
            start_sec   NUMERIC(10,3),
            confidence  TEXT,
            source_chunk_seq INTEGER
        )
        """
    )
    op.execute("CREATE INDEX claims_analysis_idx ON claims (analysis_id, start_sec)")

    op.execute(
        """
        CREATE TABLE quotes (
            id          BIGSERIAL PRIMARY KEY,
            analysis_id BIGINT NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
            text        TEXT NOT NULL,
            speaker     TEXT NOT NULL DEFAULT 'unknown',
            start_sec   NUMERIC(10,3),
            source_chunk_seq INTEGER
        )
        """
    )


def downgrade() -> None:
    # Reverse dependency order. Dropping a table drops its own indexes with
    # it, so only the function needs an explicit drop.
    op.execute("DROP TABLE quotes")
    op.execute("DROP TABLE claims")
    op.execute("DROP TABLE topics")
    op.execute("DROP TABLE analyses")
    op.execute("DROP TABLE transcript_chunks")
    op.execute("DROP FUNCTION transcript_rank(TEXT)")
    op.execute("DROP TABLE transcripts")
    op.execute("DROP TABLE videos")
    op.execute("DROP TABLE channels")
