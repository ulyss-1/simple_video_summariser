"""A cancel or a shutdown during the audio download, through a real worker (issue #302).

The real transcribe handler runs under a real ``Worker`` and Postgres. The audio
source blocks inside ``fetch_normalized`` until the test has delivered the flag
(a heartbeat tick for a stored cancel, the grace timer for a shutdown), then, like
the real adapter, raises ``Cancelled`` when its ``cancelled()`` signal is true.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest

from common.errors import Cancelled
from common.models import AudioRef
from common.queue import Job, PostgresQueue
from common.repo.jobs import cancel_video
from common.worker import JobContext, Worker
from tests.common.test_cancel_races import DEADLINE_SEC, Call, ManualTimers
from tests.services.transcriber.test_transcribe import (
    VID,
    Rig,
    make_rig,  # noqa: F401 - fixture
)

pytestmark = pytest.mark.integration

GRACE_SEC = 17


class DownloadInFlight:
    """An audio source that sits in the download until released, then honours its signal."""

    def __init__(self) -> None:
        self.in_download = threading.Event()
        self.go_on = threading.Event()
        self.signals: list[Callable[[], bool] | None] = []

    def fetch_normalized(
        self, video_id: str, dest: Path, *, cancelled: Callable[[], bool] | None = None
    ) -> AudioRef:
        self.signals.append(cancelled)
        self.in_download.set()
        assert self.go_on.wait(DEADLINE_SEC), "the download was never released"
        if cancelled is not None and cancelled():
            raise Cancelled()
        raise AssertionError("the download was released without a cancel")


class Setup:
    def __init__(self, rig: Rig, head_dsn: str, tmp_path: Path) -> None:
        self.rig = rig
        self.admin = psycopg.connect(head_dsn, autocommit=True)
        self.hb = psycopg.connect(head_dsn, autocommit=True)
        self.queue = PostgresQueue(self.admin, rng=random.Random(1))
        self.timers = ManualTimers()
        self.ctxs: list[JobContext] = []
        job_id = self.queue.enqueue("transcribe", VID)
        assert job_id is not None
        self.job_id = job_id

        def handler(job: Job, ctx: JobContext) -> None:
            self.ctxs.append(ctx)
            rig.handler(job, ctx)

        self.worker = Worker(
            "w1",
            ["transcribe"],
            handler,
            self.queue,
            liveness_path=tmp_path / "hb",
            heartbeat_sec=60,
            shutdown_grace_sec=GRACE_SEC,
            heartbeat_queue_factory=lambda: PostgresQueue(self.hb),
            runtime_limit_sec=lambda job: None,
            timer_factory=self.timers,
            install_signal_handlers=False,
            sleep=lambda seconds: None,
        )

    def job(self) -> tuple[Any, ...]:
        row = self.admin.execute(
            "SELECT state, error_class, attempts FROM jobs WHERE id = %s", (self.job_id,)
        ).fetchone()
        assert row is not None
        return tuple(row)

    def close(self) -> None:
        self.admin.close()
        self.hb.close()


@pytest.fixture
def setup(
    make_rig: Callable[..., Rig],  # noqa: F811
    head_dsn: str,
    tmp_path: Path,
) -> Any:
    audio = DownloadInFlight()
    rig = make_rig(audio=cast(Any, audio))
    built = Setup(rig, head_dsn, tmp_path)
    yield built, audio
    audio.go_on.set()
    built.close()


def test_a_stored_cancel_during_the_download_ends_the_job_cancelled_with_no_media_row(
    setup: tuple[Setup, DownloadInFlight],
) -> None:
    s, audio = setup
    runner = Call(lambda: s.worker.run(max_iterations=1))
    assert audio.in_download.wait(DEADLINE_SEC), "the download never started"
    [signal] = audio.signals
    assert signal is not None and not signal()

    assert [e.outcome for e in cancel_video(s.admin, VID)] == ["cancel_requested"]
    s.timers.fire(60.0)  # one heartbeat tick delivers the request
    [ctx] = s.ctxs
    assert ctx._cancel_event.wait(DEADLINE_SEC), "the heartbeat tick never set ctx.cancelled"
    assert signal()  # the handler handed the adapter a view of ctx.cancelled
    audio.go_on.set()
    runner.result("the worker")

    assert s.job() == ("dead", "CANCELLED", 1)
    assert s.rig.media() == []
    assert s.rig.count("transcripts") == 0
    assert s.rig.analyze_jobs() == []


def test_a_shutdown_cancel_during_the_download_hands_the_job_back_with_its_attempt_refunded(
    setup: tuple[Setup, DownloadInFlight],
) -> None:
    s, audio = setup
    runner = Call(lambda: s.worker.run(max_iterations=1))
    assert audio.in_download.wait(DEADLINE_SEC), "the download never started"

    s.worker.request_shutdown()  # arms the grace timer
    s.timers.fire(float(GRACE_SEC))  # the grace period expires: ctx.cancelled
    [ctx] = s.ctxs
    assert ctx._cancel_event.wait(DEADLINE_SEC), "the grace timer never set ctx.cancelled"
    audio.go_on.set()
    runner.result("the worker")

    state, error_class, attempts = s.job()
    assert (state, error_class, attempts) == ("pending", None, 0)
    assert s.rig.media() == []


def test_a_stored_cancel_and_a_shutdown_together_still_end_cancelled(
    setup: tuple[Setup, DownloadInFlight],
) -> None:
    s, audio = setup
    runner = Call(lambda: s.worker.run(max_iterations=1))
    assert audio.in_download.wait(DEADLINE_SEC), "the download never started"

    s.worker.request_shutdown()
    assert [e.outcome for e in cancel_video(s.admin, VID)] == ["cancel_requested"]
    s.timers.fire(float(GRACE_SEC))
    [ctx] = s.ctxs
    assert ctx._cancel_event.wait(DEADLINE_SEC), "the grace timer never set ctx.cancelled"
    audio.go_on.set()
    runner.result("the worker")

    assert s.job() == ("dead", "CANCELLED", 1)
