"""Migration ``0019_llm_batches_set_null`` (issue #251).

The first two tests read the revision files only. The integration tests need
Postgres (``postgres_dsn``).
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from common.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
integration = pytest.mark.integration


def _config_for(dsn: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


def _fk(dsn: str) -> tuple[str, str] | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            SELECT conname, confdeltype::text FROM pg_constraint
            WHERE conrelid = 'llm_batches'::regclass AND contype = 'f'
              AND confrelid = 'jobs'::regclass
            """
        ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


def _job_id_nullable(dsn: str) -> str:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'llm_batches' AND column_name = 'job_id'"
        ).fetchone()
    assert row is not None
    return str(row[0])


def _seed_jobs(dsn: str) -> tuple[int, int]:
    """Two jobs, each with one batch row; returns their ids."""
    with psycopg.connect(dsn) as conn:
        ids = []
        for n, video in enumerate(("aaaaaaaaaaa", "bbbbbbbbbbb")):
            conn.execute("INSERT INTO videos (video_id) VALUES (%s)", (video,))
            row = conn.execute(
                "INSERT INTO jobs (kind, video_id, dedupe_key) VALUES ('analyze', %s, 'k') RETURNING id",
                (video,),
            ).fetchone()
            assert row is not None
            ids.append(int(row[0]))
            conn.execute(
                "INSERT INTO llm_batches (job_id, step_key, batch_id, request_sha256) "
                "VALUES (%s, 'roster', %s, 'sha')",
                (ids[-1], f"msgbatch_{n}"),
            )
        conn.commit()
    return ids[0], ids[1]


def test_0019_is_chained_to_0018_and_the_history_has_one_head() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)

    revision = script.get_revision("0019")

    assert revision is not None
    assert revision.down_revision == "0018"
    assert len(script.get_heads()) == 1  # a later revision may sit on top of 0019


def test_the_upgrade_finds_the_foreign_key_in_the_catalog() -> None:
    source = (REPO_ROOT / "migrations" / "versions" / "0019_llm_batches_set_null.py").read_text()

    assert "pg_constraint" in source
    assert "DROP CONSTRAINT llm_batches_job_id_fkey" not in source


@integration
def test_upgrade_makes_job_id_nullable_with_set_null_and_a_deleted_job_leaves_its_row(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0018")
    assert _job_id_nullable(postgres_dsn) == "NO"
    fk = _fk(postgres_dsn)
    assert fk is not None and fk[1] == "c"

    command.upgrade(config, "0019")

    assert _job_id_nullable(postgres_dsn) == "YES"
    fk = _fk(postgres_dsn)
    assert fk is not None and fk[1] == "n"
    first, second = _seed_jobs(postgres_dsn)
    with psycopg.connect(postgres_dsn) as conn:
        before = conn.execute(
            "SELECT submitted_at FROM llm_batches WHERE job_id = %s", (first,)
        ).fetchone()
        conn.execute("DELETE FROM jobs WHERE id = %s", (first,))
        conn.execute("DELETE FROM jobs WHERE id = %s", (second,))
        conn.commit()
        rows = conn.execute(
            "SELECT job_id, step_key, batch_id, request_sha256, submitted_at FROM llm_batches "
            "ORDER BY batch_id"
        ).fetchall()
    # Two NULL rows with the same step_key coexist (UNIQUE treats NULLs as distinct).
    assert [r[:4] for r in rows] == [
        (None, "roster", "msgbatch_0", "sha"),
        (None, "roster", "msgbatch_1", "sha"),
    ]
    assert before is not None and rows[0][4] == before[0]


@integration
def test_downgrade_drops_null_rows_keeps_the_rest_and_restores_cascade(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0019")
    first, second = _seed_jobs(postgres_dsn)
    with psycopg.connect(postgres_dsn) as conn:
        conn.execute("DELETE FROM jobs WHERE id = %s", (first,))  # row 0 becomes NULL
        conn.commit()

    command.downgrade(config, "0018")

    assert _job_id_nullable(postgres_dsn) == "NO"
    fk = _fk(postgres_dsn)
    assert fk is not None and fk[1] == "c"
    with psycopg.connect(postgres_dsn) as conn:
        assert conn.execute("SELECT job_id, batch_id FROM llm_batches").fetchall() == [
            (second, "msgbatch_1")
        ]
        conn.execute("DELETE FROM jobs WHERE id = %s", (second,))
        conn.commit()
        assert conn.execute("SELECT count(*) FROM llm_batches").fetchone() == (0,)

    command.upgrade(config, "head")

    fk = _fk(postgres_dsn)
    assert fk is not None and fk[1] == "n"
    assert _job_id_nullable(postgres_dsn) == "YES"
