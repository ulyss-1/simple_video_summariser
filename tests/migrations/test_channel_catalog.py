from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from common.config import get_settings

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _config_for(dsn: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


def _table_exists(dsn: str) -> bool:
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT to_regclass('channel_catalog_entries')").fetchone()
    assert row is not None
    return bool(row[0] == "channel_catalog_entries")


def test_0017_is_the_only_head_and_is_chained_to_0016() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)

    revision = script.get_revision("0017")

    assert revision is not None
    assert revision.down_revision == "0016"
    assert script.get_heads() == ["0017"]


def test_0017_catalog_table_enforces_state_and_identity(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    command.upgrade(_config_for(postgres_dsn, monkeypatch), "head")
    with psycopg.connect(postgres_dsn) as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'channel_catalog_entries'"
            )
        }
        assert {
            "channel_id",
            "video_id",
            "position",
            "snapshot_limit",
            "total_count",
            "title",
            "description",
            "published_at",
            "enrichment_state",
            "enrichment_error",
            "attempts",
            "discovered_at",
            "updated_at",
        } <= columns
        conn.execute("INSERT INTO channels (channel_id) VALUES ('UCx')")
        invalid_rows: tuple[tuple[Any, ...], ...] = (
            ("aaaaaaaaaaa", -1, 50, "pending", None, 0),
            ("bbbbbbbbbbb", 0, 51, "pending", None, 0),
            ("ccccccccccc", 0, 50, "pending", None, -1),
            ("ddddddddddd", 0, 50, "unknown", None, 0),
            ("eeeeeeeeeee", 0, 50, "failed", None, 0),
            ("fffffffffff", 0, 50, "pending", "ErrorClass", 0),
        )
        for video_id, position, limit, state, error, attempts in invalid_rows:
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                conn.execute(
                    "INSERT INTO channel_catalog_entries "
                    "(channel_id, video_id, position, snapshot_limit, enrichment_state, "
                    "enrichment_error, attempts) VALUES ('UCx', %s, %s, %s, %s, %s, %s)",
                    (video_id, position, limit, state, error, attempts),
                )
        conn.execute(
            "INSERT INTO channel_catalog_entries "
            "(channel_id, video_id, position, snapshot_limit, enrichment_state, enrichment_error) "
            "VALUES ('UCx', 'ggggggggggg', 0, 50, 'failed', 'ToolFailureError')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute(
                "INSERT INTO channel_catalog_entries "
                "(channel_id, video_id, position, snapshot_limit) "
                "VALUES ('UCx', 'ggggggggggg', 1, 50)"
            )


def test_0017_upgrade_downgrade_upgrade_round_trip_preserves_0016(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0016")
    with psycopg.connect(postgres_dsn) as conn:
        conn.execute("INSERT INTO deleted_videos (video_id) VALUES ('dQw4w9WgXcQ')")
        conn.commit()

    command.upgrade(config, "head")
    assert _table_exists(postgres_dsn)
    command.downgrade(config, "0016")
    assert not _table_exists(postgres_dsn)
    with psycopg.connect(postgres_dsn) as conn:
        assert conn.execute(
            "SELECT video_id FROM deleted_videos WHERE video_id = 'dQw4w9WgXcQ'"
        ).fetchone() == ("dQw4w9WgXcQ",)

    command.upgrade(config, "head")
    assert _table_exists(postgres_dsn)
