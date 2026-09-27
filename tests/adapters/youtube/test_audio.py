"""YouTube audio adapter (issue #19).

No test here downloads anything: the yt-dlp runner is injected with a fake
that generates a tone/noise clip via a real ``ffmpeg -f lavfi`` call, so the
"download" step is a local subprocess, not a network call. Conversion always
runs the real ffmpeg/ffprobe on the host (AGENTS.md -> Setup).
"""

from __future__ import annotations

import errno
import json
import os
import re
import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from adapters.youtube.audio import DEFAULT_DOWNLOAD_TIMEOUT_SEC, YouTubeAudio, run_tool
from common.errors import ResourceError, ToolFailureError, TransientNetworkError
from common.models import AudioRef, AudioSource
from tests.adapters.youtube.fakes import FakeRunner, completed

VIDEO_ID = "jNQXAC9IVRw"
DASH_VIDEO_ID = "-wNyEUrxzFU"

_VALID_ID = re.compile(r"[A-Za-z0-9_-]{11}")


# --- test doubles ------------------------------------------------------


class FakeDownloader:
    """Stands in for yt-dlp: writes a generated tone to the ``-o`` path.

    No network call is made; the "download" is a real ``ffmpeg -f lavfi``
    invocation, so the file at the yt-dlp destination is real, decodable
    audio - exactly what the conversion step actually receives in
    production.
    """

    def __init__(self, *, duration_sec: float = 2.0) -> None:
        self.duration_sec = duration_sec
        self.calls: list[tuple[list[str], float]] = []

    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), timeout))
        dest = Path(argv[argv.index("-o") + 1])
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={self.duration_sec}",
                "-f",
                "wav",
                str(dest),
            ],
            check=True,
            capture_output=True,
        )
        return completed(returncode=0)


