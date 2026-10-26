"""A cancel during or between the steps of ``YouTubeAudio`` leaves no audio (issue #302).

Fake runners write the files a real yt-dlp / ffmpeg would, then raise
``Cancelled`` as the real runner does when its signal turns true.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from adapters.youtube.audio import YouTubeAudio, run_tool
from common.errors import Cancelled, ToolFailureError
from tests.adapters.youtube.fakes import completed

VIDEO_ID = "jNQXAC9IVRw"
POLL = 0.123


def files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


class Download:
    """yt-dlp stand-in: writes the target plus side files, then maybe cancels."""

    def __init__(self, *, then: BaseException | None = None) -> None:
        self.then = then
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.kwargs.append(kwargs)
        target = Path(argv[argv.index("-o") + 1])
        for suffix in (".part", ".ytdl", ".f251.webm.part"):
            Path(f"{target}{suffix}").write_bytes(b"x")
        if self.then is not None:
            raise self.then
        target.write_bytes(b"audio")
        return completed()


class Convert:
    """ffmpeg stand-in: writes the temporary opus, then maybe cancels."""

    def __init__(self, *, then: BaseException | None = None) -> None:
        self.then = then
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.kwargs.append(kwargs)
        Path(argv[-1]).write_bytes(b"opus")
        if self.then is not None:
            raise self.then
        return completed()


class Probe:
    def __init__(self, *, then: BaseException | None = None) -> None:
        self.then = then
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.kwargs.append(kwargs)
        if self.then is not None:
            raise self.then
        return completed(stdout="1.5\n")


def make(
    download: Callable[..., Any], convert: Callable[..., Any], probe: Callable[..., Any]
) -> YouTubeAudio:
    return YouTubeAudio(
        ytdlp_runner=download, ffmpeg_runner=convert, ffprobe_runner=probe, poll_interval=POLL
    )


def test_a_cancel_during_the_download_leaves_no_file_with_the_token(tmp_path: Path) -> None:
    audio = make(Download(then=Cancelled()), Convert(), Probe())

    with pytest.raises(Cancelled):
        audio.fetch_normalized(VIDEO_ID, tmp_path, cancelled=lambda: True)

    assert files(tmp_path) == []  # .download-<token>, .part, .ytdl and fragments: all gone


def test_a_cancel_during_the_conversion_removes_the_temporary_and_the_download(
    tmp_path: Path,
) -> None:
    audio = make(Download(), Convert(then=Cancelled()), Probe())

    with pytest.raises(Cancelled):
        audio.fetch_normalized(VIDEO_ID, tmp_path, cancelled=lambda: True)

    assert files(tmp_path) == []


def test_a_cancel_during_ffprobe_removes_the_temporary_and_the_download(tmp_path: Path) -> None:
    audio = make(Download(), Convert(), Probe(then=Cancelled()))

    with pytest.raises(Cancelled):
        audio.fetch_normalized(VIDEO_ID, tmp_path, cancelled=lambda: True)

    assert files(tmp_path) == []


@pytest.mark.parametrize("trips_at", [1, 2, 3])
def test_a_cancel_between_steps_leaves_no_final_file(tmp_path: Path, trips_at: int) -> None:
    # 1: before the download, 2: after it, 3: after the conversion, before os.replace.
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= trips_at

    audio = make(Download(), Convert(), Probe())

    with pytest.raises(Cancelled):
        audio.fetch_normalized(VIDEO_ID, tmp_path, cancelled=cancelled)

    assert files(tmp_path) == []
    assert calls == trips_at  # the signal was read at each step, and not after the cancel


def test_a_cancel_after_the_replace_returns_the_finished_file(tmp_path: Path) -> None:
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls > 3  # true only from the fourth look on: after os.replace

    audio = make(Download(), Convert(), Probe())

    ref = audio.fetch_normalized(VIDEO_ID, tmp_path, cancelled=cancelled)

    assert ref.rel_path == f"jN/{VIDEO_ID}.opus"
    assert files(tmp_path) == [f"jN/{VIDEO_ID}.opus"]
    assert calls == 3  # nothing looks after os.replace


def test_every_step_gets_the_signal_and_the_poll_interval(tmp_path: Path) -> None:
    download, convert, probe = Download(), Convert(), Probe()

    def signal() -> bool:
        return False

    make(download, convert, probe).fetch_normalized(VIDEO_ID, tmp_path, cancelled=signal)

    for runner in (download, convert, probe):
        assert len(runner.kwargs) == 1
        assert runner.kwargs[0]["cancelled"] is signal
        assert runner.kwargs[0]["poll_interval"] == POLL


def test_without_a_signal_a_conversion_timeout_is_still_a_tool_failure(tmp_path: Path) -> None:
    convert = Convert(then=subprocess.TimeoutExpired(["ffmpeg"], 1))
    with pytest.raises(ToolFailureError):
        make(Download(), convert, Probe()).fetch_normalized(VIDEO_ID, tmp_path)
    assert files(tmp_path) == []


def test_run_tool_returns_a_finished_process_whatever_the_signal_says() -> None:
    result = run_tool([sys.executable, "-c", "print(1)"], timeout=30, cancelled=lambda: True)
    assert result.stdout.strip() == "1"


def test_run_tool_maps_a_missing_binary_to_a_tool_failure() -> None:
    with pytest.raises(ToolFailureError):
        run_tool(["definitely-not-a-binary-302"], timeout=5, cancelled=lambda: False)


def test_run_tool_kills_a_running_tool_when_the_signal_turns_true() -> None:
    # A real child that would run for ten minutes; the signal is already true at
    # the first poll, so the loop kills it and raises within one slice.
    with pytest.raises(Cancelled):
        run_tool(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            timeout=60,
            cancelled=lambda: True,
            poll_interval=0.01,
        )


def test_ffmpeg_and_ffprobe_use_run_tool_by_default() -> None:
    audio = YouTubeAudio()
    assert audio._ffmpeg_runner is run_tool
    assert audio._ffprobe_runner is run_tool
