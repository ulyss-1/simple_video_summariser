"""Migration ``0005_jobs_video_idx`` (issue #42)."""

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
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


def _indexdef(dsn: str) -> str | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'jobs_video_idx'"
        ).fetchone()
    return None if row is None else str(row[0])


def test_upgrade_adds_the_per_video_jobs_index_and_downgrade_drops_it(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0005")

    assert _indexdef(postgres_dsn) == (
        "CREATE INDEX jobs_video_idx ON public.jobs USING btree "
        "(video_id, created_at DESC, id DESC)"
    )

    command.downgrade(config, "0004")
    assert _indexdef(postgres_dsn) is None

    command.upgrade(config, "head")
    assert _indexdef(postgres_dsn) is not None
