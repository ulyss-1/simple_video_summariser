"""Migration `0003_jobs` (issue #9, architecture.md §6, correction C1).

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
    # Same pattern as tests/migrations/test_core_schema.py (#8): env.py reads
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
    """A DSN for a database migrated all the way to head (0003)."""
    command.upgrade(_config_for(postgres_dsn, monkeypatch), "head")
    return postgres_dsn


def _insert_job(
    cur: psycopg.Cursor,
    video_id: str = "v1",
    kind: str = "ingest",
    **overrides: object,
) -> int:
    columns = ["video_id", "kind", *overrides.keys()]
    values = [video_id, kind, *overrides.values()]
    placeholders = ", ".join(["%s"] * len(values))
    cur.execute(
        f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({placeholders}) RETURNING id",
        values,
    )
    row = cur.fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# Table, columns and no FK to videos
# ---------------------------------------------------------------------------


def test_jobs_table_exists_with_every_column_from_ac(head_dsn: str) -> None:
    expected = {
        "id",
        "video_id",
        "kind",
        "dedupe_key",
        "state",
        "priority",
        "payload",
        "attempts",
        "last_error",
        "error_class",
        "run_after",
        "locked_by",
        "locked_at",
        "heartbeat_at",
        "finished_at",
        "created_at",
    }
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'jobs'
            """
        )
        actual = {row[0] for row in cur.fetchall()}
    assert expected <= actual


def test_video_id_has_no_foreign_key_to_videos(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'jobs'::regclass AND contype = 'f'
            """
        )
        assert cur.fetchall() == []


def test_job_row_survives_deletion_of_its_video(head_dsn: str) -> None:
    # Functional counterpart to the FK check above: a video_id that never
    # existed in videos (or has since been deleted) is accepted, so job
    # history outlives the video (§6, AC: "job history survives a video's
    # deletion").
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        job_id = _insert_job(cur, video_id="never-existed", kind="ingest")
        conn.commit()

        cur.execute("SELECT video_id FROM jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
    assert row == ("never-existed",)


# ---------------------------------------------------------------------------
# Indexes: exact column order and WHERE predicate from §6
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("index_name", "expected_fragment"),
    [
        (
            "jobs_active_uniq",
            (
                "USING btree (video_id, kind, dedupe_key) WHERE "
                "(state = ANY (ARRAY['pending'::text, 'running'::text]))"
            ),
        ),
        (
            "jobs_claim_idx",
            "USING btree (kind, priority DESC, run_after) WHERE (state = 'pending'::text)",
        ),
        (
            "jobs_reap_idx",
            "USING btree (heartbeat_at) WHERE (state = 'running'::text)",
        ),
        (
            "jobs_dead_idx",
            "USING btree (created_at DESC) WHERE (state = 'dead'::text)",
        ),
    ],
)
def test_index_has_exact_column_order_and_predicate(
    head_dsn: str, index_name: str, expected_fragment: str
) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = %s",
            (index_name,),
        )
        row = cur.fetchone()
    assert row is not None
    assert expected_fragment in row[0]


def test_jobs_active_uniq_is_unique(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT indexdef FROM pg_indexes
            WHERE schemaname = 'public' AND indexname = 'jobs_active_uniq'
            """
        )
        row = cur.fetchone()
    assert row is not None
    assert "CREATE UNIQUE INDEX" in row[0]


# ---------------------------------------------------------------------------
# Defaults (inserting with only video_id and kind)
# ---------------------------------------------------------------------------


