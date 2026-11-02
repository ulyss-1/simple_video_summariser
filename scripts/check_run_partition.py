"""Check that a parallel test run executed exactly the serial set of tests (#296).

    check_run_partition.py EXPECTED LEDGER_DIR

EXPECTED is the output of ``pytest --collect-only -q --no-shuffle <same -m>``
(node ids, one per line; other lines are ignored). LEDGER_DIR holds the
``*.tsv`` files that ``--run-ledger`` (``tests/run_ledger_plugin.py``) wrote:
``nodeid<TAB>worker_id<TAB>outcome`` lines, plus ``#`` header lines. The outcome is
``passed``, ``failed``, ``skipped``, ``xfailed`` (the test ran, or was legitimately
skipped) or ``error`` (its setup failed, so it never ran).

Exit 0 when every expected id was recorded exactly once, by one worker, with a ran
outcome. Exit 1 naming each id that is missing, extra, recorded more than once (and
the workers involved), or errored in setup. Exit 2 on a usage or input error (including an empty
expectation or an empty ledger, which would otherwise pass vacuously).
Standard library only.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path


def read_expected(path: Path) -> list[str]:
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if "::" in line and not line[0].isspace() and not line.startswith(("#", "=")):
            ids.append(line.strip())
    return ids


RAN_OUTCOMES = frozenset({"passed", "failed", "skipped", "xfailed"})


def read_ledger(directory: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(node id -> worker of each record, node id -> outcome of each record)."""
    runs: dict[str, list[str]] = defaultdict(list)
    outcomes: dict[str, list[str]] = defaultdict(list)
    for ledger in sorted(directory.glob("*.tsv")):
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.rsplit("\t", 2)
            if len(parts) != 3:
                parts = [line, "?", "?"]  # malformed: no outcome, reported below
            nodeid, worker, outcome = parts
            runs[nodeid].append(worker)
            outcomes[nodeid].append(outcome)
    return runs, outcomes


def check(
    expected: list[str], runs: dict[str, list[str]], outcomes: dict[str, list[str]]
) -> list[str]:
    problems: list[str] = []
    wanted = set(expected)
    for nodeid in sorted(wanted & runs.keys()):
        for outcome in outcomes[nodeid]:
            if outcome == "error":
                problems.append(f"errored in setup (never ran): {nodeid}")
            elif outcome not in RAN_OUTCOMES:
                problems.append(f"unknown outcome {outcome!r}: {nodeid}")
    problems += [f"missing (never ran): {i}" for i in sorted(wanted - runs.keys())]
    problems += [f"extra (not in the serial set): {i}" for i in sorted(runs.keys() - wanted)]
    for nodeid in sorted(wanted & runs.keys()):
        workers = runs[nodeid]
        if len(workers) > 1:
            kind = "by two workers" if len(set(workers)) > 1 else "more than once"
            problems.append(f"recorded {kind} ({', '.join(workers)}): {nodeid}")
    seen: set[str] = set()
    for nodeid in expected:
        if nodeid in seen:
            problems.append(f"duplicate in the expected list: {nodeid}")
        seen.add(nodeid)
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    expected_path, ledger_dir = Path(argv[0]), Path(argv[1])
    if not expected_path.is_file() or not ledger_dir.is_dir():
        print(f"error: need an expected file and a ledger directory: {argv}", file=sys.stderr)
        return 2
    expected = read_expected(expected_path)
    runs, outcomes = read_ledger(ledger_dir)
    if not expected or not runs:
        print(
            f"error: {len(expected)} expected ids, {len(runs)} ledger ids; "
            "an empty side would pass vacuously",
            file=sys.stderr,
        )
        return 2
    problems = check(expected, runs, outcomes)
    if problems:
        print(f"partition check FAILED: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(f"partition check ok: {len(expected)} tests, each ran exactly once")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
