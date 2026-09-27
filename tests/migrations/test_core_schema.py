"""Migration `0002_core_schema` (issue #8, architecture.md §6).

Every test here needs a live Postgres, so the module is marked
`integration` and drives the `postgres_dsn` fixture from `tests/conftest.py`
(task #7) up to `alembic head` before asserting on the resulting schema.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from common.config import get_settings

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _config_for(dsn: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    # Same pattern as tests/migrations/test_upgrade_downgrade.py (#7):
    # env.py reads DATABASE_URL via common.config.get_settings(), which is
    # lru_cache'd per process, so the cache has to be cleared after pointing
    # it at the test database.
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


@pytest.fixture
def head_dsn(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """A DSN for a database migrated all the way to head (0002)."""
    command.upgrade(_config_for(postgres_dsn, monkeypatch), "head")
    return postgres_dsn


def _insert_video(cur: psycopg.Cursor, video_id: str, channel_id: str | None = None) -> None:
    cur.execute(
        "INSERT INTO videos (video_id, channel_id) VALUES (%s, %s)",
        (video_id, channel_id),
    )


def _insert_transcript(
    cur: psycopg.Cursor, video_id: str, source: str = "whisper", full_text: str = "hello world"
) -> int:
    cur.execute(
        """
        INSERT INTO transcripts (video_id, source, segments, full_text)
        VALUES (%s, %s, '[]', %s)
        RETURNING id
        """,
        (video_id, source, full_text),
    )
    row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _insert_analysis(cur: psycopg.Cursor, video_id: str, transcript_id: int) -> int:
    cur.execute(
        """
        INSERT INTO analyses
            (video_id, transcript_id, chunk_strategy, model, prompt_version, tldr)
        VALUES (%s, %s, 'time:900:60', 'test-model', 'v1', 'tldr')
        RETURNING id
        """,
        (video_id, transcript_id),
    )
    row = cur.fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# Tables, columns and indexes exist as specified in §6
# ---------------------------------------------------------------------------


def test_all_eight_tables_exist(head_dsn: str) -> None:
    expected = {
        "channels",
        "videos",
        "transcripts",
        "transcript_chunks",
        "analyses",
        "topics",
        "claims",
        "quotes",
    }
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        actual = {row[0] for row in cur.fetchall()}
    assert expected <= actual


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("channels", "last_poll_err"),
        ("channels", "added_at"),
        ("analyses", "cost_usd"),
        ("analyses", "duration_ms"),
        ("claims", "confidence"),
        ("claims", "source_chunk_seq"),
        ("quotes", "source_chunk_seq"),
    ],
)
def test_column_from_ac_exists(head_dsn: str, table: str, column: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
            """,
            (table, column),
        )
        assert cur.fetchone() is not None


@pytest.mark.parametrize(
    "index_name",
    [
        "videos_channel_pub_idx",
        "chunks_lookup_idx",
        "analyses_video_idx",
        "analyses_version_idx",
        "claims_analysis_idx",
    ],
)
def test_index_from_ac_exists(head_dsn: str, index_name: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = %s",
            (index_name,),
        )
        assert cur.fetchone() is not None


# ---------------------------------------------------------------------------
# transcripts.fts: STORED generated column, GIN index, full-text search
# ---------------------------------------------------------------------------


def test_fts_is_a_stored_generated_column(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT attgenerated FROM pg_attribute
            WHERE attrelid = 'transcripts'::regclass AND attname = 'fts'
            """
        )
        row = cur.fetchone()
    assert row == ("s",)


def test_fts_has_a_gin_index(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT indexdef FROM pg_indexes
            WHERE schemaname = 'public' AND indexname = 'transcripts_fts_idx'
            """
        )
        row = cur.fetchone()
    assert row is not None
    assert "USING gin" in row[0]


