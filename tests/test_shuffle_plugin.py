"""The in-repo shuffle plugin (issue #72), exercised on generated suites.

Uses pytest's built-in ``pytester``; fixed seeds, no Docker, no sleep.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.xdist_support import (
    collected_ids,
    ledger_headers,
    read_ledger_lines,
    run_pytest,
)

_PLUGIN = ("-p", "tests.shuffle_plugin")

_MODULE_TEMPLATE = """
import pytest

SETUPS = []

@pytest.fixture(scope="module")
def mod_fixture():
    SETUPS.append(1)
    return len(SETUPS)

def test_one(mod_fixture): assert mod_fixture == 1
def test_two(mod_fixture): assert mod_fixture == 1
def test_three(mod_fixture): assert mod_fixture == 1
def test_four(mod_fixture): assert mod_fixture == 1
def test_five(mod_fixture): assert mod_fixture == 1

@pytest.mark.parametrize("n", range(5))
def test_param(n, mod_fixture): assert mod_fixture == 1

class TestGroup:
    def test_a(self, mod_fixture): assert mod_fixture == 1
    def test_b(self, mod_fixture): assert mod_fixture == 1
    def test_c(self, mod_fixture): assert mod_fixture == 1
    def test_d(self, mod_fixture): assert mod_fixture == 1
