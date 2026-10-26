"""A cancel request kills a running subprocess (issue #302).

Fake ``Popen`` objects and an injected clock force every interleaving; the one
real-process test proves the group kill takes a grandchild with it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest

from adapters.youtube import ytdlp
from adapters.youtube.ytdlp import run_process, run_ytdlp
from common.errors import Cancelled, ToolFailureError, TransientNetworkError
from tests.adapters.youtube.fakes import FakeRunner, completed

POLL = 0.001
DEADLINE_SEC = 5.0


class FakePopen:
    """``communicate`` times out until ``finish_after`` slices, or until killed."""

    instances: ClassVar[list[FakePopen]] = []

    def __init__(self, argv: Sequence[str], **_kwargs: Any) -> None:
        self.argv = list(argv)
        self.pid = 4242
        self.returncode: int | None = None
        self.slices: list[float | None] = []
        self.finish_after: int | None = None
        FakePopen.instances.append(self)

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.slices.append(timeout)
        if self.finish_after is not None and len(self.slices) > self.finish_after:
            self.returncode = 0
            return "out", ""
        raise subprocess.TimeoutExpired(self.argv, timeout or 0)


@pytest.fixture
def popen(monkeypatch: pytest.MonkeyPatch) -> list[FakePopen]:
    FakePopen.instances = []
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    return FakePopen.instances


@pytest.fixture
def kills(monkeypatch: pytest.MonkeyPatch) -> list[FakePopen]:
    killed: list[FakePopen] = []
    monkeypatch.setattr(ytdlp, "_kill_group", killed.append)
    return killed


def test_a_cancel_kills_the_group_once_and_raises_within_one_slice(
    popen: list[FakePopen], kills: list[FakePopen]
) -> None:
    def cancelled() -> bool:
        # The first check happens after the second slice: the flag is set between slices.
        return len(popen[0].slices) >= 2

    with pytest.raises(Cancelled):
        run_process(["x"], timeout=60, cancelled=cancelled, poll_interval=POLL)

    assert kills == popen  # the group kill ran once, on the one process
    assert len(popen[0].slices) == 2  # killed in the slice the flag appeared, not later
    assert all(s is not None and s <= POLL for s in popen[0].slices)


def test_a_process_that_finished_in_the_slice_the_flag_was_set_is_returned(
    popen: list[FakePopen], kills: list[FakePopen], monkeypatch: pytest.MonkeyPatch
) -> None:
    def finishing_popen(argv: Sequence[str], **kwargs: Any) -> FakePopen:
        proc = FakePopen(argv, **kwargs)
        proc.finish_after = 0  # the first communicate returns
        return proc

    monkeypatch.setattr(subprocess, "Popen", finishing_popen)

    result = run_process(["x"], timeout=60, cancelled=lambda: True, poll_interval=POLL)

    assert result.stdout == "out"
    assert kills == []


def test_cancel_wins_over_a_timeout_due_in_the_same_slice(
    popen: list[FakePopen], kills: list[FakePopen], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]

    class AdvancingPopen(FakePopen):
        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            now[0] += 100.0  # the slice ends far past the deadline
            return super().communicate(timeout)

    monkeypatch.setattr(subprocess, "Popen", AdvancingPopen)

    with pytest.raises(Cancelled):
        run_process(
            ["x"], timeout=10, cancelled=lambda: True, poll_interval=POLL, clock=lambda: now[0]
        )
    assert len(kills) == 1


def test_a_timeout_without_a_cancel_still_raises_timeout_expired(
    popen: list[FakePopen], kills: list[FakePopen], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]

    class AdvancingPopen(FakePopen):
        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            now[0] += 100.0
            return super().communicate(timeout)

    monkeypatch.setattr(subprocess, "Popen", AdvancingPopen)

    with pytest.raises(subprocess.TimeoutExpired):
        run_process(
            ["x"], timeout=10, cancelled=lambda: False, poll_interval=POLL, clock=lambda: now[0]
        )
    assert len(kills) == 1


def test_without_a_signal_run_process_waits_in_one_communicate_call(
    popen: list[FakePopen], monkeypatch: pytest.MonkeyPatch
) -> None:
    def one_shot(argv: Sequence[str], **kwargs: Any) -> FakePopen:
        proc = FakePopen(argv, **kwargs)
        proc.finish_after = 0
        return proc

    monkeypatch.setattr(subprocess, "Popen", one_shot)

    run_process(["x"], timeout=60)

    assert popen[0].slices == [60]


def test_a_non_positive_poll_interval_is_rejected() -> None:
    with pytest.raises(ValueError):
        run_process(["x"], timeout=60, cancelled=lambda: False, poll_interval=0)


# --- run_ytdlp ---------------------------------------------------------


def test_run_ytdlp_hands_the_signal_and_poll_interval_to_the_runner() -> None:
    seen: list[dict[str, Any]] = []

    def runner(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs)
        return completed(stdout="ok")

    def signal() -> bool:
        return False

    run_ytdlp([], timeout=5, runner=runner, cancelled=signal, poll_interval=0.25)

    assert seen == [{"timeout": 5, "cancelled": signal, "poll_interval": 0.25}]


def test_a_cancel_from_the_runner_propagates_unchanged() -> None:
    runner = FakeRunner(Cancelled())
    with pytest.raises(Cancelled):
        run_ytdlp([], timeout=5, runner=runner, cancelled=lambda: True)


def test_a_cancel_with_a_cookie_file_removes_the_temporary_copy(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n")
    copies: list[Path] = []

    def runner(argv: Sequence[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        copies.append(Path(argv[argv.index("--cookies") + 1]))
        assert copies[0].exists()
        raise Cancelled()

    with pytest.raises(Cancelled):
        run_ytdlp([], timeout=5, runner=runner, cookie_file=cookies, cancelled=lambda: True)

    assert len(copies) == 1
    assert not copies[0].exists()
    assert not copies[0].parent.exists()


def test_without_a_signal_a_timeout_is_still_a_transient_network_error() -> None:
    runner = FakeRunner(subprocess.TimeoutExpired(["yt-dlp"], 5))
    with pytest.raises(TransientNetworkError):
        run_ytdlp([], timeout=5, runner=runner)


def test_a_failure_of_the_runner_stays_classified() -> None:
    runner = FakeRunner(FileNotFoundError("nope"))
    with pytest.raises(ToolFailureError):
        run_ytdlp([], timeout=5, runner=runner, cancelled=lambda: False)


# --- the real group kill -----------------------------------------------

_PARENT = textwrap.dedent(
    """
    import os, subprocess, sys, time
    marker = sys.argv[1]
    grandchild = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"]
    )
    with open(marker + ".part", "w") as handle:
        handle.write(f"{sys.argv[2]}\\n{grandchild.pid}\\n")
    os.replace(marker + ".part", marker)
    time.sleep(600)
    """
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers signal 0; its state is Z in /proc.
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


def test_a_cancel_kills_the_whole_group_including_a_grandchild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "ready"
    script = tmp_path / "parent.py"
    script.write_text(_PARENT)
    flag = threading.Event()
    result: list[BaseException] = []
    parent_pid: list[int] = []

    real_popen = subprocess.Popen

    def recording_popen(*args: Any, **kwargs: Any) -> Any:
        proc = real_popen(*args, **kwargs)
        parent_pid.append(proc.pid)
        return proc

    def run() -> None:
        try:
            run_process(
                [sys.executable, str(script), str(marker), "parent"],
                timeout=60,
                cancelled=flag.is_set,
                poll_interval=0.01,
            )
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            result.append(exc)

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        _wait_for(marker.exists, "the child never wrote its ready marker")
        grandchild_pid = int(marker.read_text().split()[1])
        assert _pid_alive(grandchild_pid)
    finally:
        flag.set()
    worker.join(DEADLINE_SEC)
    assert not worker.is_alive(), "run_process never returned after the cancel"

    assert len(result) == 1 and isinstance(result[0], Cancelled)
    _wait_for(lambda: not _pid_alive(grandchild_pid), "the grandchild is still alive")
    _wait_for(lambda: not _pid_alive(parent_pid[0]), "the child is still alive")


def _wait_for(condition: Callable[[], bool], message: str) -> None:
    deadline = time.monotonic() + DEADLINE_SEC
    event = threading.Event()
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        event.wait(0.005)


# --- the three yt-dlp callers --------------------------------------------


def test_metadata_and_subtitles_hand_the_signal_to_the_runner() -> None:
    from adapters.youtube.metadata import YouTubeMetadata
    from adapters.youtube.subtitles import YouTubeSubtitles

    seen: list[dict[str, Any]] = []

    def runner(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs)
        raise Cancelled()

    def signal() -> bool:
        return True

    with pytest.raises(Cancelled):
        YouTubeMetadata(runner=runner, poll_interval=0.2).fetch("jNQXAC9IVRw", cancelled=signal)
    with pytest.raises(Cancelled):
        YouTubeSubtitles(runner=runner, poll_interval=0.2).fetch(
            "jNQXAC9IVRw", "en", "manual", cancelled=signal
        )

    assert [(k["cancelled"], k["poll_interval"]) for k in seen] == [(signal, 0.2)] * 2
