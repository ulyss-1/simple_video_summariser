"""Migration `0004_media` (issue #10, architecture.md §6, correction D6b).

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
    # Same pattern as tests/migrations/test_jobs.py (#9): env.py reads
    # DATABASE_URL via common.config.get_settings(), which is lru_cache'd
    # per process, so the cache has to be cleared after pointing it at the
    # test database.
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


@pytest.fixture
def head_dsn(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """A DSN for a database migrated all the way to head (0004)."""
    command.upgrade(_config_for(postgres_dsn, monkeypatch), "head")
    return postgres_dsn


def _insert_video(cur: psycopg.Cursor, video_id: str) -> None:
    cur.execute("INSERT INTO videos (video_id) VALUES (%s)", (video_id,))


def _insert_media(
    cur: psycopg.Cursor,
    video_id: str = "v1",
    path: str = "v1/v1.opus",
    bytes_: int = 1024,
    **overrides: object,
) -> int:
    columns = ["video_id", "path", "bytes", *overrides.keys()]
    values = [video_id, path, bytes_, *overrides.values()]
    placeholders = ", ".join(["%s"] * len(values))
    cur.execute(
        f"INSERT INTO media ({', '.join(columns)}) VALUES ({placeholders}) RETURNING id",
        values,
    )
    row = cur.fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# Table, columns, defaults
# ---------------------------------------------------------------------------


def test_media_table_exists_with_every_column_from_ac(head_dsn: str) -> None:
    expected = {
        "id",
        "video_id",
        "path",
        "bytes",
        "format",
        "created_at",
        "expires_at",
    }
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'media'
            """
        )
        actual = {row[0] for row in cur.fetchall()}
    assert expected <= actual


def test_format_defaults_to_opus16k(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        _insert_video(cur, "v1")
        media_id = _insert_media(cur, video_id="v1", expires_at="2030-01-01T00:00:00Z")
        conn.commit()

        cur.execute("SELECT format FROM media WHERE id = %s", (media_id,))
        row = cur.fetchone()
    assert row == ("opus16k",)


# ---------------------------------------------------------------------------
# Indexes: exact columns from §6
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("index_name", "expected_fragment"),
    [
        ("media_expiry_idx", "USING btree (expires_at)"),
        ("media_lru_idx", "USING btree (created_at)"),
    ],
)
def test_index_has_exact_columns(head_dsn: str, index_name: str, expected_fragment: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = %s",
            (index_name,),
        )
        row = cur.fetchone()
    assert row is not None
    assert expected_fragment in row[0]


# ---------------------------------------------------------------------------
# UNIQUE (video_id, format)
# ---------------------------------------------------------------------------


def test_second_row_for_same_video_and_format_is_rejected(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        _insert_video(cur, "v1")
        _insert_media(cur, video_id="v1", format="opus16k", expires_at="2030-01-01T00:00:00Z")
        conn.commit()

        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            _insert_media(
                cur, video_id="v1", format="opus16k", expires_at="2030-01-01T00:00:00Z"
            )


def test_same_video_with_different_format_is_allowed(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        _insert_video(cur, "v1")
        _insert_media(cur, video_id="v1", format="opus16k", expires_at="2030-01-01T00:00:00Z")
        _insert_media(cur, video_id="v1", format="wav16k", expires_at="2030-01-01T00:00:00Z")
        conn.commit()

        cur.execute("SELECT count(*) FROM media WHERE video_id = 'v1'")
        row = cur.fetchone()
    assert row == (2,)


# ---------------------------------------------------------------------------
# expires_at NOT NULL
# ---------------------------------------------------------------------------


def test_insert_without_expires_at_is_rejected(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        _insert_video(cur, "v1")
        conn.commit()

        with pytest.raises(psycopg.errors.NotNullViolation), conn.transaction():
            _insert_media(cur, video_id="v1")


# ---------------------------------------------------------------------------
# ON DELETE CASCADE
# ---------------------------------------------------------------------------


def test_deleting_parent_video_deletes_its_media_rows(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        _insert_video(cur, "v1")
        media_id = _insert_media(cur, video_id="v1", expires_at="2030-01-01T00:00:00Z")
        conn.commit()

        cur.execute("DELETE FROM videos WHERE video_id = 'v1'")
        conn.commit()

        cur.execute("SELECT 1 FROM media WHERE id = %s", (media_id,))
        row = cur.fetchone()
    assert row is None


# ---------------------------------------------------------------------------
# Storage rule: `path` is relative to AUDIO_DIR, never absolute (#10 AC)
# ---------------------------------------------------------------------------
#
# §6 has no CHECK constraint on `path` (nothing in the DDL enforces the
# relative form at the database level), so this doesn't invent one. Instead
# it pins the convention at the value level: it builds a path the same way
# #19's adapters/youtube/audio.py does (`f"{video_id[:2]}/{video_id}.opus"`,
# see YouTubeAudio.fetch_normalized's `rel_path`), inserts it, and asserts
# it round-trips byte-for-byte and is not absolute. This test lives in
# tests/migrations/ and does not import adapters/, keeping the migration
# tests independent of #19's module per the import-direction rule
# (services/ -> adapters/ -> common/, AGENTS.md) and per the issue's
# constraint to stay inside migrations/versions/ and tests/migrations/.


def _rel_path_for(video_id: str) -> str:
    """Mirrors the storage rule documented in the migration and in
    common/models.py's AudioRef.rel_path docstring: relative to AUDIO_DIR,
    e.g. "ab/abc123def45.opus", never an absolute path."""
    return f"{video_id[:2]}/{video_id}.opus"


def test_path_column_holds_a_relative_path_not_an_absolute_one(head_dsn: str) -> None:
    video_id = "abc123def45"
    rel_path = _rel_path_for(video_id)
    assert rel_path == "ab/abc123def45.opus"  # matches the issue's own example

    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        _insert_video(cur, video_id)
        media_id = _insert_media(
            cur, video_id=video_id, path=rel_path, expires_at="2030-01-01T00:00:00Z"
        )
        conn.commit()

        cur.execute("SELECT path FROM media WHERE id = %s", (media_id,))
        row = cur.fetchone()
    assert row is not None
    stored_path = row[0]
    assert stored_path == rel_path
    assert not Path(stored_path).is_absolute()


# ---------------------------------------------------------------------------
# downgrade / upgrade round trip
# ---------------------------------------------------------------------------


def test_downgrade_drops_the_table_and_its_indexes_and_upgrade_succeeds_again(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "head")

    # Target 0004's own down_revision explicitly, not the relative "-1":
    # "-1" from head only undoes whatever the *latest* migration is, which
    # would stop being media the moment a later one (0005, ...) lands on
    # top - the same bug this fixes in test_jobs.py alongside 0004_media.
    command.downgrade(config, "0003")

    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'media'
            """
        )
        table_row = cur.fetchone()
        cur.execute(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname = 'public' AND indexname LIKE 'media_%'
            """
        )
        index_rows = cur.fetchall()
    assert table_row is None
    assert index_rows == []

    # upgrade head succeeds again on the same, now media-less database.
    command.upgrade(config, "head")
    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'media'
            """
        )
        table_row_after = cur.fetchone()
    assert table_row_after is not None
