"""Offline tests of the multi-command helper `tests/docker_steps.py` (#330).

The generated script runs through the local `sh`, so no Docker is needed.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.docker_steps import run_steps


def _local(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, timeout=30, check=False
    )


def test_each_command_keeps_its_own_status_stdout_and_stderr() -> None:
    good, bad, empty, multi = run_steps(
        _local,
        "echo hello",
        "echo oops >&2; echo partial; exit 3",
        "true",
        "printf 'a\\nb\\n'",
    )
    assert (good.returncode, good.stdout, good.stderr) == (0, "hello", "")
    assert (bad.returncode, bad.stdout, bad.stderr) == (3, "partial", "oops")
    assert (empty.returncode, empty.stdout, empty.stderr) == (0, "", "")
    assert multi.stdout == "a\nb"


def test_a_failing_command_does_not_stop_the_later_ones() -> None:
    first, second = run_steps(_local, "false", "echo after")
    first.failed()
    assert second.ok() == "after"


def test_ok_failure_message_names_the_command_and_shows_its_output() -> None:
    (step,) = run_steps(_local, "echo out; echo err >&2; exit 4")
    with pytest.raises(AssertionError) as info:
        step.ok()
    message = str(info.value)
    assert "echo out; echo err >&2; exit 4" in message
    assert "exited 4" in message
    assert "out" in message
    assert "err" in message


def test_failed_raises_when_the_command_succeeds() -> None:
    (step,) = run_steps(_local, "true")
    with pytest.raises(AssertionError, match="`true` exited 0"):
        step.failed()


def test_a_missing_step_is_reported_instead_of_misaligning_results() -> None:
    def truncated(_script: str) -> subprocess.CompletedProcess[str]:
        return _local("echo '@@STEP@@'; echo 0; echo x; echo '@@ERR@@'")

    with pytest.raises(AssertionError, match="expected 2 steps, got 1"):
        run_steps(truncated, "a", "b")
