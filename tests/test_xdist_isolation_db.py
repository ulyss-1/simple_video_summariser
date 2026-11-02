"""Workers never share or clobber database state under xdist (#296, group C).

Needs Docker. One real ``-n 2 --dist loadfile`` run of the two probe modules
``tests/xdist_probe_a.py`` and ``tests/xdist_probe_b.py`` (one per worker), which use
the repo's real ``postgres_container`` / ``migrated_template`` / ``head_dsn`` fixtures.
The probes prove overlap with marker files and a bounded deadline, not by hoping for
a race, and assert their own invariants inside the worker; this module checks what
both sides reported.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.xdist_support import REPO_ROOT, run_pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def probes(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    markers = tmp_path_factory.mktemp("xdist_probe")
    run = run_pytest(
        REPO_ROOT,
        "tests/xdist_probe_a.py", "tests/xdist_probe_b.py",
        "-n", "2", "--dist", "loadfile", "--no-shuffle", "-p", "tests.shuffle_plugin",
        "-rA", "-q", "--rootdir", str(REPO_ROOT),
        env_extra={"XDIST_PROBE_DIR": str(markers)},
        timeout=600,
    )
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-2000:]
    assert "12 passed" in run.stdout, run.stdout[-2000:]
    yield markers


def _both(markers: Path, name: str) -> tuple[dict[str, object], dict[str, object]]:
    a = json.loads((markers / f"{name}_a.json").read_text())
    b = json.loads((markers / f"{name}_b.json").read_text())
    assert isinstance(a, dict) and isinstance(b, dict)
    return a, b


def test_each_worker_has_its_own_container_and_template(probes: Path) -> None:
    first_a, first_b = _both(probes, "identity")
    again_a, again_b = _both(probes, "identity_again")
    assert first_a["worker"] != first_b["worker"]
    assert first_a["container"] != first_b["container"]
    assert first_a["template"] != first_b["template"]
    # Created once per worker: a second test on the same worker sees the same ones.
    for first, again in ((first_a, again_a), (first_b, again_b)):
        assert (first["worker"], first["container"], first["template"]) == (
            again["worker"], again["container"], again["template"],
        )


def test_concurrent_tests_never_share_a_database(probes: Path) -> None:
    a, b = _both(probes, "concurrent")
    assert a["worker"] != b["worker"]
    assert a["db"] != b["db"]
    assert a["rows"] == ["UCa"] and b["rows"] == ["UCb"]


def test_dropping_one_workers_database_does_not_touch_the_other(probes: Path) -> None:
    assert (probes / "dropped_b").read_text() == "dropped"  # b's database really was dropped
    assert json.loads((probes / "drop_a.json").read_text())["count"] == 2


def test_template_clone_not_blocked_across_workers(probes: Path) -> None:
    a, b = _both(probes, "clone")
    assert a["clone"] != b["clone"]


def test_unfiltered_pg_stat_activity_counts_see_only_own_worker(probes: Path) -> None:
    a, b = _both(probes, "stat")
    # 3 and 7 held connections plus the counting one; a shared container would show 12.
    assert (a["count"], b["count"]) == (4, 8)