"""


@pytest.fixture
def suite(pytester: pytest.Pytester) -> pytest.Pytester:
    for name in ("test_m1", "test_m2", "test_m3", "test_m4"):
        pytester.makepyfile(**{name: _MODULE_TEMPLATE})
    return pytester


def _collected(pytester: pytest.Pytester, *args: str) -> list[str]:
    result = pytester.runpytest(*_PLUGIN, "--collect-only", "-q", *args)
    return [line for line in result.stdout.lines if "::" in line]


def test_same_seed_gives_same_order(suite: pytest.Pytester) -> None:
    first = _collected(suite, "--shuffle-seed=1")
    second = _collected(suite, "--shuffle-seed=1")
    assert len(first) == 4 * 14
    assert first == second


def test_different_seeds_give_different_orders(suite: pytest.Pytester) -> None:
    assert _collected(suite, "--shuffle-seed=1") != _collected(
        suite, "--shuffle-seed=2"
    )


def test_shuffle_actually_differs_from_collection_order(suite: pytest.Pytester) -> None:
    assert _collected(suite, "--shuffle-seed=1") != _collected(suite, "--no-shuffle")


def test_modules_and_classes_are_not_interleaved(suite: pytest.Pytester) -> None:
    for seed in (1, 2, 3):
        ids = _collected(suite, f"--shuffle-seed={seed}")
        modules = [i.split("::")[0] for i in ids]
        runs = [m for idx, m in enumerate(modules) if idx == 0 or modules[idx - 1] != m]
        assert sorted(runs) == sorted(set(modules)), f"module interleaved, seed {seed}"

        for module in set(modules):
            in_module = [i for i in ids if i.startswith(module + "::")]
            classes = ["TestGroup" in i for i in in_module]
            switches = sum(
                1 for k in range(1, len(classes)) if classes[k] != classes[k - 1]
            )
            assert switches <= 2, f"class interleaved, seed {seed}"
            # the class block is contiguous
            assert "".join("1" if c else "0" for c in classes).count("10") <= 1


def test_parametrized_cases_are_shuffled_independently(suite: pytest.Pytester) -> None:
    orders = set()
    for seed in range(1, 8):
        ids = _collected(suite, f"--shuffle-seed={seed}")
        orders.add(tuple(i for i in ids if i.startswith("test_m1.py::test_param")))
    assert len(orders) > 1
    assert all(len(o) == 5 for o in orders)


def test_module_fixture_set_up_once_per_module(suite: pytest.Pytester) -> None:
    result = suite.runpytest(*_PLUGIN, "--shuffle-seed=3", "-q")
    result.assert_outcomes(passed=4 * 14)


def test_no_shuffle_keeps_collection_order(suite: pytest.Pytester) -> None:
    ids = _collected(suite, "--no-shuffle")
    assert [i.split("::")[0] for i in ids] == sorted(i.split("::")[0] for i in ids)
    one = [i for i in ids if i.startswith("test_m1.py::test_")][:5]
    assert [i.rsplit("::", 1)[1] for i in one] == [
        "test_one",
        "test_two",
        "test_three",
        "test_four",
        "test_five",
    ]


def test_header_shows_seed_or_off(suite: pytest.Pytester) -> None:
    on = suite.runpytest(*_PLUGIN, "--shuffle-seed=77", "--collect-only")
    on.stdout.fnmatch_lines(["*shuffle seed: 77*"])
    off = suite.runpytest(*_PLUGIN, "--no-shuffle", "--collect-only")
    off.stdout.fnmatch_lines(["*shuffle: off*"])


def test_seed_in_summary_under_quiet(suite: pytest.Pytester) -> None:
    result = suite.runpytest(*_PLUGIN, "--shuffle-seed=77", "-q")
    assert result.stdout.lines[-1] != ""
    tail = "\n".join(result.stdout.lines[-4:])
    assert "shuffle seed: 77" in tail


def test_default_seed_is_random_integer_and_printed(suite: pytest.Pytester) -> None:
    result = suite.runpytest(*_PLUGIN, "--collect-only")
    match = re.search(r"shuffle seed: (\d+)", result.stdout.str())
    assert match is not None


def test_non_integer_seed_is_usage_error(suite: pytest.Pytester) -> None:
    result = suite.runpytest(*_PLUGIN, "--shuffle-seed=abc")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "Traceback" not in result.stdout.str() + result.stderr.str()


def test_options_listed_in_help(suite: pytest.Pytester) -> None:
    result = suite.runpytest(*_PLUGIN, "--help")
    result.stdout.fnmatch_lines(["*--shuffle-seed*", "*--no-shuffle*"])


def test_plugin_does_not_touch_global_random_state(suite: pytest.Pytester) -> None:
    import random

    before = random.getstate()
    _collected(suite, "--shuffle-seed=5")
    assert random.getstate() == before


def test_deselection_does_not_change_selected_count(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_sel="""
        import pytest

        @pytest.mark.slow
        def test_x(): pass
        def test_y(): pass
        def test_z(): pass
        """
    )
    pytester.makeini("[pytest]\nmarkers = slow\n")
    shuffled = _collected(pytester, "-m", "not slow", "--shuffle-seed=1")
    plain = _collected(pytester, "-m", "not slow", "--no-shuffle")
    assert sorted(shuffled) == sorted(plain)
    assert len(shuffled) == 2


def test_ff_still_runs_previous_failure_first(pytester: pytest.Pytester) -> None:
    body = "\n".join(f"def test_{c}(): pass" for c in "abcdefghij")
    pytester.makepyfile(test_ff=body + "\ndef test_zfail(): assert False\n")
    pytester.runpytest(*_PLUGIN, "--no-shuffle", "-q")
    for seed in (1, 2, 3, 4, 5):
        result = pytester.runpytest(*_PLUGIN, f"--shuffle-seed={seed}", "--ff", "-v")
        outcomes = [
            line for line in result.stdout.lines if "PASSED" in line or "FAILED" in line
        ]
        assert "test_zfail" in outcomes[0], f"seed {seed}: {outcomes[0]}"


# --- xdist composition (#296). Real workers, so fresh interpreters, no Docker. ---


@pytest.fixture
def xsuite(tmp_path: Path) -> Path:
    for name in ("test_m1", "test_m2", "test_m3", "test_m4"):
        (tmp_path / f"{name}.py").write_text(_MODULE_TEMPLATE, encoding="utf-8")
    return tmp_path


_XPLUGINS = ("-p", "tests.shuffle_plugin", "-p", "tests.run_ledger_plugin")


def _xrun(suite: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run_pytest(
        suite, "-n", "2", "--dist", "loadfile", f"--run-ledger={suite / 'led'}", *_XPLUGINS, *args
    )


def _seed_lines(output: str) -> list[str]:
    return [m.group(1) for line in output.splitlines() if (m := re.fullmatch(r"shuffle seed: (\d+)", line.strip()))]


def _header_seeds(suite: Path) -> dict[str, str]:
    seeds: dict[str, str] = {}
    for stem, head in ledger_headers(suite / "led").items():
        match = re.match(r"# seed=(\w+) ", head)
        assert match is not None, head
        seeds[stem] = match.group(1)
    return seeds


def _is_subsequence(part: list[str], whole: list[str]) -> bool:
    it = iter(whole)
    return all(item in it for item in part)


def test_workers_share_the_controller_seed(xsuite: Path) -> None:
    result = _xrun(xsuite)  # no seed given: the controller draws one
    assert result.returncode == 0, result.stdout + result.stderr
    printed = _seed_lines(result.stdout)
    assert len(printed) == 2  # one header line, one summary line, both from the controller
    assert printed[0] == printed[1]
    seeds = _header_seeds(xsuite)
    assert sorted(seeds) == ["gw0", "gw1"]
    assert set(seeds.values()) == {printed[0]}


def test_workers_without_shared_seed_disagree(xsuite: Path) -> None:
    """Regression guard: without the handoff each worker draws its own seed and xdist aborts."""
    broken = _xrun(xsuite, "--shuffle-no-handoff")
    assert broken.returncode != 0
    assert "Different tests were collected" in broken.stdout + broken.stderr
    fixed = _xrun(xsuite)
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr


def test_each_workers_order_is_a_subsequence_of_the_serial_shuffle(xsuite: Path) -> None:
    serial = run_pytest(
        xsuite, "--collect-only", "-q", "-p", "tests.shuffle_plugin", "--shuffle-seed=7"
    )
    order = collected_ids(serial.stdout)
    assert len(order) == 4 * 14
    result = _xrun(xsuite, "--shuffle-seed=7")
    assert result.returncode == 0, result.stdout + result.stderr
    rows = read_ledger_lines(xsuite / "led")
    assert sum(len(r) for r in rows.values()) == len(order)
    for worker, entries in rows.items():
        assert _is_subsequence([nodeid for nodeid, _ in entries], order), worker
    # Which module lands on which worker depends on how fast each worker finishes, so
    # it is deliberately not asserted; the order within a worker is what the seed fixes.


def test_no_shuffle_and_explicit_seed_pass_through_xdist(xsuite: Path) -> None:
    plain = run_pytest(xsuite, "--collect-only", "-q", "-p", "tests.shuffle_plugin", "--no-shuffle")
    plain_order = collected_ids(plain.stdout)
    off = _xrun(xsuite, "--no-shuffle")
    assert off.returncode == 0, off.stdout + off.stderr
    assert _seed_lines(off.stdout) == []
    assert "shuffle: off" in off.stdout
    assert set(_header_seeds(xsuite).values()) == {"off"}
    for worker, entries in read_ledger_lines(xsuite / "led").items():
        assert _is_subsequence([n for n, _ in entries], plain_order), worker
    shutil.rmtree(xsuite / "led")

    explicit = _xrun(xsuite, "--shuffle-seed=12345")
    assert explicit.returncode == 0, explicit.stdout + explicit.stderr
    assert _seed_lines(explicit.stdout) == ["12345", "12345"]
    assert set(_header_seeds(xsuite).values()) == {"12345"}