class RecordingRealFfmpeg:
    """Records argv, then runs the real ``run_tool`` (real ffmpeg/ffprobe)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return run_tool(argv, timeout=timeout)


class FailingFfmpeg:
    """Writes some bytes, then fails - the partial-output ffmpeg stand-in."""

    def __init__(self, *, stderr_lines: int = 30) -> None:
        self.stderr = "\n".join(f"line {i}" for i in range(stderr_lines))
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        Path(argv[-1]).write_bytes(b"not actually opus")
        return completed(stderr=self.stderr, returncode=1)


def files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def probe(path: Path) -> dict[str, str | int | float]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    info: dict[str, str | int | float] = dict(data["streams"][0])
    info["duration"] = float(data["format"]["duration"])
    return info


# --- port --------------------------------------------------------------


def test_youtube_audio_is_an_audio_source() -> None:
    source: AudioSource = YouTubeAudio(ytdlp_runner=FakeDownloader())
    assert callable(source.fetch_normalized)


def test_audio_ref_is_immutable() -> None:
    ref = AudioRef(rel_path="ab/abc.opus", bytes=1, duration_sec=1.0)
    with pytest.raises(AttributeError):
        ref.bytes = 2  # type: ignore[misc]


# --- input validation ----------------------------------------------------


@pytest.mark.parametrize(
    "video_id",
    [
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "jNQXAC9IVR",  # 10 characters
        "jNQXAC9IVRww",  # 12 characters
        "jNQX C9IVRw",  # space
        "jNQXAC9;IVR",  # ;
        "jNQXAC9IVR\n",
        "\njNQXAC9IVR",
        "jNQXAC9IVRé",
        "jNQXAC9IVR.",
        "",
    ],
)
def test_a_malformed_video_id_is_rejected_before_any_process_starts(
    video_id: str, tmp_path: Path
) -> None:
    ytdlp_runner = FakeDownloader()
    ffmpeg_runner = FailingFfmpeg()
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner, ffmpeg_runner=ffmpeg_runner)

    with pytest.raises(ValueError):
        audio.fetch_normalized(video_id, tmp_path)

    assert ytdlp_runner.calls == []
    assert ffmpeg_runner.calls == []
    assert files_under(tmp_path) == []


@given(st.text(max_size=20).filter(lambda s: not _VALID_ID.fullmatch(s)))
def test_any_string_that_is_not_an_11_char_id_is_rejected(video_id: str) -> None:
    # No tmp_path fixture: validation fails before anything is written, so
    # a fixed, never-touched directory is enough (and avoids hypothesis's
    # function-scoped-fixture health check).
    ytdlp_runner = FakeDownloader()
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner)

    with pytest.raises(ValueError):
        audio.fetch_normalized(video_id, Path("/nonexistent/audio-dir"))

    assert ytdlp_runner.calls == []


def test_a_valid_id_is_passed_to_yt_dlp_as_a_full_watch_url(tmp_path: Path) -> None:
    ytdlp_runner = FakeDownloader(duration_sec=1.0)
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner)

    audio.fetch_normalized(VIDEO_ID, tmp_path)

    [(argv, _)] = ytdlp_runner.calls
    assert argv[-1] == f"https://www.youtube.com/watch?v={VIDEO_ID}"


def test_an_id_starting_with_a_dash_is_passed_as_a_watch_url_never_bare(
    tmp_path: Path,
) -> None:
    ytdlp_runner = FakeDownloader(duration_sec=1.0)
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner)

    ref = audio.fetch_normalized(DASH_VIDEO_ID, tmp_path)

    assert ref.rel_path == f"{DASH_VIDEO_ID[:2]}/{DASH_VIDEO_ID}.opus"
    [(argv, _)] = ytdlp_runner.calls
    assert argv[-1] == f"https://www.youtube.com/watch?v={DASH_VIDEO_ID}"


# --- download -------------------------------------------------------------


def test_download_uses_bestaudio_into_a_temp_file_inside_audio_dir(
    tmp_path: Path,
) -> None:
    ytdlp_runner = FakeDownloader(duration_sec=1.0)
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner)

    audio.fetch_normalized(VIDEO_ID, tmp_path)

    [(argv, _)] = ytdlp_runner.calls
    assert "-f" in argv
    assert argv[argv.index("-f") + 1] == "bestaudio"
    dest = Path(argv[argv.index("-o") + 1])
    assert dest.parent == tmp_path  # same filesystem as the final path


def test_default_download_timeout_is_two_hours(tmp_path: Path) -> None:
    ytdlp_runner = FakeDownloader(duration_sec=1.0)
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner)

    audio.fetch_normalized(VIDEO_ID, tmp_path)

    [(_, timeout)] = ytdlp_runner.calls
    assert timeout == DEFAULT_DOWNLOAD_TIMEOUT_SEC == 2 * 60 * 60


def test_download_timeout_is_configurable(tmp_path: Path) -> None:
    ytdlp_runner = FakeDownloader(duration_sec=1.0)
    audio = YouTubeAudio(download_timeout=45, ytdlp_runner=ytdlp_runner)

    audio.fetch_normalized(VIDEO_ID, tmp_path)

    [(_, timeout)] = ytdlp_runner.calls
    assert timeout == 45


def test_a_download_timeout_raises_transient_network_and_leaves_no_files(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(subprocess.TimeoutExpired(["yt-dlp"], 5))
    audio = YouTubeAudio(ytdlp_runner=runner)

    with pytest.raises(TransientNetworkError):
        audio.fetch_normalized(VIDEO_ID, tmp_path)

    assert files_under(tmp_path) == []


# --- conversion -------------------------------------------------------


def test_ffmpeg_converts_to_mono_opus_with_the_ac_flags(tmp_path: Path) -> None:
    # Opus's bitstream is always 48 kHz internally (RFC 6716 2); ffprobe's
    # `sample_rate` on any libopus stream reads 48000 no matter what `-ar`
    # was given at encode time - confirmed against this host's real ffmpeg
    # 8.0.1 for -ar 16000 and -ar 8000, .opus and .webm alike. So this test
    # checks codec and channel count via ffprobe (both are genuinely
    # affected by our flags) and checks -ar 16000 was actually passed to
    # ffmpeg via the recorded argv. See the comment posted on issue #19.
    ytdlp_runner = FakeDownloader(duration_sec=3.0)
    ffmpeg_runner = RecordingRealFfmpeg()
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner, ffmpeg_runner=ffmpeg_runner)

    ref = audio.fetch_normalized(VIDEO_ID, tmp_path)

    info = probe(tmp_path / ref.rel_path)
    assert info["codec_name"] == "opus"
    assert info["channels"] == 1
    [argv] = ffmpeg_runner.calls
    assert argv[argv.index("-ac") + 1] == "1"
    assert argv[argv.index("-ar") + 1] == "16000"
    assert argv[argv.index("-c:a") + 1] == "libopus"
    assert argv[argv.index("-application") + 1] == "voip"


def test_rel_path_matches_the_storage_rule(tmp_path: Path) -> None:
    ytdlp_runner = FakeDownloader(duration_sec=1.0)
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner)

    ref = audio.fetch_normalized(VIDEO_ID, tmp_path)

    assert ref.rel_path == f"{VIDEO_ID[:2]}/{VIDEO_ID}.opus"
    assert (tmp_path / ref.rel_path).is_file()


def test_a_ten_minute_fixture_is_at_most_11mb_per_hour(tmp_path: Path) -> None:
    ytdlp_runner = FakeDownloader(duration_sec=600.0)
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner)

    ref = audio.fetch_normalized(VIDEO_ID, tmp_path)

    hours = ref.duration_sec / 3600
    assert ref.bytes <= 11 * 1024 * 1024 * hours


def test_duration_sec_comes_from_ffprobe_on_the_output(tmp_path: Path) -> None:
    ytdlp_runner = FakeDownloader(duration_sec=4.0)
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner)

    ref = audio.fetch_normalized(VIDEO_ID, tmp_path)

    info = probe(tmp_path / ref.rel_path)
    assert ref.duration_sec == pytest.approx(float(info["duration"]), abs=0.05)
    assert ref.duration_sec == pytest.approx(4.0, abs=0.2)


def test_the_downloaded_stream_is_deleted_after_a_successful_conversion(
    tmp_path: Path,
) -> None:
    ytdlp_runner = FakeDownloader(duration_sec=1.0)
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner)

    audio.fetch_normalized(VIDEO_ID, tmp_path)

    [(argv, _)] = ytdlp_runner.calls
    dest = Path(argv[argv.index("-o") + 1])
    assert not dest.exists()


def test_ffmpeg_exiting_non_zero_raises_tool_failure_with_stderr_tail(
    tmp_path: Path,
) -> None:
    ytdlp_runner = FakeDownloader(duration_sec=1.0)
    ffmpeg_runner = FailingFfmpeg(stderr_lines=30)
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner, ffmpeg_runner=ffmpeg_runner)

    with pytest.raises(ToolFailureError) as exc_info:
        audio.fetch_normalized(VIDEO_ID, tmp_path)

    message = str(exc_info.value)
    expected_tail = "\n".join(f"line {i}" for i in range(10, 30))
    assert expected_tail in message
    assert "line 9" not in message


def test_a_partial_conversion_failure_leaves_no_files_at_all(
    tmp_path: Path,
) -> None:
    ytdlp_runner = FakeDownloader(duration_sec=1.0)
    ffmpeg_runner = FailingFfmpeg()
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner, ffmpeg_runner=ffmpeg_runner)

    with pytest.raises(ToolFailureError):
        audio.fetch_normalized(VIDEO_ID, tmp_path)

    final_path = tmp_path / VIDEO_ID[:2] / f"{VIDEO_ID}.opus"
    assert not final_path.exists()
    assert files_under(tmp_path) == []


def test_an_existing_final_file_is_replaced_atomically_without_error(
    tmp_path: Path,
) -> None:
    final_dir = tmp_path / VIDEO_ID[:2]
    final_dir.mkdir(parents=True)
    stub = final_dir / f"{VIDEO_ID}.opus"
    stub.write_bytes(b"leftover from a crashed run")

    ytdlp_runner = FakeDownloader(duration_sec=2.0)
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner)

    ref = audio.fetch_normalized(VIDEO_ID, tmp_path)

    assert stub.read_bytes() != b"leftover from a crashed run"
    assert stub.stat().st_size == ref.bytes


# --- ENOSPC -----------------------------------------------------------


def test_enospc_during_download_raises_resource_error(tmp_path: Path) -> None:
    runner = FakeRunner(OSError(errno.ENOSPC, "No space left on device"))
    audio = YouTubeAudio(ytdlp_runner=runner)

    with pytest.raises(ResourceError):
        audio.fetch_normalized(VIDEO_ID, tmp_path)

    assert files_under(tmp_path) == []


def test_enospc_during_conversion_raises_resource_error(tmp_path: Path) -> None:
    ytdlp_runner = FakeDownloader(duration_sec=1.0)
    ffmpeg_runner = FakeRunner(OSError(errno.ENOSPC, "No space left on device"))
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner, ffmpeg_runner=ffmpeg_runner)

    with pytest.raises(ResourceError):
        audio.fetch_normalized(VIDEO_ID, tmp_path)

    assert files_under(tmp_path) == []


def test_a_real_ffmpeg_disk_full_failure_during_conversion_raises_resource_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA's repro for issue #19: a full disk never makes the parent's
    ``subprocess.run()`` raise ``OSError`` - ffmpeg is a subprocess, so it
    writes "No space left on device" to its own stderr and exits non-zero
    (228). The conversion temp path is symlinked to ``/dev/full`` so the
    *real* ffmpeg binary hits that exact failure, not a simulated one.
    """
    if not Path("/dev/full").exists():
        pytest.skip("/dev/full is not available on this host")

    # fetch_normalized names its temp file with a random uuid4 token, so the
    # token is pinned to pre-place the /dev/full symlink at the exact path
    # ffmpeg will be told to write to.
    token = "deadbeefdeadbeefdeadbeefdeadbeef"
    monkeypatch.setattr(
        "adapters.youtube.audio.uuid.uuid4", lambda: uuid.UUID(hex=token)
    )

    final_dir = tmp_path / VIDEO_ID[:2]
    final_dir.mkdir(parents=True)
    convert_tmp_path = final_dir / f".tmp-{token}.opus"
    convert_tmp_path.symlink_to("/dev/full")

    ytdlp_runner = FakeDownloader(duration_sec=1.0)
    audio = YouTubeAudio(ytdlp_runner=ytdlp_runner)  # real ffmpeg/ffprobe

    with pytest.raises(ResourceError):
        audio.fetch_normalized(VIDEO_ID, tmp_path)

    final_path = final_dir / f"{VIDEO_ID}.opus"
    assert not final_path.exists()
    assert not os.path.lexists(convert_tmp_path)
    download_path = tmp_path / f".download-{token}"
    assert not download_path.exists()
