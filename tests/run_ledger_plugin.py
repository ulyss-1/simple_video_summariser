"""A ledger of the tests that really ran, for the xdist partition check (#296).

Inert without ``--run-ledger=<dir>``. With it, every process that *runs* tests
appends to ``<dir>/<worker_id>.tsv``:

* a first line ``# seed=<N|off> worker=<id>`` (the shuffle seed that process used),
* then one line ``<nodeid>TAB<worker_id>TAB<outcome>`` per item, written from
  ``pytest_runtest_logreport`` when the test's fate is known, so a test that was
  only *started* is not recorded. The outcome is ``passed`` / ``failed`` (the
  ``call`` phase report), ``skipped`` / ``xfailed`` (a legitimate skip or xfail, in
  either phase) or ``error`` (the setup phase failed, so the test never ran).
  A teardown error adds no line; pytest fails the run for it itself.

Under xdist the controller replays every worker report through the same hook;
it records nothing, so each test is written exactly once, by the worker that ran
it. A serial run records as worker ``main``.
``scripts/check_run_partition.py`` compares the ledger with the serial
collection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.shuffle_plugin import _SEED_KEY


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("shuffle", "test order shuffling").addoption(
        "--run-ledger",
        action="store",
        default=None,
        metavar="DIR",
        help="Record every test this process runs under DIR (xdist partition check).",
    )


def _outcome(report: pytest.TestReport) -> str | None:
    """The ledger outcome this report decides, or None when a later phase decides it."""
    if report.when == "teardown":
        return None
    if report.when == "setup":
        if report.passed:
            return None  # the call phase decides
        if report.failed:
            return "error"
        return "xfailed" if hasattr(report, "wasxfail") else "skipped"
    if report.skipped:
        return "xfailed" if hasattr(report, "wasxfail") else "skipped"
    if hasattr(report, "wasxfail"):
        return "xfailed"  # an xfail that failed as expected, or a non-strict xpass
    return "passed" if report.passed else "failed"


class _Ledger:
    """Registered per process only when it runs tests, so the hooks stay inert elsewhere."""

    def __init__(self, path: Path, worker_id: str, seed: int | None) -> None:
        self._worker_id = worker_id
        self._handle = path.open("a", encoding="utf-8", buffering=1)
        self._handle.write(f"# seed={'off' if seed is None else seed} worker={worker_id}\n")

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        outcome = _outcome(report)
        if outcome is not None:
            self._handle.write(f"{report.nodeid}\t{self._worker_id}\t{outcome}\n")

    def pytest_unconfigure(self) -> None:
        self._handle.close()


def pytest_configure(config: pytest.Config) -> None:
    directory = config.getoption("run_ledger")
    workerinput = getattr(config, "workerinput", None)
    if directory is None or (workerinput is None and getattr(config.option, "numprocesses", None)):
        return  # off, or the xdist controller (it only replays worker reports)
    worker_id = "main" if workerinput is None else str(workerinput["workerid"])
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    seed = config.stash.get(_SEED_KEY, None)
    config.pluginmanager.register(_Ledger(root / f"{worker_id}.tsv", worker_id, seed))
