"""alembic upgrade/downgrade round-trip against a real Postgres (issue #7).

Needs a live database, so every test here is `@pytest.mark.integration`
(module-level `pytestmark`) and uses the `postgres_dsn` fixture from
`tests/conftest.py` — a fresh database per test on one session-scoped
container.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from common.config import get_settings

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _config_for(dsn: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    # migrations/env.py reads DATABASE_URL via common.config.get_settings(),
    # never from alembic.ini, so pointing alembic at the test database means
    # setting the env var and clearing the settings cache (architecture.md
    # 10; common/config.py is @lru_cache'd per process).
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


def _current_head(config: Config) -> str:
    (head,) = ScriptDirectory.from_config(config).get_heads()
    return head


def _version_rows(dsn: str) -> list[tuple[str, ...]]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        return cur.fetchall()


def test_upgrade_head_creates_alembic_version_at_head(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Name kept generic from #7; compares against the script directory's
    # own head rather than a hardcoded id, so it keeps working as later
    # migrations (#9, #10, ...) move head forward.
    config = _config_for(postgres_dsn, monkeypatch)

    command.upgrade(config, "head")

    assert _version_rows(postgres_dsn) == [(_current_head(config),)]


def test_downgrade_base_empties_alembic_version(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "head")

    command.downgrade(config, "base")

    assert _version_rows(postgres_dsn) == []


def test_upgrade_downgrade_upgrade_round_trip_succeeds(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)

    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    assert _version_rows(postgres_dsn) == [(_current_head(config),)]


@pytest.mark.parametrize("scheme", ["postgresql://", "postgresql+psycopg://"])
def test_env_accepts_both_the_plain_and_psycopg_url_schemes(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    # compose.yml's DATABASE_URL uses postgresql://, which SQLAlchemy would
    # otherwise map to the uninstalled psycopg2 driver.
    dsn = scheme + postgres_dsn.split("://", 1)[1]
    config = _config_for(dsn, monkeypatch)

    command.upgrade(config, "head")

    assert _version_rows(postgres_dsn) == [(_current_head(config),)]
