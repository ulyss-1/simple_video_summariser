"""Run several shell commands in one container and read each result back (#330).

A `docker run --rm` costs about half a second, so a test that checks several
things on the same image with the same flags runs them in one `sh -c` script.
Each command's stdout, stderr and exit status are recorded under a fixed
delimiter, so every check is still made on its own and a failure names the
command and shows its output (a bare `set -e` would hide which one failed).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass

STEP = "@@STEP@@"
ERR = "@@ERR@@"
_ERR_FILE = "/tmp/.docker-steps-stderr"  # inside the throwaway container


@dataclass(frozen=True)
class Step:
    command: str
    returncode: int
    stdout: str
    stderr: str

    def describe(self) -> str:
        return (
            f"`{self.command}` exited {self.returncode}\n"
            f"stdout: {self.stdout!r}\nstderr: {self.stderr!r}"
        )

    def ok(self) -> str:
        """Assert a zero exit and return the stripped stdout."""
        assert self.returncode == 0, self.describe()
        return self.stdout.strip()

    def failed(self) -> None:
        assert self.returncode != 0, self.describe()


def script(commands: tuple[str, ...]) -> str:
    """One `sh` script that runs every command, whatever the earlier ones do."""
    parts = []
    for command in commands:
        parts.append(
            f"o=$({{ {command}\n}} 2>{_ERR_FILE}); rc=$?; "
            f"printf '%s\\n' '{STEP}' \"$rc\" \"$o\" '{ERR}'; cat {_ERR_FILE}"
        )
    return "\n".join(parts)


def parse(
    commands: tuple[str, ...], result: subprocess.CompletedProcess[str]
) -> list[Step]:
    chunks = result.stdout.split(f"{STEP}\n")[1:]
    assert len(chunks) == len(commands), (
        f"expected {len(commands)} steps, got {len(chunks)}: "
        f"exit {result.returncode}\n{result.stdout}\n{result.stderr}"
    )
    steps = []
    for command, chunk in zip(commands, chunks, strict=True):
        head, _, err = chunk.partition(f"{ERR}\n")
        code, _, out = head.partition("\n")
        steps.append(Step(command, int(code), out.rstrip("\n"), err.rstrip("\n")))
    return steps


def run_steps(
    run: Callable[[str], subprocess.CompletedProcess[str]], *commands: str
) -> list[Step]:
    """`run` executes one `sh -c` script in the container and returns the process."""
    return parse(commands, run(script(commands)))
