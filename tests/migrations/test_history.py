"""The migration history has exactly one head (issue #7).

No database is needed — `ScriptDirectory` only reads the revision files on
disk — so this runs under `pytest -m "not integration"`. It exists to catch
#8, #9 and #10 branching the revision history in parallel, before merge
rather than as a runtime surprise from `alembic upgrade head`.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]


def _script_directory() -> ScriptDirectory:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def test_migration_history_has_exactly_one_head() -> None:
    heads = _script_directory().get_heads()
    assert len(heads) == 1


def test_revision_0001_baseline_is_a_real_revision() -> None:
    revisions = [rev.revision for rev in _script_directory().walk_revisions()]
    assert revisions == ["0001"]
