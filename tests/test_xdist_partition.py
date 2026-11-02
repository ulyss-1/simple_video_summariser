"""Every test runs exactly once under xdist, and the set equals the serial set (#296).

Groups A of the issue's test plan. No Docker. Synthetic suites run in a fresh
interpreter (``tests.xdist_support.run_pytest``) because xdist workers are
subprocesses; the real-suite test uses ``DryRun`` so no test body executes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.xdist_support import (
    REPO_ROOT,
    collected_ids,
    ledger_headers,
    ledger_outcomes,
    read_ledger_lines,
    run_pytest,
)

_CHECKER = REPO_ROOT / "scripts" / "check_run_partition.py"

_MODULE = '''
import pytest
from pathlib import Path

@pytest.fixture(scope="module")
def mod_fixture(request):
    with (Path(__file__).parent / "setups.log").open("a") as log:
        log.write(Path(request.fspath).name + "\\n")
    return 1

def test_plain(mod_fixture): pass
def test_other(mod_fixture): pass

@pytest.mark.parametrize("v", ["a/b-1", "ünï-✓", "x y", "[br]", "a::b"])
def test_awkward(v, mod_fixture): pass

@pytest.mark.parametrize("n", range(3))
def test_numbered(n, mod_fixture): pass

class TestGroup:
    def test_a(self, mod_fixture): pass
    def test_b(self, mod_fixture): pass

@pytest.mark.skip(reason="skipped on purpose")
def test_skipped(): pass

@pytest.mark.xfail(reason="xfail on purpose", strict=True)
def test_xfailed(): assert False
'''
_MODULES = [f"test_m{i}" for i in range(1, 7)]
_PER_MODULE = 2 + 5 + 3 + 2 + 1 + 1


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_run_partition", _CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def suite(tmp_path: Path) -> Path:
    for name in _MODULES:
        (tmp_path / f"{name}.py").write_text(_MODULE, encoding="utf-8")
    return tmp_path


@pytest.fixture
def parallel(suite: Path) -> tuple[Path, list[str]]:
    """(ledger dir, serial expected ids) after one ``-n 3`` run of the synthetic suite."""
    serial = run_pytest(suite, "--collect-only", "-q", "--no-shuffle", "-p", "tests.shuffle_plugin")
    assert serial.returncode == 0, serial.stdout + serial.stderr
    ledger = suite / "ledger"
    run = run_pytest(
        suite, "-n", "3", "--dist", "loadfile", f"--run-ledger={ledger}", "-q",
        "-p", "tests.shuffle_plugin", "-p", "tests.run_ledger_plugin",
    )
    assert run.returncode == 0, run.stdout + run.stderr
    return ledger, collected_ids(serial.stdout)


def _all_rows(ledger: Path) -> list[tuple[str, str]]:
    return [row for rows in read_ledger_lines(ledger).values() for row in rows]


def test_parallel_run_executes_exact_serial_set(parallel: tuple[Path, list[str]]) -> None:
    ledger, expected = parallel
    ids = [nodeid for nodeid, _ in _all_rows(ledger)]
    assert len(expected) == len(_MODULES) * _PER_MODULE  # skipped and xfail included
    assert set(ids) == set(expected)
    assert len(ids) == len(set(ids))


def test_each_test_recorded_exactly_once(parallel: tuple[Path, list[str]]) -> None:
    ledger, expected = parallel
    workers_by_id: dict[str, list[str]] = {}
    for nodeid, worker in _all_rows(ledger):
        workers_by_id.setdefault(nodeid, []).append(worker)
    assert sorted(workers_by_id) == sorted(expected)
    assert all(len(workers) == 1 for workers in workers_by_id.values())
    # The id column agrees with the file it was written in, and the controller wrote none.
    for stem, rows in read_ledger_lines(ledger).items():
        assert {worker for _, worker in rows} <= {stem}
    assert "main" not in read_ledger_lines(ledger)


def test_loadfile_keeps_a_module_on_one_worker(parallel: tuple[Path, list[str]]) -> None:
    ledger, _ = parallel
    workers_by_module: dict[str, set[str]] = {}
    for nodeid, worker in _all_rows(ledger):
        workers_by_module.setdefault(nodeid.split("::")[0], set()).add(worker)
    assert sorted(workers_by_module) == sorted(f"{m}.py" for m in _MODULES)
    assert all(len(workers) == 1 for workers in workers_by_module.values())
    setups = (ledger.parent / "setups.log").read_text(encoding="utf-8").split()
    assert sorted(setups) == sorted(f"{m}.py" for m in _MODULES)  # one setup per module


def test_ledger_header_names_the_seed_and_worker(parallel: tuple[Path, list[str]]) -> None:
    ledger, _ = parallel
    for stem, header in ledger_headers(ledger).items():
        assert header.startswith("# seed=") and header.endswith(f"worker={stem}")


def test_checker_flags_missing_extra_and_duplicate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    checker = _load_checker()
    expected = tmp_path / "expected.txt"
    expected.write_text(
        "t.py::a\nt.py::b\nt.py::c\nt.py::d\nt.py::e[x y]\n\n5 tests collected in 0.01s\n",
        encoding="utf-8",
    )

    def ledger(name: str, files: dict[str, str]) -> Path:
        directory = tmp_path / name
        directory.mkdir()
        for stem, body in files.items():
            (directory / f"{stem}.tsv").write_text(body, encoding="utf-8")
        return directory

    good = ledger("good", {
        "gw0": "# seed=1 worker=gw0\nt.py::a\tgw0\tpassed\nt.py::b\tgw0\tpassed\n",
        "gw1": "t.py::c\tgw1\tpassed\nt.py::d\tgw1\tpassed\nt.py::e[x y]\tgw1\tpassed\n",
    })
    assert checker.main([str(expected), str(good)]) == 0

    bad = ledger("bad", {
        # t.py::b missing; t.py::zzz extra; t.py::a twice on gw0; t.py::c on two workers
        "gw0": "t.py::a\tgw0\tpassed\nt.py::a\tgw0\tpassed\nt.py::c\tgw0\tpassed\nt.py::zzz\tgw0\tpassed\n",
        "gw1": "t.py::c\tgw1\tpassed\nt.py::d\tgw1\tpassed\nt.py::e[x y]\tgw1\tpassed\n",
    })
    capsys.readouterr()
    assert checker.main([str(expected), str(bad)]) == 1
    out = capsys.readouterr().out
    assert "missing (never ran): t.py::b" in out
    assert "extra (not in the serial set): t.py::zzz" in out
    assert "more than once (gw0, gw0): t.py::a" in out
    assert "by two workers (gw0, gw1): t.py::c" in out
    assert "t.py::d" not in out and "t.py::e" not in out


_OUTCOME_SUITE = '''
import pytest

@pytest.fixture
def broken():
    raise RuntimeError("setup boom")

def test_passes(): pass
def test_setup_errors(broken): pass
def test_hook_skipped(): pass

@pytest.mark.skip(reason="on purpose")
def test_marked_skip(): pass

def test_runtime_skip():
    pytest.skip("on purpose")

@pytest.mark.xfail(reason="on purpose", strict=True)
def test_xfails(): assert False

@pytest.mark.xfail(run=False, reason="on purpose")
def test_xfail_not_run(): pass
'''
_OUTCOME_CONFTEST = '''
import pytest

def pytest_collection_modifyitems(items):
    for item in items:
        if item.name == "test_hook_skipped":
            item.add_marker(pytest.mark.skip(reason="skipped by a hook"))
'''


def _outcome_run(tmp_path: Path, *extra: str) -> tuple[Path, Path]:
    """(expected file, ledger dir) after running the outcome suite."""
    (tmp_path / "test_outcomes.py").write_text(_OUTCOME_SUITE, encoding="utf-8")
    (tmp_path / "conftest.py").write_text(_OUTCOME_CONFTEST, encoding="utf-8")
    plugins = ("-p", "tests.shuffle_plugin", "-p", "tests.run_ledger_plugin")
    serial = run_pytest(tmp_path, "--collect-only", "-q", "--no-shuffle", *plugins)
    assert serial.returncode == 0, serial.stdout + serial.stderr
    expected = tmp_path / "expected.txt"
    expected.write_text(serial.stdout, encoding="utf-8")
    ledger = tmp_path / "ledger"
    run_pytest(tmp_path, "-q", "--no-shuffle", f"--run-ledger={ledger}", *plugins, *extra)
    return expected, ledger


@pytest.mark.parametrize("parallel_args", [(), ("-n", "2", "--dist", "loadfile")])
def test_ledger_records_outcomes_and_a_setup_error_fails_the_checker(
    tmp_path: Path, parallel_args: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    expected, ledger = _outcome_run(tmp_path, *parallel_args)
    outcomes = {k.split("::")[1]: v for k, v in ledger_outcomes(ledger).items()}
    assert outcomes == {
        "test_passes": "passed",
        "test_setup_errors": "error",
        "test_hook_skipped": "skipped",
        "test_marked_skip": "skipped",
        "test_runtime_skip": "skipped",
        "test_xfails": "xfailed",
        "test_xfail_not_run": "xfailed",
    }
    checker = _load_checker()
    capsys.readouterr()
    assert checker.main([str(expected), str(ledger)]) == 1
    out = capsys.readouterr().out
    assert "errored in setup (never ran): test_outcomes.py::test_setup_errors" in out
    assert "test_hook_skipped" not in out  # a hook skip is a legitimate skip
    assert "test_passes" not in out and "test_xfails" not in out


def test_legitimate_skips_and_xfails_pass_the_checker(tmp_path: Path) -> None:
    checker = _load_checker()
    expected = tmp_path / "expected.txt"
    expected.write_text("t.py::a\nt.py::b\nt.py::c\nt.py::d\n", encoding="utf-8")
    led = tmp_path / "led"
    led.mkdir()
    (led / "gw0.tsv").write_text(
        "t.py::a\tgw0\tpassed\nt.py::b\tgw0\tskipped\nt.py::c\tgw0\txfailed\nt.py::d\tgw0\tfailed\n",
        encoding="utf-8",
    )
    assert checker.main([str(expected), str(led)]) == 0  # a failed test did run


def test_checker_flags_setup_error_unknown_outcome_and_a_missing_column(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checker = _load_checker()
    expected = tmp_path / "expected.txt"
    expected.write_text("t.py::a\nt.py::b\nt.py::c\n", encoding="utf-8")
    led = tmp_path / "led"
    led.mkdir()
    (led / "gw0.tsv").write_text(
        "t.py::a\tgw0\terror\nt.py::b\tgw0\tmaybe\nt.py::c\tgw0\n", encoding="utf-8"
    )
    assert checker.main([str(expected), str(led)]) == 1
    out = capsys.readouterr().out
    assert "errored in setup (never ran): t.py::a" in out
    assert "unknown outcome 'maybe': t.py::b" in out
    assert "t.py::c" in out  # the old two-column line is not accepted as ran


def test_checker_refuses_a_vacuous_comparison(tmp_path: Path) -> None:
    checker = _load_checker()
    expected = tmp_path / "expected.txt"
    expected.write_text("t.py::a\n", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert checker.main([str(expected), str(empty)]) == 2
    assert checker.main([str(expected)]) == 2


def test_ledger_off_without_option(suite: Path) -> None:
    plugins = ("-p", "tests.shuffle_plugin", "-p", "tests.run_ledger_plugin")
    before = sorted(p.name for p in suite.iterdir())
    without = run_pytest(suite, "-q", "--no-shuffle", *plugins)
    after = sorted(p.name for p in suite.iterdir() if p.name not in {"__pycache__", "setups.log"})
    assert without.returncode == 0, without.stdout
    assert after == before  # no ledger directory or file appeared
    with_option = run_pytest(suite, "-q", "--no-shuffle", *plugins, f"--run-ledger={suite / 'led'}")
    assert with_option.returncode == 0, with_option.stdout
    assert (suite / "led" / "main.tsv").is_file()
    # Output is identical apart from the timing in the last line.
    def body(out: str) -> list[str]:
        return out.strip().splitlines()[:-1]

    assert body(with_option.stdout) == body(without.stdout)


def test_real_suite_collects_same_set_serial_and_parallel(tmp_path: Path) -> None:
    """The real suite, dry-run under ``-n 2`` (no test body runs), equals the serial collection.

    ``--collect-only`` disables xdist, so a parallel *collection* is only reached by
    running; ``tests.xdist_support`` replaces each test's body with nothing. The real
    conftests, the ``image``/``whisper`` deselect hooks and the checker script all take part.
    """
    marker = ("-m", "integration and not image")
    serial = run_pytest(REPO_ROOT, "--collect-only", "-q", "--no-shuffle", *marker)
    assert serial.returncode == 0, serial.stdout[-2000:] + serial.stderr[-2000:]
    expected = tmp_path / "expected.txt"
    expected.write_text(serial.stdout, encoding="utf-8")
    ledger = tmp_path / "ledger"
    run = run_pytest(
        REPO_ROOT, "-n", "2", "--dist", "loadfile", f"--run-ledger={ledger}", "-q",
        "-p", "tests.xdist_support", *marker,
    )
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    assert len(collected_ids(serial.stdout)) > 500
    assert _load_checker().main([str(expected), str(ledger)]) == 0
