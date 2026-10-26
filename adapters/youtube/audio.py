"""YouTube implementation of the ``AudioSource`` port (architecture.md 3, D6b).

Downloads a video's best audio stream with the shared yt-dlp runner (#16),
then converts it with ffmpeg to 16 kHz mono Opus (~7-10 MB/hour). The final
file appears at ``<audio_dir>/<first 2 chars of id>/<id>.opus`` through
``os.replace``, so a crash or full disk never leaves a partial file there.

Only the shared runner spawns yt-dlp; ffmpeg and ffprobe are run through
the same wait loop and process-group kill (``run_process``) with list argv,
never ``shell=True`` (issue #19's own constraint). With a ``cancelled``
signal (#302) a cancel kills the running tool and removes every file of the
job's token, so no partial audio stays behind.

``AudioRef``/``AudioSource`` are defined in ``common/models.py`` - see the
comment there for why.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable, Sequence
from glob import escape as glob_escape
from pathlib import Path

from adapters.youtube.metadata import watch_url
from adapters.youtube.ytdlp import (
    DEFAULT_POLL_INTERVAL_SEC,
    ProcessRunner,
    run_process,
    run_ytdlp,
)
from common.errors import Cancelled, ResourceError, ToolFailureError
from common.models import AudioRef

#: Default download timeout (issue #19's acceptance criteria).
DEFAULT_DOWNLOAD_TIMEOUT_SEC = 2 * 60 * 60

# ffmpeg has no long-running network step, so one generous fixed ceiling
# covers it; unlike the download, this is not configurable per the AC.
_CONVERT_TIMEOUT_SEC = 60 * 60
_PROBE_TIMEOUT_SEC = 60

_DOWNLOAD_FLAGS = ("-f", "bestaudio", "--no-playlist", "--no-warnings")

#: Module constant so #3's bake-off can copy exactly the same pipeline.
FFMPEG_AUDIO_FLAGS = (
    "-ac",
    "1",
    "-ar",
    "16000",
    "-c:a",
    "libopus",
    "-b:a",
    "16k",
    "-application",
    "voip",
)

# ToolFailureError carries only the last 20 lines of ffmpeg stderr.
_STDERR_TAIL_LINES = 20

_RESOURCE_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})

# ffmpeg/ffprobe are subprocesses: a full disk never makes subprocess.run()
# raise OSError the way it would for a syscall in this process. Instead the
# tool writes its own message to stderr and exits non-zero, the same class
# of failure adapters/youtube/errors.py's from_ytdlp already matches for
# yt-dlp.
_RESOURCE_STDERR = re.compile(
    r"No space left on device|Disk quota exceeded", re.IGNORECASE
)


def _is_resource_exhausted(stderr: str) -> bool:
    return bool(_RESOURCE_STDERR.search(stderr))


def run_tool(
    argv: Sequence[str],
    *,
    timeout: float,
    cancelled: Callable[[], bool] | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SEC,
) -> subprocess.CompletedProcess[str]:
    """Run ``ffmpeg``/``ffprobe`` to completion and capture output as text.

    Shares the wait loop and the process-group kill of ``run_process`` (#302),
    so a cancel kills the tool and anything it started. It stays out of
    ``run_ytdlp``, which remains "the one way YouTube adapters run yt-dlp".
    """
    try:
        return run_process(argv, timeout=timeout, cancelled=cancelled, poll_interval=poll_interval)
    except FileNotFoundError as exc:
        raise ToolFailureError(f"cannot start {argv[0]}: {exc}") from exc


class YouTubeAudio:
    """``AudioSource`` backed by yt-dlp (download) and ffmpeg (normalize)."""

    def __init__(
        self,
        *,
        download_timeout: float = DEFAULT_DOWNLOAD_TIMEOUT_SEC,
        ytdlp_runner: ProcessRunner = run_process,
        cookie_file: Path | None = None,
        ffmpeg_runner: ProcessRunner = run_tool,
        ffprobe_runner: ProcessRunner = run_tool,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SEC,
    ) -> None:
        self._download_timeout = download_timeout
        self._ytdlp_runner = ytdlp_runner
        self._cookie_file = cookie_file
        self._ffmpeg_runner = ffmpeg_runner
        self._ffprobe_runner = ffprobe_runner
        self._poll_interval = poll_interval

    def fetch_normalized(
        self, video_id: str, dest: Path, *, cancelled: Callable[[], bool] | None = None
    ) -> AudioRef:
        url = watch_url(video_id)  # raises ValueError before anything starts

        def checkpoint() -> None:
            if cancelled is not None and cancelled():
                raise Cancelled()

        audio_dir = Path(dest)
        prefix = video_id[:2]
        final_dir = audio_dir / prefix
        final_path = final_dir / f"{video_id}.opus"
        rel_path = f"{prefix}/{video_id}.opus"

        checkpoint()
        final_dir.mkdir(parents=True, exist_ok=True)

        token = uuid.uuid4().hex
        download_name = f".download-{token}"
        download_path = audio_dir / download_name
        convert_tmp_path = final_dir / f".tmp-{token}.opus"

        try:
            self._download(url, download_path, cancelled)
            checkpoint()
            duration_sec = self._convert(download_path, convert_tmp_path, cancelled)
            checkpoint()
            os.replace(convert_tmp_path, final_path)
        except OSError as exc:
            if exc.errno in _RESOURCE_ERRNOS:
                raise ResourceError(str(exc)) from exc
            raise
        finally:
            # By prefix: yt-dlp leaves side files (.part, .ytdl, fragments) beside its target.
            _remove_with_prefix(audio_dir, download_name)
            _remove_if_exists(convert_tmp_path)

        return AudioRef(
            rel_path=rel_path,
            bytes=final_path.stat().st_size,
            duration_sec=duration_sec,
        )

    def _download(
        self, url: str, dest: Path, cancelled: Callable[[], bool] | None = None
    ) -> None:
        argv = [*_DOWNLOAD_FLAGS, "-o", str(dest), url]
        run_ytdlp(
            argv,
            timeout=self._download_timeout,
            runner=self._ytdlp_runner,
            cookie_file=self._cookie_file,
            cancelled=cancelled,
            poll_interval=self._poll_interval,
        )

    def _convert(
        self, src: Path, dest_tmp: Path, cancelled: Callable[[], bool] | None = None
    ) -> float:
        argv = ["ffmpeg", "-y", "-i", str(src), *FFMPEG_AUDIO_FLAGS, str(dest_tmp)]
        try:
            result = self._ffmpeg_runner(
                argv,
                timeout=_CONVERT_TIMEOUT_SEC,
                cancelled=cancelled,
                poll_interval=self._poll_interval,
            )
        except subprocess.TimeoutExpired as exc:
            _remove_if_exists(dest_tmp)
            raise ToolFailureError(
                f"ffmpeg timed out after {_CONVERT_TIMEOUT_SEC}s"
            ) from exc
        except BaseException:
            _remove_if_exists(dest_tmp)
            raise

        if result.returncode != 0:
            _remove_if_exists(dest_tmp)
            if _is_resource_exhausted(result.stderr):
                raise ResourceError(_ffmpeg_failure_message(result))
            raise ToolFailureError(_ffmpeg_failure_message(result))

        try:
            return _probe_duration(
                dest_tmp,
                runner=self._ffprobe_runner,
                cancelled=cancelled,
                poll_interval=self._poll_interval,
            )
        except BaseException:
            _remove_if_exists(dest_tmp)
            raise


def _ffmpeg_failure_message(result: subprocess.CompletedProcess[str]) -> str:
    tail = "\n".join(result.stderr.splitlines()[-_STDERR_TAIL_LINES:])
    return f"ffmpeg exited with {result.returncode}:\n{tail}"


def _probe_duration(
    path: Path,
    *,
    runner: ProcessRunner,
    cancelled: Callable[[], bool] | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SEC,
) -> float:
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = runner(
            argv, timeout=_PROBE_TIMEOUT_SEC, cancelled=cancelled, poll_interval=poll_interval
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolFailureError(
            f"ffprobe timed out after {_PROBE_TIMEOUT_SEC}s"
        ) from exc
    if result.returncode != 0:
        message = f"ffprobe exited with {result.returncode}: {result.stderr.strip()}"
        if _is_resource_exhausted(result.stderr):
            raise ResourceError(message)
        raise ToolFailureError(message)
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise ToolFailureError(
            f"ffprobe returned a non-numeric duration: {result.stdout!r}"
        ) from exc


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _remove_with_prefix(directory: Path, prefix: str) -> None:
    """Remove every entry of ``directory`` whose name starts with ``prefix``."""
    for entry in directory.glob(f"{glob_escape(prefix)}*"):
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
        except FileNotFoundError:
            pass
