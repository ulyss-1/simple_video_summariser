"""The CI workflow's job gating (issue #316): a static check of its YAML text.

No YAML parser is a dependency, so these read `ci.yml` with regexes, in the
style of tests/scripts/test_lock_update_workflow.py. Nothing here calls GitHub.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
SCRIPT = ROOT / "scripts" / "ci_changes.sh"


def _text() -> str:
    return CI.read_text()


def _job(name: str) -> str:
    """The text of one top-level job: from `  <name>:` to the next job."""
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)", _text(), re.MULTILINE | re.DOTALL)
    assert match, f"no job {name}"
    return match.group(1)


def _script_keys() -> list[str]:
    out = subprocess.run(
        ["bash", str(SCRIPT)], input="", capture_output=True, text=True, check=True
    ).stdout
    return [line.split("=")[0] for line in out.split()]


def _changes_outputs() -> list[str]:
    block = re.search(r"^    outputs:\n((?:      \S.*\n)+)", _job("changes"), re.MULTILINE)
    assert block
    return re.findall(r"^      (\w+): \$\{\{ steps\.c\.outputs\.\1 \}\}$", block.group(1), re.MULTILINE)


def test_changes_outputs_are_the_keys_the_script_prints() -> None:
    assert _changes_outputs() == _script_keys()


def test_non_pull_request_branch_prints_every_key_true() -> None:
    printed = re.search(r"printf '([^']*)' >> \"\$GITHUB_OUTPUT\"", _job("changes"))
    assert printed
    pairs = [line.split("=") for line in printed.group(1).replace("\\n", "\n").split()]
    assert pairs == [[key, "true"] for key in _script_keys()]


def test_every_job_gate_reads_a_key_the_script_prints() -> None:
    gates = re.findall(r"^    if: needs\.changes\.outputs\.(\w+) == 'true'$", _text(), re.MULTILINE)
    assert set(gates) == set(_script_keys())
    for read in re.findall(r"needs\.changes\.outputs\.(\w+)", _text()):
        assert read in _script_keys()


def test_gating_is_job_level_only() -> None:
    text = _text()
    # Every `if:` that reads the path decision is a job-level one.
    for match in re.finditer(r"^(\s*)(?:- )?if: .*needs\.changes", text, re.MULTILINE):
        assert match.group(1) == "    "
    assert "paths:" not in _job("bakeoff") and "workflow_run" not in text


def test_images_and_bakeoff_split_the_image_tests() -> None:
    assert 'run: pytest -m "image and not bakeoff"' in _job("images")
    assert "name: pytest -m \"image and not bakeoff\"" in _job("images")
    bakeoff = _job("bakeoff")
    assert 'run: pytest -m "image and bakeoff"' in bakeoff
    assert 'name: pytest -m "image and bakeoff"' in bakeoff
    assert "needs: changes" in bakeoff
    assert "if: needs.changes.outputs.bakeoff == 'true'" in bakeoff
    assert "runs-on: ubuntu-latest" in bakeoff
    assert "timeout-minutes: 30" in bakeoff
    comment = re.search(r"((?:^  #.*\n)+)^  bakeoff:$", _text(), re.MULTILINE)
    assert comment
    assert "#76" in comment.group(1) and "#316" in comment.group(1)
    assert "scripts/ci_changes.sh" in comment.group(1)


def _setup(job: str) -> str:
    """Steps from checkout up to (not including) the pytest step."""
    text = _job(job)
    head = text[text.index("steps:") : text.index("      - name: pytest")]
    lines = head.splitlines()
    # The comment that introduces the pytest step belongs to that step.
    while lines[-1].lstrip().startswith("#"):
        lines.pop()
    return "\n".join(lines)


def test_bakeoff_setup_steps_are_the_images_setup_steps() -> None:
    images_setup = _setup("images")
    # The prebuilt-image step (#368) is the images job's own; the rest is shared.
    own = images_setup.index("      # Finds the prebuilt dependency images")
    assert _setup("bakeoff") == images_setup[:own].rstrip("\n")
    assert "actions/setup-python@" in images_setup and "check-installed" in images_setup



def test_images_job_resolves_prebuilt_images_before_the_tests_with_the_checked_out_script() -> None:
    images = _job("images")
    step = "run: python scripts/deps_image_tag.py resolve | tee -a \"$GITHUB_ENV\""
    assert step in images
    # After the locks are installed and the project checked out; before pytest.
    assert images.index("actions/checkout@") < images.index(step) < images.index("run: pytest")
    assert "YTDIGEST_DEPS_IMAGE" in images and "YTDIGEST_WHISPER_DEPS_IMAGE" in images
    assert "#368" in images


def test_images_job_stays_read_only_and_adds_no_layer_cache() -> None:
    text = _text()
    assert "packages: write" not in text
    assert "packages:" not in text
    assert not re.search(r"^\s+permissions:", _job("images"), re.MULTILINE)
    assert re.search(r"^permissions:\n  contents: read$", text, re.MULTILINE)
    for forbidden in ("cache-from", "cache-to", "type=gha", "type=registry", "docker login"):
        assert forbidden not in text


def test_no_workflow_names_a_ghcr_image_by_a_literal_tag() -> None:
    # Consumers use a digest the script resolved for a tag it computed; the
    # publisher builds its tags from variables. A literal `:latest` or any other
    # literal tag in a ghcr.io reference would be a floating or hand-picked one.
    assert "ghcr.io" not in _text(), "the lookup lives in scripts/deps_image_tag.py"
    for workflow in CI.parent.glob("*.yml"):
        for ref in re.findall(r"ghcr\.io/[\w./-]+:[\w.-]+", workflow.read_text()):
            raise AssertionError(f"{workflow.name}: {ref}")


# --- #296: the integration step runs under pytest-xdist, in the python job only ---


def _python_job() -> str:
    return _job("python")


def test_exactly_the_two_pytest_steps_of_the_python_job_use_xdist() -> None:
    """#384: the fast step and the integration step; no other step or job."""
    fast = re.findall(r'run: (pytest -m "not integration"[^\n]*)', _python_job())
    integ = re.findall(r'run: (pytest -m "integration and not image"[^\n]*)', _python_job())
    assert len(fast) == 1 and len(integ) == 1
    assert re.search(r" -n \d+ --dist loadfile ", fast[0] + " ")
    assert re.search(r" -n 3 --dist loadfile ", integ[0] + " ")
    assert "-n auto" not in _text()  # one reviewable number
    assert len(re.findall(r"(?:^|\s)-n \d+ --dist loadfile", _text())) == 2
    assert len(re.findall(r"(?:^|\s)-n \d+", _python_job())) == 2
    for job in ("images", "bakeoff", "frontend", "e2e"):
        if re.search(rf"^  {job}:", _text(), re.MULTILINE):
            assert " -n " not in _job(job)


