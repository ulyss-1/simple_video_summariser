"""Migration ``0021_search_description`` (issue #168)."""

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


def _matches(conn: psycopg.Connection[tuple[object, ...]], word: str) -> list[str]:
    rows = conn.execute(
        "SELECT video_id FROM search_docs WHERE fts @@ plainto_tsquery('english', %s)"
        " ORDER BY video_id",
        (word,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _trigger_def(conn: psycopg.Connection[tuple[object, ...]]) -> str:
    row = conn.execute(
        "SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgname = 'videos_search_docs_trg'"
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_0021_is_chained_to_0020() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))

    revision = ScriptDirectory.from_config(config).get_revision("0021")

    assert revision is not None
    assert revision.down_revision == "0020"


@integration
def test_upgrade_backfills_descriptions_and_downgrade_removes_them(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0020")
    with psycopg.connect(postgres_dsn) as conn:
        conn.execute(
            "INSERT INTO videos (video_id, title, description)"
            " VALUES ('mig15video1', 'Plain', 'about okapis'), ('mig15video2', 'Plain', 'x')"
        )
        conn.commit()
        assert _matches(conn, "okapis") == []

    command.upgrade(config, "0021")

    with psycopg.connect(postgres_dsn) as conn:
        assert _matches(conn, "okapis") == ["mig15video1"]
        doc = conn.execute(
            "SELECT fts::text FROM search_docs WHERE video_id = 'mig15video1'"
        ).fetchone()
        assert doc is not None and "'okapi':3C" in doc[0]
        assert "UPDATE OF title, description" in _trigger_def(conn)

    command.downgrade(config, "0020")

    with psycopg.connect(postgres_dsn) as conn:
        assert _matches(conn, "okapis") == []
        assert _matches(conn, "plain") == ["mig15video1", "mig15video2"]
        trigger = _trigger_def(conn)
        assert "UPDATE OF title ON" in trigger and "description" not in trigger

    command.upgrade(config, "0021")
    with psycopg.connect(postgres_dsn) as conn:
        assert _matches(conn, "okapis") == ["mig15video1"]


@integration
def test_changing_only_the_description_refreshes_the_search_doc(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    command.upgrade(_config_for(postgres_dsn, monkeypatch), "0021")
    with psycopg.connect(postgres_dsn) as conn:
        conn.execute("INSERT INTO videos (video_id, title) VALUES ('mig15trig01', 'Plain')")
        assert _matches(conn, "okapis") == []

        conn.execute("UPDATE videos SET description = 'about okapis' WHERE video_id = 'mig15trig01'")

        assert _matches(conn, "okapis") == ["mig15trig01"]
