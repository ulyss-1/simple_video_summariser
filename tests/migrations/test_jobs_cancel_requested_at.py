"""Migration ``0023_jobs_cancel_requested_at`` (issue #237)."""

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


def _columns(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'jobs'"
            ).fetchall()
        }


def _column_type(dsn: str) -> tuple[str, str] | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name='jobs' AND column_name='cancel_requested_at'"
        ).fetchone()
    return None if row is None else (row[0], row[1])


def test_0023_is_chained_to_0022() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))

    revision = ScriptDirectory.from_config(config).get_revision("0023")

    assert revision is not None
    assert revision.down_revision == "0022"


@integration
def test_upgrade_adds_a_nullable_timestamptz_and_downgrade_removes_only_that_column(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0022")
    before = _columns(postgres_dsn)
    assert _column_type(postgres_dsn) is None

    command.upgrade(config, "0023")

    assert _column_type(postgres_dsn) == ("timestamp with time zone", "YES")
    assert _columns(postgres_dsn) == before | {"cancel_requested_at"}

    command.downgrade(config, "-1")

    assert _columns(postgres_dsn) == before

    command.upgrade(config, "head")
    assert _column_type(postgres_dsn) == ("timestamp with time zone", "YES")


@integration
def test_existing_rows_get_a_null_request(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0022")
    with psycopg.connect(postgres_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO jobs (video_id, kind) VALUES ('dQw4w9WgXcQ', 'ingest')"
        )

    command.upgrade(config, "0023")

    with psycopg.connect(postgres_dsn) as conn:
        assert conn.execute("SELECT cancel_requested_at FROM jobs").fetchall() == [
            (None,)
        ]
