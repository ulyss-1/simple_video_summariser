"""``run_queue``: the bounded FIFO loop that runs the real handlers (issue #31)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from common.errors import BugError, Defer
from common.queue import Job
from common.worker import JobContext
from services.cli.inprocess import InProcessQueue
from services.cli.main import MAX_JOBS, PipelineError, run_queue

VID = "iG9CE55wbtY"
OTHER = "aaaaaaaaaaa"

Handler = Callable[[Job, JobContext], None]


def seeded() -> InProcessQueue:
    queue = InProcessQueue()
    queue.enqueue("ingest", VID, priority=10)
    return queue


def test_jobs_run_fifo_until_the_queue_is_empty_with_a_real_context() -> None:
    queue = seeded()
    seen: list[tuple[str, int, bool]] = []

    def ingest(job: Job, ctx: JobContext) -> None:
        seen.append((job.kind, job.priority, isinstance(ctx, JobContext)))
        queue.enqueue("transcribe", VID, priority=job.priority)

    def transcribe(job: Job, ctx: JobContext) -> None:
        seen.append((job.kind, job.priority, isinstance(ctx, JobContext)))
        queue.enqueue("analyze", VID, priority=job.priority)

    def analyze(job: Job, ctx: JobContext) -> None:
        seen.append((job.kind, job.priority, isinstance(ctx, JobContext)))

    ran = run_queue(
        queue, {"ingest": ingest, "transcribe": transcribe, "analyze": analyze}, VID
    )
    assert ran == ["ingest", "transcribe", "analyze"]
    assert seen == [("ingest", 10, True), ("transcribe", 10, True), ("analyze", 10, True)]
    assert queue.pending == 0


def test_on_start_and_on_finish_bracket_every_job() -> None:
    queue = seeded()
    events: list[str] = []

    def ingest(job: Job, ctx: JobContext) -> None:
        events.append("handler")

    run_queue(
        queue,
        {"ingest": ingest},
        VID,
        on_start=lambda job: events.append(f"start {job.kind}"),
        on_finish=lambda job: events.append(f"finish {job.kind}"),
    )
    assert events == ["start ingest", "handler", "finish ingest"]


def test_a_failing_job_reports_no_finish() -> None:
    queue = seeded()
    events: list[str] = []

    def ingest(job: Job, ctx: JobContext) -> None:
        raise RuntimeError("boom")

    with pytest.raises(PipelineError):
        run_queue(
            queue,
            {"ingest": ingest},
            VID,
            on_start=lambda job: events.append("start"),
            on_finish=lambda job: events.append("finish"),
        )
    assert events == ["start"]


def test_the_run_stops_after_max_jobs_with_a_bug_when_handlers_keep_enqueueing() -> None:
    assert MAX_JOBS == 10
    queue = seeded()
    calls: list[str] = []

    def ingest(job: Job, ctx: JobContext) -> None:
        calls.append("ingest")
        queue.enqueue("transcribe", VID)

    def transcribe(job: Job, ctx: JobContext) -> None:
        calls.append("transcribe")
        queue.enqueue("ingest", VID)

    with pytest.raises(PipelineError) as excinfo:
        run_queue(queue, {"ingest": ingest, "transcribe": transcribe}, VID)
    assert len(calls) == 10
    assert isinstance(excinfo.value.cause, BugError)
    assert "10" in str(excinfo.value.cause)


def test_exactly_max_jobs_that_leave_the_queue_empty_is_fine() -> None:
    queue = seeded()
    count = 0

    def handler(other: str) -> Handler:
        def run(job: Job, ctx: JobContext) -> None:
            nonlocal count
            count += 1
            if count < 10:
                queue.enqueue(other, VID)

        return run

    ran = run_queue(
        queue, {"ingest": handler("transcribe"), "transcribe": handler("ingest")}, VID
    )
    assert len(ran) == 10


def test_a_job_for_a_different_video_is_a_bug_and_its_handler_never_runs() -> None:
    queue = seeded()
    ran: list[str] = []

    def ingest(job: Job, ctx: JobContext) -> None:
        ran.append(job.video_id)
        queue.enqueue("analyze", OTHER)

    def analyze(job: Job, ctx: JobContext) -> None:
        ran.append(job.video_id)

    with pytest.raises(PipelineError) as excinfo:
        run_queue(queue, {"ingest": ingest, "analyze": analyze}, VID)
    assert ran == [VID]
    assert isinstance(excinfo.value.cause, BugError)


def test_a_job_of_an_unknown_kind_is_a_bug() -> None:
    queue = seeded()

    def ingest(job: Job, ctx: JobContext) -> None:
        queue.enqueue("notify", VID)

    with pytest.raises(PipelineError) as excinfo:
        run_queue(queue, {"ingest": ingest}, VID)
    assert isinstance(excinfo.value.cause, BugError)
    assert "notify" in str(excinfo.value.cause)


def test_a_raising_handler_stops_the_run_and_names_the_stage_and_cause() -> None:
    queue = seeded()
    later: list[str] = []
    failure = RuntimeError("boom")

    def ingest(job: Job, ctx: JobContext) -> None:
        queue.enqueue("transcribe", VID)

    def transcribe(job: Job, ctx: JobContext) -> None:
        queue.enqueue("analyze", VID)
        raise failure

    def analyze(job: Job, ctx: JobContext) -> None:
        later.append("analyze")

    with pytest.raises(PipelineError) as excinfo:
        run_queue(queue, {"ingest": ingest, "transcribe": transcribe, "analyze": analyze}, VID)
    assert excinfo.value.stage == "transcribe"
    assert excinfo.value.cause is failure
    assert later == []


def test_defer_is_reported_as_the_cause_and_not_retried() -> None:
    queue = seeded()
    until = datetime(2026, 10, 1, tzinfo=UTC)
    attempts = 0

    def ingest(job: Job, ctx: JobContext) -> None:
        nonlocal attempts
        attempts += 1
        raise Defer(until)

    with pytest.raises(PipelineError) as excinfo:
        run_queue(queue, {"ingest": ingest}, VID)
    assert isinstance(excinfo.value.cause, Defer)
    assert excinfo.value.cause.until == until
    assert attempts == 1


def test_keyboard_interrupt_is_not_wrapped() -> None:
    queue = seeded()

    def ingest(job: Job, ctx: JobContext) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_queue(queue, {"ingest": ingest}, VID)