def test_xdist_is_never_in_addopts() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    addopts = re.search(r"^addopts = (\[.*?\])", pyproject, re.MULTILINE | re.DOTALL)
    assert addopts and "-n" not in addopts.group(1) and "dist" not in addopts.group(1)


def test_partition_steps_run_in_order_and_the_ledger_is_kept_on_failure() -> None:
    job = _python_job()
    checks = [m.start() for m in re.finditer(r"run: python scripts/check_run_partition\.py", job)]
    assert len(checks) == 2
    order = [
        job.index("ruff check ."),
        job.index("mypy ."),
        job.index('--collect-only -q --no-shuffle -m "not integration"'),
        job.index('run: pytest -m "not integration" -n '),
        checks[0],
        job.index('--collect-only -q --no-shuffle -m "integration and not image"'),
        job.index('run: pytest -m "integration and not image" -n 3'),
        checks[1],
        job.index("actions/upload-artifact@"),
    ]
    assert order == sorted(order)
    # Nothing is shared between the two checks.
    fast_check = re.search(r"check_run_partition\.py (\S+) (\S+)", job[checks[0] :])
    integ_check = re.search(r"check_run_partition\.py (\S+) (\S+)", job[checks[1] :])
    assert fast_check and integ_check
    assert fast_check.group(1) == '"$RUNNER_TEMP/expected-fast-tests.txt"'
    assert fast_check.group(2) == '"$RUNNER_TEMP/run-ledger-fast"'
    assert fast_check.groups() != integ_check.groups()
    assert fast_check.group(1) != integ_check.group(1) and fast_check.group(2) != integ_check.group(2)
    fast_run = re.search(r'run: (pytest -m "not integration"[^\n]*)', job)
    assert fast_run and '--run-ledger="$RUNNER_TEMP/run-ledger-fast"' in fast_run.group(1)
    # The nested-parallel isolation proof runs alone, after the parallel step, and is
    # left out of the collected and the parallel set (same flag on both).
    ignore = "--ignore=tests/test_xdist_isolation_db.py"
    collect = re.search(r'run: (pytest --collect-only[^\n]*"integration and not image"[^\n]*)', job)
    parallel = re.search(r'run: (pytest -m "integration and not image" -n 3[^\n]*)', job)
    assert collect and ignore in collect.group(1)
    assert parallel and ignore in parallel.group(1)
    serial = re.search(r"run: (pytest tests/test_xdist_isolation_db\.py[^\n]*)", job)
    assert serial and " -n " not in serial.group(1)
    assert job.index(serial.group(1)) > job.index("scripts/check_run_partition.py")
    assert "--run-ledger=" in job
    upload = job[order[-1] - 200 :]
    assert re.search(r"if: failure\(\)\n\s+uses: actions/upload-artifact@[0-9a-f]{40}", upload)
    uploaded = re.findall(r"\$\{\{ runner\.temp \}\}/(\S+)", upload)
    assert sorted(uploaded) == ["expected-fast-tests.txt", "expected-tests.txt", "run-ledger", "run-ledger-fast"]
