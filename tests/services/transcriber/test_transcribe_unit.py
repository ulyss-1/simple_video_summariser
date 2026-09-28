"""Transcribe handler checks that need no database (issue #29).

Everything here happens before the handler reaches Postgres: id validation and the
progress callback. The rest is in ``test_transcribe.py`` (integration).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from common.errors import BugError, Cancelled
from common.models import AudioRef, TranscriptResult
from services.transcriber.transcribe import make_transcribe_handler, progress_reporter
from tests.services.transcriber.fakes import make_ctx, make_job, make_settings


class Reached(Exception):
    """Raised by the fake ``connect``: the handler got past validation."""


class Spy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch_normalized(self, video_id: str, dest: Path) -> AudioRef:
        self.calls.append("fetch")
        raise AssertionError("audio must not be fetched")

    def transcribe(self, audio: AudioRef, **_: Any) -> TranscriptResult:
        self.calls.append("transcribe")
        raise AssertionError("must not transcribe")

    def chunk(self, *_: Any, **__: Any) -> list[Any]:
        self.calls.append("chunk")
        return []


def build(spy: Spy) -> Any:
    def connect() -> Any:
        raise Reached

    return make_transcribe_handler(
        connect=connect,
        queue=cast(Any, object()),
        audio_source=spy,
        transcriber=spy,
        chunker=spy.chunk,
        settings=make_settings(),
    )


@pytest.mark.parametrize("bad", ["", "a" * 10, "a" * 12, "abc/../defgh", "abcdefgh ij", "abcdefghij\n"])
def test_an_invalid_video_id_is_a_bug_before_any_work(bad: str) -> None:
    spy = Spy()
    ctx, _ = make_ctx()

    with pytest.raises(BugError):
        build(spy)(make_job(bad), ctx)

    assert spy.calls == []


def test_a_video_id_with_a_leading_dash_is_valid() -> None:
    spy = Spy()
    ctx, _ = make_ctx()

    with pytest.raises(Reached):
        build(spy)(make_job("-wNyEUrxzFU"), ctx)


def progress_lines(logs: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = logs.records("info")
    return records


def test_progress_checks_for_cancellation_on_every_call() -> None:
    ctx, _ = make_ctx()
    on_progress = progress_reporter(ctx, 100.0)
    on_progress(1.0)

    ctx._cancel()

    with pytest.raises(Cancelled):
        on_progress(2.0)


def test_progress_logs_at_most_once_per_ten_percent() -> None:
    ctx, logs = make_ctx()
    on_progress = progress_reporter(ctx, 1000.0)

    for done in range(1000):
        on_progress(float(done))

    lines = progress_lines(logs)
    assert 1 < len(lines) <= 11


def test_progress_past_the_duration_still_logs_at_most_eleven_lines() -> None:
    ctx, logs = make_ctx()
    on_progress = progress_reporter(ctx, 10.0)

    for done in range(200):
        on_progress(done * 0.5)

    assert 1 < len(progress_lines(logs)) <= 11


def test_progress_with_a_zero_duration_does_not_raise() -> None:
    ctx, logs = make_ctx()
    on_progress = progress_reporter(ctx, 0.0)

    on_progress(0.0)
    on_progress(5.0)

    assert len(progress_lines(logs)) <= 11


def test_progress_ignores_a_non_finite_position() -> None:
    ctx, _ = make_ctx()
    on_progress = progress_reporter(ctx, 100.0)

    on_progress(float("nan"))
    on_progress(float("inf"))
