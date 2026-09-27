"""The migration history has exactly one head (issue #7).

No database is needed — `ScriptDirectory` only reads the revision files on
disk — so this runs under `pytest -m "not integration"`. It exists to catch
#8, #9 and #10 branching the revision history in parallel, before merge
rather than as a runtime surprise from `alembic upgrade head`.

Assertions here are deliberately structural (single head, an unbroken chain
down to the "0001" baseline, every id reachable in a file on disk) rather
than a hardcoded list of revision ids — each new migration (#9, #10, ...)
extends the chain without anyone having to bump a literal here.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]


def _script_directory() -> ScriptDirectory:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def test_migration_history_has_exactly_one_head() -> None:
    # Kept strict: more than one head means two migrations branched off the
    # same parent in parallel, which is exactly what this test exists to
    # catch before merge.
    heads = _script_directory().get_heads()
    assert len(heads) == 1


def test_revision_chain_is_unbroken_down_to_the_0001_baseline() -> None:
    # Walked from head to base. Grows by one entry each time a later
    # migration (#9, #10, ...) extends the chain, without needing a bump
    # here — the assertion is against the script directory's own head and
    # down_revision links, not a hardcoded id list.
    script_directory = _script_directory()
    (head,) = script_directory.get_heads()
    revisions = [rev.revision for rev in script_directory.walk_revisions()]

    assert revisions[0] == head
    assert revisions[-1] == "0001"
    # Each step in the walk is the previous revision's declared parent.
    for rev_id, parent_id in pairwise(revisions):
        assert script_directory.get_revision(rev_id).down_revision == parent_id
