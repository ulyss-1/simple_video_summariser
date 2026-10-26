"""The shared ``head_dsn`` fixture (tests/conftest.py, #290)."""

from __future__ import annotations

import os

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from testcontainers.community.postgres import PostgresContainer

from common.config import get_settings
from tests.conftest import _REPO_ROOT, _with_dbname

pytestmark = pytest.mark.integration


def _single_head() -> str:
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    (head,) = ScriptDirectory.from_config(config).get_heads()
    return head


def test_head_dsn_is_at_head_and_wires_env_and_settings(head_dsn: str) -> None:
    assert os.environ["DATABASE_URL"] == head_dsn  # noqa: TID251 - the env var is the contract
    # The settings cache was cleared, so it now reads the fixture's DSN.
    assert get_settings().DATABASE_URL.get_secret_value() == head_dsn
    with psycopg.connect(head_dsn) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row == (_single_head(),)


def test_head_dsn_is_a_clone_not_the_template(head_dsn: str, migrated_template: str) -> None:
    with psycopg.connect(head_dsn) as conn:
        row = conn.execute("SELECT current_database()").fetchone()
    assert row is not None
    assert row[0] != migrated_template
    assert row[0].startswith("test_")


def test_template_accepts_no_connections(
    head_dsn: str, migrated_template: str, postgres_container: PostgresContainer
) -> None:
    admin_dsn = postgres_container.get_connection_url()
    with pytest.raises(psycopg.OperationalError, match="not currently accepting connections"):
        psycopg.connect(_with_dbname(admin_dsn, migrated_template))
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT datallowconn FROM pg_database WHERE datname = %s", (migrated_template,)
        ).fetchone()
    assert row == (False,)
