"""Migration ``0025_failed_attempt_usage`` (issue #252).

The first test reads the revision chain only. The integration tests need
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
USAGE_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "calls",
    "cost_usd",
)
integration = pytest.mark.integration


def _config_for(dsn: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


def _columns(dsn: str, table: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
        ).fetchall()
    return {str(r[0]) for r in rows}


def _seed(dsn: str) -> None:
    """A video with a job, a checkpoint row and an analysis row, as of revision 0024."""
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO videos (video_id) VALUES ('aaaaaaaaaaa')")
        conn.execute(
            "INSERT INTO jobs (id, kind, video_id, dedupe_key) VALUES (7, 'analyze', 'aaaaaaaaaaa', 'k')"
        )
        conn.execute(
            "INSERT INTO llm_batches (job_id, step_key, batch_id, request_sha256) "
            "VALUES (7, 'roster', 'msgbatch_x', 'sha')"
        )
        conn.commit()


def test_0025_is_chained_to_0024() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    revision = ScriptDirectory.from_config(config).get_revision("0025")

    assert revision is not None
    assert revision.down_revision == "0024"


@integration
def test_upgrade_downgrade_upgrade_keeps_existing_rows(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0024")
    _seed(postgres_dsn)

    command.upgrade(config, "0025")
    assert set(USAGE_COLUMNS) <= _columns(postgres_dsn, "llm_batches")
    assert _columns(postgres_dsn, "analyze_failed_usage")
    command.downgrade(config, "-1")
    assert not set(USAGE_COLUMNS) & _columns(postgres_dsn, "llm_batches")
    assert not _columns(postgres_dsn, "analyze_failed_usage")
    command.upgrade(config, "0025")

    with psycopg.connect(postgres_dsn) as conn:
        row = conn.execute(
            "SELECT job_id, step_key, batch_id, request_sha256, calls, cost_usd FROM llm_batches"
        ).fetchone()
    assert row == (7, "roster", "msgbatch_x", "sha", None, None)  # NULL: results not fetched yet


@integration
def test_deleting_the_video_removes_its_failed_attempts_and_deleting_the_job_nulls_job_id(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0025")
    _seed(postgres_dsn)
    with psycopg.connect(postgres_dsn) as conn:
        for outcome in ("failed", "cancelled"):
            conn.execute(
                "INSERT INTO analyze_failed_usage (video_id, job_id, model, outcome, calls) "
                "VALUES ('aaaaaaaaaaa', 7, 'm', %s, 1)",
                (outcome,),
            )
        conn.commit()

        conn.execute("DELETE FROM jobs WHERE id = 7")
        conn.commit()
        rows = conn.execute("SELECT job_id FROM analyze_failed_usage").fetchall()
        assert rows == [(None,), (None,)]

        conn.execute("DELETE FROM videos WHERE video_id = 'aaaaaaaaaaa'")
        conn.commit()
        assert conn.execute("SELECT count(*) FROM analyze_failed_usage").fetchone() == (0,)


@integration
def test_an_unknown_outcome_is_refused(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0025")
    _seed(postgres_dsn)
    with psycopg.connect(postgres_dsn) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO analyze_failed_usage (video_id, model, outcome) "
            "VALUES ('aaaaaaaaaaa', 'm', 'weird')"
        )
