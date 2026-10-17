"""Migration ``0016_search_analysis_text`` (issue #169)."""

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


def _columns(conn: psycopg.Connection[tuple[object, ...]]) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'search_docs'"
    ).fetchall()
    return {str(r[0]) for r in rows}


def test_0016_is_chained_to_0015() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))

    revision = ScriptDirectory.from_config(config).get_revision("0016")

    assert revision is not None
    assert revision.down_revision == "0015"


@integration
def test_upgrade_backfills_the_analysis_text_and_downgrade_drops_it(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0015")
    with psycopg.connect(postgres_dsn) as conn:
        conn.execute(
            "INSERT INTO videos (video_id, title, description)"
            " VALUES ('mig16video1', 'Plain', 'about okapis')"
        )
        conn.commit()
        assert "analysis_text" not in _columns(conn)

    command.upgrade(config, "0016")

    with psycopg.connect(postgres_dsn) as conn:
        row = conn.execute(
            "SELECT analysis_text FROM search_docs WHERE video_id = 'mig16video1'"
        ).fetchone()
        assert row == ("about okapis",)

    command.downgrade(config, "0015")

    with psycopg.connect(postgres_dsn) as conn:
        assert "analysis_text" not in _columns(conn)
        conn.execute("UPDATE videos SET description = 'still okapis' WHERE video_id = 'mig16video1'")
        conn.commit()
        found = conn.execute(
            "SELECT 1 FROM search_docs WHERE fts @@ plainto_tsquery('english', 'okapis')"
        ).fetchone()
        assert found is not None

    command.upgrade(config, "0016")
    with psycopg.connect(postgres_dsn) as conn:
        row = conn.execute(
            "SELECT analysis_text FROM search_docs WHERE video_id = 'mig16video1'"
        ).fetchone()
        assert row == ("still okapis",)
