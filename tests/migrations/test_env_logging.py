"""``migrations/env.py`` no longer disables existing loggers (issue #71).

The old ``logging.config.fileConfig(alembic.ini)`` call disabled every
logger already instantiated in the process that ``alembic.ini`` didn't name
(``common.errors``, ``common.queue``, ...), and it did so on every
in-process ``alembic upgrade`` — order-dependent breakage for any test suite
that imports those modules before running a migration. ``env.py`` now routes
through ``configure_logging(get_settings())`` (#6) instead.

Most of these tests run in offline mode (``sql=True``): ``env.py`` still
calls ``get_settings().DATABASE_URL`` even offline, so ``_config_for`` always
sets a syntactically valid (but never-connected-to) DSN. Only the first test
needs a real database, so it alone carries ``@pytest.mark.integration`` —
the rest run under ``pytest -m "not integration"`` (this is the "offline
mode" test the issue asks for, plus siblings that don't need Postgres
either). A module-level ``pytestmark`` would force every test here to need
Postgres, which the issue's own acceptance criteria rule out.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from common.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]

#: A syntactically valid DSN that is never actually connected to in offline
#: mode (``sql=True``); ``env.py`` still reads it via ``get_settings()``.
_FAKE_DSN = "postgresql://u:p@h/db"


def _config_for(dsn: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    # Same pattern as tests/migrations/test_upgrade_downgrade.py (#7): env.py
    # reads DATABASE_URL via common.config.get_settings(), which is
    # lru_cache'd per process, so the cache has to be cleared after pointing
    # it at the test (or fake) database.
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    # Same shape as tests/common/test_logging.py's fixture of the same name:
    # each test here reconfigures process-wide logging, so it must not leak
    # into whatever test order pytest picks next.
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
    root.setLevel(level)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    get_settings.cache_clear()


@pytest.mark.integration
def test_online_upgrade_does_not_disable_a_pre_existing_logger(
    postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    errors_logger = logging.getLogger("common.errors")
    pre_existing = logging.getLogger("tests.pre_existing")
    config = _config_for(postgres_dsn, monkeypatch)

    command.upgrade(config, "head")

    assert errors_logger.disabled is False
    assert pre_existing.disabled is False
    pre_existing.error("marker-71-online")
    assert "marker-71-online" in capsys.readouterr().out


def test_offline_upgrade_does_not_disable_a_pre_existing_logger(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    errors_logger = logging.getLogger("common.errors")
    pre_existing = logging.getLogger("tests.pre_existing_offline")
    config = _config_for(_FAKE_DSN, monkeypatch)

    command.upgrade(config, "head", sql=True)

    assert errors_logger.disabled is False
    assert pre_existing.disabled is False
    pre_existing.error("marker-71-offline")
    # Offline mode keeps stdout clean for the emitted SQL; logs go to stderr.
    assert "marker-71-offline" in capsys.readouterr().err


def test_configure_logger_false_leaves_root_logging_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config_for(_FAKE_DSN, monkeypatch)
    config.attributes["configure_logger"] = False
    root = logging.getLogger()
    handlers_before = root.handlers[:]
    level_before = root.level

    command.upgrade(config, "head", sql=True)

    assert root.handlers == handlers_before
    assert root.level == level_before


def test_two_inprocess_upgrades_do_not_duplicate_log_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config_for(_FAKE_DSN, monkeypatch)
    n_revisions = len(
        list(ScriptDirectory.from_config(config).walk_revisions("base", "heads"))
    )

    command.upgrade(config, "head", sql=True)
    capsys.readouterr()  # discard the first run's output
    command.upgrade(config, "head", sql=True)

    err = capsys.readouterr().err
    # Count matching *lines*, not substring occurrences: each JSON record
    # legitimately contains "Running upgrade ..." twice (the "event" and
    # "message" keys both carry the original text), so counting the
    # substring directly would overcount even a single, non-duplicated run.
    matching_lines = [line for line in err.splitlines() if "Running upgrade" in line]
    assert len(matching_lines) == n_revisions
