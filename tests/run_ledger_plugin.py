"""A ledger of the tests that really ran, for the xdist partition check (#296).

Inert without ``--run-ledger=<dir>``. With it, every process that *runs* tests
appends to ``<dir>/<worker_id>.tsv``:

* a first line ``# seed=<N|off> worker=<id>`` (the shuffle seed that process used),
* then one line ``<nodeid>TAB<worker_id>`` per item, written from
  ``pytest_runtest_logstart`` so skipped and xfailed tests are recorded too.

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


class _Ledger:
    """Registered per process only when it runs tests, so the hooks stay inert elsewhere."""

    def __init__(self, path: Path, worker_id: str, seed: int | None) -> None:
        self._worker_id = worker_id
        self._handle = path.open("a", encoding="utf-8", buffering=1)
        self._handle.write(f"# seed={'off' if seed is None else seed} worker={worker_id}\n")

    def pytest_runtest_logstart(self, nodeid: str, location: object) -> None:
        self._handle.write(f"{nodeid}\t{self._worker_id}\n")

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
