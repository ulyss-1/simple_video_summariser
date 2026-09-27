"""Test doubles for the yt-dlp runner seam (adapters/youtube/ytdlp.py)."""

import subprocess
from collections.abc import Sequence


class FakeRunner:
    """Records each argv and answers with canned results, in order."""

    def __init__(self, *results: subprocess.CompletedProcess[str] | BaseException):
        self.results = list(results)
        self.calls: list[tuple[list[str], float]] = []

    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), timeout))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)