def test_insert_with_only_video_id_and_kind_gets_expected_defaults(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        job_id = _insert_job(cur, video_id="v1", kind="ingest")
        conn.commit()

        cur.execute(
            """
            SELECT dedupe_key, state, priority, payload, attempts,
                   run_after, now() - run_after < interval '1 minute'
            FROM jobs WHERE id = %s
            """,
            (job_id,),
        )
        row = cur.fetchone()
    assert row is not None
    dedupe_key, state, priority, payload, attempts, run_after, run_after_is_now = row
    assert dedupe_key == "default"
    assert state == "pending"
    assert priority == 0
    assert payload == {}
    assert attempts == 0
    assert run_after is not None
    assert run_after_is_now is True


# ---------------------------------------------------------------------------
# Uniqueness while active (C1)
# ---------------------------------------------------------------------------


def test_second_pending_row_with_same_dedupe_key_is_rejected(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        _insert_job(cur, video_id="v1", kind="ingest")
        conn.commit()

        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            _insert_job(cur, video_id="v1", kind="ingest")


def test_second_pending_row_is_rejected_when_existing_row_is_running(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        job_id = _insert_job(cur, video_id="v1", kind="ingest")
        cur.execute("UPDATE jobs SET state = 'running' WHERE id = %s", (job_id,))
        conn.commit()

        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            _insert_job(cur, video_id="v1", kind="ingest")


@pytest.mark.parametrize("terminal_state", ["done", "dead"])
def test_new_pending_row_succeeds_once_existing_row_is_terminal(
    head_dsn: str, terminal_state: str
) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        job_id = _insert_job(cur, video_id="v1", kind="ingest")
        cur.execute("UPDATE jobs SET state = %s WHERE id = %s", (terminal_state, job_id))
        conn.commit()

        new_id = _insert_job(cur, video_id="v1", kind="ingest")
        conn.commit()

        cur.execute("SELECT state FROM jobs WHERE id = %s", (new_id,))
        row = cur.fetchone()
    assert row == ("pending",)


def test_analyze_rows_with_different_dedupe_keys_can_both_be_pending(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        _insert_job(cur, video_id="v1", kind="analyze", dedupe_key="v1:ollama")
        _insert_job(cur, video_id="v1", kind="analyze", dedupe_key="v2:ollama")
        conn.commit()

        cur.execute(
            "SELECT count(*) FROM jobs WHERE video_id = 'v1' AND kind = 'analyze' "
            "AND state = 'pending'"
        )
        row = cur.fetchone()
    assert row == (2,)


# ---------------------------------------------------------------------------
# ON CONFLICT DO NOTHING (exact form #11's enqueue will use)
# ---------------------------------------------------------------------------


def _enqueue_on_conflict_do_nothing(
    cur: psycopg.Cursor, video_id: str, kind: str, dedupe_key: str = "default"
) -> int:
    cur.execute(
        """
        INSERT INTO jobs (video_id, kind, dedupe_key)
        VALUES (%s, %s, %s)
        ON CONFLICT (video_id, kind, dedupe_key) WHERE state IN ('pending','running')
        DO NOTHING
        """,
        (video_id, kind, dedupe_key),
    )
    return cur.rowcount


def test_on_conflict_do_nothing_affects_zero_rows_when_duplicate_pending_exists(
    head_dsn: str,
) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        _insert_job(cur, video_id="v1", kind="ingest")
        conn.commit()

        affected = _enqueue_on_conflict_do_nothing(cur, "v1", "ingest")
        conn.commit()
    assert affected == 0


def test_on_conflict_do_nothing_inserts_when_no_duplicate_exists(head_dsn: str) -> None:
    with psycopg.connect(head_dsn) as conn, conn.cursor() as cur:
        affected = _enqueue_on_conflict_do_nothing(cur, "v1", "ingest")
        conn.commit()

        cur.execute("SELECT count(*) FROM jobs WHERE video_id = 'v1' AND kind = 'ingest'")
        row = cur.fetchone()
    assert affected == 1
    assert row == (1,)


# ---------------------------------------------------------------------------
# downgrade / upgrade round trip
# ---------------------------------------------------------------------------


def test_downgrade_drops_the_table_and_its_indexes_and_upgrade_succeeds_again(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "head")

    command.downgrade(config, "-1")

    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'jobs'
            """
        )
        table_row = cur.fetchone()
        cur.execute(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname = 'public' AND indexname LIKE 'jobs_%'
            """
        )
        index_rows = cur.fetchall()
    assert table_row is None
    assert index_rows == []

    # upgrade head succeeds again on the same, now jobs-less database.
    command.upgrade(config, "head")
    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'jobs'
            """
        )
        table_row_after = cur.fetchone()
    assert table_row_after is not None
