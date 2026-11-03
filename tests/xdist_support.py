"""Helpers for the xdist partition and isolation tests (#296).

``DryRun`` is a plugin (``-p tests.xdist_support``) that runs no test body: it
fires only ``logstart``/``logfinish``, so a real parallel run over the *real*
suite exercises collection, the deselect hooks, the shuffle handoff, the xdist
scheduler and the run ledger without Docker.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ("-p", "tests.shuffle_plugin", "-p", "tests.run_ledger_plugin")


def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> bool:
    """Only with ``-p tests.xdist_support``: log the item, run nothing."""
    item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    report = pytest.TestReport(
        nodeid=item.nodeid,
        location=item.location,
        keywords={},
        outcome="passed",
        longrepr=None,
        when="call",
    )
    item.ihook.pytest_runtest_logreport(report=report)
    item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return True


def nested_env(env_extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a pytest run started by a test."""
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_XDIST_")}  # noqa: TID251
    return {**inherited, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1", **(env_extra or {})}


def run_pytest(
    cwd: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    timeout: float = 300,
) -> subprocess.CompletedProcess[str]:
    """``python -m pytest`` in a fresh interpreter, with the repo importable."""
    env = nested_env(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def collected_ids(output: str) -> list[str]:
    return [
        line.strip()
        for line in output.splitlines()
        if "::" in line and not line[0].isspace() and not line.startswith(("#", "="))
    ]


def read_ledger_lines(directory: Path) -> dict[str, list[tuple[str, str]]]:
    """worker file stem -> [(nodeid, worker_id)] in run order; ``#`` lines skipped."""
    out: dict[str, list[tuple[str, str]]] = {}
    for ledger in sorted(directory.glob("*.tsv")):
        rows: list[tuple[str, str]] = []
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                nodeid, worker, _outcome = line.rsplit("\t", 2)
                rows.append((nodeid, worker))
        out[ledger.stem] = rows
    return out


def ledger_outcomes(directory: Path) -> dict[str, str]:
    """node id -> outcome column, over every ledger file."""
    out: dict[str, str] = {}
    for ledger in sorted(directory.glob("*.tsv")):
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                nodeid, _worker, outcome = line.rsplit("\t", 2)
                out[nodeid] = outcome
    return out


def ledger_headers(directory: Path) -> dict[str, str]:
    """worker file stem -> its first (``# seed=... worker=...``) line."""
    return {
        p.stem: p.read_text(encoding="utf-8").splitlines()[0] for p in sorted(directory.glob("*.tsv"))
    }


def rendezvous(
    directory: Path,
    mine: str,
    other: str,
    *,
    deadline_sec: float = 30.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Announce ``mine`` and wait until ``other`` exists, so both sides are alive at once.

    Fails at the deadline naming the missing marker (for example when both probes
    landed on one worker).
    """
    (directory / mine).write_text("here", encoding="utf-8")
    deadline = monotonic() + deadline_sec
    while not (directory / other).exists():
        if monotonic() > deadline:
            raise AssertionError(
                f"rendezvous: {other!r} did not appear within {deadline_sec} s; "
                "the two probes were not on two live workers at once"
            )
        sleep(0.02)