def test_fts_matches_present_word_and_not_absent_word(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        _insert_video(cur, "v1")
        _insert_transcript(
            cur, "v1", full_text="a talk about reinforcement learning and robotics"
        )
        conn.commit()

        cur.execute(
            "SELECT id FROM transcripts WHERE fts @@ websearch_to_tsquery('english', 'reinforcement')"
        )
        assert len(cur.fetchall()) == 1

        cur.execute(
            "SELECT id FROM transcripts WHERE fts @@ websearch_to_tsquery('english', 'xylophone')"
        )
        assert cur.fetchall() == []


# ---------------------------------------------------------------------------
# transcript_rank()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected_rank"),
    [
        ("youtube_manual", 0),
        ("whisper", 1),
        ("youtube_auto", 2),
        ("something_else", 3),
        (None, 3),
    ],
)
def test_transcript_rank_returns_expected_rank(
    head_dsn: str, source: str | None, expected_rank: int
) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT transcript_rank(%s)", (source,))
        row = cur.fetchone()
    assert row == (expected_rank,)


def test_transcript_rank_is_immutable(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT provolatile FROM pg_proc
            WHERE proname = 'transcript_rank'
            """
        )
        row = cur.fetchone()
    assert row == ("i",)


# ---------------------------------------------------------------------------
# Uniqueness constraints
# ---------------------------------------------------------------------------


def test_duplicate_transcript_source_for_same_video_is_rejected(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        _insert_video(cur, "v1")
        _insert_transcript(cur, "v1", source="whisper")
        conn.commit()

        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            _insert_transcript(cur, "v1", source="whisper")

        # A different source for the same video is allowed.
        _insert_transcript(cur, "v1", source="youtube_auto")
        conn.commit()


def test_duplicate_chunk_seq_for_same_strategy_is_rejected(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        _insert_video(cur, "v1")
        transcript_id = _insert_transcript(cur, "v1")
        cur.execute(
            """
            INSERT INTO transcript_chunks
                (transcript_id, seq, start_sec, end_sec, text, chunk_strategy)
            VALUES (%s, 0, 0, 10, 'chunk 0', 'time:900:60')
            """,
            (transcript_id,),
        )
        conn.commit()

        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            cur.execute(
                """
                    INSERT INTO transcript_chunks
                        (transcript_id, seq, start_sec, end_sec, text, chunk_strategy)
                    VALUES (%s, 0, 10, 20, 'chunk 0 again', 'time:900:60')
                    """,
                (transcript_id,),
            )

        # The same seq under a different chunk_strategy is allowed.
        cur.execute(
            """
            INSERT INTO transcript_chunks
                (transcript_id, seq, start_sec, end_sec, text, chunk_strategy)
            VALUES (%s, 0, 0, 10, 'chunk 0', 'time:600:30')
            """,
            (transcript_id,),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# videos.channel_id is nullable (#14)
# ---------------------------------------------------------------------------


def test_video_can_be_inserted_with_null_channel_id(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        _insert_video(cur, "v1", channel_id=None)
        conn.commit()

        cur.execute("SELECT channel_id FROM videos WHERE video_id = 'v1'")
        row = cur.fetchone()
    assert row == (None,)


# ---------------------------------------------------------------------------
# Cascades and restricts
# ---------------------------------------------------------------------------


def test_deleting_a_video_cascades_and_leaves_other_videos_untouched(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        # v1: full tree, to be deleted.
        _insert_video(cur, "v1")
        t1 = _insert_transcript(cur, "v1")
        cur.execute(
            """
            INSERT INTO transcript_chunks
                (transcript_id, seq, start_sec, end_sec, text, chunk_strategy)
            VALUES (%s, 0, 0, 10, 'chunk 0', 'time:900:60')
            """,
            (t1,),
        )
        a1 = _insert_analysis(cur, "v1", t1)
        cur.execute(
            "INSERT INTO topics (analysis_id, seq, title) VALUES (%s, 0, 'topic')", (a1,)
        )
        cur.execute("INSERT INTO claims (analysis_id, text) VALUES (%s, 'claim')", (a1,))
        cur.execute("INSERT INTO quotes (analysis_id, text) VALUES (%s, 'quote')", (a1,))

        # v2: untouched control.
        _insert_video(cur, "v2")
        t2 = _insert_transcript(cur, "v2")
        a2 = _insert_analysis(cur, "v2", t2)
        cur.execute(
            "INSERT INTO topics (analysis_id, seq, title) VALUES (%s, 0, 'topic 2')", (a2,)
        )
        cur.execute("INSERT INTO claims (analysis_id, text) VALUES (%s, 'claim 2')", (a2,))
        cur.execute("INSERT INTO quotes (analysis_id, text) VALUES (%s, 'quote 2')", (a2,))
        conn.commit()

        cur.execute("DELETE FROM videos WHERE video_id = 'v1'")
        conn.commit()

        for table, id_column, video_id in [
            ("transcripts", "video_id", "v1"),
            ("transcript_chunks", "transcript_id", t1),
            ("analyses", "video_id", "v1"),
            ("topics", "analysis_id", a1),
            ("claims", "analysis_id", a1),
            ("quotes", "analysis_id", a1),
        ]:
            cur.execute(f"SELECT 1 FROM {table} WHERE {id_column} = %s", (video_id,))
            assert cur.fetchall() == [], f"{table} row for v1 survived the delete"

        for table, id_column, kept_id in [
            ("videos", "video_id", "v2"),
            ("transcripts", "id", t2),
            ("analyses", "id", a2),
            ("topics", "analysis_id", a2),
            ("claims", "analysis_id", a2),
            ("quotes", "analysis_id", a2),
        ]:
            cur.execute(f"SELECT 1 FROM {table} WHERE {id_column} = %s", (kept_id,))
            assert cur.fetchall() != [], f"{table} row for v2 was wrongly removed"


def test_deleting_a_transcript_referenced_by_an_analysis_is_restricted(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        _insert_video(cur, "v1")
        transcript_id = _insert_transcript(cur, "v1")
        _insert_analysis(cur, "v1", transcript_id)
        conn.commit()

        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            cur.execute("DELETE FROM transcripts WHERE id = %s", (transcript_id,))


def test_deleting_an_analysis_cascades_topics_claims_and_quotes(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        _insert_video(cur, "v1")
        transcript_id = _insert_transcript(cur, "v1")
        analysis_id = _insert_analysis(cur, "v1", transcript_id)
        cur.execute(
            "INSERT INTO topics (analysis_id, seq, title) VALUES (%s, 0, 'topic')",
            (analysis_id,),
        )
        cur.execute(
            "INSERT INTO claims (analysis_id, text) VALUES (%s, 'claim')", (analysis_id,)
        )
        cur.execute(
            "INSERT INTO quotes (analysis_id, text) VALUES (%s, 'quote')", (analysis_id,)
        )
        conn.commit()

        cur.execute("DELETE FROM analyses WHERE id = %s", (analysis_id,))
        conn.commit()

        for table in ("topics", "claims", "quotes"):
            cur.execute(f"SELECT 1 FROM {table} WHERE analysis_id = %s", (analysis_id,))
            assert cur.fetchall() == []


# ---------------------------------------------------------------------------
# downgrade / upgrade round trip
# ---------------------------------------------------------------------------


def test_downgrade_to_0001_drops_everything_including_the_function(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "head")

    command.downgrade(config, "0001")

    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
            """
        )
        tables = {row[0] for row in cur.fetchall()}
        cur.execute("SELECT 1 FROM pg_proc WHERE proname = 'transcript_rank'")
        function_row = cur.fetchone()

    created = {
        "channels",
        "videos",
        "transcripts",
        "transcript_chunks",
        "analyses",
        "topics",
        "claims",
        "quotes",
    }
    assert created.isdisjoint(tables)
    assert function_row is None

    # upgrade head succeeds again on the same, now-empty database.
    command.upgrade(config, "head")
    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        tables_after = {row[0] for row in cur.fetchall()}
    assert created <= tables_after
