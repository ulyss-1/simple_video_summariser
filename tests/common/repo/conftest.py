"""Shared fixtures for ``common/repo/`` tests (issue #14).

Every test in this package needs a live Postgres migrated to head (#8, #9,
#10), so the whole package is marked ``integration`` here rather than in
each module.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from common.config import get_settings

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]


def _config_for(dsn: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    # Same pattern as tests/migrations/test_upgrade_downgrade.py (#7).
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


@pytest.fixture
def head_dsn(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """A DSN for a database migrated all the way to head."""
    command.upgrade(_config_for(postgres_dsn, monkeypatch), "head")
    return postgres_dsn


@pytest.fixture
def conn(head_dsn: str) -> Iterator[psycopg.Connection]:
    """A connection to a database at head, closed after the test.

    No commit or rollback happens here: repo functions never commit
    (module docstrings), so a test that only reads back through the same
    connection sees its own writes regardless.
    """
    with psycopg.connect(head_dsn) as connection:
        yield connection


@pytest.fixture
def video_id(conn: psycopg.Connection) -> str:
    """A bare ``videos`` row with no metadata, for FK-satisfying tests."""
    vid = "v" + uuid.uuid4().hex[:10]
    conn.execute("INSERT INTO videos (video_id) VALUES (%s)", (vid,))
    return vid
