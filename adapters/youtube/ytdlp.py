"""The one way YouTube adapters run yt-dlp.

yt-dlp runs as ``sys.executable -m yt_dlp``, so the copy pinned in this
interpreter's environment (requirements.backend.txt) is the one used, never
whatever ``yt-dlp`` happens to be on ``PATH``. It runs in its own process
group: on a timeout the whole group is killed, including anything yt-dlp
started (ffmpeg, a JS runtime).

The process call is injectable (``runner``), so tests never start yt-dlp.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from typing import Protocol

from adapters.youtube.errors import from_ytdlp
from common.errors import ToolFailureError, TransientNetworkError

# How long to wait for a killed process group to close its pipes.
_REAP_TIMEOUT_SEC = 5.0

# What `python -m yt_dlp` prints when the module is missing.
_NOT_INSTALLED = "No module named yt_dlp"


class ProcessRunner(Protocol):
    """Runs ``argv`` to completion; raises ``subprocess.TimeoutExpired`` on timeout."""

    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]: ...


def run_process(
    argv: Sequence[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` in a new process group and capture its output as text.

    On timeout (or any interruption) the whole group is killed and reaped
    before the exception propagates, so nothing keeps running.
    """
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except BaseException:
        _kill_group(proc)
        raise
    return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)


def _kill_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        # Returns once every holder of the pipes, grandchildren too, has exited.
        proc.communicate(timeout=_REAP_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        # Something left the group and still holds a pipe; stop waiting on it.
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        proc.wait()


def run_ytdlp(
    args: Sequence[str], *, timeout: float, runner: ProcessRunner = run_process
) -> str:
    """Run yt-dlp with ``args`` and return its stdout.

    Raises ``TransientNetworkError`` on timeout, ``ToolFailureError`` when
    yt-dlp is not installed, and ``from_ytdlp(stderr, returncode)`` on any
    other non-zero exit.
    """
    if timeout <= 0:
        raise ValueError(f"timeout must be > 0, got {timeout}")
    argv = [sys.executable, "-m", "yt_dlp", *args]
    try:
        result = runner(argv, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TransientNetworkError(f"yt-dlp timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise ToolFailureError(f"cannot start {sys.executable}: {exc}") from exc
    if result.returncode != 0:
        if _NOT_INSTALLED in result.stderr:
            raise ToolFailureError(f"yt-dlp is not installed for {sys.executable}")
        raise from_ytdlp(result.stderr, result.returncode)
    return result.stdout
