"""Migration ``0018_analyses_usage_columns`` (issue #139).

The first two tests read the revision file only and run without a database. The
integration tests need Postgres (``postgres_dsn``).
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
COLUMNS = ("cache_read_tokens", "cache_write_tokens", "calls")
integration = pytest.mark.integration


def _config_for(dsn: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


def _column(dsn: str, column: str) -> tuple[str, str, str | None] | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            SELECT data_type, is_nullable, column_default FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'analyses' AND column_name = %s
            """,
            (column,),
        ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]), row[2])


def test_0018_is_chained_to_0017_and_the_history_has_one_head() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)

    revision = script.get_revision("0018")

    assert revision is not None
    assert revision.down_revision == "0017"
    assert len(script.get_heads()) == 1  # a later revision may sit on top of 0018


def test_0018_downgrade_drops_exactly_what_upgrade_adds() -> None:
    source = (REPO_ROOT / "migrations" / "versions" / "0018_analyses_usage_columns.py").read_text()

    for column in COLUMNS:
        assert f"ADD COLUMN {column} INTEGER NULL" in source
        assert f"DROP COLUMN {column}" in source


@integration
def test_upgrade_adds_three_nullable_columns_without_default_and_downgrade_drops_them(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0017")
    assert all(_column(postgres_dsn, c) is None for c in COLUMNS)

    command.upgrade(config, "0018")

    for column in COLUMNS:
        assert _column(postgres_dsn, column) == ("integer", "YES", None)

    command.downgrade(config, "0017")

    assert all(_column(postgres_dsn, c) is None for c in COLUMNS)

    command.upgrade(config, "head")
    assert all(_column(postgres_dsn, c) is not None for c in COLUMNS)


@integration
def test_a_row_from_before_the_upgrade_reads_null_not_zero(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0017")
    with psycopg.connect(postgres_dsn) as conn:
        conn.execute("INSERT INTO videos (video_id) VALUES ('v1')")
        conn.execute(
            "INSERT INTO transcripts (video_id, source, segments, full_text)"
            " VALUES ('v1', 'whisper', '[]', 'text')"
        )
        conn.execute(
            "INSERT INTO analyses (video_id, transcript_id, chunk_strategy, model,"
            " prompt_version, tldr) SELECT 'v1', id, 's', 'm', 'p', 't' FROM transcripts"
        )
        conn.commit()

    command.upgrade(config, "0018")

    with psycopg.connect(postgres_dsn) as conn:
        row = conn.execute(
            "SELECT cache_read_tokens, cache_write_tokens, calls FROM analyses"
        ).fetchone()
    assert row == (None, None, None)
